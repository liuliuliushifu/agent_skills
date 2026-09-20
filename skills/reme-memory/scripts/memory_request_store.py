#!/usr/bin/env python3
import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from diagnostics_utils import persisted_error_fields
from memory_bus_io import atomic_write_json, claim_file, read_json
from memory_bus_paths import MemoryBusPaths, build_bus_paths, ensure_bus_dirs
from memory_request_schema import MemoryRequest, MemoryRequestStatus
from reme_runtime import REME_WORKDIR


PHASE_TO_INBOX_DIR = {
    "memory_write": "write",
    "memory_index": "write",
    "memory_capture": "capture",
    "memory_refine": "refine",
    "memory_maintenance": "write",
    "memory_query": "query",
    "memory_status": "status",
    "memory_flush": "flush",
}


@dataclass(frozen=True)
class ClaimedRequest:
    request: MemoryRequest
    processing_path: Path
    status_path: Path
    previous_status: Dict[str, Any]


class MemoryRequestStore:
    def __init__(
        self,
        paths: Optional[MemoryBusPaths] = None,
        daemon_id: Optional[str] = None,
        lease_seconds: int = 30,
        lock_stale_seconds: int = 120,
    ) -> None:
        self.paths = paths or build_bus_paths()
        ensure_bus_dirs(self.paths)
        self.daemon_id = daemon_id or "daemon-" + socket.gethostname()
        self.lease_seconds = lease_seconds
        self.lock_stale_seconds = lock_stale_seconds
        self._lock_fd = None
        self._lock_path = self.paths.lock / "daemon.lock"
        self._status_lock = threading.RLock()

    def _request_status_path(self, request_id: str) -> Path:
        return self.paths.result / f"{request_id}.status.json"

    def _commit_marker_path(self, idempotency_key: str) -> Path:
        safe_name = idempotency_key.replace(":", "_")
        return self.paths.archive_completed / f"{safe_name}.commit.json"

    def get_commit_metadata(self, idempotency_key: str) -> Optional[dict]:
        commit_path = self._commit_marker_path(idempotency_key)
        if not commit_path.exists():
            return None
        return read_json(commit_path)

    def _lease_expiry(self) -> str:
        return time.strftime(
            "%Y-%m-%dT%H:%M:%S%z",
            time.localtime(time.time() + self.lease_seconds),
        )

    def acquire_daemon_lock(self) -> None:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            self._lock_fd = os.open(str(self._lock_path), flags, 0o600)
        except FileExistsError:
            if not self._recover_stale_daemon_lock():
                raise
            self._lock_fd = os.open(str(self._lock_path), flags, 0o600)
        payload = {
            "daemon_id": self.daemon_id,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "reme_workdir": os.environ.get("REME_WORKDIR", REME_WORKDIR),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            "heartbeat_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        }
        with os.fdopen(self._lock_fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        self._lock_fd = None

    def refresh_daemon_lock(self) -> None:
        if not self._lock_path.exists():
            raise RuntimeError("daemon lock missing")
        payload = read_json(self._lock_path)
        if self.daemon_id != payload.get("daemon_id", ""):
            raise RuntimeError("daemon lock owner mismatch")
        payload["heartbeat_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
        atomic_write_json(self._lock_path, payload)

    def release_daemon_lock(self) -> None:
        if not self._lock_path.exists():
            return
        try:
            payload = read_json(self._lock_path)
        except Exception:
            return
        if self.daemon_id == payload.get("daemon_id", ""):
            self._lock_path.unlink(missing_ok=True)

    def read_status(self, request_id: str) -> Optional[dict]:
        status_path = self._request_status_path(request_id)
        if not status_path.exists():
            return None
        return read_json(status_path)

    def refresh_request_lease(self, request_id: str) -> None:
        with self._status_lock:
            status_path = self._request_status_path(request_id)
            if not status_path.exists():
                raise RuntimeError(f"status missing for request_id={request_id}")
            status = read_json(status_path)
            if self.daemon_id != status.get("lease_owner", ""):
                raise RuntimeError("request lease owner mismatch")
            status["lease_owner"] = self.daemon_id
            status["lease_expires_at"] = self._lease_expiry()
            status["heartbeat_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
            atomic_write_json(status_path, status)

    def _load_request_from_path(self, path: Path) -> MemoryRequest:
        return MemoryRequest.from_dict(read_json(path))

    def _write_status(
        self,
        request_id: str,
        state: str,
        phase: str,
        attempt: int,
        next_retry_at: str = "",
        last_error: Any = "",
        last_error_meta: Optional[Dict[str, Any]] = None,
    ) -> Path:
        with self._status_lock:
            normalized_last_error, normalized_last_error_meta = persisted_error_fields(
                last_error,
                last_error_meta=last_error_meta,
            )
            status = MemoryRequestStatus.new(
                request_id=request_id,
                state=state,
                phase=phase,
                attempt=attempt,
                lease_owner=self.daemon_id,
                lease_expires_at=self._lease_expiry(),
                heartbeat_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
                next_retry_at=next_retry_at,
                last_error=normalized_last_error,
                last_error_meta=normalized_last_error_meta,
            )
            status_path = self._request_status_path(request_id)
            atomic_write_json(status_path, status.to_dict())
            return status_path

    def archive_raw_request(self, request: MemoryRequest) -> Path:
        archive_path = self.paths.archive_raw / f"{request.request_id}.json"
        atomic_write_json(archive_path, request.to_dict())
        return archive_path

    def is_write_committed(self, idempotency_key: str) -> bool:
        return self._commit_marker_path(idempotency_key).exists()

    def mark_write_committed(self, request: MemoryRequest, metadata: Optional[dict] = None) -> Path:
        commit_path = self._commit_marker_path(request.idempotency_key)
        payload = {
            "request_id": request.request_id,
            "idempotency_key": request.idempotency_key,
            "request_type": request.request_type,
            "committed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            "metadata": metadata or {},
        }
        atomic_write_json(commit_path, payload)
        return commit_path

    def claim_next(self, request_type: str) -> Optional[ClaimedRequest]:
        inbox_dir = getattr(self.paths, f"inbox_{PHASE_TO_INBOX_DIR[request_type]}")
        processing_dir = getattr(self.paths, f"processing_{PHASE_TO_INBOX_DIR.get(request_type, 'query')}", None)
        if processing_dir is None:
            processing_dir = self.paths.processing_query

        for path in sorted(inbox_dir.glob("*.json")):
            dst = processing_dir / path.name
            try:
                claim_file(path, dst)
            except FileNotFoundError:
                continue
            request = self._load_request_from_path(dst)
            previous_status = self.read_status(request.request_id) or {}
            status_path = self._write_status(
                request_id=request.request_id,
                state="processing",
                phase=previous_status.get("phase", "queued"),
                attempt=int(previous_status.get("attempt", 0)),
            )
            return ClaimedRequest(
                request=request,
                processing_path=dst,
                status_path=status_path,
                previous_status=previous_status,
            )
        return None

    def update_phase(
        self,
        request_id: str,
        state: str,
        phase: str,
        attempt: int,
        next_retry_at: str = "",
        last_error: Any = "",
        last_error_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._write_status(
            request_id=request_id,
            state=state,
            phase=phase,
            attempt=attempt,
            next_retry_at=next_retry_at,
            last_error=last_error,
            last_error_meta=last_error_meta,
        )

    def complete_request(self, claimed: ClaimedRequest, state: str, phase: str = "completed") -> Path:
        self.update_phase(
            request_id=claimed.request.request_id,
            state=state,
            phase=phase,
            attempt=0,
        )
        archive_path = self.paths.archive_completed / claimed.processing_path.name
        claim_file(claimed.processing_path, archive_path)
        return archive_path

    def fail_request(
        self,
        claimed: ClaimedRequest,
        last_error: Any,
        last_error_meta: Optional[Dict[str, Any]] = None,
    ) -> Path:
        self.update_phase(
            request_id=claimed.request.request_id,
            state="deadletter",
            phase="completed",
            attempt=0,
            last_error=last_error,
            last_error_meta=last_error_meta,
        )
        deadletter_path = self.paths.deadletter / claimed.processing_path.name
        claim_file(claimed.processing_path, deadletter_path)
        return deadletter_path

    def schedule_retry(
        self,
        claimed: ClaimedRequest,
        attempt: int,
        delay_seconds: int,
        last_error: Any,
        phase: str = "apply_started",
        last_error_meta: Optional[Dict[str, Any]] = None,
    ) -> Path:
        retry_at = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() + delay_seconds))
        self.update_phase(
            request_id=claimed.request.request_id,
            state="retrying",
            phase=phase,
            attempt=attempt,
            next_retry_at=retry_at,
            last_error=last_error,
            last_error_meta=last_error_meta,
        )
        if 0 >= delay_seconds:
            target_dir = getattr(self.paths, f"inbox_{PHASE_TO_INBOX_DIR[claimed.request.request_type]}")
            target_path = target_dir / claimed.processing_path.name
            claim_file(claimed.processing_path, target_path)
            return target_path
        return claimed.processing_path

    def recover_processing(self, now_epoch: Optional[float] = None) -> int:
        current = now_epoch if now_epoch is not None else time.time()
        recovered = 0
        for processing_dir in (
            self.paths.processing_write,
            self.paths.processing_capture,
            self.paths.processing_refine,
            self.paths.processing_query,
        ):
            for path in sorted(processing_dir.glob("*.json")):
                request = self._load_request_from_path(path)
                status_path = self._request_status_path(request.request_id)
                if not status_path.exists():
                    target_dir = getattr(self.paths, f"inbox_{PHASE_TO_INBOX_DIR[request.request_type]}")
                    claim_file(path, target_dir / path.name)
                    recovered += 1
                    continue

                status = read_json(status_path)
                lease_expires_at = status.get("lease_expires_at", "")
                next_retry_at = status.get("next_retry_at", "")

                lease_expired = bool(lease_expires_at) and _iso_to_epoch(lease_expires_at) <= current
                retry_due = bool(next_retry_at) and _iso_to_epoch(next_retry_at) <= current
                if lease_expired or retry_due:
                    target_dir = getattr(self.paths, f"inbox_{PHASE_TO_INBOX_DIR[request.request_type]}")
                    claim_file(path, target_dir / path.name)
                    recovered += 1
        return recovered

    def _recover_stale_daemon_lock(self) -> bool:
        if not self._lock_path.exists():
            return False
        try:
            payload = read_json(self._lock_path)
        except Exception:
            self._lock_path.unlink()
            return True

        heartbeat_at = payload.get("heartbeat_at", "")
        if not heartbeat_at:
            self._lock_path.unlink()
            return True

        pid = payload.get("pid")
        if _pid_missing(pid):
            self._lock_path.unlink()
            return True

        age_seconds = time.time() - _iso_to_epoch(heartbeat_at)
        if self.lock_stale_seconds < age_seconds:
            self._lock_path.unlink()
            return True
        return False


def _iso_to_epoch(text: str) -> float:
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+0000"
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%S%z"))
    except ValueError:
        return 0.0


def _pid_missing(pid_value) -> bool:
    try:
        pid = int(pid_value)
    except (TypeError, ValueError):
        return False
    return not Path(f"/proc/{pid}").exists()


def is_daemon_active_for_workdir(
    target_workdir: str,
    *,
    paths: Optional[MemoryBusPaths] = None,
    stale_seconds: int = 120,
) -> bool:
    resolved_target = Path(target_workdir).expanduser().resolve()
    lock_path = (paths or build_bus_paths()).lock / "daemon.lock"
    if not lock_path.exists():
        return False
    try:
        payload = read_json(lock_path)
    except Exception:
        return False
    heartbeat_at = payload.get("heartbeat_at", "")
    if not heartbeat_at:
        return False
    if _pid_missing(payload.get("pid")):
        return False
    age_seconds = time.time() - _iso_to_epoch(heartbeat_at)
    if stale_seconds < age_seconds:
        return False
    lock_workdir = payload.get("reme_workdir") or REME_WORKDIR
    return Path(lock_workdir).expanduser().resolve() == resolved_target
