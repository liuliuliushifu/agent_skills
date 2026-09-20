#!/usr/bin/env python3

from daemon_trace import current_trace, record_event, update_latest_status


def handle_refine_request(claimed, store) -> None:
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

    supervisor = getattr(store, "refine_supervisor", None)
    if supervisor is None:
        raise RuntimeError("refine supervisor is not attached to MemoryRequestStore")
    job = supervisor.enqueue_request(request)
    record_event(
        trace,
        "refine_async_enqueued",
        refine_id=job.get("refine_id", ""),
        evidence_count=len(request.payload.get("evidence", [])),
    )
    store.complete_request(claimed, state="accepted_async", phase="async_enqueued")
    update_latest_status(trace, store.read_status(request.request_id) or {})


def main() -> None:
    raise SystemExit("refine_adapter.py is a library module; use memory_daemon.py to drive it.")


if __name__ == "__main__":
    main()
