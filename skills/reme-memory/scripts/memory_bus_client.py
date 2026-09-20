#!/usr/bin/env python3
import argparse
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from memory_bus_io import atomic_write_json, read_json
from memory_bus_paths import MemoryBusPaths, build_bus_paths, ensure_bus_dirs
from memory_request_schema import MemoryRequest


DEFAULT_CLIENT_ID = os.environ.get("REME_BUS_CLIENT_ID", "codex")
DEFAULT_PROJECT = os.environ.get("REME_BUS_PROJECT", "cling_glb")
DEFAULT_LANGUAGE = os.environ.get("REME_BUS_LANGUAGE", "zh")


@dataclass(frozen=True)
class MemoryBusAccepted:
    request_id: str
    idempotency_key: str
    request_type: str
    request_path: Path

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "request_type": self.request_type,
            "request_path": str(self.request_path),
        }


def new_request_id(prefix: str = "req") -> str:
    suffix = uuid.uuid4().hex[:12]
    timestamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
    return f"{prefix}_{timestamp}_{suffix}"


def result_path(paths: MemoryBusPaths, request_id: str) -> Path:
    return paths.result / f"{request_id}.json"


def status_path(paths: MemoryBusPaths, request_id: str) -> Path:
    return paths.result / f"{request_id}.status.json"


def _target_inbox(paths: MemoryBusPaths, request_type: str) -> Path:
    if request_type in {"memory_write", "memory_index", "memory_maintenance"}:
        return paths.inbox_write
    if "memory_capture" == request_type:
        return paths.inbox_capture
    if "memory_refine" == request_type:
        return paths.inbox_refine
    if "memory_query" == request_type:
        return paths.inbox_query
    if "memory_status" == request_type:
        return paths.inbox_status
    if "memory_flush" == request_type:
        return paths.inbox_flush
    raise ValueError(f"unsupported request_type: {request_type}")


def enqueue_request(
    request_type: str,
    payload: dict,
    client_id: str = DEFAULT_CLIENT_ID,
    project: str = DEFAULT_PROJECT,
    language: str = DEFAULT_LANGUAGE,
    priority: str = "normal",
    paths: Optional[MemoryBusPaths] = None,
    request_id: Optional[str] = None,
) -> MemoryBusAccepted:
    bus_paths = paths or build_bus_paths()
    ensure_bus_dirs(bus_paths)

    request = MemoryRequest.new(
        request_id=request_id or new_request_id(),
        request_type=request_type,
        client_id=client_id,
        project=project,
        language=language,
        payload=payload,
        priority=priority,
    )

    target_dir = _target_inbox(bus_paths, request_type)
    target_path = target_dir / f"{request.request_id}.json"
    atomic_write_json(target_path, request.to_dict())
    return MemoryBusAccepted(
        request_id=request.request_id,
        idempotency_key=request.idempotency_key,
        request_type=request.request_type,
        request_path=target_path,
    )


def enqueue_write(
    lesson: str,
    task: str = "",
    outcome: str = "",
    tags: Optional[list] = None,
    durability: str = "high",
    source_thread: str = "",
    memory_json: Optional[dict] = None,
    reconcile: Optional[dict] = None,
    **kwargs,
) -> MemoryBusAccepted:
    payload = {
        "task": task,
        "outcome": outcome,
        "lesson": lesson,
        "tags": tags or [],
        "durability": durability,
        "source_thread": source_thread,
    }
    if memory_json is not None:
        payload["memory_json"] = memory_json
    if reconcile is not None:
        payload["reconcile"] = reconcile
    return enqueue_request(
        request_type="memory_write",
        payload=payload,
        **kwargs,
    )


def enqueue_capture(
    capture: dict,
    **kwargs,
) -> MemoryBusAccepted:
    return enqueue_request(
        request_type="memory_capture",
        payload=capture,
        **kwargs,
    )


def enqueue_index(path: str, **kwargs) -> MemoryBusAccepted:
    return enqueue_request(
        request_type="memory_index",
        payload={"path": path},
        **kwargs,
    )


def enqueue_refine(
    refine: dict,
    **kwargs,
) -> MemoryBusAccepted:
    return enqueue_request(
        request_type="memory_refine",
        payload=refine,
        **kwargs,
    )


def enqueue_maintenance(
    *,
    maintenance_date: str,
    cleanup_fallback: bool = True,
    compact_retention: bool = True,
    compact_active_days: int = 30,
    compact_delete_days: int = 90,
    dry_run: bool = False,
    **kwargs,
) -> MemoryBusAccepted:
    return enqueue_request(
        request_type="memory_maintenance",
        payload={
            "maintenance_date": maintenance_date,
            "cleanup_fallback": cleanup_fallback,
            "compact_retention": compact_retention,
            "compact_active_days": compact_active_days,
            "compact_delete_days": compact_delete_days,
            "dry_run": dry_run,
        },
        **kwargs,
    )


def enqueue_query(
    query: str,
    max_results: int = 5,
    min_score: float = 0.1,
    vector_weight: float = 0.7,
    candidate_multiplier: float = 3.0,
    **kwargs,
) -> MemoryBusAccepted:
    return enqueue_request(
        request_type="memory_query",
        payload={
            "query": query,
            "max_results": max_results,
            "min_score": min_score,
            "vector_weight": vector_weight,
            "candidate_multiplier": candidate_multiplier,
        },
        **kwargs,
    )


def enqueue_status(target_request_id: str, **kwargs) -> MemoryBusAccepted:
    return enqueue_request(
        request_type="memory_status",
        payload={"target_request_id": target_request_id},
        **kwargs,
    )


def enqueue_flush(scope: str = "writes", **kwargs) -> MemoryBusAccepted:
    return enqueue_request(
        request_type="memory_flush",
        payload={"scope": scope},
        **kwargs,
    )


def wait_for_result(
    request_id: str,
    timeout_seconds: float = 5.0,
    poll_interval_seconds: float = 0.1,
    paths: Optional[MemoryBusPaths] = None,
) -> dict:
    bus_paths = paths or build_bus_paths()
    target = result_path(bus_paths, request_id)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if target.exists():
            return read_json(target)
        time.sleep(poll_interval_seconds)
    raise TimeoutError(f"result timeout for request_id={request_id}")


def read_status_file(request_id: str, paths: Optional[MemoryBusPaths] = None) -> dict:
    bus_paths = paths or build_bus_paths()
    target = status_path(bus_paths, request_id)
    if not target.exists():
        raise FileNotFoundError(str(target))
    return read_json(target)


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Client helpers for ReMe memory bus.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    write_parser = subparsers.add_parser("write")
    write_parser.add_argument("--lesson", required=True)
    write_parser.add_argument("--task", default="")
    write_parser.add_argument("--outcome", default="")

    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--capture-json", required=True)

    index_parser = subparsers.add_parser("index")
    index_parser.add_argument("--path", required=True)

    refine_parser = subparsers.add_parser("refine")
    refine_parser.add_argument("--refine-json", required=True)

    maintenance_parser = subparsers.add_parser("maintenance")
    maintenance_parser.add_argument("--maintenance-date", required=True)
    maintenance_parser.add_argument("--compact-active-days", type=int, default=30)
    maintenance_parser.add_argument("--compact-delete-days", type=int, default=90)
    maintenance_parser.add_argument("--skip-fallback-cleanup", action="store_true")
    maintenance_parser.add_argument("--skip-compact-retention", action="store_true")
    maintenance_parser.add_argument("--dry-run", action="store_true")

    query_parser = subparsers.add_parser("query")
    query_parser.add_argument("--query", required=True)
    query_parser.add_argument("--max-results", type=int, default=5)
    query_parser.add_argument("--wait-timeout", type=float, default=0.0)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--target-request-id", required=True)

    flush_parser = subparsers.add_parser("flush")
    flush_parser.add_argument("--wait-timeout", type=float, default=0.0)

    args = parser.parse_args()
    if "write" == args.command:
        accepted = enqueue_write(lesson=args.lesson, task=args.task, outcome=args.outcome)
        _print_json({"status": "accepted", **accepted.to_dict()})
        return
    if "capture" == args.command:
        payload = read_json(Path(args.capture_json))
        accepted = enqueue_capture(capture=payload)
        _print_json({"status": "accepted", **accepted.to_dict()})
        return
    if "index" == args.command:
        accepted = enqueue_index(path=args.path)
        _print_json({"status": "accepted", **accepted.to_dict()})
        return
    if "maintenance" == args.command:
        accepted = enqueue_maintenance(
            maintenance_date=args.maintenance_date,
            cleanup_fallback=not args.skip_fallback_cleanup,
            compact_retention=not args.skip_compact_retention,
            compact_active_days=args.compact_active_days,
            compact_delete_days=args.compact_delete_days,
            dry_run=args.dry_run,
        )
        _print_json({"status": "accepted", **accepted.to_dict()})
        return
    if "refine" == args.command:
        payload = read_json(Path(args.refine_json))
        accepted = enqueue_refine(refine=payload)
        _print_json({"status": "accepted", **accepted.to_dict()})
        return
    if "query" == args.command:
        accepted = enqueue_query(query=args.query, max_results=args.max_results)
        if 0 < args.wait_timeout:
            result = wait_for_result(accepted.request_id, timeout_seconds=args.wait_timeout)
            _print_json({"status": "answered", "accepted": accepted.to_dict(), "result": result})
            return
        _print_json({"status": "accepted", **accepted.to_dict()})
        return
    if "status" == args.command:
        accepted = enqueue_status(target_request_id=args.target_request_id)
        _print_json({"status": "accepted", **accepted.to_dict()})
        return

    accepted = enqueue_flush()
    if 0 < args.wait_timeout:
        result = wait_for_result(accepted.request_id, timeout_seconds=args.wait_timeout)
        _print_json({"status": "answered", "accepted": accepted.to_dict(), "result": result})
        return
    _print_json({"status": "accepted", **accepted.to_dict()})


if __name__ == "__main__":
    main()
