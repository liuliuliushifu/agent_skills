#!/usr/bin/env python3
"""Prepare per-turn outcome inputs for AI daily report generation."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from extract_turn_bundle import ExtractError, extract_bundles, load_session_meta  # noqa: E402


TZ = dt.timezone(dt.timedelta(hours=8))
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
DAILY_DIR = Path(os.environ.get("AI_DAILY_REPORT_DIR", str(CODEX_HOME / "daily_report")))
SESSIONS_ROOT = CODEX_HOME / "sessions"
OUTCOME_ROOT = Path(os.environ.get("AI_DAILY_OUTCOME_DIR", str(DAILY_DIR / "outcomes")))


def now_iso() -> str:
    return dt.datetime.now(TZ).replace(microsecond=0).isoformat()


def parse_date(value: str) -> dt.date:
    value = value.strip().lower()
    today = dt.datetime.now(TZ).date()
    if value == "today":
        return today
    if value == "yesterday":
        return today - dt.timedelta(days=1)
    return dt.date.fromisoformat(value)


def day_session_dir(day: dt.date) -> Path:
    return SESSIONS_ROOT / day.strftime("%Y") / day.strftime("%m") / day.strftime("%d")


def path_has_blocked_component(path: Path) -> bool:
    if "\x00" in str(path):
        return True
    return any(part in {".agent", ".agents"} for part in path.parts)


def session_path_date(path: Path) -> dt.date | None:
    try:
        parts = path.relative_to(SESSIONS_ROOT).parts
        if len(parts) < 4:
            return None
        return dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
    except Exception:
        return None


def iter_session_paths(day: dt.date):
    root = day_session_dir(day)
    candidates: dict[str, Path] = {}
    if root.exists():
        for path in root.glob("*.jsonl"):
            if not path_has_blocked_component(path):
                candidates[str(path)] = path

    if SESSIONS_ROOT.exists():
        start = dt.datetime.combine(day, dt.time.min, TZ)
        start_ts = start.timestamp()
        for path in SESSIONS_ROOT.rglob("*.jsonl"):
            if path_has_blocked_component(path):
                continue
            created_date = session_path_date(path)
            if created_date is not None and created_date > day:
                continue
            try:
                if path.stat().st_mtime >= start_ts:
                    candidates[str(path)] = path
            except Exception:
                continue
    return [candidates[key] for key in sorted(candidates)]


def is_owner_transcript(path: Path) -> bool:
    meta = load_session_meta(path)
    source = meta.get("source")
    if meta.get("thread_source") == "subagent":
        return False
    if isinstance(source, dict) and source.get("subagent"):
        return False
    return True


def cache_path(bundle: dict[str, Any]) -> Path:
    day = str(bundle.get("bundle_date"))
    year, month = day[:4], day[5:7]
    name = f"{bundle.get('session_id')}__{bundle.get('turn_id')}.json".replace("/", "_")
    return OUTCOME_ROOT / "turn_bundles" / year / month / day / name


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_cached(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def analysis_input(bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "turn_analysis_input.v1",
        "bundle_hash": bundle.get("bundle_hash"),
        "session_id": bundle.get("session_id"),
        "turn_id": bundle.get("turn_id"),
        "turn_status": bundle.get("turn_status"),
        "cwd": bundle.get("cwd"),
        "started_at": bundle.get("started_at"),
        "completed_at": bundle.get("completed_at"),
        "cutoff_at": bundle.get("cutoff_at"),
        "user_prompt": bundle.get("user_prompt"),
        "agent_final_answer": bundle.get("agent_final_answer"),
        "assistant_updates": bundle.get("assistant_updates"),
        "tool_summary": bundle.get("tool_summary"),
        "instructions": (
            "Extract mission/result/decision/blocker/follow_up candidates only. "
            "Do not treat build/deploy/test as standalone outcomes; use them only as evidence. "
            "For in_progress turns, do not mark result/implemented/verified as done."
        ),
    }


def is_daily_report_generation_bundle(bundle: dict[str, Any]) -> bool:
    prompt = str(bundle.get("user_prompt") or "").lstrip()
    return prompt.startswith("使用 $ai-daily-report 生成 ") and "中文 Codex/AI 工作日报" in prompt[:2000]


def run_pipeline(day: dt.date, *, include_in_progress: bool) -> dict[str, Any]:
    started = now_iso()
    run_id = "RUN-" + dt.datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    run_dir = OUTCOME_ROOT / "runs" / day.isoformat() / run_id
    bundles: list[dict[str, Any]] = []
    analysis_rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    cache_hits = 0
    cache_misses = 0
    hash_mismatches = 0
    skipped_subagents = 0
    skipped_report_generation_turns = 0
    skipped_out_of_day_turns = 0
    processed_transcripts = 0

    cutoff = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, TZ)
    target_day = day.isoformat()

    for path in iter_session_paths(day):
        if not is_owner_transcript(path):
            skipped_subagents += 1
            continue
        processed_transcripts += 1
        try:
            extracted = extract_bundles(path, include_in_progress=include_in_progress, cutoff_at=cutoff)
        except ExtractError as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        for bundle in extracted:
            if bundle.get("bundle_date") != target_day:
                skipped_out_of_day_turns += 1
                continue
            if is_daily_report_generation_bundle(bundle):
                skipped_report_generation_turns += 1
                continue
            path_in_cache = cache_path(bundle)
            cached = read_cached(path_in_cache)
            if cached and cached.get("bundle_hash") == bundle.get("bundle_hash"):
                cache_hits += 1
                selected = cached
            else:
                if cached:
                    hash_mismatches += 1
                else:
                    cache_misses += 1
                selected = bundle
                write_json_atomic(path_in_cache, bundle)
            bundles.append(selected)
            analysis_rows.append(analysis_input(selected))

    write_jsonl(run_dir / "turn_bundles.jsonl", bundles)
    write_jsonl(run_dir / "turn_analysis_input.jsonl", analysis_rows)
    summary = {
        "schema_version": "outcome_pipeline_summary.v1",
        "run_id": run_id,
        "date": day.isoformat(),
        "started_at": started,
        "finished_at": now_iso(),
        "run_dir": str(run_dir),
        "include_in_progress": include_in_progress,
        "processed_transcripts": processed_transcripts,
        "skipped_subagent_transcripts": skipped_subagents,
        "skipped_report_generation_turns": skipped_report_generation_turns,
        "skipped_out_of_day_turns": skipped_out_of_day_turns,
        "turn_bundles": len(bundles),
        "completed_turns": sum(1 for item in bundles if item.get("turn_status") == "completed"),
        "in_progress_turns": sum(1 for item in bundles if item.get("turn_status") == "in_progress"),
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "hash_mismatches": hash_mismatches,
        "errors": errors,
    }
    write_json_atomic(run_dir / "pipeline_summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare daily outcome turn bundles and analysis inputs.")
    parser.add_argument("--date", default="yesterday")
    parser.add_argument("--include-in-progress", action="store_true", default=True)
    parser.add_argument("--completed-only", action="store_true", help="Do not include in-progress turns.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    day = parse_date(args.date)
    summary = run_pipeline(day, include_in_progress=(args.include_in_progress and not args.completed_only))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(f"run_dir: {summary['run_dir']}")
        print(f"turn_bundles: {summary['turn_bundles']}")
        print(f"completed_turns: {summary['completed_turns']}")
        print(f"in_progress_turns: {summary['in_progress_turns']}")
        print(f"cache_hits: {summary['cache_hits']}")
        print(f"cache_misses: {summary['cache_misses']}")
        print(f"hash_mismatches: {summary['hash_mismatches']}")
        if summary["errors"]:
            print(f"errors: {len(summary['errors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
