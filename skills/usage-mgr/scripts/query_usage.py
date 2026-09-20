#!/usr/bin/env python3
"""Query local Codex token usage ledger or Stop-hook probe logs."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from functools import lru_cache
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


def local_tz() -> dt.tzinfo:
    if ZoneInfo is not None:
        return ZoneInfo("Asia/Shanghai")
    return dt.timezone(dt.timedelta(hours=8))


TZ = local_tz()


def parse_date(value: str) -> dt.date:
    value = value.strip().lower()
    today = dt.datetime.now(TZ).date()
    if value == "today":
        return today
    if value == "yesterday":
        return today - dt.timedelta(days=1)
    return dt.date.fromisoformat(value)


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


def date_range(start: dt.date, end: dt.date):
    current = start
    while current <= end:
        yield current
        current += dt.timedelta(days=1)


def zero_usage() -> dict[str, int]:
    return {field: 0 for field in TOKEN_FIELDS}


def add_usage(total: dict[str, int], usage: dict[str, Any]) -> None:
    for field in TOKEN_FIELDS:
        total[field] += int(usage.get(field) or 0)


def read_jsonl(path: Path):
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"skip invalid jsonl {path}:{line_no}: {exc}", file=sys.stderr)
                continue
            if isinstance(obj, dict):
                yield obj, path, line_no


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


@lru_cache(maxsize=512)
def transcript_has_daily_report_prompt(path_text: str, max_records: int = 80) -> bool:
    path = Path(path_text)
    if not path.is_file():
        return False
    for index, (obj, _path, _line_no) in enumerate(read_jsonl(path) or [], start=1):
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


def load_agents(path: Path | None) -> dict[str, dict[str, Any]]:
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


def resolve_agent_query(query: str | None, agents: dict[str, dict[str, Any]]) -> str | None:
    if not query:
        return None
    wanted = norm(query)
    for name, info in agents.items():
        choices = {norm(name), norm(info.get("name")), norm(info.get("chat_name"))}
        choices.update(norm(alias) for alias in info.get("aliases", []) if alias)
        if wanted in choices:
            return info.get("name") or name
    return query


def canonical_agent_name(value: Any, agents: dict[str, dict[str, Any]]) -> str | None:
    wanted = norm(value)
    if not wanted:
        return None
    for name, info in agents.items():
        choices = {norm(name), norm(info.get("name")), norm(info.get("chat_name"))}
        choices.update(norm(alias) for alias in info.get("aliases", []) if alias)
        if wanted in choices:
            return info.get("name") or name
    return None


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


def display_agent_for_record(record_agent: Any, cwd: str | None, agents: dict[str, dict[str, Any]]) -> str:
    """Prefer co-agent registry ownership by cwd, then canonical names, then cwd."""
    cwd_agent = agent_for_cwd(cwd, agents)
    if cwd_agent != "unknown" and norm(cwd_agent) != norm(cwd):
        return cwd_agent
    canonical = canonical_agent_name(record_agent, agents)
    if canonical:
        return canonical
    if cwd_agent != "unknown":
        return cwd_agent
    text = str(record_agent or "").strip()
    if text and norm(text) != "unknown":
        return text
    return "unknown"


def display_agent_for_usage_record(record: dict[str, Any], cwd: str | None, agents: dict[str, dict[str, Any]]) -> str:
    if record.get("usage_category") == PERMISSION_APPROVAL_CATEGORY:
        return display_agent_for_record(record.get("owner_agent") or record.get("agent") or record.get("agent_name"), cwd, agents)
    if (
        record.get("agent_locked")
        or record.get("source") in {"external", "codex_exec_json"}
    ):
        text = str(record.get("agent") or record.get("agent_name") or "").strip()
        return text or "unknown"
    return display_agent_for_record(record.get("agent") or record.get("agent_name"), cwd, agents)


def usage_date_from_record(record: dict[str, Any]) -> dt.date | None:
    explicit = record.get("usage_date")
    if isinstance(explicit, str):
        try:
            return dt.date.fromisoformat(explicit)
        except ValueError:
            pass

    recorded_at = parse_datetime(record.get("recorded_at"))
    if recorded_at:
        return recorded_at.date()

    token_ts = (
        record.get("latest_token_count", {}).get("timestamp")
        if isinstance(record.get("latest_token_count"), dict)
        else None
    )
    parsed_token_ts = parse_datetime(token_ts)
    return parsed_token_ts.date() if parsed_token_ts else None


def usage_from_record(record: dict[str, Any]) -> dict[str, int]:
    candidates = [
        record.get("delta"),
        record.get("usage"),
        record.get("latest_token_count", {}).get("total_token_usage")
        if isinstance(record.get("latest_token_count"), dict)
        else None,
        record.get("accounting_transcript", {})
        .get("latest_token_count", {})
        .get("total_token_usage")
        if isinstance(record.get("accounting_transcript"), dict)
        else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, dict) and any(field in candidate for field in TOKEN_FIELDS):
            return {field: int(candidate.get(field) or 0) for field in TOKEN_FIELDS}
    return zero_usage()


def meta_from_record(record: dict[str, Any]) -> dict[str, Any]:
    meta = record.get("session_meta")
    if isinstance(meta, dict):
        return meta
    accounting = record.get("accounting_transcript")
    if isinstance(accounting, dict) and isinstance(accounting.get("session_meta"), dict):
        return accounting["session_meta"]
    return {}


def is_daily_report_record(record: dict[str, Any], meta: dict[str, Any]) -> bool:
    if record.get("agent_locked") and norm(record.get("agent")) == norm(DAILY_REPORT_AGENT):
        return True
    if record.get("is_subagent") or str(record.get("usage_category") or DEFAULT_USAGE_CATEGORY) == PERMISSION_APPROVAL_CATEGORY:
        return False
    if str(meta.get("originator") or "").strip() not in {"codex_exec", ""}:
        return False
    if str(meta.get("source") or "").strip() not in {"exec", ""}:
        return False
    transcript_path = str(record.get("transcript_path") or record.get("trigger_transcript_path") or "").strip()
    return bool(transcript_path and transcript_has_daily_report_prompt(transcript_path))


def normalize_record(
    record: dict[str, Any],
    source: str,
    path: Path,
    line_no: int,
    agents: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    usage_date = usage_date_from_record(record)
    if usage_date is None:
        return None

    meta = meta_from_record(record)
    if is_daily_report_record(record, meta):
        record = dict(record)
        record["agent"] = DAILY_REPORT_AGENT
        record["owner_agent"] = DAILY_REPORT_AGENT
        record["agent_locked"] = True
    subagent = record.get("subagent") if isinstance(record.get("subagent"), dict) else {}
    is_subagent = bool(record.get("is_subagent")) or bool(subagent.get("is_subagent"))

    accounting_session_id = (
        record.get("raw_session_id")
        or record.get("accounting_session_id")
        or meta.get("id")
        or record.get("agent_id")
        or record.get("session_id")
    )
    owner_session_id = record.get("owner_session_id") or accounting_session_id
    if is_subagent:
        owner_session_id = (
            subagent.get("parent_thread_id")
            or meta.get("parent_thread_id")
            or meta.get("session_id")
            or owner_session_id
        )

    raw_cwd = record.get("raw_cwd") or record.get("cwd") or meta.get("cwd") or ""
    owner_cwd = record.get("owner_cwd") or record.get("cwd") or raw_cwd
    cwd = owner_cwd
    usage_category = str(record.get("usage_category") or DEFAULT_USAGE_CATEGORY)
    is_approval = usage_category == PERMISSION_APPROVAL_CATEGORY
    raw_session_id = accounting_session_id
    approval_session_id = record.get("approval_session_id") or (raw_session_id if is_approval else None)
    if is_approval:
        accounting_session_id = owner_session_id
    agent_name = display_agent_for_usage_record(record, cwd, agents)
    usage = usage_from_record(record)
    usage_kind = str(record.get("usage_kind") or "")
    if not usage_kind:
        if is_approval:
            usage_kind = "approval"
        elif is_subagent:
            usage_kind = "subagent"
        elif record.get("source") in {"external", "codex_exec_json"}:
            usage_kind = "external"
        else:
            usage_kind = "owner"

    return {
        "source": source,
        "record_path": str(path),
        "line_no": line_no,
        "usage_date": usage_date.isoformat(),
        "recorded_at": record.get("recorded_at"),
        "session_id": record.get("session_id"),
        "accounting_session_id": accounting_session_id,
        "raw_session_id": raw_session_id,
        "raw_accounting_session_id": raw_session_id,
        "owner_session_id": owner_session_id,
        "approval_session_id": approval_session_id,
        "agent": agent_name,
        "agent_locked": bool(record.get("agent_locked")),
        "owner_agent": record.get("owner_agent"),
        "usage_kind": usage_kind,
        "usage_category": usage_category,
        "cwd": cwd,
        "raw_cwd": raw_cwd,
        "owner_cwd": owner_cwd,
        "model": record.get("model") or meta.get("model"),
        "is_subagent": is_subagent and not is_approval,
        "is_approval": is_approval,
        "usage": usage,
    }


def ledger_paths(root: Path, start: dt.date, end: dt.date) -> list[Path]:
    paths = []
    for day in date_range(start, end):
        paths.append(root / "ledger" / f"{day:%Y}" / f"{day:%m}" / f"{day:%Y-%m-%d}.jsonl")
    return paths


def read_records(
    root: Path,
    source: str,
    start: dt.date,
    end: dt.date,
    agents: dict[str, dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    selected_source = source
    records: list[dict[str, Any]] = []

    if source in ("auto", "ledger"):
        ledger_records: dict[str, dict[str, Any]] = {}
        for path in ledger_paths(root, start, end):
            for record, record_path, line_no in read_jsonl(path) or []:
                normalized = normalize_record(record, "ledger", record_path, line_no, agents)
                if normalized:
                    key = str(record.get("event_key") or f"{record_path}:{line_no}")
                    ledger_records[key] = normalized
        records = list(ledger_records.values())
        if source == "ledger" or records:
            return "ledger", records

    if source in ("auto", "probe"):
        path = root / "test-hooks" / "stop-hook-events.jsonl"
        snapshots: dict[str, dict[str, Any]] = {}
        for record, record_path, line_no in read_jsonl(path) or []:
            normalized = normalize_record(record, "probe", record_path, line_no, agents)
            if not normalized:
                continue
            day = dt.date.fromisoformat(normalized["usage_date"])
            if day < start or day > end:
                continue
            key = f"{normalized['usage_date']}:{normalized.get('accounting_session_id') or f'{record_path}:{line_no}'}"
            snapshots[key] = normalized
        selected_source = "probe"
        records = list(snapshots.values())

    return selected_source, records


def id_matches(value: Any, query: str | None) -> bool:
    if not query:
        return True
    text = str(value or "")
    return text == query or text.startswith(query)


def filter_records(
    records: list[dict[str, Any]],
    agent_query: str | None,
    session_query: str | None,
    owner_query: str | None,
    cwd_query: str | None,
    agents: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    resolved_agent = resolve_agent_query(agent_query, agents)
    filtered = []
    for record in records:
        if resolved_agent and norm(record.get("agent")) != norm(resolved_agent):
            continue
        if not id_matches(record.get("accounting_session_id"), session_query):
            continue
        if not id_matches(record.get("owner_session_id"), owner_query):
            continue
        if cwd_query and cwd_query not in str(record.get("owner_cwd") or record.get("cwd") or "") and cwd_query not in str(record.get("raw_cwd") or ""):
            continue
        filtered.append(record)
    return filtered


def group_key(record: dict[str, Any], group_by: str) -> str:
    if group_by == "none":
        return "total"
    if group_by == "day":
        return str(record.get("usage_date") or "unknown")
    if group_by == "agent":
        return str(record.get("agent") or "unknown")
    if group_by == "session":
        return str(record.get("accounting_session_id") or "unknown")
    if group_by == "owner":
        return str(record.get("owner_session_id") or "unknown")
    if group_by == "cwd":
        if record.get("agent_locked"):
            return str(record.get("agent") or "unknown")
        if record.get("usage_kind") == "external":
            return str(record.get("agent") or record.get("owner_cwd") or record.get("cwd") or "unknown")
        return str(record.get("owner_cwd") or record.get("cwd") or "unknown")
    if group_by == "model":
        return str(record.get("model") or "unknown")
    if group_by == "category":
        return str(record.get("usage_category") or DEFAULT_USAGE_CATEGORY)
    raise ValueError(f"unsupported group: {group_by}")


def summarize(records: list[dict[str, Any]], group_by: str) -> dict[str, Any]:
    total = zero_usage()
    groups: dict[str, dict[str, Any]] = {}
    sessions = set()
    owners = set()
    subagent_sessions = set()
    approval_sessions = set()
    agents = set()

    for record in records:
        usage = record["usage"]
        add_usage(total, usage)
        if record.get("accounting_session_id"):
            sessions.add(record["accounting_session_id"])
            if record.get("is_subagent"):
                subagent_sessions.add(record["accounting_session_id"])
        if record.get("is_approval") and record.get("approval_session_id"):
            approval_sessions.add(record["approval_session_id"])
        if record.get("owner_session_id"):
            owners.add(record["owner_session_id"])
        if record.get("agent"):
            agents.add(record["agent"])

        key = group_key(record, group_by)
        entry = groups.setdefault(
            key,
            {
                "key": key,
                "records": 0,
                "sessions": set(),
                "owners": set(),
                "subagent_sessions": set(),
                "approval_sessions": set(),
                "usage": zero_usage(),
            },
        )
        entry["records"] += 1
        if record.get("accounting_session_id"):
            entry["sessions"].add(record["accounting_session_id"])
            if record.get("is_subagent"):
                entry["subagent_sessions"].add(record["accounting_session_id"])
        if record.get("is_approval") and record.get("approval_session_id"):
            entry["approval_sessions"].add(record["approval_session_id"])
        if record.get("owner_session_id"):
            entry["owners"].add(record["owner_session_id"])
        add_usage(entry["usage"], usage)

    rendered_groups = []
    for entry in groups.values():
        rendered_groups.append(
            {
                "key": entry["key"],
                "records": entry["records"],
                "session_count": len(entry["sessions"]),
                "owner_count": len(entry["owners"]),
                "subagent_count": len(entry["subagent_sessions"]),
                "approval_count": len(entry["approval_sessions"]),
                "permission_approval_count": len(entry["approval_sessions"]),
                "usage": entry["usage"],
            }
        )
    rendered_groups.sort(key=lambda item: item["usage"]["total_tokens"], reverse=True)

    return {
        "records": len(records),
        "session_count": len(sessions),
        "owner_count": len(owners),
        "subagent_count": len(subagent_sessions),
        "approval_count": len(approval_sessions),
        "permission_approval_count": len(approval_sessions),
        "agent_count": len(agents),
        "usage": total,
        "groups": rendered_groups,
    }


def print_human(result: dict[str, Any]) -> None:
    usage = result["summary"]["usage"]
    print(f"source: {result['source']}")
    print(f"date: {result['date_start']}..{result['date_end']}")
    print(f"filters: {result['filters']}")
    print(
        "records: {records}, sessions: {session_count}, owners: {owner_count}, "
        "subagents: {subagent_count}, approvals: {approval_count}, agents: {agent_count}".format(
            **result["summary"]
        )
    )
    print(
        "total_tokens: {total_tokens} "
        "(input={input_tokens}, cached_input={cached_input_tokens}, "
        "output={output_tokens}, reasoning_output={reasoning_output_tokens})".format(**usage)
    )

    groups = result["summary"]["groups"]
    if groups:
        print("")
        print(f"breakdown by {result['group_by']}:")
        for item in groups:
            item_usage = item["usage"]
            print(
                f"- {item['key']}: total={item_usage['total_tokens']} "
                f"input={item_usage['input_tokens']} output={item_usage['output_tokens']} "
                f"records={item['records']} sessions={item['session_count']} "
                f"subagents={item['subagent_count']} approvals={item.get('approval_count', 0)}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query local Codex token usage.")
    parser.add_argument("--date", help="Date to query: YYYY-MM-DD, today, or yesterday.")
    parser.add_argument("--since", help="Start date, inclusive. Defaults to --date or today.")
    parser.add_argument("--until", help="End date, inclusive. Defaults to --date/--since.")
    parser.add_argument("--agent", help="Agent name or alias from co-agent agents.json.")
    parser.add_argument("--session", help="Accounting session id or prefix.")
    parser.add_argument("--owner", help="Owner session id or prefix.")
    parser.add_argument("--cwd", help="Substring match on cwd.")
    parser.add_argument(
        "--group-by",
        choices=("none", "day", "agent", "session", "owner", "cwd", "model", "category"),
        default="none",
    )
    parser.add_argument("--source", choices=("auto", "ledger", "probe"), default="auto")
    parser.add_argument("--usage-root", default=str(DEFAULT_USAGE_ROOT))
    parser.add_argument("--agents-json", help="Override co-agent agents.json path.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    start = parse_date(args.date or args.since or "today")
    end = parse_date(args.date or args.until or args.since or start.isoformat())
    if end < start:
        raise SystemExit("--until must be >= --since")

    agents = load_agents(Path(args.agents_json) if args.agents_json else None)
    source, records = read_records(Path(args.usage_root), args.source, start, end, agents)
    filtered = filter_records(records, args.agent, args.session, args.owner, args.cwd, agents)
    summary = summarize(filtered, args.group_by)
    result = {
        "source": source,
        "date_start": start.isoformat(),
        "date_end": end.isoformat(),
        "filters": {
            "agent": args.agent,
            "resolved_agent": resolve_agent_query(args.agent, agents),
            "session": args.session,
            "owner": args.owner,
            "cwd": args.cwd,
        },
        "group_by": args.group_by,
        "summary": summary,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print_human(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
