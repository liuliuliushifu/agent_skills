#!/usr/bin/env python3
import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

from capture_schema import load_capture_json, normalize_capture_input
from handoff_writer import write_handoff
from memory_bus_client import (
    enqueue_capture,
    enqueue_flush,
    enqueue_query,
    enqueue_refine,
    enqueue_status,
    enqueue_write,
    read_status_file,
    wait_for_result,
)
from reme_daemon_guard import ensure_daemon_running


ROOT = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(ROOT.parents[2])))
METRICS_DIR = Path(os.environ.get("REME_MEMORY_METRICS_DIR", str(CODEX_HOME / "memories/reme-memory")))
METRICS_LOG = METRICS_DIR / "memory_workflow_events.jsonl"
QUERY_WAIT_TIMEOUT_SECONDS = float(os.environ.get("REME_QUERY_WAIT_TIMEOUT_SECONDS", "90.0"))
WRITE_WAIT_TIMEOUT_SECONDS = float(os.environ.get("REME_WRITE_WAIT_TIMEOUT_SECONDS", "900.0"))
WRITE_WAIT_PROGRESS_INTERVAL_SECONDS = float(os.environ.get("REME_WRITE_WAIT_PROGRESS_INTERVAL_SECONDS", "30.0"))
STATUS_WAIT_TIMEOUT_SECONDS = float(os.environ.get("REME_STATUS_WAIT_TIMEOUT_SECONDS", "10.0"))
FLUSH_WAIT_TIMEOUT_SECONDS = float(os.environ.get("REME_FLUSH_WAIT_TIMEOUT_SECONDS", "30.0"))
DAEMON_ENSURE_TIMEOUT_SECONDS = float(os.environ.get("REME_DAEMON_ENSURE_TIMEOUT_SECONDS", "30.0"))
TERMINAL_STATES = {"stored", "captured", "answered", "accepted_async", "failed", "deadletter"}


def _stage_log(stage: str, start_ts: float) -> None:
    elapsed = time.monotonic() - start_ts
    now = datetime.datetime.now().isoformat(timespec="seconds")
    print(f"[{now}] memory_workflow {stage} elapsed={elapsed:.3f}s", file=sys.stderr, flush=True)


def _run_search(query: str, max_results: int) -> List[Dict]:
    _ensure_daemon("prepare")
    accepted = enqueue_query(
        query=query,
        max_results=max_results,
    )
    result = wait_for_result(
        request_id=accepted.request_id,
        timeout_seconds=QUERY_WAIT_TIMEOUT_SECONDS,
    )
    items = result.get("items", [])
    if not items:
        return []
    return items


def _ensure_daemon(operation: str) -> None:
    result = ensure_daemon_running(wait_seconds=DAEMON_ENSURE_TIMEOUT_SECONDS)
    if result.get("started"):
        waited = result.get("waited_seconds", 0)
        print(
            f"ReMe daemon started for {operation}; healthy after {waited}s",
            file=sys.stderr,
            flush=True,
        )


def _wait_for_terminal_status(request_id: str, timeout_seconds: float) -> Dict:
    deadline = time.monotonic() + timeout_seconds
    next_progress_log = time.monotonic() + WRITE_WAIT_PROGRESS_INTERVAL_SECONDS
    last_status: Dict = {}
    while time.monotonic() < deadline:
        try:
            status = read_status_file(request_id)
        except FileNotFoundError:
            time.sleep(0.2)
            continue
        last_status = status
        if status.get("state", "") in TERMINAL_STATES:
            return status
        now = time.monotonic()
        if now >= next_progress_log:
            state = status.get("state", "")
            phase = status.get("phase", "")
            attempt = status.get("attempt", "")
            heartbeat = status.get("heartbeat_at", "")
            print(
                f"Memory write-back still running: request_id={request_id} "
                f"state={state} phase={phase} attempt={attempt} heartbeat={heartbeat}",
                file=sys.stderr,
                flush=True,
            )
            next_progress_log = now + WRITE_WAIT_PROGRESS_INTERVAL_SECONDS
        time.sleep(0.2)
    detail = json.dumps(last_status, ensure_ascii=False, sort_keys=True) if last_status else "no status file"
    raise TimeoutError(f"status timeout for request_id={request_id}; last_status={detail}")


def _extract_highlights(snippet: str, query: str, limit: int = 5) -> List[str]:
    terms = [term.lower() for term in query.split() if term.strip()]
    selected = []
    for raw_line in snippet.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        score = sum(1 for term in terms if term in line.lower())
        if score == 0 and not line.startswith(("-", "#", "*")):
            continue
        selected.append((score, line))

    selected.sort(key=lambda item: item[0], reverse=True)
    highlights = []
    for _score, line in selected:
        cleaned = line.lstrip("- ").strip()
        if cleaned not in highlights:
            highlights.append(cleaned)
        if len(highlights) >= limit:
            break
    return highlights


def _append_event(event: Dict) -> None:
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        **event,
    }
    with METRICS_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def prepare(task: str, max_results: int) -> int:
    results = _run_search(task, max_results)
    _append_event(
        {
            "event": "prepare",
            "task": task,
            "max_results": max_results,
            "result_count": len(results),
            "hit": bool(results),
            "paths": [item["path"] for item in results],
        }
    )
    if not results:
        print("ReMe brief: no relevant local memory found.")
        return 0

    print("ReMe brief")
    print("Task: {}".format(task))
    for idx, item in enumerate(results, start=1):
        print("")
        print("Memory {}: {}".format(idx, item["path"]))
        for highlight in _extract_highlights(item.get("snippet", ""), task):
            print("- {}".format(highlight))
    return 0


def finalize(outcome: str, lesson: str, language: str, wait_timeout: float) -> int:
    start_ts = time.monotonic()
    _stage_log(f"finalize_start language={language}", start_ts)

    note = lesson.strip() if lesson else ""
    if not note and not outcome.strip():
        _append_event(
            {
                "event": "finalize",
                "outcome": outcome,
                "lesson_present": False,
                "write_attempted": False,
                "write_success": False,
                "reason": "empty_outcome_and_lesson",
            }
        )
        _stage_log("finalize_skip_empty", start_ts)
        print("No outcome or lesson provided; skip memory write-back.")
        return 0

    if not note:
        _append_event(
            {
                "event": "finalize",
                "outcome": outcome,
                "lesson_present": False,
                "write_attempted": False,
                "write_success": False,
                "reason": "no_durable_lesson",
            }
        )
        _stage_log("finalize_skip_no_lesson", start_ts)
        print("No durable lesson provided; skip memory write-back.")
        return 0

    try:
        _ensure_daemon("finalize")
        accepted = enqueue_write(
            lesson=note,
            task="",
            outcome=outcome,
            project=os.environ.get("REME_BUS_PROJECT", "cling_glb"),
            language=language,
        )
        _stage_log(f"store_memory_enqueued request_id={accepted.request_id}", start_ts)
        try:
            terminal_status = _wait_for_terminal_status(
                request_id=accepted.request_id,
                timeout_seconds=wait_timeout,
            )
        except TimeoutError as exc:
            _append_event(
                {
                    "event": "finalize",
                    "outcome": outcome,
                    "lesson_present": True,
                    "write_attempted": True,
                    "write_success": False,
                    "language": language,
                    "write_enqueued": True,
                    "request_id": accepted.request_id,
                    "reason": "write_wait_timeout",
                    "error": str(exc),
                }
            )
            _stage_log(f"store_memory_wait_timeout {accepted.request_id}", start_ts)
            print(f"Memory write-back timeout: request_id={accepted.request_id}", file=sys.stderr)
            print(str(exc), file=sys.stderr)
            return 1

        state = terminal_status.get("state", "")
        phase = terminal_status.get("phase", "")
        write_success = state == "stored" and phase == "indexed"
        _append_event(
            {
                "event": "finalize",
                "outcome": outcome,
                "lesson_present": True,
                "write_attempted": True,
                "write_success": write_success,
                "language": language,
                "write_enqueued": True,
                "request_id": accepted.request_id,
                "write_state": state,
                "write_phase": phase,
            }
        )
        if not write_success:
            _stage_log(f"store_memory_failed state={state} phase={phase}", start_ts)
            print(
                f"Memory write-back failed: request_id={accepted.request_id} state={state} phase={phase}",
                file=sys.stderr,
            )
            return 1
        _stage_log(f"store_memory_completed state={state} phase={phase}", start_ts)
        print(f"Memory write-back completed: request_id={accepted.request_id} state={state} phase={phase}")
        return 0
    except Exception as exc:
        _append_event(
            {
                "event": "finalize",
                "outcome": outcome,
                "lesson_present": True,
                "write_attempted": True,
                "write_success": False,
                "language": language,
                "reason": "store_memory_enqueue_failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        _stage_log(f"store_memory_enqueue_failed {type(exc).__name__}: {exc}", start_ts)
        return 1


def capture_context(capture_json_path: str, thread_id: str, session_scope_key: str, enqueue_durable: bool) -> int:
    start_ts = time.monotonic()
    _stage_log("capture_context_start", start_ts)
    try:
        raw_capture = load_capture_json(Path(capture_json_path))
        capture = normalize_capture_input(
            raw_capture,
            thread_id_override=thread_id,
            session_scope_key_override=session_scope_key,
        )
        written = write_handoff(capture)
        accepted = None
        if enqueue_durable:
            _ensure_daemon("capture_context")
            accepted = enqueue_capture(
                capture=capture,
                project=capture["project"],
            )
        _append_event(
            {
                "event": "capture_context",
                "capture_id": capture["capture_id"],
                "thread_id": capture.get("thread_id"),
                "session_scope_key": capture.get("session_scope_key"),
                "handoff_idempotency_key": capture["handoff_idempotency_key"],
                "durable_idempotency_key": capture["durable_idempotency_key"],
                "stable_session_scope": written["stable_session_scope"],
                "overwrote": written["overwrote"],
                "md_path": written["md_path"],
                "json_path": written["json_path"],
                "enqueue_durable": enqueue_durable,
                "capture_request_id": accepted.request_id if accepted is not None else "",
            }
        )
        _stage_log("capture_context_written", start_ts)
        message = "Handoff written: capture_id={} md_path={} json_path={} overwrote={}".format(
                capture["capture_id"],
                written["md_path"],
                written["json_path"],
                "yes" if written["overwrote"] else "no",
        )
        if accepted is not None:
            message += f" capture_request_id={accepted.request_id}"
        print(message)
        return 0
    except Exception as exc:
        _append_event(
            {
                "event": "capture_context",
                "write_success": False,
                "reason": "capture_context_failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "capture_json_path": capture_json_path,
            }
        )
        _stage_log(f"capture_context_failed {type(exc).__name__}: {exc}", start_ts)
        print(f"capture_context failed: {exc}", file=sys.stderr)
        return 1


def refine(refine_json_path: str) -> int:
    start_ts = time.monotonic()
    _stage_log("refine_start", start_ts)
    try:
        payload = json.loads(Path(refine_json_path).read_text(encoding="utf-8"))
        _ensure_daemon("refine")
        accepted = enqueue_refine(
            refine=payload,
            project=os.environ.get("REME_BUS_PROJECT", "cling_glb"),
            language=os.environ.get("REME_BUS_LANGUAGE", "zh"),
        )
        _append_event(
            {
                "event": "refine",
                "refine_json_path": refine_json_path,
                "request_id": accepted.request_id,
                "accepted": True,
            }
        )
        _stage_log(f"refine_enqueued request_id={accepted.request_id}", start_ts)
        print(f"Memory refine enqueued: request_id={accepted.request_id}")
        return 0
    except Exception as exc:
        _append_event(
            {
                "event": "refine",
                "refine_json_path": refine_json_path,
                "accepted": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        _stage_log(f"refine_failed {type(exc).__name__}: {exc}", start_ts)
        print(f"refine failed: {exc}", file=sys.stderr)
        return 1


def status(target_request_id: str, wait_timeout: float) -> int:
    _ensure_daemon("status")
    accepted = enqueue_status(target_request_id=target_request_id)
    result = wait_for_result(accepted.request_id, timeout_seconds=wait_timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def flush(wait_timeout: float) -> int:
    _ensure_daemon("flush")
    accepted = enqueue_flush()
    result = wait_for_result(accepted.request_id, timeout_seconds=wait_timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Wrapper workflow for local ReMe memory.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare", help="Retrieve relevant memory before work")
    prepare_parser.add_argument("--task", required=True, help="Current task description")
    prepare_parser.add_argument("--max-results", type=int, default=3, help="Maximum number of memory hits")

    finalize_parser = subparsers.add_parser("finalize", help="Optionally write back a durable lesson")
    finalize_parser.add_argument("--outcome", default="", help="Task result summary")
    finalize_parser.add_argument("--lesson", default="", help="Durable lesson worth storing")
    finalize_parser.add_argument("--language", default="en", help="Summary language, e.g. en or zh")
    finalize_parser.add_argument("--wait-timeout", type=float, default=WRITE_WAIT_TIMEOUT_SECONDS, help="Seconds to wait for stored/indexed write completion")

    capture_parser = subparsers.add_parser("capture_context", help="Write a structured handoff capture")
    capture_parser.add_argument("--capture-json", required=True, help="Path to the source capture JSON file")
    capture_parser.add_argument("--thread-id", default="", help="Optional Codex thread id")
    capture_parser.add_argument("--session-scope-key", default="", help="Optional stable session scope key")
    capture_parser.add_argument("--enqueue-durable", action="store_true", help="Also enqueue the normalized capture to the daemon bus")

    refine_parser = subparsers.add_parser("refine", help="Enqueue evidence-only async refine")
    refine_parser.add_argument("--refine-json", required=True, help="Path to evidence refine JSON")

    status_parser = subparsers.add_parser("status", help="Query the status of a memory bus request")
    status_parser.add_argument("--target-request-id", required=True, help="Request id to inspect")
    status_parser.add_argument("--wait-timeout", type=float, default=STATUS_WAIT_TIMEOUT_SECONDS, help="Seconds to wait for status response")

    flush_parser = subparsers.add_parser("flush", help="Insert a write barrier request")
    flush_parser.add_argument("--wait-timeout", type=float, default=FLUSH_WAIT_TIMEOUT_SECONDS, help="Seconds to wait for flush response")

    args = parser.parse_args()
    if args.command == "prepare":
        return prepare(args.task, args.max_results)
    if args.command == "finalize":
        return finalize(args.outcome, args.lesson, args.language, args.wait_timeout)
    if args.command == "capture_context":
        return capture_context(args.capture_json, args.thread_id, args.session_scope_key, args.enqueue_durable)
    if args.command == "refine":
        return refine(args.refine_json)
    if args.command == "status":
        return status(args.target_request_id, args.wait_timeout)
    return flush(args.wait_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
