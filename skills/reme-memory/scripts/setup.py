#!/usr/bin/env python3
"""Reconcile or bootstrap the local ReMe runtime for Codex."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
HOOKS = {
    "PreCompact": {
        "source": SCRIPT_DIR / "precompact_capture_hook.py",
        "target_name": "precompact_capture_hook.py",
        "timeout": 30,
        "statusMessage": "Capturing ReMe pre-compact context",
    },
    "PostCompact": {
        "source": SCRIPT_DIR / "probe_hook_logger.py",
        "target_name": "probe_hook_logger.py",
        "timeout": 30,
        "statusMessage": "Recording ReMe post-compact hook probe",
    },
}
DEFAULT_EMBEDDING_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_EMBEDDING_MODEL = "embedding-3"
DEFAULT_EMBEDDING_DIMENSIONS = "2048"


class SetupError(RuntimeError):
    pass


def dedupe_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        expanded = path.expanduser()
        key = str(expanded)
        if key in seen:
            continue
        seen.add(key)
        result.append(expanded)
    return result


def candidate_codex_homes() -> list[Path]:
    candidates: list[Path] = []
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        candidates.append(Path(env_home))
    candidates.append(Path.home() / ".codex")
    return dedupe_paths(candidates)


def resolve_codex_home(explicit: str | None) -> tuple[Path, list[Path]]:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"explicit Codex home does not exist: {path}")
        return path, [path]

    candidates = candidate_codex_homes()
    for path in candidates:
        if path.is_dir():
            return path, candidates
    rendered = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(f"cannot find Codex home; checked: {rendered}")


def load_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip("'\"")
    return data


def write_env_file(path: Path, values: dict[str, str]) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        backup.write_bytes(path.read_bytes())
    body = env_body(values)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)
    return backup


def env_body(values: dict[str, str]) -> str:
    return "".join(f"{key}={value}\n" for key, value in values.items())


def load_hooks(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"hooks": {}}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"hooks file is not a JSON object: {path}")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"hooks field is not a JSON object: {path}")
    return data


def event_group(data: dict[str, Any], event: str) -> list[dict[str, Any]]:
    hooks = data.setdefault("hooks", {})
    entries = hooks.setdefault(event, [])
    if not isinstance(entries, list):
        raise ValueError(f"hooks.{event} is not a list")
    if not entries:
        entries.append({"hooks": []})
    first = entries[0]
    if not isinstance(first, dict):
        raise ValueError(f"hooks.{event}[0] is not an object")
    hook_list = first.setdefault("hooks", [])
    if not isinstance(hook_list, list):
        raise ValueError(f"hooks.{event}[0].hooks is not a list")
    return hook_list


def write_hooks(path: Path, data: dict[str, Any]) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        backup.write_bytes(path.read_bytes())
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return backup


def desired_hook(event: str, runtime_hooks_dir: Path) -> dict[str, Any]:
    spec = HOOKS[event]
    target = runtime_hooks_dir / str(spec["target_name"])
    return {
        "type": "command",
        "command": f"/usr/bin/python3 {target}",
        "timeout": spec["timeout"],
        "async": False,
        "statusMessage": spec["statusMessage"],
    }


def hook_matches(item: dict[str, Any], target_name: str, desired_command: str) -> bool:
    raw = item.get("command")
    return isinstance(raw, str) and (raw == desired_command or raw.endswith(target_name))


def ensure_hook(hook_list: list[dict[str, Any]], target_name: str, desired: dict[str, Any]) -> str:
    matched_index: int | None = None
    duplicate_indexes: list[int] = []
    desired_command = str(desired["command"])
    for index, item in enumerate(hook_list):
        if not isinstance(item, dict):
            continue
        if hook_matches(item, target_name, desired_command):
            if matched_index is None:
                matched_index = index
            else:
                duplicate_indexes.append(index)

    changed = False
    if matched_index is None:
        hook_list.append(dict(desired))
        return "added"

    item = hook_list[matched_index]
    for key, value in desired.items():
        if item.get(key) != value:
            item[key] = value
            changed = True
    for index in reversed(duplicate_indexes):
        del hook_list[index]
        changed = True
    return "updated" if changed else "present"


def prompt_value(label: str, default: str = "", *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    if secret:
        value = getpass.getpass(f"{label}{suffix}: ").strip()
    else:
        value = input(f"{label}{suffix}: ").strip()
    return value or default


def build_env(args: argparse.Namespace, codex_home: Path) -> tuple[dict[str, str], list[str]]:
    user_home = codex_home.parent
    reme_home = Path(args.reme_home).expanduser() if args.reme_home else user_home / ".local/share/reme"
    workdir = Path(args.reme_workdir).expanduser() if args.reme_workdir else reme_home / "light_data"
    active_env = codex_home / "memories/reme-memory/daemon/config/active.env"
    legacy_env = reme_home / ".env"

    existing = {}
    existing.update(load_env_file(legacy_env))
    existing.update(load_env_file(active_env))

    env = {
        "CODEX_HOME": str(codex_home),
        "CODEX_USER_HOME": str(user_home),
        "HOME": str(user_home),
        "REME_HOME": str(reme_home),
        "REME_WORKDIR": str(workdir),
        "REME_VECTOR_ENABLED": "1",
        "REME_FTS_ENABLED": "1",
        "EMBEDDING_API_KEY": args.embedding_api_key
        or os.environ.get("EMBEDDING_API_KEY")
        or existing.get("EMBEDDING_API_KEY", ""),
        "EMBEDDING_BASE_URL": args.embedding_base_url
        or os.environ.get("EMBEDDING_BASE_URL")
        or existing.get("EMBEDDING_BASE_URL", DEFAULT_EMBEDDING_BASE_URL),
        "REME_EMBEDDING_MODEL": args.embedding_model
        or os.environ.get("REME_EMBEDDING_MODEL")
        or existing.get("REME_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        "REME_EMBEDDING_DIMENSIONS": str(
            args.embedding_dimensions
            or os.environ.get("REME_EMBEDDING_DIMENSIONS")
            or existing.get("REME_EMBEDDING_DIMENSIONS", DEFAULT_EMBEDDING_DIMENSIONS)
        ),
        "REME_EMBEDDING_USE_DIMENSIONS": "1",
        "REME_REFINE_MODEL": "codex-exec",
        "REME_PYTHON": str(reme_home / ".venv/bin/python"),
    }

    missing: list[str] = []
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not args.non_interactive
    if not env["EMBEDDING_API_KEY"] and interactive:
        env["EMBEDDING_API_KEY"] = prompt_value("Embedding API key", secret=True)
    if not env["EMBEDDING_BASE_URL"] and interactive:
        env["EMBEDDING_BASE_URL"] = prompt_value("Embedding base URL", DEFAULT_EMBEDDING_BASE_URL)
    if not env["REME_EMBEDDING_MODEL"] and interactive:
        env["REME_EMBEDDING_MODEL"] = prompt_value("Embedding model", DEFAULT_EMBEDDING_MODEL)
    if not env["REME_EMBEDDING_DIMENSIONS"] and interactive:
        env["REME_EMBEDDING_DIMENSIONS"] = prompt_value("Embedding dimensions", DEFAULT_EMBEDDING_DIMENSIONS)

    for key in ("EMBEDDING_API_KEY", "EMBEDDING_BASE_URL", "REME_EMBEDDING_MODEL", "REME_EMBEDDING_DIMENSIONS"):
        if not env.get(key):
            missing.append(key)
    return env, missing


def redact_env(env: dict[str, str]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in env.items():
        if key == "EMBEDDING_API_KEY":
            redacted["EMBEDDING_API_KEY_present"] = bool(value)
        else:
            redacted[key] = value
    return redacted


def copy_file(source: Path, target: Path) -> str:
    if not source.is_file():
        raise FileNotFoundError(f"missing source file: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_bytes() == source.read_bytes():
        return "present"
    action = "updated" if target.exists() else "added"
    shutil.copy2(source, target)
    return action


def run_checked(cmd: list[str], *, env: dict[str, str]) -> None:
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise SetupError(f"command failed ({proc.returncode}): {' '.join(cmd)} {detail}")


def setup(args: argparse.Namespace) -> dict[str, Any]:
    codex_home, checked_homes = resolve_codex_home(args.codex_home)
    env, missing = build_env(args, codex_home)
    reme_home = Path(env["REME_HOME"])
    workdir = Path(env["REME_WORKDIR"])
    runtime_root = codex_home / "memories/reme-memory/daemon"
    runtime_hooks_dir = codex_home / "memories/reme-memory/hooks"
    active_env = runtime_root / "config/active.env"
    legacy_env = reme_home / ".env"
    hooks_file = Path(args.hooks_file).expanduser() if args.hooks_file else codex_home / "hooks.json"

    actions: list[dict[str, Any]] = []
    if missing:
        raise SetupError("missing required values: " + ", ".join(missing))

    dirs = [
        reme_home,
        workdir,
        workdir / "memory",
        workdir / "compact_memory",
        workdir / "compact_archive",
        workdir / "file_store",
        workdir / "embedding_cache",
        runtime_root,
        runtime_root / "config",
        runtime_hooks_dir,
        codex_home / "memories/reme-memory/artifacts",
        codex_home / "memories/reme-memory/audit",
        codex_home / "memories/reme-memory/bus",
        codex_home / "memories/reme-memory/tmp",
    ]
    for directory in dirs:
        exists = directory.is_dir()
        if not args.dry_run:
            directory.mkdir(parents=True, exist_ok=True)
        actions.append({"type": "dir", "path": str(directory), "action": "present" if exists else "create"})

    python_bin = Path(env["REME_PYTHON"])
    actions.append({"type": "check", "name": "reme_python", "path": str(python_bin), "ok": python_bin.is_file()})
    if not python_bin.is_file():
        raise SetupError(f"ReMe python is missing: {python_bin}")

    if not args.dry_run:
        probe = subprocess.run(
            [str(python_bin), "-c", "import reme, httpx, pydantic, numpy"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **env},
        )
        if probe.returncode != 0:
            raise SetupError(f"ReMe python dependency check failed: {probe.stderr.strip()}")

    target_env_body = env_body(env)
    for env_path in (active_env, legacy_env):
        changed = (not env_path.exists()) or env_path.read_text(encoding="utf-8") != target_env_body
        backup = None
        if changed and not args.dry_run:
            backup = write_env_file(env_path, env)
        actions.append(
            {
                "type": "env",
                "path": str(env_path),
                "changed": changed,
                "backup": str(backup) if backup else None,
            }
        )

    for event, spec in HOOKS.items():
        target = runtime_hooks_dir / str(spec["target_name"])
        if args.dry_run:
            action = "would_copy" if not target.exists() else "would_verify"
        else:
            action = copy_file(Path(spec["source"]), target)
        actions.append({"type": "runtime_hook_file", "event": event, "path": str(target), "action": action})

    if args.dry_run:
        actions.append({"type": "runtime_scripts", "path": str(runtime_root / "scripts"), "action": "would_deploy"})
    else:
        run_checked([sys.executable, str(SCRIPT_DIR / "deploy_runtime.py"), "--runtime-root", str(runtime_root)], env={**os.environ, **env})
        actions.append({"type": "runtime_scripts", "path": str(runtime_root / "scripts"), "action": "deployed"})

    hook_results: list[dict[str, str]] = []
    if not args.no_hooks:
        data = load_hooks(hooks_file)
        before = json.dumps(data, ensure_ascii=False, sort_keys=True)
        for event in HOOKS:
            desired = desired_hook(event, runtime_hooks_dir)
            action = ensure_hook(event_group(data, event), str(HOOKS[event]["target_name"]), desired)
            hook_results.append({"event": event, "action": action, "command": str(desired["command"])})
        changed = before != json.dumps(data, ensure_ascii=False, sort_keys=True)
        backup = None
        if changed and not args.dry_run:
            backup = write_hooks(hooks_file, data)
        actions.append(
            {
                "type": "hooks",
                "path": str(hooks_file),
                "changed": changed,
                "backup": str(backup) if backup else None,
                "results": hook_results,
            }
        )

    if not args.no_start_daemon:
        start_script = runtime_root / "start_daemon.sh"
        actions.append({"type": "daemon_start", "path": str(start_script), "action": "would_start" if args.dry_run else "start"})
        if not args.dry_run:
            run_checked(["bash", str(start_script)], env={**os.environ, **env})

    return {
        "skill": "reme-memory",
        "codex_home": str(codex_home),
        "checked_codex_homes": [str(path) for path in checked_homes],
        "dry_run": bool(args.dry_run),
        "env": redact_env(env),
        "actions": actions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Setup or reconcile ReMe memory runtime.")
    parser.add_argument("--codex-home", help="Target Codex home. Overrides auto-discovery.")
    parser.add_argument("--hooks-file", help="Target hooks.json. Defaults to <codex-home>/hooks.json.")
    parser.add_argument("--reme-home", help="Target ReMe home. Defaults to <codex-user-home>/.local/share/reme.")
    parser.add_argument("--reme-workdir", help="Target ReMe workdir. Defaults to <reme-home>/light_data.")
    parser.add_argument("--embedding-api-key", help="Embedding API key. Prefer interactive input or environment variable.")
    parser.add_argument("--embedding-base-url", help="Embedding base URL.")
    parser.add_argument("--embedding-model", help="Embedding model name.")
    parser.add_argument("--embedding-dimensions", help="Embedding dimensions.")
    parser.add_argument("--non-interactive", action="store_true", help="Fail instead of prompting for missing required values.")
    parser.add_argument("--dry-run", action="store_true", help="Report required changes without writing.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--no-hooks", action="store_true", help="Do not install Codex compact hooks.")
    parser.add_argument("--no-start-daemon", action="store_true", help="Do not start or restart the ReMe daemon.")
    args = parser.parse_args()

    try:
        result = setup(args)
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        else:
            print(f"setup failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    else:
        print(f"skill: {result['skill']}")
        print(f"codex_home: {result['codex_home']}")
        print(f"dry_run: {result['dry_run']}")
        print(f"embedding_api_key_present: {result['env']['EMBEDDING_API_KEY_present']}")
        for action in result["actions"]:
            print(json.dumps(action, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
