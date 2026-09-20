#!/usr/bin/env python3
"""Store a durable note into ReMe memory without an external LLM."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from typing import Any

from memory_block_renderer import reconcile_memory_block, write_memory_block
from memory_json_schema import normalize_memory_json
from reme_runtime import REME_WORKDIR, load_runtime_env


MAX_TOPIC_CHARS = 180
MAX_CONCLUSION_CHARS = 4000
MAX_APPLICABILITY_CHARS = 1000


def _stage_log(stage: str, start_ts: float) -> None:
    elapsed = time.monotonic() - start_ts
    now = dt.datetime.now().isoformat(timespec="seconds")
    print(f"[{now}] store_memory {stage} elapsed={elapsed:.3f}s", file=sys.stderr, flush=True)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def _clean_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _first_nonempty_line(*values: str) -> str:
    for value in values:
        for line in str(value or "").splitlines():
            text = _clean_line(line)
            if text:
                return text[:MAX_TOPIC_CHARS]
    return "durable ReMe memory"


def _split_tags(values: list[str]) -> list[str]:
    tags: list[str] = []
    for value in values:
        for raw in re.split(r"[, ]+", value):
            tag = raw.strip()
            if tag and tag not in tags:
                tags.append(tag[:64])
    return tags[:32]


def _stable_digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_capture(
    *,
    note: str,
    task: str,
    outcome: str,
    tags: list[str],
    source_thread: str,
) -> dict[str, Any]:
    digest = _stable_digest(
        {
            "note": note,
            "task": task,
            "outcome": outcome,
            "tags": tags,
            "source_thread": source_thread,
        }
    )
    created_at = _now_iso()
    retrieval_surface = " ".join(
        part
        for part in [
            task,
            outcome,
            note,
            " ".join(tags),
            source_thread,
        ]
        if part
    )
    return {
        "capture_id": f"write_{digest[:16]}",
        "created_at": created_at,
        "durable_idempotency_key": f"write_{digest[:32]}",
        "retrieval_surface": retrieval_surface,
        "task": task,
        "capture_reason": "manual durable write",
        "scenario": outcome or "Durable memory recorded from Codex workflow.",
        "source_thread": source_thread,
        "tags": tags,
    }


def _build_memory_json(
    *,
    capture: dict[str, Any],
    note: str,
    task: str,
    outcome: str,
    tags: list[str],
) -> dict[str, Any]:
    topic = _first_nonempty_line(task, note, outcome)
    applicability = _clean_line(outcome)[:MAX_APPLICABILITY_CHARS] or "Use when this durable lesson applies to future Codex work."
    conclusion = note.strip()[:MAX_CONCLUSION_CHARS]
    solutions = [outcome.strip()[:MAX_CONCLUSION_CHARS]] if outcome.strip() else []
    payload = {
        "topic": topic,
        "applicability": applicability,
        "conclusions": [conclusion],
        "root_cause_patterns": [],
        "solutions": solutions,
        "key_locations": {
            "files": [],
            "symbols": [],
            "errors": [],
        },
        "aliases": tags,
        "benchmark_names": [],
        "retrieval_surface": capture["retrieval_surface"],
        "confidence": "high",
        "source_capture_id": capture["capture_id"],
        "evidence_at": capture["created_at"],
        "evidence_hashes": [],
        "review_after": "",
        "supersedes": [],
    }
    return normalize_memory_json(payload)


def store_note(args: argparse.Namespace) -> dict[str, Any]:
    start_ts = time.monotonic()
    load_runtime_env(override=True)
    note = args.note.strip()
    if not note:
        raise ValueError("--note must not be empty")
    task = args.task.strip()
    outcome = args.outcome.strip()
    source_thread = args.source_thread.strip()
    tags = _split_tags(args.tag or [])

    _stage_log("local_write_start", start_ts)
    capture = _build_capture(
        note=note,
        task=task,
        outcome=outcome,
        tags=tags,
        source_thread=source_thread,
    )
    if args.memory_json:
        memory_json = normalize_memory_json(json.loads(args.memory_json))
        capture["capture_id"] = memory_json["source_capture_id"]
        capture["retrieval_surface"] = memory_json["retrieval_surface"]
        if memory_json.get("evidence_at"):
            capture["created_at"] = memory_json["evidence_at"]
    else:
        memory_json = _build_memory_json(
            capture=capture,
            note=note,
            task=task,
            outcome=outcome,
            tags=tags,
        )
    if args.reconcile_action:
        result = reconcile_memory_block(
            memory_json,
            capture,
            action=args.reconcile_action,
            target_path=args.target_path,
            target_durable_idempotency_key=args.target_key,
        )
    else:
        result = write_memory_block(memory_json, capture)
    _stage_log(f"local_write_done path={result['path']}", start_ts)
    return {
        "status": "stored",
        "backend": "local_deterministic",
        "language": args.language,
        "workdir": REME_WORKDIR,
        **result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Store a durable note into local ReMe memory without an external LLM.")
    parser.add_argument("--note", required=True, help="Durable lesson, decision, or debugging result")
    parser.add_argument("--language", default="en", help="Accepted for compatibility; no LLM translation is performed")
    parser.add_argument("--task", default="", help="Optional task title used as memory topic")
    parser.add_argument("--outcome", default="", help="Optional outcome/reuse context")
    parser.add_argument("--source-thread", default="", help="Optional source Codex thread/session id")
    parser.add_argument("--tag", action="append", default=[], help="Optional tag; may be repeated or comma-separated")
    parser.add_argument("--memory-json", default="", help="Optional normalized structured memory JSON")
    parser.add_argument(
        "--reconcile-action",
        choices=["create", "overwrite", "merge", "keep_both"],
        default="",
        help="Optional write-time reconciliation action",
    )
    parser.add_argument("--target-path", default="", help="Durable memory file selected for overwrite or merge")
    parser.add_argument("--target-key", default="", help="Durable memory block key selected for overwrite or merge")
    args = parser.parse_args()
    result = store_note(args)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
