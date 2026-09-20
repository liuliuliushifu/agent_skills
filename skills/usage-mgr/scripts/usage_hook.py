#!/usr/bin/env python3
"""Ingest Codex Stop/SubagentStop usage into a local daily ledger."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 fallback
    ZoneInfo = None  # type: ignore


CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
DEFAULT_USAGE_ROOT = Path(
    os.environ.get("CODEX_USAGE_ROOT", str(CODEX_HOME / "usage"))
).expanduser()
DEFAULT_SESSIONS_ROOT = Path(
    os.environ.get("CODEX_SESSIONS_ROOT", str(CODEX_HOME / "sessions"))
).expanduser()
DEFAULT_AGENTS_JSON = Path(
    os.environ.get("COAGENT_REGISTRY", str(CODEX_HOME / "coagents" / "agents.json"))
).expanduser()
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
DEFAULT_USAGE_CATEGORY = "agent"
PERMISSION_APPROVAL_CATEGORY = "permission_approval"
DAILY_REPORT_AGENT = "Daily Report"
HOOK_STALE_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0)
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def local_tz() -> dt.tzinfo:
    if ZoneInfo is not None:
        return ZoneInfo("Asia/Shanghai")
    return dt.timezone(dt.timedelta(hours=8))


TZ = local_tz()


class UsageError(Exception):
    """Expected ingestion error that should be logged, not raised by hook."""


def now_local() -> dt.datetime:
    return dt.datetime.now(TZ)


def iso_now() -> str:
    return now_local().isoformat(timespec="seconds")


def parse_datetime(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def parse_date(value: str) -> dt.date:
    value = value.strip().lower()
    today = now_local().date()
    if value == "today":
        return today
    if value == "yesterday":
        return today - dt.timedelta(days=1)
    return dt.date.fromisoformat(value)


def zero_usage() -> dict[str, int]:
    return {field: 0 for field in TOKEN_FIELDS}


def clean_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return zero_usage()
    return {field: int(value.get(field) or 0) for field in TOKEN_FIELDS}


def usage_positive(usage: dict[str, int]) -> bool:
    return usage.get("total_tokens", 0) > 0


def usage_delta(current: dict[str, int], previous: dict[str, int]) -> dict[str, int]:
    return {field: max(0, current.get(field, 0) - previous.get(field, 0)) for field in TOKEN_FIELDS}


def newer_usage(current: dict[str, int], previous: dict[str, int]) -> dict[str, int]:
    if current.get("total_tokens", 0) >= previous.get("total_tokens", 0):
        return current
    return previous


def read_jsonl(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield line_no, obj


def extract_session_meta(path: Path) -> dict[str, Any]:
    for _line_no, obj in read_jsonl(path):
        if obj.get("type") == "session_meta":
            payload = obj.get("payload")
            return payload if isinstance(payload, dict) else obj
    return {}


def content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return ""


def transcript_has_daily_report_prompt(path: Path, max_records: int = 80) -> bool:
    for index, (_line_no, obj) in enumerate(read_jsonl(path), start=1):
        if index > max_records:
            return False
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        text = ""
        if obj.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            text = content_text(payload.get("content"))
        elif obj.get("type") == "event_msg" and payload.get("type") == "user_message":
            text = str(payload.get("message") or "")
        if not text:
            continue
        if "$ai-daily-report" in text or "ai-daily-report" in text:
            if "工作日报" in text or "AI Daily" in text or "daily report" in text.lower():
                return True
    return False


def is_daily_report_session(path: str, session_meta: dict[str, Any]) -> bool:
    if str(session_meta.get("originator") or "").strip() != "codex_exec":
        return False
    if str(session_meta.get("source") or "").strip() != "exec":
        return False
    try:
        return transcript_has_daily_report_prompt(Path(path))
    except Exception:
        return False


def read_jsonl_reverse(path: Path, chunk_size: int = 1024 * 1024):
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        tail = b""

        while pos > 0:
            read_size = min(chunk_size, pos)
            pos -= read_size
            fh.seek(pos)
            data = fh.read(read_size) + tail
            parts = data.split(b"\n")

            if pos > 0:
                tail = parts[0]
                parts = parts[1:]
                offset = pos + len(tail) + 1
            else:
                tail = b""
                offset = 0

            lines = []
            for raw in parts:
                lines.append((offset, raw))
                offset += len(raw) + 1

            for byte_offset, raw in reversed(lines):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield byte_offset, obj


def extract_latest_token_count_reverse(path: Path) -> dict[str, Any]:
    for byte_offset, obj in read_jsonl_reverse(path):
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        if obj.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        return {
            "line_no": None,
            "byte_offset": byte_offset,
            "timestamp": obj.get("timestamp"),
            "info": info if isinstance(info, dict) else {},
        }
    return {}


def token_count_record(line_no: int, obj: dict[str, Any]) -> dict[str, Any] | None:
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None
    if obj.get("type") != "event_msg" or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    total_usage = clean_usage(info.get("total_token_usage"))
    if not usage_positive(total_usage):
        return None
    return {
        "line_no": line_no,
        "timestamp": obj.get("timestamp"),
        "info": info,
    }


def uuid7_at_or_after(value: Any, reference: Any) -> bool:
    candidate = str(value or "").strip().lower()
    baseline = str(reference or "").strip().lower()
    if not UUID_RE.fullmatch(candidate) or not UUID_RE.fullmatch(baseline):
        return False
    if candidate[14] != "7" or baseline[14] != "7":
        return False
    return candidate >= baseline


def is_live_subagent_task_start(
    obj: dict[str, Any],
    session_id: str,
    target_turn_id: str | None = None,
) -> bool:
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return False
    if obj.get("type") != "event_msg" or payload.get("type") != "task_started":
        return False
    turn_id = str(payload.get("turn_id") or "")
    if target_turn_id and turn_id == target_turn_id:
        return True
    return uuid7_at_or_after(turn_id, session_id)


def extract_subagent_bootstrap_baseline(
    path: Path,
    session_meta: dict[str, Any],
    target_turn_id: str | None = None,
) -> dict[str, Any]:
    subagent = subagent_info(session_meta)
    if not subagent.get("is_subagent"):
        return {}

    session_id = str(
        session_meta.get("id")
        or session_meta.get("session_id")
        or session_id_from_path(str(path))
        or ""
    )
    if not session_id:
        return {}

    latest_before_live: dict[str, Any] | None = None
    inherited_token_events = 0
    live_task_found = False
    for line_no, obj in read_jsonl(path):
        if is_live_subagent_task_start(obj, session_id, target_turn_id):
            live_task_found = True
            break
        token_count = token_count_record(line_no, obj)
        if token_count is not None:
            latest_before_live = token_count
            inherited_token_events += 1

    if not live_task_found or latest_before_live is None:
        return {}

    info = latest_before_live["info"]
    return {
        "baseline_total": clean_usage(info.get("total_token_usage")),
        "baseline_token_count": {
            "line_no": latest_before_live.get("line_no"),
            "timestamp": latest_before_live.get("timestamp"),
        },
        "baseline_source": "inherited_subagent_history",
        "inherited_token_events": inherited_token_events,
    }


def extract_transcript(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise UsageError(f"transcript not found: {path}")

    session_meta = extract_session_meta(path)
    latest_token_count = extract_latest_token_count_reverse(path)

    if not latest_token_count:
        raise UsageError(f"token_count not found: {path}")
    return {
        "path": str(path),
        "session_meta": session_meta,
        "latest_token_count": latest_token_count,
    }


def extract_transcript_for_date(path: Path, day: dt.date) -> dict[str, Any] | None:
    if not path.is_file():
        raise UsageError(f"transcript not found: {path}")

    start = dt.datetime.combine(day, dt.time.min, tzinfo=TZ)
    end = start + dt.timedelta(days=1)
    session_meta: dict[str, Any] = {}
    baseline: dict[str, Any] | None = None
    latest_within: dict[str, Any] | None = None

    for line_no, obj in read_jsonl(path):
        if obj.get("type") == "session_meta" and not session_meta:
            payload = obj.get("payload")
            session_meta = payload if isinstance(payload, dict) else obj
            continue

        token_time = parse_datetime(obj.get("timestamp"))
        token_count = token_count_record(line_no, obj)
        if token_time is None or token_count is None:
            continue
        if token_time < start:
            baseline = token_count
        elif start <= token_time < end:
            latest_within = token_count

    if latest_within is None:
        return None

    result = {
        "path": str(path),
        "session_meta": session_meta,
        "latest_token_count": latest_within,
    }
    baseline_source = "previous_day"
    if baseline is not None:
        baseline_info = baseline.get("info") if isinstance(baseline.get("info"), dict) else {}
        result["baseline_total"] = clean_usage(baseline_info.get("total_token_usage"))
        result["baseline_token_count"] = {
            "line_no": baseline.get("line_no"),
            "timestamp": baseline.get("timestamp"),
        }
    else:
        result["baseline_total"] = zero_usage()
        result["baseline_token_count"] = None
        baseline_source = None

    bootstrap = extract_subagent_bootstrap_baseline(path, session_meta)
    bootstrap_total = clean_usage(bootstrap.get("baseline_total"))
    if bootstrap_total["total_tokens"] > result["baseline_total"]["total_tokens"]:
        result.update(bootstrap)
    else:
        result["baseline_source"] = baseline_source
    return result


def subagent_info(session_meta: dict[str, Any]) -> dict[str, Any]:
    source = session_meta.get("source")
    parent_thread_id = session_meta.get("parent_thread_id")
    thread_source = session_meta.get("thread_source")

    if not isinstance(source, dict):
        return {
            "is_subagent": bool(parent_thread_id) or thread_source == "subagent",
            "parent_thread_id": parent_thread_id,
            "source": source,
        }

    subagent = source.get("subagent")
    if isinstance(subagent, dict):
        thread_spawn = subagent.get("thread_spawn")
        if isinstance(thread_spawn, dict):
            parent_thread_id = thread_spawn.get("parent_thread_id") or parent_thread_id
        parent_thread_id = subagent.get("parent_thread_id") or parent_thread_id

    return {
        "is_subagent": bool(subagent) or bool(parent_thread_id) or thread_source == "subagent",
        "parent_thread_id": parent_thread_id,
        "source": source,
    }


def usage_category_from_subagent(subagent: dict[str, Any]) -> str:
    source = subagent.get("source")
    if isinstance(source, dict):
        subagent_source = source.get("subagent")
        if isinstance(subagent_source, dict) and subagent_source.get("other") == "guardian":
            return PERMISSION_APPROVAL_CATEGORY
    return DEFAULT_USAGE_CATEGORY


def load_agents(path: Path | None = None) -> dict[str, dict[str, Any]]:
    candidates = [path] if path else [DEFAULT_AGENTS_JSON]
    for candidate in candidates:
        try:
            if candidate and candidate.is_file():
                with candidate.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                agents = data.get("agents")
                if isinstance(agents, dict):
                    return agents
        except Exception:
            continue
    return {}


def norm(value: Any) -> str:
    return str(value or "").strip().lower()


def resolve_agent_query(query: str, agents: dict[str, dict[str, Any]]) -> str:
    wanted = norm(query)
    for name, info in agents.items():
        choices = {norm(name), norm(info.get("name")), norm(info.get("chat_name"))}
        choices.update(norm(alias) for alias in info.get("aliases", []) if alias)
        if wanted in choices:
            return info.get("name") or name
    return query


def agent_for_cwd(cwd: str | None, agents: dict[str, dict[str, Any]]) -> str:
    if not cwd:
        return "unknown"
    cwd_path = cwd.rstrip("/")
    best_name = ""
    best_len = -1
    for name, info in agents.items():
        root = str(info.get("cwd") or "").rstrip("/")
        if not root:
            continue
        if cwd_path == root or cwd_path.startswith(root + "/"):
            if len(root) > best_len:
                best_name = info.get("name") or name
                best_len = len(root)
    return best_name or cwd_path or "unknown"


def accounting_ids(
    payload: dict[str, Any],
    session_meta: dict[str, Any],
    subagent: dict[str, Any],
    fallback_session_id: str | None = None,
) -> tuple[str, str]:
    subagent_evidence = (
        subagent.get("is_subagent")
        or payload.get("hook_event_name") == "SubagentStop"
        or bool(payload.get("agent_id") and payload.get("agent_transcript_path"))
        or session_meta.get("thread_source") == "subagent"
        or bool(session_meta.get("parent_thread_id"))
    )

    if subagent_evidence:
        accounting_session_id = (
            session_meta.get("id")
            or payload.get("agent_id")
            or fallback_session_id
            or payload.get("session_id")
            or session_meta.get("session_id")
        )
    else:
        accounting_session_id = (
            session_meta.get("id")
            or payload.get("session_id")
            or session_meta.get("session_id")
            or fallback_session_id
        )
    if not accounting_session_id:
        raise UsageError("missing accounting session id")

    owner_session_id = accounting_session_id
    if subagent_evidence:
        owner_session_id = (
            subagent.get("parent_thread_id")
            or session_meta.get("parent_thread_id")
            or payload.get("session_id")
            or session_meta.get("session_id")
            or accounting_session_id
        )
    return str(accounting_session_id), str(owner_session_id)


def session_id_from_path(path: str) -> str | None:
    match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", path)
    return match.group(1) if match else None


def choose_hook_transcript(payload: dict[str, Any]) -> Path:
    event = payload.get("hook_event_name")
    if event == "SubagentStop":
        agent_path = payload.get("agent_transcript_path")
        if not isinstance(agent_path, str) or not agent_path:
            raise UsageError("SubagentStop missing agent_transcript_path")
        return Path(agent_path).expanduser()

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        raise UsageError("missing transcript_path")
    return Path(transcript_path).expanduser()


def build_event_from_transcript(
    transcript: dict[str, Any],
    payload: dict[str, Any],
    source: str,
    agents: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    session_meta = transcript["session_meta"]
    token_count = transcript["latest_token_count"]
    info = token_count.get("info") if isinstance(token_count.get("info"), dict) else {}
    total_usage = clean_usage(info.get("total_token_usage"))
    last_usage = clean_usage(info.get("last_token_usage"))
    if not usage_positive(total_usage):
        raise UsageError(f"empty total_token_usage: {transcript['path']}")

    subagent = subagent_info(session_meta)
    fallback_session_id = session_id_from_path(str(transcript["path"]))
    accounting_session_id, owner_session_id = accounting_ids(payload, session_meta, subagent, fallback_session_id)
    token_time = parse_datetime(token_count.get("timestamp"))
    usage_date = (token_time or now_local()).date().isoformat()
    raw_cwd = payload.get("cwd") or session_meta.get("cwd") or ""
    owner_cwd = payload.get("owner_cwd") or raw_cwd
    usage_category = usage_category_from_subagent(subagent)
    is_subagent = bool(subagent.get("is_subagent")) or payload.get("hook_event_name") == "SubagentStop" or bool(payload.get("agent_id") and payload.get("agent_transcript_path"))
    if usage_category == PERMISSION_APPROVAL_CATEGORY:
        usage_kind = "approval"
    elif is_subagent:
        usage_kind = "subagent"
    else:
        usage_kind = "owner"
    owner_agent = owner_cwd or "unknown"
    agent = owner_agent
    agent_locked = False
    if not is_subagent and usage_category == DEFAULT_USAGE_CATEGORY and is_daily_report_session(transcript["path"], session_meta):
        agent = DAILY_REPORT_AGENT
        owner_agent = DAILY_REPORT_AGENT
        agent_locked = True
    approval_session_id = accounting_session_id if usage_category == PERMISSION_APPROVAL_CATEGORY else None
    hook_event = payload.get("hook_event_name") or source
    turn_id = payload.get("turn_id")

    return {
        "recorded_at": iso_now(),
        "usage_date": usage_date,
        "source": source,
        "hook_event_name": hook_event,
        "turn_id": turn_id,
        "event_key": make_event_key(source, hook_event, accounting_session_id, turn_id, token_count),
        "session_id": payload.get("session_id") or session_meta.get("session_id"),
        "raw_session_id": accounting_session_id,
        "accounting_session_id": accounting_session_id,
        "owner_session_id": owner_session_id,
        "approval_session_id": approval_session_id,
        "agent": agent,
        "owner_agent": owner_agent,
        "agent_locked": agent_locked,
        "usage_kind": usage_kind,
        "usage_category": usage_category,
        "cwd": owner_cwd,
        "raw_cwd": raw_cwd,
        "owner_cwd": owner_cwd,
        "model": payload.get("model") or session_meta.get("model"),
        "is_subagent": is_subagent,
        "session_id_fallback_used": not bool(
            session_meta.get("id") or payload.get("agent_id") or payload.get("session_id") or session_meta.get("session_id")
        ),
        "agent_id": payload.get("agent_id"),
        "agent_type": payload.get("agent_type"),
        "agent_nickname": session_meta.get("agent_nickname"),
        "agent_role": session_meta.get("agent_role"),
        "transcript_path": transcript["path"],
        "trigger_transcript_path": payload.get("transcript_path"),
        "agent_transcript_path": payload.get("agent_transcript_path"),
        "token_count": {
            "line_no": token_count.get("line_no"),
            "byte_offset": token_count.get("byte_offset"),
            "timestamp": token_count.get("timestamp"),
            "model_context_window": info.get("model_context_window"),
        },
        "baseline_total": clean_usage(transcript.get("baseline_total")),
        "baseline_token_count": transcript.get("baseline_token_count"),
        "baseline_source": transcript.get("baseline_source"),
        "inherited_token_events": int(transcript.get("inherited_token_events") or 0),
        "total": total_usage,
        "last": last_usage,
    }


def make_event_key(
    source: str,
    hook_event: Any,
    accounting_session_id: str,
    turn_id: Any,
    token_count: dict[str, Any],
) -> str:
    if turn_id:
        return f"{source}:{hook_event}:{accounting_session_id}:{turn_id}"
    marker = token_count.get("line_no")
    if marker is None:
        marker = token_count.get("byte_offset")
    return (
        f"{source}:{hook_event}:{accounting_session_id}:"
        f"{token_count.get('timestamp')}:{marker}"
    )


def state_path(root: Path) -> Path:
    return root / "state.json"


def lock_path(root: Path) -> Path:
    return root / ".usage.lock"


def diagnostics_path(root: Path) -> Path:
    return root / "logs" / "usage_hook_diagnostics.jsonl"


def ledger_path(root: Path, usage_date: str) -> Path:
    day = dt.date.fromisoformat(usage_date)
    return root / "ledger" / f"{day:%Y}" / f"{day:%m}" / f"{day:%Y-%m-%d}.jsonl"


def load_state(root: Path) -> dict[str, Any]:
    path = state_path(root)
    if not path.is_file():
        return {"version": 1, "sessions": {}, "events": {}}
    with path.open("r", encoding="utf-8") as fh:
        state = json.load(fh)
    if not isinstance(state, dict):
        return {"version": 1, "sessions": {}, "events": {}}
    state.setdefault("version", 1)
    state.setdefault("sessions", {})
    state.setdefault("events", {})
    return state


def write_state(root: Path, state: dict[str, Any]) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, sort_keys=True, indent=2)
        fh.write("\n")
    os.replace(tmp_path, path)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def rollback_jsonl_append(path: Path, previous_size: int, existed: bool) -> None:
    if not path.exists():
        return
    if not existed:
        path.unlink()
        return
    with path.open("r+b") as fh:
        fh.truncate(previous_size)


def prune_events(state: dict[str, Any], keep: int = 5000) -> None:
    events = state.get("events")
    if not isinstance(events, dict) or len(events) <= keep:
        return
    ordered = sorted(events.items(), key=lambda item: str(item[1]))
    state["events"] = dict(ordered[-keep:])


def token_marker(token_count: Any) -> tuple[dt.datetime | None, int]:
    if not isinstance(token_count, dict):
        return None, -1
    marker = token_count.get("line_no")
    if marker is None:
        marker = token_count.get("byte_offset")
    return parse_datetime(token_count.get("timestamp")), int(marker or -1)


def token_is_after(current: Any, previous: Any) -> bool:
    current_time, current_line = token_marker(current)
    previous_time, previous_line = token_marker(previous)
    if current_time is None:
        return False
    if previous_time is None:
        return True
    if current_time != previous_time:
        return current_time > previous_time
    return current_line > previous_line


def ledger_has_event(root: Path, event: dict[str, Any]) -> bool:
    path = ledger_path(root, event["usage_date"])
    if not path.is_file():
        return False
    event_key = event.get("event_key")
    if not event_key:
        return False
    for record, _, _ in read_jsonl_with_path(path):
        if record.get("event_key") == event_key:
            return True
    return False


def ledger_has_session_day(root: Path, event: dict[str, Any]) -> bool:
    path = ledger_path(root, event["usage_date"])
    if not path.is_file():
        return False
    session_id = str(event.get("accounting_session_id") or "")
    if not session_id:
        return False
    for record, _, _ in read_jsonl_with_path(path):
        if str(record.get("accounting_session_id") or "") == session_id:
            return True
    return False


def read_jsonl_with_path(path: Path):
    for line_no, obj in read_jsonl(path):
        yield obj, path, line_no


def session_has_usage_state(root: Path, session_id: str) -> bool:
    root.mkdir(parents=True, exist_ok=True)
    lock_path(root).parent.mkdir(parents=True, exist_ok=True)
    with lock_path(root).open("a", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        state = load_state(root)
        return bool(state.get("sessions", {}).get(session_id))


def ingest_event(root: Path, event: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    lock_path(root).parent.mkdir(parents=True, exist_ok=True)
    with lock_path(root).open("a", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        state = load_state(root)
        sessions = state.setdefault("sessions", {})
        seen_events = state.setdefault("events", {})

        event_key = event["event_key"]
        if event_key in seen_events:
            return {"status": "skipped", "reason": "duplicate_event", "event": event}
        if ledger_has_event(root, event):
            seen_events[event_key] = iso_now()
            if not dry_run:
                write_state(root, state)
            return {"status": "skipped", "reason": "duplicate_ledger_event", "event": event}
        if event.get("source") == "backfill" and ledger_has_session_day(root, event):
            seen_events[event_key] = iso_now()
            if not dry_run:
                write_state(root, state)
            return {"status": "skipped", "reason": "backfill_session_day_already_recorded", "event": event}

        session_id = event["accounting_session_id"]
        session_state = sessions.get(session_id, {})
        state_total = clean_usage(session_state.get("total"))
        previous_total = state_total
        baseline_total = clean_usage(event.get("baseline_total"))
        if event.get("source") == "backfill":
            previous_total = baseline_total
        elif baseline_total["total_tokens"] > previous_total["total_tokens"]:
            previous_total = baseline_total
        current_total = clean_usage(event.get("total"))
        last_usage = clean_usage(event.get("last"))

        if event.get("source") == "external":
            if not usage_positive(last_usage):
                seen_events[event_key] = iso_now()
                if not dry_run:
                    write_state(root, state)
                return {"status": "skipped", "reason": "empty_external_usage", "event": event}
            delta = last_usage
            reason = "external_delta"
            update_total = {
                field: state_total.get(field, 0) + delta.get(field, 0)
                for field in TOKEN_FIELDS
            }
            current_total = update_total
        elif event.get("source") == "backfill":
            if current_total["total_tokens"] <= previous_total["total_tokens"]:
                seen_events[event_key] = iso_now()
                if not dry_run:
                    write_state(root, state)
                return {"status": "skipped", "reason": "no_backfill_window_delta", "event": event}
            delta = usage_delta(current_total, previous_total)
            reason = "backfill_window_delta"
            update_total = newer_usage(current_total, state_total)
        elif current_total["total_tokens"] > previous_total["total_tokens"]:
            delta = usage_delta(current_total, previous_total)
            if (
                event.get("baseline_source") == "inherited_subagent_history"
                and not usage_positive(state_total)
                and usage_positive(baseline_total)
            ):
                reason = "subagent_bootstrap_delta"
            else:
                reason = "cumulative_delta"
            update_total = current_total
        elif current_total["total_tokens"] == previous_total["total_tokens"]:
            seen_events[event_key] = iso_now()
            if not dry_run:
                write_state(root, state)
            return {"status": "skipped", "reason": "no_new_tokens", "event": event}
        elif not token_is_after(event.get("token_count"), session_state.get("token_count")):
            seen_events[event_key] = iso_now()
            if not dry_run:
                write_state(root, state)
            return {"status": "skipped", "reason": "older_or_equal_token_count", "event": event}
        elif usage_positive(last_usage):
            delta = last_usage
            reason = "regressed_total_using_last_usage"
            update_total = previous_total
        else:
            seen_events[event_key] = iso_now()
            if not dry_run:
                write_state(root, state)
            return {"status": "skipped", "reason": "regressed_total_no_last_usage", "event": event}

        if not usage_positive(delta):
            seen_events[event_key] = iso_now()
            if not dry_run:
                write_state(root, state)
            return {"status": "skipped", "reason": "empty_delta", "event": event}

        ledger_record = dict(event)
        ledger_record["total"] = update_total
        ledger_record["previous_total"] = previous_total
        ledger_record["delta"] = delta
        ledger_record["ingest_reason"] = reason

        if update_total["total_tokens"] >= state_total["total_tokens"]:
            session_state.update(
                {
                    "total": update_total,
                    "updated_at": iso_now(),
                    "usage_date": event.get("usage_date"),
                    "raw_session_id": event.get("raw_session_id") or event.get("accounting_session_id"),
                    "agent": event.get("agent"),
                    "owner_session_id": event.get("owner_session_id"),
                    "raw_cwd": event.get("raw_cwd") or event.get("cwd"),
                    "owner_cwd": event.get("owner_cwd") or event.get("cwd"),
                    "cwd": event.get("cwd"),
                    "usage_kind": event.get("usage_kind"),
                    "model": event.get("model"),
                    "is_subagent": event.get("is_subagent"),
                    "transcript_path": event.get("transcript_path"),
                    "token_count": event.get("token_count"),
                }
            )
        sessions[session_id] = session_state
        seen_events[event_key] = iso_now()
        prune_events(state)

        if not dry_run:
            path = ledger_path(root, event["usage_date"])
            existed = path.exists()
            previous_size = path.stat().st_size if existed else 0
            try:
                append_jsonl(path, ledger_record)
                write_state(root, state)
            except Exception as exc:
                try:
                    rollback_jsonl_append(path, previous_size, existed)
                except Exception as rollback_exc:
                    raise RuntimeError(f"{exc}; ledger rollback failed: {rollback_exc}") from exc
                raise

        return {"status": "recorded", "reason": reason, "record": ledger_record}


def log_diagnostic(root: Path, message: str, payload: Any = None, severity: str = "error") -> None:
    record = {
        "recorded_at": iso_now(),
        "severity": severity,
        "message": message,
        "payload": payload,
    }
    try:
        append_jsonl(diagnostics_path(root), record)
    except Exception:
        pass


def read_stdin_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        raise UsageError("empty hook payload")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError(f"invalid hook payload json: {exc}") from exc
    if not isinstance(payload, dict):
        raise UsageError("hook payload is not an object")
    return payload


def is_missing_token_count_error(exc: Exception) -> bool:
    return isinstance(exc, UsageError) and str(exc).startswith("token_count not found:")


def hook_event_may_be_stale(root: Path, event: dict[str, Any]) -> bool:
    if event.get("source") != "hook":
        return False
    session_id = event.get("accounting_session_id")
    if not session_id:
        return False

    root.mkdir(parents=True, exist_ok=True)
    lock_path(root).parent.mkdir(parents=True, exist_ok=True)
    with lock_path(root).open("a", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        state = load_state(root)
        if event.get("event_key") in state.get("events", {}):
            return False
        if ledger_has_event(root, event):
            return False
        session_state = state.get("sessions", {}).get(str(session_id), {})

    if not session_state:
        return False

    current_total = clean_usage(event.get("total"))
    state_total = clean_usage(session_state.get("total"))
    token_after_state = token_is_after(event.get("token_count"), session_state.get("token_count"))
    if not token_after_state:
        return True
    return current_total["total_tokens"] == state_total["total_tokens"]


def build_fresh_hook_event(
    root: Path,
    transcript_path: Path,
    payload: dict[str, Any],
    agents: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    event: dict[str, Any] | None = None
    last_missing_token_count_error: UsageError | None = None

    for delay in (0.0, *HOOK_STALE_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            transcript = extract_transcript(transcript_path)
            event = build_event_from_transcript(transcript, payload, "hook", agents)
            if (
                event.get("is_subagent")
                and not session_has_usage_state(root, event["accounting_session_id"])
            ):
                bootstrap = extract_subagent_bootstrap_baseline(
                    transcript_path,
                    transcript["session_meta"],
                    str(payload.get("turn_id") or "") or None,
                )
                if bootstrap:
                    transcript.update(bootstrap)
                    event = build_event_from_transcript(transcript, payload, "hook", agents)
            last_missing_token_count_error = None
        except UsageError as exc:
            if is_missing_token_count_error(exc):
                last_missing_token_count_error = exc
                continue
            raise

        if not hook_event_may_be_stale(root, event):
            return event, False

    if event is None:
        if last_missing_token_count_error is not None:
            raise last_missing_token_count_error
        raise UsageError(f"token_count not found: {transcript_path}")
    return event, hook_event_may_be_stale(root, event)


def ingest_hook(args: argparse.Namespace) -> int:
    root = Path(args.usage_root)
    try:
        payload = read_stdin_payload()
        agents = load_agents(Path(args.agents_json) if args.agents_json else None)
        transcript_path = choose_hook_transcript(payload)
        event, stale = build_fresh_hook_event(root, transcript_path, payload, agents)
        if stale:
            log_diagnostic(
                root,
                "stale token_count after retries; skip without marking event seen",
                {
                    "event_key": event.get("event_key"),
                    "session_id": event.get("accounting_session_id"),
                    "transcript_path": event.get("transcript_path"),
                    "token_count": event.get("token_count"),
                    "total": event.get("total"),
                },
                severity="warning",
            )
            if args.verbose:
                print(json.dumps({"status": "skipped", "reason": "stale_token_count_retry_exhausted", "event": event}, ensure_ascii=False, sort_keys=True))
            return 0
        result = ingest_event(root, event, dry_run=args.dry_run)
        if args.verbose:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        log_diagnostic(root, str(exc), severity="error")
        if args.verbose:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 0
    return 0


def session_files_for_date(sessions_root: Path, day: dt.date) -> list[Path]:
    del day
    if not sessions_root.is_dir():
        return []
    return sorted(sessions_root.rglob("*.jsonl"))


def backfill(args: argparse.Namespace) -> int:
    root = Path(args.usage_root)
    sessions_root = Path(args.sessions_root)
    agents = load_agents(Path(args.agents_json) if args.agents_json else None)
    selected_agents = [resolve_agent_query(agent, agents) for agent in (args.agent or [])]
    selected_agent_set = {norm(agent) for agent in selected_agents}
    day = parse_date(args.date)
    results: list[dict[str, Any]] = []

    for path in session_files_for_date(sessions_root, day):
        try:
            transcript = extract_transcript_for_date(path, day)
            if transcript is None:
                continue
            event = build_event_from_transcript(transcript, {}, "backfill", agents)
            if event["usage_date"] != day.isoformat():
                continue
            event_agent = agent_for_cwd(event.get("owner_cwd") or event.get("cwd"), agents)
            if selected_agent_set and norm(event_agent) not in selected_agent_set:
                continue
            if args.owner and not str(event.get("owner_session_id", "")).startswith(args.owner):
                continue
            if args.session and not str(event.get("raw_session_id") or event.get("accounting_session_id", "")).startswith(args.session):
                continue
            results.append(ingest_event(root, event, dry_run=args.dry_run))
        except Exception as exc:
            log_diagnostic(root, str(exc), {"transcript_path": str(path)}, severity="warning")
            if args.verbose:
                results.append({"status": "error", "path": str(path), "error": str(exc)})

    summary = summarize_results(results)
    if args.json:
        print(json.dumps({"date": day.isoformat(), "agents": selected_agents, "summary": summary, "results": results}, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"backfill date={day.isoformat()} agents={selected_agents or ['all']} "
            f"recorded={summary['recorded']} skipped={summary['skipped']} errors={summary['errors']} "
            f"total_delta_tokens={summary['delta']['total_tokens']}"
        )
    return 0


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    delta = zero_usage()
    recorded = skipped = errors = 0
    for result in results:
        status = result.get("status")
        if status == "recorded":
            recorded += 1
            record = result.get("record") or {}
            add_usage(delta, clean_usage(record.get("delta")))
        elif status == "error":
            errors += 1
        else:
            skipped += 1
    return {"recorded": recorded, "skipped": skipped, "errors": errors, "delta": delta}


def add_usage(total: dict[str, int], usage: dict[str, int]) -> None:
    for field in TOKEN_FIELDS:
        total[field] += int(usage.get(field) or 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record local Codex usage from hooks or transcripts.")
    parser.add_argument("--usage-root", default=str(DEFAULT_USAGE_ROOT))
    parser.add_argument("--agents-json", help="Override co-agent agents.json path.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("hook", help="Read a hook payload from stdin and ingest it.")

    backfill_parser = subparsers.add_parser("backfill", help="Backfill a local date from Codex transcripts.")
    backfill_parser.add_argument("--date", default="today", help="YYYY-MM-DD, today, or yesterday.")
    backfill_parser.add_argument("--sessions-root", default=str(DEFAULT_SESSIONS_ROOT))
    backfill_parser.add_argument("--agent", action="append", help="Agent name or alias; may be repeated.")
    backfill_parser.add_argument("--session", help="Accounting session id prefix.")
    backfill_parser.add_argument("--owner", help="Owner session id prefix.")
    backfill_parser.add_argument("--json", action="store_true")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command in (None, "hook"):
        return ingest_hook(args)
    if args.command == "backfill":
        return backfill(args)
    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
