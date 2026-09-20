#!/usr/bin/env python3
from pathlib import Path

from daemon_trace import current_trace, record_event, update_latest_status
from reme_runtime import get_indexable_memory_dirs


def _validated_index_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser().resolve()
    allowed_dirs = {directory.expanduser().resolve() for directory in get_indexable_memory_dirs()}
    if path.suffix.lower() != ".md" or path.parent not in allowed_dirs:
        raise ValueError(f"incremental index path is outside an indexable memory directory: {path}")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def handle_index_request(claimed, store, index_backend=None) -> None:
    request = claimed.request
    trace = current_trace()
    current_status = store.read_status(request.request_id) or {}
    current_attempt = int(current_status.get("attempt", 0))
    store.archive_raw_request(request)
    store.update_phase(
        request.request_id,
        state="processing",
        phase="raw_archived",
        attempt=current_attempt,
    )
    update_latest_status(trace, store.read_status(request.request_id) or {})

    if store.is_write_committed(request.idempotency_key):
        store.complete_request(claimed, state="stored", phase="indexed")
        update_latest_status(trace, store.read_status(request.request_id) or {})
        return

    path = _validated_index_path(str(request.payload.get("path") or ""))
    store.update_phase(
        request.request_id,
        state="processing",
        phase="memory_index_started",
        attempt=current_attempt,
    )
    update_latest_status(trace, store.read_status(request.request_id) or {})

    if index_backend is not None:
        index_result = index_backend(str(path))
    else:
        runtime_worker = getattr(store, "runtime_worker", None)
        if runtime_worker is None:
            raise RuntimeError("runtime worker unavailable for incremental memory indexing")
        index_result = runtime_worker.upsert_memory_file(str(path))

    store.mark_write_committed(
        request,
        {
            **dict(index_result or {}),
            "indexed": True,
            "path": str(path),
        },
    )
    record_event(
        trace,
        "incremental_index_completed",
        path_name=path.name,
        indexed_files=int((index_result or {}).get("indexed_files", 0)),
        indexed_chunks=int((index_result or {}).get("indexed_chunks", 0)),
    )
    store.complete_request(claimed, state="stored", phase="indexed")
    update_latest_status(trace, store.read_status(request.request_id) or {})


def main() -> None:
    raise SystemExit("index_adapter.py is a library module; use memory_daemon.py to drive it.")


if __name__ == "__main__":
    main()
