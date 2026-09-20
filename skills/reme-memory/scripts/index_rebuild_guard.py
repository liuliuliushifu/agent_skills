#!/usr/bin/env python3
import fcntl
import json
import os
import shutil
import time
from pathlib import Path
from typing import Dict, Tuple

from memory_bus_io import atomic_write_json


ACTIVE_INDEX_DIR_NAME = "file_store"
BACKUP_INDEX_DIR_NAME = "file_store.rebuild-backup"
REBUILD_MARKER_NAME = "rebuild_index.inprogress.json"
REBUILD_LOCK_NAME = "rebuild_index.lock"


class RebuildProtectionError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def rebuild_paths(workdir: str) -> Tuple[Path, Path, Path]:
    root = Path(workdir).expanduser().resolve()
    return (
        root / ACTIVE_INDEX_DIR_NAME,
        root / BACKUP_INDEX_DIR_NAME,
        root / REBUILD_MARKER_NAME,
    )


def is_rebuild_in_progress(workdir: str) -> bool:
    lock_path = Path(workdir).expanduser().resolve() / REBUILD_LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        lock_handle.close()


def recover_interrupted_rebuild(workdir: str) -> Dict[str, object]:
    active_dir, backup_dir, marker_path = rebuild_paths(workdir)
    if not marker_path.exists():
        if backup_dir.exists():
            raise RebuildProtectionError(
                "stale_backup_without_marker",
                f"refusing to guess between active index and untracked backup: {backup_dir}",
            )
        return {"recovered": False, "action": "none"}

    marker = _read_marker(marker_path)
    state = str(marker.get("state", ""))
    had_previous_index = bool(marker.get("had_previous_index", False))

    if state == "committed":
        if active_dir.exists():
            _remove_path(backup_dir)
            marker_path.unlink(missing_ok=True)
            _fsync_directory(active_dir.parent)
            return {"recovered": True, "action": "kept_committed_index"}
        if backup_dir.exists():
            os.replace(backup_dir, active_dir)
            marker_path.unlink(missing_ok=True)
            _fsync_directory(active_dir.parent)
            return {"recovered": True, "action": "restored_backup_after_missing_commit"}
        raise RebuildProtectionError(
            "committed_index_missing",
            "rebuild marker says committed but neither active index nor backup exists",
        )

    if backup_dir.exists():
        _remove_path(active_dir)
        os.replace(backup_dir, active_dir)
        marker_path.unlink(missing_ok=True)
        _fsync_directory(active_dir.parent)
        return {"recovered": True, "action": "restored_backup"}

    if state == "prepared" and active_dir.exists():
        marker_path.unlink(missing_ok=True)
        _fsync_directory(active_dir.parent)
        return {"recovered": True, "action": "kept_untouched_active_index"}

    if not had_previous_index:
        _remove_path(active_dir)
        marker_path.unlink(missing_ok=True)
        _fsync_directory(marker_path.parent)
        return {"recovered": True, "action": "removed_partial_index_without_backup"}

    raise RebuildProtectionError(
        "backup_missing_during_recovery",
        "interrupted rebuild expected a backup, but the backup directory is missing",
    )


def begin_protected_rebuild(workdir: str) -> Dict[str, object]:
    recovery = recover_interrupted_rebuild(workdir)
    active_dir, backup_dir, marker_path = rebuild_paths(workdir)
    if backup_dir.exists():
        raise RebuildProtectionError(
            "backup_path_busy",
            f"rebuild backup path already exists: {backup_dir}",
        )

    had_previous_index = active_dir.exists()
    marker = {
        "schema_version": 1,
        "state": "prepared",
        "pid": os.getpid(),
        "started_at_epoch": time.time(),
        "workdir": str(Path(workdir).expanduser().resolve()),
        "active_index_dir": str(active_dir),
        "backup_index_dir": str(backup_dir),
        "had_previous_index": had_previous_index,
    }
    atomic_write_json(marker_path, marker)
    _fsync_directory(marker_path.parent)

    try:
        if had_previous_index:
            if not active_dir.is_dir() or active_dir.is_symlink():
                raise RebuildProtectionError(
                    "unsupported_active_index_path",
                    f"active index must be a real directory: {active_dir}",
                )
            os.replace(active_dir, backup_dir)
            _fsync_directory(active_dir.parent)

        marker["state"] = "building"
        atomic_write_json(marker_path, marker)
        _fsync_directory(marker_path.parent)
        return {
            "protected": True,
            "had_previous_index": had_previous_index,
            "active_index_dir": str(active_dir),
            "backup_index_dir": str(backup_dir),
            "marker_path": str(marker_path),
            "recovery": recovery,
        }
    except BaseException:
        rollback_protected_rebuild(workdir)
        raise


def commit_protected_rebuild(workdir: str) -> Dict[str, object]:
    active_dir, backup_dir, marker_path = rebuild_paths(workdir)
    marker = _read_marker(marker_path)
    if str(marker.get("state", "")) != "building":
        raise RebuildProtectionError(
            "invalid_commit_state",
            f"cannot commit rebuild from state={marker.get('state')!r}",
        )
    if not active_dir.is_dir():
        raise RebuildProtectionError(
            "rebuilt_index_missing",
            f"rebuilt index directory does not exist: {active_dir}",
        )

    marker["state"] = "committed"
    marker["committed_at_epoch"] = time.time()
    atomic_write_json(marker_path, marker)
    _fsync_directory(marker_path.parent)

    _remove_path(backup_dir)
    marker_path.unlink(missing_ok=True)
    _fsync_directory(active_dir.parent)
    return {
        "committed": True,
        "active_index_dir": str(active_dir),
        "backup_removed": not backup_dir.exists(),
    }


def rollback_protected_rebuild(workdir: str) -> Dict[str, object]:
    active_dir, backup_dir, marker_path = rebuild_paths(workdir)
    marker = _read_marker(marker_path) if marker_path.exists() else {}
    if str(marker.get("state", "")) == "committed":
        recovered = recover_interrupted_rebuild(workdir)
        return {
            "rolled_back": False,
            "action": str(recovered.get("action", "kept_committed_index")),
            "active_index_dir": str(active_dir),
        }
    had_previous_index = bool(marker.get("had_previous_index", backup_dir.exists()))

    _remove_path(active_dir)
    if backup_dir.exists():
        os.replace(backup_dir, active_dir)
        action = "restored_backup"
    elif had_previous_index:
        raise RebuildProtectionError(
            "rollback_backup_missing",
            "cannot roll back rebuild because the previous index backup is missing",
        )
    else:
        action = "removed_partial_index_without_backup"

    marker_path.unlink(missing_ok=True)
    _fsync_directory(marker_path.parent)
    return {
        "rolled_back": True,
        "action": action,
        "active_index_dir": str(active_dir),
    }


def _read_marker(marker_path: Path) -> Dict[str, object]:
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RebuildProtectionError(
            "invalid_rebuild_marker",
            f"cannot read rebuild marker {marker_path}: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise RebuildProtectionError(
            "invalid_rebuild_marker",
            f"rebuild marker must contain a JSON object: {marker_path}",
        )
    return payload


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
