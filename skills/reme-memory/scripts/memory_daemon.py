#!/usr/bin/env python3
import argparse
import datetime as dt
import os
import threading
import time
from typing import Callable, Dict, Optional

from capture_adapter import handle_capture_request
from daemon_trace import (
    TERMINAL_STATES,
    activate_trace,
    build_runtime_identity,
    finish_attempt,
    finish_trace,
    prune_logs,
    record_exception,
    start_attempt,
    start_trace,
    update_latest_status,
)
from diagnostics_utils import ensure_safe_error_record
from memory_bus_client import enqueue_maintenance, result_path
from memory_bus_io import atomic_write_json
from memory_request_store import ClaimedRequest, MemoryRequestStore
from flush_adapter import handle_flush_request
from index_rebuild_guard import is_rebuild_in_progress, recover_interrupted_rebuild
from index_adapter import handle_index_request
from maintenance_adapter import handle_maintenance_request
from query_adapter import handle_query_request
from refine_adapter import handle_refine_request
from refine_async import AsyncRefineSupervisor
from reme_runtime import REME_WORKDIR
from runtime_worker import RuntimeWorkerClient
from status_adapter import handle_status_request
from write_adapter import handle_write_request


class MemoryDaemon:
    def __init__(
        self,
        store: Optional[MemoryRequestStore] = None,
        poll_interval_seconds: float = 0.2,
        heartbeat_interval_seconds: float = 10.0,
        request_lease_interval_seconds: float = 10.0,
    ) -> None:
        self.store = store or MemoryRequestStore()
        self.poll_interval_seconds = poll_interval_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.request_lease_interval_seconds = request_lease_interval_seconds
        self.handlers: Dict[str, Callable[[ClaimedRequest, MemoryRequestStore], None]] = {}
        self._running = False
        self.runtime_worker = RuntimeWorkerClient()
        setattr(self.store, "runtime_worker", self.runtime_worker)
        self.refine_supervisor = AsyncRefineSupervisor(store=self.store)
        setattr(self.store, "refine_supervisor", self.refine_supervisor)
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None
        self._request_lease_stop = threading.Event()
        self._request_lease_thread = None
        self.rebuild_recovery = {"recovered": False, "action": "none"}
        self.maintenance_interval_seconds = float(
            os.environ.get("REME_MAINTENANCE_INTERVAL_SECONDS", "86400")
        )
        self._next_maintenance_at = time.monotonic() + max(0.0, self.maintenance_interval_seconds)

    def register_handler(self, request_type: str, handler: Callable[[ClaimedRequest, MemoryRequestStore], None]) -> None:
        self.handlers[request_type] = handler

    def start(self) -> None:
        self.store.acquire_daemon_lock()
        try:
            if is_rebuild_in_progress(REME_WORKDIR):
                raise RuntimeError("cannot start ReMe daemon while a full index rebuild is running")
            self.rebuild_recovery = recover_interrupted_rebuild(REME_WORKDIR)
            self.runtime_worker.start()
            self.refine_supervisor.start()
            prune_logs()
            self._running = True
            self._heartbeat_stop.clear()
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()
        except BaseException:
            try:
                self.refine_supervisor.stop()
            except Exception:
                pass
            try:
                self.runtime_worker.stop()
            except Exception:
                pass
            self.store.release_daemon_lock()
            raise

    def stop(self) -> None:
        self._running = False
        self._heartbeat_stop.set()
        self._stop_request_lease_refresh()
        try:
            self.refine_supervisor.stop()
        except Exception:
            pass
        try:
            self.runtime_worker.stop()
        except Exception:
            pass
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=max(1.0, self.heartbeat_interval_seconds * 2.0))
            self._heartbeat_thread = None
        self.store.release_daemon_lock()

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self.heartbeat_interval_seconds):
            try:
                self.store.refresh_daemon_lock()
            except Exception:
                if self._running:
                    raise
                break

    def _start_request_lease_refresh(self, request_id: str) -> None:
        self._request_lease_stop.clear()

        def _loop() -> None:
            while not self._request_lease_stop.wait(self.request_lease_interval_seconds):
                try:
                    self.store.refresh_request_lease(request_id)
                except Exception:
                    break

        self._request_lease_thread = threading.Thread(target=_loop, daemon=True)
        self._request_lease_thread.start()

    def _stop_request_lease_refresh(self) -> None:
        self._request_lease_stop.set()
        if self._request_lease_thread is not None:
            self._request_lease_thread.join(timeout=max(1.0, self.request_lease_interval_seconds * 2.0))
            self._request_lease_thread = None

    def _write_error_result(self, claimed: ClaimedRequest, error_value) -> None:
        if claimed.request.request_type in {"memory_write", "memory_capture", "memory_index"}:
            return
        safe_error = ensure_safe_error_record(error_value)
        payload = {
            "request_id": claimed.request.request_id,
            "request_type": claimed.request.request_type,
            "status": "failed",
            "error": safe_error.summary(),
            "error_meta": safe_error.to_dict(),
        }
        atomic_write_json(result_path(self.store.paths, claimed.request.request_id), payload)

    def process_once(self) -> bool:
        self.store.recover_processing()
        claimed = self.store.claim_next("memory_write")
        if claimed is None:
            claimed = self.store.claim_next("memory_capture")
        if claimed is None:
            claimed = self.store.claim_next("memory_refine")
        if claimed is None:
            claimed = self.store.claim_next("memory_status")
        if claimed is None:
            claimed = self.store.claim_next("memory_flush")
        if claimed is None:
            claimed = self.store.claim_next("memory_query")
        if claimed is None:
            return False

        runtime_identity = build_runtime_identity()
        trace = start_trace(claimed.request, claimed.previous_status, runtime_identity=runtime_identity)
        start_attempt(trace, daemon_id=self.store.daemon_id, status_snapshot=claimed.previous_status)
        handler = self.handlers.get(claimed.request.request_type)
        if handler is None:
            error = RuntimeError(f"no handler for {claimed.request.request_type}")
            with activate_trace(trace):
                self._write_error_result(claimed, error)
                self.store.fail_request(claimed, error)
                current_status = self.store.read_status(claimed.request.request_id) or {}
                update_latest_status(trace, current_status)
                finish_attempt(
                    trace,
                    final_state=current_status.get("state", ""),
                    final_phase=current_status.get("phase", ""),
                    error=error,
                )
                finish_trace(
                    trace,
                    final_state=current_status.get("state", ""),
                    final_phase=current_status.get("phase", ""),
                    error=error,
                )
            return True

        self._start_request_lease_refresh(claimed.request.request_id)
        current_status = {}
        error = None
        try:
            with activate_trace(trace):
                handler(claimed, self.store)
                current_status = self.store.read_status(claimed.request.request_id) or {}
                update_latest_status(trace, current_status)
        except Exception as exc:
            error = exc
            self._write_error_result(claimed, exc)
            self.store.fail_request(claimed, exc)
            current_status = self.store.read_status(claimed.request.request_id) or {}
            with activate_trace(trace):
                update_latest_status(trace, current_status)
                record_exception(trace, exc, context="handler")
        finally:
            self._stop_request_lease_refresh()
        with activate_trace(trace):
            finish_attempt(
                trace,
                final_state=current_status.get("state", ""),
                final_phase=current_status.get("phase", ""),
                error=error,
            )
            if current_status.get("state", "") in TERMINAL_STATES:
                finish_trace(
                    trace,
                    final_state=current_status.get("state", ""),
                    final_phase=current_status.get("phase", ""),
                    error=error,
                )
        return True

    def _maybe_enqueue_maintenance(self) -> None:
        if self.maintenance_interval_seconds <= 0:
            return
        if time.monotonic() < self._next_maintenance_at:
            return
        maintenance_date = dt.date.today().isoformat()
        enqueue_maintenance(
            maintenance_date=maintenance_date,
            cleanup_fallback=True,
            compact_retention=True,
            compact_active_days=30,
            compact_delete_days=90,
            paths=self.store.paths,
            client_id="reme-daemon-maintenance",
        )
        self._next_maintenance_at = time.monotonic() + self.maintenance_interval_seconds

    def serve_forever(self, max_loops: Optional[int] = None) -> int:
        self.start()
        loop_count = 0
        try:
            while self._running:
                self._maybe_enqueue_maintenance()
                handled = self.process_once()
                loop_count += 1
                if max_loops is not None and max_loops <= loop_count:
                    break
                if not handled:
                    time.sleep(self.poll_interval_seconds)
        finally:
            self.stop()
        return loop_count


def main() -> None:
    parser = argparse.ArgumentParser(description="ReMe memory bus daemon skeleton.")
    parser.add_argument("--max-loops", type=int, default=None)
    parser.add_argument("--heartbeat-interval", type=float, default=10.0)
    parser.add_argument("--request-lease-interval", type=float, default=10.0)
    args = parser.parse_args()

    daemon = MemoryDaemon(
        heartbeat_interval_seconds=args.heartbeat_interval,
        request_lease_interval_seconds=args.request_lease_interval,
    )
    daemon.register_handler("memory_write", handle_write_request)
    daemon.register_handler("memory_index", handle_index_request)
    daemon.register_handler("memory_capture", handle_capture_request)
    daemon.register_handler("memory_refine", handle_refine_request)
    daemon.register_handler("memory_maintenance", handle_maintenance_request)
    daemon.register_handler("memory_status", handle_status_request)
    daemon.register_handler("memory_flush", handle_flush_request)
    daemon.register_handler("memory_query", handle_query_request)
    daemon.serve_forever(max_loops=args.max_loops)


if __name__ == "__main__":
    main()
