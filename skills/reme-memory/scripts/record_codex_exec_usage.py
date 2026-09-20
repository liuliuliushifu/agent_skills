#!/usr/bin/env python3
"""Record ReMe codex exec --json usage through usage-mgr."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent


def default_usage_recorder() -> Path:
    if os.environ.get("CODEX_HOME"):
        return Path(os.environ["CODEX_HOME"]) / "skills/usage-mgr/scripts/record_external_usage.py"
    for candidate in [SCRIPT_DIR, *SCRIPT_DIR.parents]:
        recorder = candidate / "skills/usage-mgr/scripts/record_external_usage.py"
        if recorder.is_file():
            return recorder
    return SCRIPT_DIR.parents[2] / "skills/usage-mgr/scripts/record_external_usage.py"


USAGE_RECORDER = Path(os.environ.get("REME_USAGE_RECORDER", str(default_usage_recorder())))


def iter_jsonl(path: str | None):
    if path and path != "-":
        handle = Path(path).open("r", encoding="utf-8")
        close = True
    else:
        handle = sys.stdin
        close = False
    try:
        for line_no, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                yield line_no, None, f"{exc.msg} at column {exc.colno}"
                continue
            if isinstance(payload, dict):
                yield line_no, payload, ""
    finally:
        if close:
            handle.close()


def extract_usage(path: str | None, event_id_override: str | None = None) -> tuple[dict[str, Any], str, str]:
    thread_id = ""
    latest_usage: dict[str, Any] | None = None
    latest_line_no = 0
    usage_event_count = 0
    invalid_lines: list[str] = []
    for line_no, payload, error in iter_jsonl(path):
        if error:
            invalid_lines.append(f"line {line_no}: {error}")
            continue
        if payload is None:
            continue
        if payload.get("type") == "thread.started":
            thread_id = str(payload.get("thread_id") or thread_id)
        if payload.get("type") == "turn.completed" and isinstance(payload.get("usage"), dict):
            usage_event_count += 1
            latest_usage = payload["usage"]
            latest_line_no = line_no
    if invalid_lines:
        raise SystemExit("codex exec JSONL contains invalid JSON: " + "; ".join(invalid_lines[:3]))
    if latest_usage is None:
        raise SystemExit("codex exec JSONL did not contain turn.completed usage")
    if usage_event_count > 1:
        raise SystemExit(
            "codex exec JSONL contains multiple turn.completed usage events; "
            "refuse to guess whether they are per-turn or cumulative"
        )
    if not thread_id and not event_id_override:
        raise SystemExit("codex exec JSONL did not contain thread.started; pass --event-id to use an explicit stable id")
    if event_id_override:
        return latest_usage, event_id_override, thread_id
    raw_key = json.dumps(
        {"thread_id": thread_id, "line_no": latest_line_no, "usage": latest_usage},
        ensure_ascii=False,
        sort_keys=True,
    )
    event_id = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:24]
    return latest_usage, event_id, thread_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Record ReMe codex exec --json usage as agent=ReMe.")
    parser.add_argument("--events-jsonl", default="-", help="codex exec --json output file, or '-' for stdin")
    parser.add_argument("--event-id", help="Stable event id override")
    parser.add_argument("--usage-date", help="Usage date override")
    parser.add_argument("--model", default="", help="Codex model name")
    parser.add_argument("--cwd", default="", help="Associated cwd")
    parser.add_argument("--usage-root", help="Override usage ledger root")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not USAGE_RECORDER.is_file():
        raise SystemExit(f"usage recorder not found: {USAGE_RECORDER}")

    usage, event_id, thread_id = extract_usage(args.events_jsonl, args.event_id)
    cmd = [
        sys.executable,
        str(USAGE_RECORDER),
        "--agent",
        "ReMe",
        "--source",
        "codex_exec_json",
        "--event-id",
        event_id,
        "--usage-json",
        json.dumps(usage, ensure_ascii=False, sort_keys=True),
        "--json",
    ]
    if args.usage_date:
        cmd.extend(["--usage-date", args.usage_date])
    if args.model:
        cmd.extend(["--model", args.model])
    if args.cwd:
        cmd.extend(["--cwd", args.cwd])
    if args.usage_root:
        cmd.extend(["--usage-root", args.usage_root])
    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout.strip(), file=sys.stderr)
        return result.returncode
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        if result.stdout:
            print(result.stdout.strip(), file=sys.stderr)
        raise SystemExit(f"usage recorder returned invalid JSON: {exc}") from exc
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    record = payload.get("record") or payload.get("event") or {}
    delta = record.get("delta") or record.get("external_usage") or {}
    print(
        "codex exec usage {status}: reason={reason} agent=ReMe thread={thread} total={total}".format(
            status=payload.get("status", "unknown"),
            reason=payload.get("reason", ""),
            thread=thread_id or "explicit-event-id",
            total=delta.get("total_tokens", 0),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
