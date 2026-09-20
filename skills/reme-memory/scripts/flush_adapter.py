#!/usr/bin/env python3
from memory_bus_client import result_path
from memory_bus_io import atomic_write_json


def handle_flush_request(claimed, store) -> None:
    pending_writes = len(list(store.paths.inbox_write.glob("*.json"))) + len(list(store.paths.processing_write.glob("*.json")))
    pending_captures = len(list(store.paths.inbox_capture.glob("*.json"))) + len(list(store.paths.processing_capture.glob("*.json")))
    pending_refines = len(list(store.paths.inbox_refine.glob("*.json"))) + len(list(store.paths.processing_refine.glob("*.json")))
    async_refines = len(list(store.paths.async_refine_queued.glob("*.json"))) + len(list(store.paths.async_refine_running.glob("*.json")))
    scope = claimed.request.payload.get("scope", "writes")
    worker_ready = True
    worker_generation = 0
    runtime_worker = getattr(store, "runtime_worker", None)
    if runtime_worker is not None:
        try:
            worker_health = runtime_worker.health_snapshot()
            worker_generation = int(worker_health.get("worker_generation", 0))
        except Exception:
            worker_ready = False
    if "writes" == scope:
        pending_total = pending_writes
    elif "captures" == scope:
        pending_total = pending_captures
    else:
        pending_total = pending_writes + pending_captures + pending_refines + async_refines
    payload = {
        "request_id": claimed.request.request_id,
        "request_type": claimed.request.request_type,
        "status": "answered",
        "scope": scope,
        "pending_writes": pending_writes,
        "pending_captures": pending_captures,
        "pending_refines": pending_refines,
        "async_refines": async_refines,
        "pending_total": pending_total,
        "worker_ready": worker_ready,
        "worker_generation": worker_generation,
    }
    atomic_write_json(result_path(store.paths, claimed.request.request_id), payload)
    store.complete_request(claimed, state="answered")
