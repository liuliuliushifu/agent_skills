#!/usr/bin/env python3
"""Install/update Codex hooks used by AI daily report."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

OUTCOME_SCRIPT_SUFFIX = "skills/ai-daily-report/scripts/outcome_stop_hook.py"


def command(path: Path) -> str:
    return f"python3 {path}"


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


def resolve_codex_home(explicit: str | None = None) -> tuple[Path, list[Path]]:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_dir():
            raise FileNotFoundError(f"explicit codex home does not exist: {path}")
        return path, [path]

    candidates = candidate_codex_homes()
    for path in candidates:
        if path.is_dir():
            return path, candidates

    rendered = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(f"cannot find Codex home; checked: {rendered}")


def desired_hooks(codex_home: Path) -> dict[str, list[dict[str, Any]]]:
    outcome_hook = codex_home / OUTCOME_SCRIPT_SUFFIX
    if not outcome_hook.is_file():
        raise FileNotFoundError(
            f"AI daily outcome hook not found: {outcome_hook}; install ai-daily-report under the target Codex home first"
        )
    return {
        "Stop": [
            {
                "type": "command",
                "command": command(outcome_hook),
                "timeout": 10,
                "async": False,
                "statusMessage": "Capturing AI daily turn bundle",
            },
        ],
    }


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


def command_matches(item: dict[str, Any], desired_command: str) -> bool:
    raw = item.get("command")
    if not isinstance(raw, str):
        return False
    return raw == desired_command or raw.endswith(OUTCOME_SCRIPT_SUFFIX)


def ensure_hook(hook_list: list[dict[str, Any]], desired: dict[str, Any]) -> str:
    desired_command = str(desired["command"])
    matched_index: int | None = None
    duplicate_indexes: list[int] = []

    for index, item in enumerate(hook_list):
        if not isinstance(item, dict):
            continue
        if command_matches(item, desired_command):
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


def install_hooks(data: dict[str, Any], codex_home: Path) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for event, desired_items in desired_hooks(codex_home).items():
        hook_list = event_group(data, event)
        for desired in desired_items:
            action = ensure_hook(hook_list, desired)
            results.append({"event": event, "command": str(desired["command"]), "action": action})
    return results


def write_hooks(path: Path, data: dict[str, Any]) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        backup.write_bytes(path.read_bytes())
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description="Install/update AI daily report Codex hooks.")
    parser.add_argument("--codex-home", help="Target Codex home. Overrides auto-discovery.")
    parser.add_argument("--hooks-file", help="Target hooks.json. Defaults to <codex-home>/hooks.json.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    codex_home, candidates = resolve_codex_home(args.codex_home)
    hooks_file = Path(args.hooks_file).expanduser() if args.hooks_file else codex_home / "hooks.json"
    data = load_hooks(hooks_file)
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    results = install_hooks(data, codex_home)
    after = json.dumps(data, ensure_ascii=False, sort_keys=True)
    changed = before != after
    backup = None
    if changed and not args.dry_run:
        backup = write_hooks(hooks_file, data)

    output = {
        "codex_home": str(codex_home),
        "checked_codex_homes": [str(path) for path in candidates],
        "hooks_file": str(hooks_file),
        "changed": changed,
        "dry_run": bool(args.dry_run),
        "backup": str(backup) if backup else None,
        "results": results,
    }
    if args.json:
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    else:
        print(f"codex_home: {codex_home}")
        print(f"hooks_file: {hooks_file}")
        print(f"changed: {changed}")
        if backup:
            print(f"backup: {backup}")
        for result in results:
            print(f"{result['event']}: {result['action']} {result['command']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
