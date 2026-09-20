#!/usr/bin/env python3
"""Record non-transcript Codex usage, such as codex exec --json output."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from usage_hook import (
    DEFAULT_USAGE_ROOT,
    TOKEN_FIELDS,
    ingest_event,
    now_local,
    zero_usage,
)


def read_json_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid usage json: {exc}") from exc


def load_payload(args: argparse.Namespace) -> Any:
    sources = [bool(args.usage_json), bool(args.usage_file)]
    if sum(1 for item in sources if item) > 1:
        raise SystemExit("use only one of --usage-json or --usage-file")
    if args.usage_json:
        raw = sys.stdin.read() if args.usage_json == "-" else args.usage_json
        return read_json_value(raw)
    if args.usage_file:
        return read_json_value(Path(args.usage_file).read_text(encoding="utf-8"))
    raw = sys.stdin.read()
    if not raw.strip():
        raise SystemExit("usage json is required via --usage-json, --usage-file, or stdin")
    return read_json_value(raw)


def extract_usage(payload: Any) -> dict[str, int]:
    if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
        source = payload["usage"]
    elif isinstance(payload, dict):
        source = payload
    else:
        raise SystemExit("usage payload must be a JSON object")

    usage = zero_usage()
    for field in TOKEN_FIELDS:
        value = source.get(field, 0)
        try:
            usage[field] = int(value or 0)
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"usage field {field} must be an integer") from exc
        if usage[field] < 0:
            raise SystemExit(f"usage field {field} must be non-negative")

    if usage["total_tokens"] <= 0:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    if usage["total_tokens"] <= 0:
        raise SystemExit("usage total_tokens is zero; nothing to record")
    return usage


def parse_usage_date(value: str | None) -> str:
    if not value:
        return now_local().date().isoformat()
    if value in {"today", "yesterday"}:
        today = now_local().date()
        return (today if value == "today" else today - dt.timedelta(days=1)).isoformat()
    return dt.date.fromisoformat(value).isoformat()


def build_event(args: argparse.Namespace, payload: Any, usage: dict[str, int]) -> dict[str, Any]:
    agent = args.agent.strip()
    if not agent:
        raise SystemExit("--agent must not be empty")
    event_id = (args.event_id or "").strip()
    if not event_id:
        raise SystemExit("--event-id is required for idempotent external usage accounting")
    usage_date = parse_usage_date(args.usage_date)
    session_id = args.session_id or f"external:{agent}"
    owner_session_id = args.owner_session_id or session_id
    cwd = args.cwd or f"external:{agent}"
    return {
        "recorded_at": now_local().replace(microsecond=0).isoformat(),
        "usage_date": usage_date,
        "source": "external",
        "external_source": args.source,
        "hook_event_name": "ExternalUsage",
        "turn_id": event_id,
        "event_key": f"external:{agent}:{event_id}",
        "session_id": session_id,
        "raw_session_id": session_id,
        "accounting_session_id": session_id,
        "owner_session_id": owner_session_id,
        "agent": agent,
        "agent_locked": True,
        "usage_kind": "external",
        "cwd": cwd,
        "raw_cwd": cwd,
        "owner_cwd": cwd,
        "model": args.model,
        "is_subagent": False,
        "session_id_fallback_used": False,
        "transcript_path": "",
        "trigger_transcript_path": "",
        "agent_transcript_path": "",
        "token_count": {
            "line_no": None,
            "byte_offset": None,
            "timestamp": args.token_timestamp or now_local().replace(microsecond=0).isoformat(),
            "model_context_window": None,
        },
        "baseline_total": zero_usage(),
        "baseline_token_count": None,
        "total": usage,
        "last": usage,
        "external_usage": usage,
        "external_payload_type": type(payload).__name__,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record external Codex token usage into the local usage ledger.")
    parser.add_argument("--agent", required=True, help="Display agent bucket, e.g. ReMe")
    parser.add_argument("--usage-json", help="Usage JSON object, or '-' to read stdin")
    parser.add_argument("--usage-file", help="Path to JSON object containing usage")
    parser.add_argument("--event-id", required=True, help="Stable event id for idempotency")
    parser.add_argument("--usage-date", help="Usage date: YYYY-MM-DD, today, or yesterday")
    parser.add_argument("--source", default="codex_exec_json", help="External usage source label")
    parser.add_argument("--model", default="", help="Model name, when known")
    parser.add_argument("--cwd", default="", help="Associated cwd, optional")
    parser.add_argument("--session-id", help="Accounting session id; defaults to external:<agent>")
    parser.add_argument("--owner-session-id", help="Owner session id; defaults to session id")
    parser.add_argument("--token-timestamp", help="Token timestamp override")
    parser.add_argument("--usage-root", default=str(DEFAULT_USAGE_ROOT))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print machine-readable result")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = load_payload(args)
    usage = extract_usage(payload)
    event = build_event(args, payload, usage)
    result = ingest_event(Path(args.usage_root), event, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        record = result.get("record") or result.get("event") or event
        delta = record.get("delta") or usage
        print(
            "external usage {status}: agent={agent} total={total} input={input} output={output}".format(
                status=result.get("status"),
                agent=event["agent"],
                total=delta.get("total_tokens", 0),
                input=delta.get("input_tokens", 0),
                output=delta.get("output_tokens", 0),
            )
        )
    return 0 if result.get("status") in {"recorded", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
