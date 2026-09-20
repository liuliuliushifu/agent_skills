#!/usr/bin/env python3
from memory_bus_client import result_path, status_path
from memory_bus_io import atomic_write_json, read_json


def handle_status_request(claimed, store) -> None:
    target_request_id = claimed.request.payload["target_request_id"]
    target_status_path = status_path(store.paths, target_request_id)
    async_result_path = store.paths.result / f"{target_request_id}.async.json"
    payload = {
        "request_id": claimed.request.request_id,
        "request_type": claimed.request.request_type,
        "status": "answered",
        "target_request_id": target_request_id,
        "target_status": read_json(target_status_path) if target_status_path.exists() else {},
        "async_result": read_json(async_result_path) if async_result_path.exists() else {},
    }
    atomic_write_json(result_path(store.paths, claimed.request.request_id), payload)
    store.complete_request(claimed, state="answered")
