#!/usr/bin/env python3
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict


HOOK_ROOT = Path(__file__).resolve().parent
LOG_PATH = HOOK_ROOT / "logs/probe-events.jsonl"


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def safe_str(value: Any, limit: int = 4096) -> str:
    text = "" if value is None else str(value)
    if len(text) > limit:
        return text[:limit] + "...<truncated>"
    return text


def main() -> int:
    raw = sys.stdin.read()
    payload: Dict[str, Any] = {}
    parse_error = ""
    try:
        parsed = json.loads(raw) if raw.strip() else {}
        if isinstance(parsed, dict):
            payload = parsed
        else:
            parse_error = "stdin JSON is not an object"
    except Exception as exc:
        parse_error = "{}: {}".format(exc.__class__.__name__, exc)

    event = safe_str(payload.get("hook_event_name") or os.environ.get("CODEX_HOOK_EVENT") or "unknown")
    record = {
        "created_at": now_iso(),
        "event": event,
        "session_id": safe_str(payload.get("session_id")),
        "turn_id": safe_str(payload.get("turn_id")),
        "transcript_path": safe_str(payload.get("transcript_path")),
        "cwd": safe_str(payload.get("cwd")),
        "model": safe_str(payload.get("model")),
        "payload_keys": sorted(str(key) for key in payload.keys()),
        "stdin_bytes": len(raw.encode("utf-8", errors="replace")),
        "stdin_sha256": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
        "parse_error": parse_error,
    }

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
