#!/usr/bin/env python3
"""Extract normalized per-turn bundles from Codex transcript JSONL files."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import mmap
import re
import sys
from pathlib import Path
from typing import Any


TZ = dt.timezone(dt.timedelta(hours=8))
SCHEMA_VERSION = "turn_bundle.v1"
EXTRACTOR_VERSION = "2026-07-08.1"
DEFAULT_MAX_TEXT_CHARS = 30000
DEFAULT_FAST_MAX_SCAN_BYTES = 64 * 1024 * 1024
NOISE_PREFIXES = (
    "<environment_context>",
    "<skills_instructions>",
    "<plugins_instructions>",
    "<permissions instructions>",
    "<skill>",
)
SENSITIVE_RE = re.compile(
    r"\b(password|passwd|token|secret|authorization|cookie|api[_-]?key)\b"
    r"([\"'\s]*[:=]\s*[\"']?)([^\s\"']{6,})|"
    r"Bearer\s+[A-Za-z0-9._~+/-]{10,}|"
    r"BEGIN .*PRIVATE KEY",
    re.IGNORECASE,
)


class ExtractError(Exception):
    """Expected extraction failure."""


def parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def iso_time(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(TZ).replace(microsecond=0).isoformat()


def read_jsonl(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield line_no, obj


def payload_of(obj: dict[str, Any]) -> dict[str, Any]:
    payload = obj.get("payload")
    return payload if isinstance(payload, dict) else {}


def metadata_turn_id(payload: dict[str, Any]) -> str | None:
    meta = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(meta, dict) and isinstance(meta.get("turn_id"), str):
        return meta["turn_id"]
    if isinstance(payload.get("turn_id"), str):
        return payload["turn_id"]
    return None


def normalize_text(text: str) -> str:
    text = text.replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def hash_prompt_text(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def redact_sensitive(text: str) -> str:
    return SENSITIVE_RE.sub(lambda m: f"{m.group(1) or 'secret'}{m.group(2) or '='}[REDACTED]", text)


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "\n...[truncated]", True


def extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return normalize_text(content)
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            texts.append(text)
    return normalize_text("\n".join(texts))


def is_noise_user_text(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped:
        return True
    return any(stripped.startswith(prefix) for prefix in NOISE_PREFIXES)


def initial_turn(turn_id: str, line_no: int, timestamp: dt.datetime | None) -> dict[str, Any]:
    return {
        "turn_id": turn_id,
        "start_line": line_no,
        "end_line": line_no,
        "started_at": timestamp,
        "completed_at": None,
        "user_messages": [],
        "assistant_updates": [],
        "assistant_final_messages": [],
        "task_complete_message": "",
        "tool_calls": [],
        "tool_output_count": 0,
        "tool_output_chars": 0,
        "user_message_line": None,
        "task_complete_line": None,
        "status": "in_progress",
    }


def add_unique(items: list[str], text: str) -> None:
    text = normalize_text(text)
    if text and text not in items:
        items.append(text)


def load_session_meta(path: Path) -> dict[str, Any]:
    for _line_no, obj in read_jsonl(path):
        if obj.get("type") == "session_meta":
            payload = payload_of(obj)
            return payload if payload else obj
    return {}


def extract_turns(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    session_meta = load_session_meta(path)
    turns: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None

    for line_no, obj in read_jsonl(path):
        payload = payload_of(obj)
        record_type = obj.get("type")
        payload_type = payload.get("type")
        timestamp = parse_time(obj.get("timestamp"))
        turn_id = metadata_turn_id(payload)

        if record_type == "event_msg" and payload_type == "task_started":
            turn_id = payload.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id:
                continue
            current = by_id.get(turn_id)
            if current is None:
                current = initial_turn(turn_id, line_no, timestamp)
                by_id[turn_id] = current
                turns.append(current)
            current["start_line"] = min(current["start_line"], line_no)
            current["started_at"] = current["started_at"] or timestamp
            continue

        if turn_id and turn_id in by_id:
            current = by_id[turn_id]
        if current is None:
            continue

        current["end_line"] = max(current["end_line"], line_no)

        if record_type == "event_msg" and payload_type == "user_message":
            message = payload.get("message")
            if isinstance(message, str):
                add_unique(current["user_messages"], message)
                current["user_message_line"] = current["user_message_line"] or line_no
            continue

        if record_type == "response_item" and payload.get("role") == "user":
            text = extract_content_text(payload.get("content"))
            if text and not is_noise_user_text(text):
                add_unique(current["user_messages"], text)
                current["user_message_line"] = current["user_message_line"] or line_no
            continue

        if record_type == "event_msg" and payload_type == "agent_message":
            message = payload.get("message")
            if isinstance(message, str):
                phase = payload.get("phase")
                if phase == "final_answer":
                    add_unique(current["assistant_final_messages"], message)
                elif phase == "commentary":
                    add_unique(current["assistant_updates"], message)
            continue

        if record_type == "response_item" and payload.get("role") == "assistant":
            text = extract_content_text(payload.get("content"))
            if text:
                phase = payload.get("phase")
                if phase == "final_answer":
                    add_unique(current["assistant_final_messages"], text)
                elif phase == "commentary":
                    add_unique(current["assistant_updates"], text)
            continue

        if record_type == "response_item" and payload_type == "function_call":
            name = payload.get("name")
            if isinstance(name, str) and name:
                current["tool_calls"].append(name)
            continue

        if record_type == "response_item" and payload_type == "function_call_output":
            output = payload.get("output")
            current["tool_output_count"] += 1
            if isinstance(output, str):
                current["tool_output_chars"] += len(output)
            continue

        if record_type == "event_msg" and payload_type == "task_complete":
            complete_turn_id = payload.get("turn_id")
            if isinstance(complete_turn_id, str) and complete_turn_id in by_id:
                current = by_id[complete_turn_id]
            message = payload.get("last_agent_message")
            if isinstance(message, str):
                current["task_complete_message"] = message
            current["completed_at"] = timestamp
            current["task_complete_line"] = line_no
            current["status"] = "completed"
            current["end_line"] = line_no
            current = None

    return session_meta, turns


def is_task_event(obj: dict[str, Any], payload_type: str, turn_id: str) -> bool:
    if obj.get("type") != "event_msg":
        return False
    payload = payload_of(obj)
    return payload.get("type") == payload_type and payload.get("turn_id") == turn_id


def decode_json_line(raw: bytes) -> dict[str, Any] | None:
    raw = raw.rstrip(b"\r")
    if not raw.strip():
        return None
    try:
        obj = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def count_newlines_before_offset(path: Path, offset: int) -> int:
    count = 0
    remaining = max(offset, 0)
    with path.open("rb") as fh:
        while remaining:
            chunk = fh.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            count += chunk.count(b"\n")
    return count


def find_turn_block_reverse(
    path: Path,
    turn_id: str,
    *,
    max_scan_bytes: int = DEFAULT_FAST_MAX_SCAN_BYTES,
) -> list[tuple[int, dict[str, Any] | None]]:
    """Find one completed turn by scanning backward from the transcript tail."""

    size = path.stat().st_size
    if size == 0:
        raise ExtractError(f"empty transcript: {path}")

    block: list[tuple[int, bytes, dict[str, Any] | None]] = []
    collecting = False
    scanned = 0

    with path.open("rb") as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            pos = size
            while pos > 0 and scanned < max_scan_bytes:
                previous_newline = mm.rfind(b"\n", 0, pos)
                if previous_newline < 0:
                    start = 0
                    raw = mm[start:pos]
                    pos = 0
                else:
                    start = previous_newline + 1
                    raw = mm[start:pos]
                    pos = previous_newline

                scanned += max(1, len(raw) + 1)
                obj = decode_json_line(raw)

                if not collecting:
                    if obj is not None and is_task_event(obj, "task_complete", turn_id):
                        collecting = True
                        block.append((start, raw, obj))
                    continue

                block.append((start, raw, obj))
                if obj is not None and is_task_event(obj, "task_started", turn_id):
                    block.reverse()
                    base_line = count_newlines_before_offset(path, block[0][0]) + 1
                    return [
                        (base_line + index, item_obj)
                        for index, (_offset, _raw, item_obj) in enumerate(block)
                    ]

    if collecting:
        raise ExtractError(
            f"turn start not found within fast scan window for turn_id={turn_id}"
        )
    raise ExtractError(
        f"completed turn not found within fast scan window for turn_id={turn_id}"
    )


def extract_turn_from_records(
    records: list[tuple[int, dict[str, Any] | None]],
    turn_id: str,
) -> dict[str, Any]:
    turn: dict[str, Any] | None = None

    for line_no, obj in records:
        if obj is None:
            continue
        payload = payload_of(obj)
        record_type = obj.get("type")
        payload_type = payload.get("type")
        timestamp = parse_time(obj.get("timestamp"))
        metadata_id = metadata_turn_id(payload)

        if record_type == "event_msg" and payload_type == "task_started":
            started_turn_id = payload.get("turn_id")
            if started_turn_id != turn_id:
                continue
            turn = initial_turn(turn_id, line_no, timestamp)
            continue

        if turn is None:
            continue
        if metadata_id and metadata_id != turn_id:
            continue

        turn["end_line"] = max(turn["end_line"], line_no)

        if record_type == "event_msg" and payload_type == "user_message":
            message = payload.get("message")
            if isinstance(message, str):
                add_unique(turn["user_messages"], message)
                turn["user_message_line"] = turn["user_message_line"] or line_no
            continue

        if record_type == "response_item" and payload.get("role") == "user":
            text = extract_content_text(payload.get("content"))
            if text and not is_noise_user_text(text):
                add_unique(turn["user_messages"], text)
                turn["user_message_line"] = turn["user_message_line"] or line_no
            continue

        if record_type == "event_msg" and payload_type == "agent_message":
            message = payload.get("message")
            if isinstance(message, str):
                phase = payload.get("phase")
                if phase == "final_answer":
                    add_unique(turn["assistant_final_messages"], message)
                elif phase == "commentary":
                    add_unique(turn["assistant_updates"], message)
            continue

        if record_type == "response_item" and payload.get("role") == "assistant":
            text = extract_content_text(payload.get("content"))
            if text:
                phase = payload.get("phase")
                if phase == "final_answer":
                    add_unique(turn["assistant_final_messages"], text)
                elif phase == "commentary":
                    add_unique(turn["assistant_updates"], text)
            continue

        if record_type == "response_item" and payload_type == "function_call":
            name = payload.get("name")
            if isinstance(name, str) and name:
                turn["tool_calls"].append(name)
            continue

        if record_type == "response_item" and payload_type == "function_call_output":
            output = payload.get("output")
            turn["tool_output_count"] += 1
            if isinstance(output, str):
                turn["tool_output_chars"] += len(output)
            continue

        if record_type == "event_msg" and payload_type == "task_complete":
            complete_turn_id = payload.get("turn_id")
            if complete_turn_id != turn_id:
                continue
            message = payload.get("last_agent_message")
            if isinstance(message, str):
                turn["task_complete_message"] = message
            turn["completed_at"] = timestamp
            turn["task_complete_line"] = line_no
            turn["status"] = "completed"
            turn["end_line"] = line_no

    if turn is None:
        raise ExtractError(f"turn_id not found in fast block: {turn_id}")
    if turn["status"] != "completed":
        raise ExtractError(f"turn_id is not completed in fast block: {turn_id}")
    return turn


def build_bundle(
    transcript_path: Path,
    session_meta: dict[str, Any],
    turn: dict[str, Any],
    *,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    cutoff_at: dt.datetime | None = None,
) -> dict[str, Any]:
    session_id = (
        session_meta.get("id")
        or session_meta.get("session_id")
        or transcript_path.stem.rsplit("-", 1)[-1]
    )
    thread_session_id = session_meta.get("session_id") or session_id
    cwd = session_meta.get("cwd") or ""
    user_prompt = "\n\n".join(turn["user_messages"])
    final_answer = turn["task_complete_message"] or "\n\n".join(turn["assistant_final_messages"])
    updates = turn["assistant_updates"][-10:]

    user_prompt = redact_sensitive(normalize_text(user_prompt))
    final_answer = redact_sensitive(normalize_text(final_answer))
    updates = [redact_sensitive(normalize_text(item)) for item in updates if normalize_text(item)]
    user_prompt, user_truncated = truncate_text(user_prompt, max_text_chars)
    final_answer, final_truncated = truncate_text(final_answer, max_text_chars)

    hash_payload = {
        "schema_version": SCHEMA_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "session_id": session_id,
        "turn_id": turn["turn_id"],
        "cwd": cwd,
        "user_prompt": hash_prompt_text(user_prompt),
    }
    bundle_hash = hashlib.sha256(
        json.dumps(hash_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    event_time = turn["completed_at"] or turn["started_at"] or cutoff_at
    bundle_date = (event_time or dt.datetime.now(TZ)).astimezone(TZ).date().isoformat()
    tool_names = sorted(set(turn["tool_calls"]))

    return {
        "schema_version": SCHEMA_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "bundle_hash": bundle_hash,
        "bundle_date": bundle_date,
        "transcript_path": str(transcript_path),
        "session_id": session_id,
        "thread_session_id": thread_session_id,
        "turn_id": turn["turn_id"],
        "turn_status": turn["status"],
        "cwd": cwd,
        "started_at": iso_time(turn["started_at"]),
        "completed_at": iso_time(turn["completed_at"]),
        "cutoff_at": iso_time(cutoff_at) if turn["status"] != "completed" else None,
        "source_lines": {
            "start": turn["start_line"],
            "end": turn["end_line"],
            "user_message": turn["user_message_line"],
            "task_complete": turn["task_complete_line"],
        },
        "user_prompt": user_prompt,
        "agent_final_answer": final_answer,
        "assistant_updates": updates,
        "text_truncated": {
            "user_prompt": user_truncated,
            "agent_final_answer": final_truncated,
        },
        "tool_summary": {
            "call_count": len(turn["tool_calls"]),
            "output_count": turn["tool_output_count"],
            "output_chars": turn["tool_output_chars"],
            "tool_names": tool_names,
        },
    }


def extract_bundles(
    transcript_path: Path,
    *,
    turn_id: str | None = None,
    include_in_progress: bool = False,
    cutoff_at: dt.datetime | None = None,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> list[dict[str, Any]]:
    transcript_path = transcript_path.expanduser()
    if not transcript_path.is_file():
        raise ExtractError(f"transcript not found: {transcript_path}")
    session_meta, turns = extract_turns(transcript_path)
    selected = [turn for turn in turns if turn_id is None or turn["turn_id"] == turn_id]
    if turn_id and not selected:
        raise ExtractError(f"turn_id not found: {turn_id}")
    if not include_in_progress:
        selected = [turn for turn in selected if turn["status"] == "completed"]
    return [
        build_bundle(
            transcript_path,
            session_meta,
            turn,
            max_text_chars=max_text_chars,
            cutoff_at=cutoff_at,
        )
        for turn in selected
    ]


def extract_bundle_fast(
    transcript_path: Path,
    *,
    turn_id: str,
    max_scan_bytes: int = DEFAULT_FAST_MAX_SCAN_BYTES,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> dict[str, Any]:
    transcript_path = transcript_path.expanduser()
    if not transcript_path.is_file():
        raise ExtractError(f"transcript not found: {transcript_path}")
    session_meta = load_session_meta(transcript_path)
    records = find_turn_block_reverse(transcript_path, turn_id, max_scan_bytes=max_scan_bytes)
    turn = extract_turn_from_records(records, turn_id)
    return build_bundle(
        transcript_path,
        session_meta,
        turn,
        max_text_chars=max_text_chars,
    )


def parse_cutoff(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = parse_time(value)
    if parsed is None:
        raise ExtractError(f"invalid cutoff time: {value}")
    return parsed


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract normalized Codex turn bundles.")
    parser.add_argument("--transcript", required=True, help="Transcript JSONL path.")
    parser.add_argument("--turn-id", help="Extract one turn id. Omit with --all to extract all turns.")
    parser.add_argument("--all", action="store_true", help="Extract all turns from the transcript.")
    parser.add_argument("--fast", action="store_true", help="Use reverse-scan fast path for one completed turn.")
    parser.add_argument("--include-in-progress", action="store_true", help="Include started but incomplete turns.")
    parser.add_argument("--cutoff-at", help="ISO timestamp used for in-progress bundles.")
    parser.add_argument("--max-text-chars", type=int, default=DEFAULT_MAX_TEXT_CHARS)
    parser.add_argument("--fast-max-scan-bytes", type=int, default=DEFAULT_FAST_MAX_SCAN_BYTES)
    parser.add_argument("--output", help="Write JSON/JSONL output to this path.")
    args = parser.parse_args()

    if not args.all and not args.turn_id:
        parser.error("pass --turn-id or --all")

    try:
        if args.fast:
            if args.all:
                raise ExtractError("--fast cannot be combined with --all")
            if args.include_in_progress:
                raise ExtractError("--fast only supports completed turns")
            bundles = [
                extract_bundle_fast(
                    Path(args.transcript),
                    turn_id=args.turn_id,
                    max_scan_bytes=args.fast_max_scan_bytes,
                    max_text_chars=args.max_text_chars,
                )
            ]
        else:
            bundles = extract_bundles(
                Path(args.transcript),
                turn_id=args.turn_id,
                include_in_progress=args.include_in_progress,
                cutoff_at=parse_cutoff(args.cutoff_at),
                max_text_chars=args.max_text_chars,
            )
    except ExtractError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    if args.output:
        output = Path(args.output)
        if args.all:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in bundles),
                encoding="utf-8",
            )
        else:
            write_json(output, bundles[0] if bundles else {})
    else:
        if args.all:
            for bundle in bundles:
                print(json.dumps(bundle, ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(bundles[0] if bundles else {}, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
