#!/usr/bin/env python3
"""Probe Codex Stop hook payloads and transcript token data."""

from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
LOG_DIR = Path(
    os.environ.get("CODEX_USAGE_TEST_ROOT", str(CODEX_HOME / "usage" / "test-hooks"))
).expanduser()
LOG_PATH = LOG_DIR / "stop-hook-events.jsonl"
LOCK_PATH = LOG_DIR / ".stop-hook-events.lock"


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def read_stdin_json() -> tuple[dict[str, Any], str]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}, raw
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"_parse_error": str(exc)}, raw
    if isinstance(payload, dict):
        return payload, raw
    return {"_non_object_payload": payload}, raw


def iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield line_no, json.loads(line)
            except json.JSONDecodeError:
                continue


def extract_session_meta(transcript_path: Path) -> dict[str, Any]:
    for _, obj in iter_jsonl(transcript_path):
        if obj.get("type") == "session_meta":
            payload = obj.get("payload")
            return payload if isinstance(payload, dict) else obj
    return {}


def extract_latest_token_count(transcript_path: Path) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for line_no, obj in iter_jsonl(transcript_path):
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        if obj.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        latest = {
            "line_no": line_no,
            "timestamp": obj.get("timestamp"),
            "info": info if isinstance(info, dict) else {},
        }
    return latest


def subagent_info(session_meta: dict[str, Any]) -> dict[str, Any]:
    source = session_meta.get("source")
    if not isinstance(source, dict):
        return {"is_subagent": False, "parent_thread_id": None, "source": source}

    subagent = source.get("subagent")
    parent_thread_id = None
    if isinstance(subagent, dict):
        thread_spawn = subagent.get("thread_spawn")
        if isinstance(thread_spawn, dict):
            parent_thread_id = thread_spawn.get("parent_thread_id")
        parent_thread_id = parent_thread_id or subagent.get("parent_thread_id")

    return {
        "is_subagent": bool(subagent),
        "parent_thread_id": parent_thread_id,
        "source": source,
    }


def transcript_summary(raw_path: Any) -> dict[str, Any]:
    path = Path(raw_path).expanduser() if isinstance(raw_path, str) else None
    readable = bool(path and path.is_file())
    session_meta: dict[str, Any] = {}
    latest_token_count: dict[str, Any] = {}
    transcript_error = None

    if readable and path is not None:
        try:
            session_meta = extract_session_meta(path)
            latest_token_count = extract_latest_token_count(path)
        except OSError as exc:
            transcript_error = str(exc)

    usage_info = latest_token_count.get("info") if isinstance(latest_token_count, dict) else {}
    if not isinstance(usage_info, dict):
        usage_info = {}

    return {
        "path": str(path) if path else raw_path,
        "readable": readable,
        "error": transcript_error,
        "session_meta": {
            "id": session_meta.get("id") or session_meta.get("session_id"),
            "session_id": session_meta.get("session_id"),
            "parent_thread_id": session_meta.get("parent_thread_id"),
            "cwd": session_meta.get("cwd"),
            "model": session_meta.get("model"),
            "source": session_meta.get("source"),
            "thread_source": session_meta.get("thread_source"),
            "agent_nickname": session_meta.get("agent_nickname"),
            "agent_role": session_meta.get("agent_role"),
        },
        "subagent": subagent_info(session_meta),
        "latest_token_count": {
            "line_no": latest_token_count.get("line_no"),
            "timestamp": latest_token_count.get("timestamp"),
            "total_token_usage": usage_info.get("total_token_usage"),
            "last_token_usage": usage_info.get("last_token_usage"),
            "model_context_window": usage_info.get("model_context_window"),
        },
    }


def append_record(record: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        with LOG_PATH.open("a", encoding="utf-8") as log_fh:
            log_fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def main() -> int:
    payload, raw = read_stdin_json()
    trigger_transcript = transcript_summary(payload.get("transcript_path"))
    accounting_transcript = trigger_transcript
    if payload.get("hook_event_name") == "SubagentStop" and payload.get("agent_transcript_path"):
        accounting_transcript = transcript_summary(payload.get("agent_transcript_path"))

    accounting_meta = accounting_transcript["session_meta"]
    accounting_subagent = accounting_transcript["subagent"]
    accounting_session_id = (
        accounting_meta.get("id")
        or payload.get("agent_id")
        or payload.get("session_id")
    )
    owner_session_id = accounting_session_id
    if accounting_subagent.get("is_subagent"):
        owner_session_id = (
            accounting_subagent.get("parent_thread_id")
            or accounting_meta.get("parent_thread_id")
            or accounting_meta.get("session_id")
            or accounting_session_id
        )

    record = {
        "recorded_at": utc_now(),
        "hook_event_name": payload.get("hook_event_name"),
        "session_id": payload.get("session_id"),
        "turn_id": payload.get("turn_id"),
        "cwd": payload.get("cwd"),
        "model": payload.get("model"),
        "payload_keys": sorted(payload.keys()),
        "stdin_sha256": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
        "agent_id": payload.get("agent_id"),
        "agent_type": payload.get("agent_type"),
        "accounting_session_id": accounting_session_id,
        "owner_session_id": owner_session_id,
        "trigger_transcript": trigger_transcript,
        "accounting_transcript": accounting_transcript,
        "transcript_path": trigger_transcript["path"],
        "agent_transcript_path": payload.get("agent_transcript_path"),
        "transcript_readable": trigger_transcript["readable"],
        "transcript_error": trigger_transcript["error"],
        "session_meta": accounting_transcript["session_meta"],
        "subagent": accounting_transcript["subagent"],
        "latest_token_count": accounting_transcript["latest_token_count"],
    }

    append_record(record)
    print(
        "usage-mgr hook recorded "
        f"event={record['hook_event_name']} "
        f"session={record['accounting_session_id']} owner={record['owner_session_id']} "
        f"subagent={record['subagent']['is_subagent']} "
        f"token_count={bool(record['latest_token_count']['total_token_usage'])}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
