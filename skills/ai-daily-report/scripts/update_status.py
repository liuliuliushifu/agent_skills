#!/usr/bin/env python3
"""Atomically update the AI daily report runtime status file."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
DEFAULT_DAILY_REPORT_DIR = Path(os.environ.get("AI_DAILY_REPORT_DIR", str(DEFAULT_CODEX_HOME / "daily_report")))
DEFAULT_STATUS_FILE = DEFAULT_DAILY_REPORT_DIR / "status.json"
TERMINAL_STATES = {"skipped", "success", "failed", "validation_failed", "sync_failed"}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")


def parse_value(value: str) -> Any:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def set_dotted(data: dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = dotted_key.split(".")
    cursor: dict[str, Any] = data
    for part in parts[:-1]:
        next_value = cursor.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            cursor[part] = next_value
        cursor = next_value
    cursor[parts[-1]] = value


def load_status(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_status(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        json.dump(data, tmp, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Update AI daily report status.json.")
    parser.add_argument("--status-file", type=Path, default=DEFAULT_STATUS_FILE)
    parser.add_argument("--target-date", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--reason", default="")
    parser.add_argument("--log-file")
    parser.add_argument("--report-file")
    parser.add_argument("--remote-file")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args()

    if args.reset:
        status: dict[str, Any] = {
            "date": args.target_date,
            "target_date": args.target_date,
            "run_date": dt.date.today().isoformat(),
            "started_at": now_iso(),
            "validation": {"passed": None, "errors": [], "warnings": []},
            "codex": {"started": False},
            "sync": {"attempted": False, "passed": False},
        }
    else:
        status = load_status(args.status_file)
        status.setdefault("target_date", args.target_date)
        status.setdefault("started_at", now_iso())

    status["state"] = args.state
    status["reason"] = args.reason
    status["updated_at"] = now_iso()

    if args.log_file:
        status["log_file"] = args.log_file
    if args.report_file:
        status["report_file"] = args.report_file
    if args.remote_file:
        status.setdefault("sync", {})
        status["sync"]["remote_file"] = args.remote_file

    for item in args.set:
        if "=" not in item:
            raise SystemExit(f"--set expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        set_dotted(status, key, parse_value(value))

    if args.state in TERMINAL_STATES:
        status["finished_at"] = now_iso()

    write_status(args.status_file, status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
