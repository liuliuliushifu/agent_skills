#!/usr/bin/env python3
"""Setup or reconcile AI daily report runtime wrappers, hooks, and cron."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
DEFAULT_CRON_TIME = "0 2 * * *"
DEFAULT_TIMEZONE = os.environ.get("AI_DAILY_TIMEZONE", "UTC")
BEGIN_MARKER = "# BEGIN ai-daily-report setup"
END_MARKER = "# END ai-daily-report setup"


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


def write_text(path: Path, content: str, *, mode: int | None = None) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        backup.write_bytes(path.read_bytes())
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    if mode is not None:
        tmp.chmod(mode)
    tmp.replace(path)
    if mode is not None:
        path.chmod(mode)
    return backup


def daily_wrapper(codex_home: Path, runtime_dir: Path) -> str:
    return f"""#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
CODEX_HOME="${{CODEX_HOME:-$(cd "$SCRIPT_DIR/.." && pwd)}}"
SKILL_ROOT="$CODEX_HOME/skills/ai-daily-report"
SKILL_SCRIPT="$SKILL_ROOT/scripts/run_ai_daily_report.sh"
SKILL_CONFIG="$SKILL_ROOT/scripts/.config.json"

export CODEX_HOME
export AI_DAILY_REPORT_DIR="${{AI_DAILY_REPORT_DIR:-$SCRIPT_DIR}}"
export AI_DAILY_CONFIG_FILE="${{AI_DAILY_CONFIG_FILE:-$SKILL_CONFIG}}"

exec bash "$SKILL_SCRIPT" "$@"
"""


def evidence_wrapper(codex_home: Path, runtime_dir: Path) -> str:
    return f"""#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
CODEX_HOME_DIR="${{CODEX_HOME:-$(cd "$SCRIPT_DIR/.." && pwd)}}"
CODEX_USER_HOME="${{CODEX_USER_HOME:-$(cd "$CODEX_HOME_DIR/.." && pwd)}}"
SYSTEM_HOME="$(getent passwd "$(id -un)" | cut -d: -f6 || true)"
SKILL_ROOT="$CODEX_HOME_DIR/skills/ai-daily-report"
SKILL_SCRIPT="$SKILL_ROOT/scripts/collect_evidence.py"
SKILL_CONFIG="$SKILL_ROOT/scripts/.config.json"
PYTHON_BIN="${{PYTHON_BIN:-python3}}"

export CODEX_HOME="$CODEX_HOME_DIR"
export CODEX_USER_HOME
export HOME="${{AI_DAILY_HOME:-${{SYSTEM_HOME:-${{HOME:-$CODEX_USER_HOME}}}}}}"
export AI_DAILY_REPORT_DIR="${{AI_DAILY_REPORT_DIR:-$SCRIPT_DIR}}"
export AI_DAILY_CONFIG_FILE="${{AI_DAILY_CONFIG_FILE:-$SKILL_CONFIG}}"

exec "$PYTHON_BIN" "$SKILL_SCRIPT" "$@"
"""


def run_hook_setup(codex_home: Path, hooks_file: Path | None, dry_run: bool) -> dict[str, Any]:
    cmd = [sys.executable, str(SCRIPT_DIR / "hook_setup.py"), "--codex-home", str(codex_home), "--json"]
    if hooks_file:
        cmd.extend(["--hooks-file", str(hooks_file)])
    if dry_run:
        cmd.append("--dry-run")
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise SetupError(f"hook setup failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return json.loads(proc.stdout)


def read_crontab() -> tuple[str, bool]:
    proc = subprocess.run(["crontab", "-l"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode == 0:
        return proc.stdout, True
    if "no crontab" in proc.stderr.lower():
        return "", True
    return proc.stderr.strip(), False


def cron_block(codex_home: Path, runtime_dir: Path, cron_time: str, timezone_name: str) -> str:
    command = f"{cron_time} TZ={timezone_name} {runtime_dir / 'run_ai_daily_report.sh'} >> {runtime_dir / 'logs/cron.log'} 2>&1"
    lines = [
        BEGIN_MARKER,
        "SHELL=/bin/bash",
        f"HOME={codex_home.parent}",
        f"CODEX_HOME={codex_home}",
        f"AI_DAILY_REPORT_DIR={runtime_dir}",
    ]
    proxy_env = codex_home / "proxy.env"
    if proxy_env.is_file():
        lines.append(f"BASH_ENV={proxy_env}")
    lines.extend([command, END_MARKER])
    return "\n".join(lines)


def reconcile_crontab(current: str, codex_home: Path, runtime_dir: Path, cron_time: str, timezone_name: str) -> tuple[str, bool]:
    target = cron_block(codex_home, runtime_dir, cron_time, timezone_name)
    lines = current.splitlines()
    kept: list[str] = []
    in_block = False
    runtime_runner = str(runtime_dir / "run_ai_daily_report.sh")
    for line in lines:
        if line.strip() == BEGIN_MARKER:
            in_block = True
            continue
        if line.strip() == END_MARKER:
            in_block = False
            continue
        if in_block:
            continue
        if runtime_runner in line:
            continue
        kept.append(line)
    redundant_env = {
        "SHELL=/bin/bash",
        f"HOME={codex_home.parent}",
        f"CODEX_HOME={codex_home}",
        f"AI_DAILY_REPORT_DIR={runtime_dir}",
    }
    meaningful = [line.strip() for line in kept if line.strip()]
    if meaningful and all(line in redundant_env for line in meaningful):
        kept = []
    while kept and not kept[-1].strip():
        kept.pop()
    new_text = "\n".join(kept + ([""] if kept else []) + [target]) + "\n"
    return new_text, new_text != (current if current.endswith("\n") or not current else current + "\n")


def install_crontab(content: str) -> None:
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"ai-daily-cron-{os.getpid()}"
    tmp.write_text(content, encoding="utf-8")
    try:
        subprocess.run(["crontab", str(tmp)], check=True)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def setup(args: argparse.Namespace) -> dict[str, Any]:
    codex_home, checked_homes = resolve_codex_home(args.codex_home)
    runtime_dir = Path(args.runtime_dir).expanduser() if args.runtime_dir else codex_home / "daily_report"
    hooks_file = Path(args.hooks_file).expanduser() if args.hooks_file else None
    actions: list[dict[str, Any]] = []

    required = [
        SCRIPT_DIR / "run_ai_daily_report.sh",
        SCRIPT_DIR / "collect_evidence.py",
        SCRIPT_DIR / "hook_setup.py",
        SCRIPT_DIR / "outcome_stop_hook.py",
    ]
    for path in required:
        if not path.is_file():
            raise SetupError(f"missing skill file: {path}")

    for directory in (runtime_dir, runtime_dir / "logs", runtime_dir / "report_files", codex_home / ".tmp"):
        exists = directory.is_dir()
        if not args.dry_run:
            directory.mkdir(parents=True, exist_ok=True)
        actions.append({"type": "dir", "path": str(directory), "action": "present" if exists else "create"})

    wrappers = {
        runtime_dir / "run_ai_daily_report.sh": daily_wrapper(codex_home, runtime_dir),
        runtime_dir / "run_collect_evidence.sh": evidence_wrapper(codex_home, runtime_dir),
    }
    for path, content in wrappers.items():
        changed = (not path.exists()) or path.read_text(encoding="utf-8") != content
        backup = None
        if changed and not args.dry_run:
            backup = write_text(path, content, mode=0o755)
        actions.append({"type": "wrapper", "path": str(path), "changed": changed, "backup": str(backup) if backup else None})

    hook_result = run_hook_setup(codex_home, hooks_file, args.dry_run)
    actions.append({"type": "hooks", "result": hook_result})

    if not args.no_cron:
        current_cron, ok = read_crontab()
        if not ok:
            if not args.dry_run:
                raise SetupError(f"cannot read crontab: {current_cron}")
            actions.append(
                {
                    "type": "cron",
                    "changed": None,
                    "cron_time": args.cron_time,
                    "command": str(runtime_dir / "run_ai_daily_report.sh"),
                    "dry_run": True,
                    "requires_escalation": True,
                    "error": current_cron,
                }
            )
        else:
            new_cron, changed = reconcile_crontab(
                current_cron, codex_home, runtime_dir, args.cron_time, args.timezone
            )
            if changed and not args.dry_run:
                install_crontab(new_cron)
            actions.append(
                {
                    "type": "cron",
                    "changed": changed,
                    "cron_time": args.cron_time,
                    "command": str(runtime_dir / "run_ai_daily_report.sh"),
                    "dry_run": bool(args.dry_run),
                }
            )

    return {
        "skill": "ai-daily-report",
        "codex_home": str(codex_home),
        "checked_codex_homes": [str(path) for path in checked_homes],
        "runtime_dir": str(runtime_dir),
        "dry_run": bool(args.dry_run),
        "actions": actions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Setup or reconcile AI daily report runtime.")
    parser.add_argument("--codex-home", help="Target Codex home. Overrides auto-discovery.")
    parser.add_argument("--runtime-dir", help="Runtime daily_report directory. Defaults to <codex-home>/daily_report.")
    parser.add_argument("--hooks-file", help="Target hooks.json. Defaults to <codex-home>/hooks.json.")
    parser.add_argument("--cron-time", default=DEFAULT_CRON_TIME, help="Cron time fields before the command.")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="Timezone exported by the cron entry.")
    parser.add_argument("--no-cron", action="store_true", help="Do not install or reconcile cron.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
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
        print(f"runtime_dir: {result['runtime_dir']}")
        print(f"dry_run: {result['dry_run']}")
        for action in result["actions"]:
            print(json.dumps(action, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
