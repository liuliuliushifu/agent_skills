#!/usr/bin/env python3
import argparse
import json
import statistics
import time
import uuid
from pathlib import Path

from memory_bus_client import enqueue_query, enqueue_write, read_status_file, wait_for_result


DEFAULT_WRITE_TIMEOUT_SECONDS = 900.0
DEFAULT_QUERY_TIMEOUT_SECONDS = 180.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0


def _wait_for_write(request_id: str, timeout_seconds: float, poll_interval_seconds: float) -> dict:
    deadline = time.monotonic() + timeout_seconds
    started_at = time.monotonic()
    first_seen = {}
    last_status = None

    while time.monotonic() < deadline:
        try:
            status = read_status_file(request_id)
        except FileNotFoundError:
            time.sleep(poll_interval_seconds)
            continue

        last_status = status
        phase = status.get("phase", "")
        state = status.get("state", "")
        if phase and phase not in first_seen:
            first_seen[phase] = time.monotonic() - started_at
        if "stored" == state and "indexed" == phase:
            return {
                "status": status,
                "phase_first_seen_seconds": first_seen,
                "total_seconds": time.monotonic() - started_at,
            }
        if state in {"failed", "deadletter"}:
            raise RuntimeError(f"write failed: {json.dumps(status, ensure_ascii=False)}")
        time.sleep(poll_interval_seconds)

    raise TimeoutError(
        "write timeout request_id={} last_status={}".format(
            request_id,
            json.dumps(last_status, ensure_ascii=False) if last_status else "<missing>",
        )
    )


def _run_one(index: int, query_timeout_seconds: float, write_timeout_seconds: float, poll_interval_seconds: float) -> dict:
    token = "bench-{}-{}".format(index, uuid.uuid4().hex[:8])
    lesson = (
        "ReMe benchmark sample token={} measures daemon write indexed latency and query latency."
        .format(token)
    )
    query_text = token

    write_accepted = enqueue_write(
        lesson=lesson,
        outcome="benchmark",
        tags=["benchmark", "latency"],
        durability="high",
    )
    write_result = _wait_for_write(
        request_id=write_accepted.request_id,
        timeout_seconds=write_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )

    query_started = time.monotonic()
    query_accepted = enqueue_query(
        query=query_text,
        max_results=3,
    )
    query_result = wait_for_result(
        request_id=query_accepted.request_id,
        timeout_seconds=query_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    query_elapsed = time.monotonic() - query_started

    return {
        "iteration": index,
        "token": token,
        "write_request_id": write_accepted.request_id,
        "query_request_id": query_accepted.request_id,
        "write_total_seconds": write_result["total_seconds"],
        "write_phase_first_seen_seconds": write_result["phase_first_seen_seconds"],
        "write_final_status": write_result["status"],
        "query_total_seconds": query_elapsed,
        "query_result_count": len(query_result.get("items", [])),
        "query_top_paths": [item.get("path", "") for item in query_result.get("items", [])[:3]],
    }


def _summary(name: str, values: list) -> dict:
    return {
        "name": name,
        "count": len(values),
        "min_seconds": min(values),
        "avg_seconds": statistics.mean(values),
        "median_seconds": statistics.median(values),
        "max_seconds": max(values),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark ReMe daemon write/read latency.")
    parser.add_argument("--iterations", type=int, default=3, help="Number of write+query iterations")
    parser.add_argument("--write-timeout", type=float, default=DEFAULT_WRITE_TIMEOUT_SECONDS)
    parser.add_argument("--query-timeout", type=float, default=DEFAULT_QUERY_TIMEOUT_SECONDS)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    parser.add_argument("--output", default="", help="Optional JSON output path")
    args = parser.parse_args()

    runs = []
    for index in range(1, args.iterations + 1):
        runs.append(
            _run_one(
                index=index,
                query_timeout_seconds=args.query_timeout,
                write_timeout_seconds=args.write_timeout,
                poll_interval_seconds=args.poll_interval,
            )
        )

    write_values = [item["write_total_seconds"] for item in runs]
    query_values = [item["query_total_seconds"] for item in runs]
    payload = {
        "iterations": args.iterations,
        "write_summary": _summary("write_to_indexed", write_values),
        "query_summary": _summary("query_to_answered", query_values),
        "runs": runs,
    }

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)

    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
