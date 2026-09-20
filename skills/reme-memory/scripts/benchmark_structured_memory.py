#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import statistics
import tempfile
import threading
import time
from pathlib import Path
from typing import List

from capture_adapter import handle_capture_request
from capture_schema import normalize_capture_input
from memory_block_renderer import parse_memory_blocks
from memory_bus_client import enqueue_capture, enqueue_query, wait_for_result
from memory_daemon import MemoryDaemon
from memory_request_store import MemoryRequestStore
from query_adapter import handle_query_request
from status_adapter import handle_status_request
from structured_memory_llm import StructuredMemoryFallbackRequired


def _failing_llm(_capture):
    raise StructuredMemoryFallbackRequired("force fallback for benchmark")


def _wait_for_capture(bus_root: Path, request_id: str, timeout_seconds: float) -> dict:
    deadline = time.time() + timeout_seconds
    started = time.time()
    status_path = bus_root / "result" / f"{request_id}.status.json"
    while time.time() < deadline:
        if status_path.exists():
            payload = json.loads(status_path.read_text(encoding="utf-8"))
            if payload.get("state") == "stored" and payload.get("phase") == "memory_indexed":
                return {"status": payload, "total_seconds": time.time() - started}
        time.sleep(0.05)
    raise TimeoutError(request_id)


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return not (a_end < b_start or b_end < a_start)


def _query_hit(memory_file: Path, durable_key: str, result: dict) -> bool:
    blocks = parse_memory_blocks(memory_file.read_text(encoding="utf-8"))
    expected = next(block for block in blocks if block["durable_idempotency_key"] == durable_key)
    expected_range = (expected["start_line"], expected["end_line"])
    for item in result.get("items", [])[:3]:
        if not item["path"].endswith(memory_file.name):
            continue
        if _ranges_overlap(expected_range[0], expected_range[1], int(item["start_line"]), int(item["end_line"])):
            return True
    return False


def _summary(values: List[float]) -> dict:
    return {
        "count": len(values),
        "min_seconds": min(values),
        "avg_seconds": statistics.mean(values),
        "median_seconds": statistics.median(values),
        "max_seconds": max(values),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark structured capture fallback latency and retrieval hit rate.")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    bus_root = Path(tempfile.mkdtemp(prefix="reme_structured_bench_bus_", dir="/tmp"))
    reme_root = Path(tempfile.mkdtemp(prefix="reme_structured_bench_reme_", dir="/tmp"))
    saved_env = os.environ.copy()
    payload = {}
    try:
        os.environ["REME_BUS_ROOT"] = str(bus_root)
        os.environ["REME_WORKDIR"] = str(reme_root)
        os.environ["REME_VECTOR_ENABLED"] = "false"
        os.environ["REME_FTS_ENABLED"] = "true"
        store = MemoryRequestStore(daemon_id="daemon-structured-bench")
        daemon = MemoryDaemon(store=store, poll_interval_seconds=0.02)
        daemon.register_handler(
            "memory_capture",
            lambda claimed, store: handle_capture_request(
                claimed,
                store,
                llm_backend=_failing_llm,
                enable_durable_write=True,
            ),
        )
        daemon.register_handler("memory_query", handle_query_request)
        daemon.register_handler("memory_status", handle_status_request)
        thread = threading.Thread(target=daemon.serve_forever, daemon=True)
        thread.start()
        try:
            runs = []
            for index in range(1, args.iterations + 1):
                alias = f"bench-alias-{index}"
                capture = normalize_capture_input(
                    {
                        "project": "cling_glb",
                        "task": f"Structured benchmark capture {index}",
                        "session_summary": f"Structured benchmark capture {index} should remain queryable.",
                        "facts": [{"text": f"benchmark capture {index} writes deterministic fallback memory"}],
                        "aliases": [alias],
                        "durable_identity": f"structured benchmark identity {index}",
                    },
                    thread_id_override=f"thread-bench-{index}",
                )
                request_id = f"req_structured_bench_{index:03d}"
                enqueue_capture(capture=capture, request_id=request_id)
                write_result = _wait_for_capture(bus_root, request_id, args.timeout)

                query_started = time.time()
                accepted = enqueue_query(query=alias, max_results=3)
                query_result = wait_for_result(accepted.request_id, timeout_seconds=args.timeout)
                query_elapsed = time.time() - query_started
                memory_file = next((reme_root / "memory").glob("*.md"))
                hit = _query_hit(memory_file, capture["durable_idempotency_key"], query_result)
                runs.append(
                    {
                        "iteration": index,
                        "alias": alias,
                        "capture_id": capture["capture_id"],
                        "durable_idempotency_key": capture["durable_idempotency_key"],
                        "write_total_seconds": write_result["total_seconds"],
                        "query_total_seconds": query_elapsed,
                        "top3_hit": hit,
                    }
                )

            sample_memory_file = next((reme_root / "memory").glob("*.md"))
            sample_memory_excerpt = sample_memory_file.read_text(encoding="utf-8")
            payload = {
                "mode": "fallback_fts",
                "iterations": args.iterations,
                "write_summary": _summary([item["write_total_seconds"] for item in runs]),
                "query_summary": _summary([item["query_total_seconds"] for item in runs]),
                "hit_rate_top3": round(sum(1 for item in runs if item["top3_hit"]) / len(runs), 4),
                "runs": runs,
                "sample_memory_file": str(sample_memory_file),
                "sample_memory_excerpt": sample_memory_excerpt,
            }
        finally:
            try:
                daemon.stop()
            except Exception:
                pass
            thread.join(timeout=2)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        if args.output:
            Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        shutil.rmtree(bus_root, ignore_errors=True)
        shutil.rmtree(reme_root, ignore_errors=True)


if __name__ == "__main__":
    main()
