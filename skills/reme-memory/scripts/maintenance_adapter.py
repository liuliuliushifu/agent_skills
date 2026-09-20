#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from memory_block_renderer import parse_memory_blocks
from memory_bus_client import result_path
from memory_bus_io import atomic_write_json, atomic_write_text
from memory_retrieval import parse_compact_created_at
from reme_runtime import get_compact_archive_dir, get_compact_memory_dir, get_memory_dir


SCRIPT_DIR = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
DEFAULT_BACKUP_ROOT = CODEX_HOME / "memories/reme-memory/maintenance_backups"
FALLBACK_ALIAS = "reme-refine-fallback"
FALLBACK_MARKER = "[ReMe async refine fallback]"


def handle_maintenance_request(claimed, store) -> None:
    request = claimed.request
    current_status = store.read_status(request.request_id) or {}
    current_attempt = int(current_status.get("attempt", 0))
    store.archive_raw_request(request)
    store.update_phase(
        request.request_id,
        state="processing",
        phase="maintenance_started",
        attempt=current_attempt,
    )

    commit_metadata = store.get_commit_metadata(request.idempotency_key)
    if commit_metadata is not None:
        result = dict(commit_metadata.get("metadata") or {})
    else:
        result = apply_memory_maintenance(
            request.payload,
            runtime_worker=getattr(store, "runtime_worker", None),
            request_id=request.request_id,
        )
        store.mark_write_committed(request, result)

    atomic_write_json(
        result_path(store.paths, request.request_id),
        {
            "request_id": request.request_id,
            "request_type": request.request_type,
            "status": "stored",
            **result,
        },
    )
    store.complete_request(claimed, state="stored", phase="maintenance_completed")


def apply_memory_maintenance(
    payload: Dict[str, Any],
    *,
    runtime_worker,
    request_id: str,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    dry_run = bool(payload.get("dry_run", False))
    current_time = now or dt.datetime.now(dt.timezone.utc).astimezone()
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=dt.timezone.utc)
    backup_root = Path(
        os.environ.get("REME_MAINTENANCE_BACKUP_ROOT", str(DEFAULT_BACKUP_ROOT))
    ).expanduser().resolve() / request_id
    result: Dict[str, Any] = {
        "dry_run": dry_run,
        "maintenance_date": payload["maintenance_date"],
        "fallback_blocks_removed": 0,
        "durable_files_updated": 0,
        "compact_files_archived": 0,
        "compact_files_deleted": 0,
        "index_files_upserted": 0,
        "index_files_deleted": 0,
        "backup_root": str(backup_root),
        "affected_paths": [],
    }
    upsert_paths = []
    delete_paths = []
    durable_backups = []
    compact_moves = []
    compact_delete_after_sync = []
    if payload.get("cleanup_fallback", True):
        _cleanup_fallback_blocks(
            result,
            backup_root=backup_root,
            dry_run=dry_run,
            upsert_paths=upsert_paths,
            durable_backups=durable_backups,
        )
    if payload.get("compact_retention", True):
        _apply_compact_retention(
            result,
            current_time=current_time,
            active_days=int(payload.get("compact_active_days", 30)),
            delete_days=int(payload.get("compact_delete_days", 90)),
            dry_run=dry_run,
            delete_paths=delete_paths,
            compact_moves=compact_moves,
            compact_delete_after_sync=compact_delete_after_sync,
        )
    if not dry_run:
        if (upsert_paths or delete_paths) and runtime_worker is None:
            raise RuntimeError("runtime worker unavailable for memory maintenance")
        if upsert_paths or delete_paths:
            try:
                runtime_worker.sync_memory_files(
                    upsert_paths=upsert_paths,
                    delete_paths=delete_paths,
                )
            except Exception:
                _rollback_filesystem_changes(
                    durable_backups=durable_backups,
                    compact_moves=compact_moves,
                    runtime_worker=runtime_worker,
                )
                raise
            result["index_files_upserted"] = len(upsert_paths)
            result["index_files_deleted"] = len(delete_paths)
        for path in compact_delete_after_sync:
            path.unlink(missing_ok=True)
        backup_root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(backup_root / "manifest.json", result)
    return result


def _cleanup_fallback_blocks(
    result: Dict[str, Any],
    *,
    backup_root: Path,
    dry_run: bool,
    upsert_paths: list,
    durable_backups: list,
) -> None:
    for path in sorted(get_memory_dir().glob("*.md")):
        text = path.read_text(encoding="utf-8")
        blocks = parse_memory_blocks(text)
        doomed = [block for block in blocks if _is_fallback_block(block, text)]
        if not doomed:
            continue
        result["fallback_blocks_removed"] += len(doomed)
        result["durable_files_updated"] += 1
        result["affected_paths"].append(str(path))
        if dry_run:
            continue
        backup_root.mkdir(parents=True, exist_ok=True)
        backup_path = backup_root / f"durable-{path.name}"
        shutil.copy2(path, backup_path)
        new_text = _remove_blocks(text, doomed)
        atomic_write_text(path, new_text)
        upsert_paths.append(str(path))
        durable_backups.append((path, backup_path))


def _apply_compact_retention(
    result: Dict[str, Any],
    *,
    current_time: dt.datetime,
    active_days: int,
    delete_days: int,
    dry_run: bool,
    delete_paths: list,
    compact_moves: list,
    compact_delete_after_sync: list,
) -> None:
    compact_root = get_compact_memory_dir().expanduser().resolve()
    archive_root = get_compact_archive_dir().expanduser().resolve()
    for path in sorted(compact_root.glob("*.md")):
        age_days = _age_days(path, current_time)
        if age_days < active_days:
            continue
        delete_now = age_days >= delete_days
        result["affected_paths"].append(str(path))
        if delete_now:
            result["compact_files_deleted"] += 1
        else:
            result["compact_files_archived"] += 1
        if dry_run:
            continue
        archive_root.mkdir(parents=True, exist_ok=True)
        archived_path = archive_root / path.name
        if archived_path.exists():
            if archived_path.read_bytes() != path.read_bytes():
                raise FileExistsError(f"compact archive collision: {archived_path}")
            path.unlink()
        else:
            os.replace(path, archived_path)
        delete_paths.append(str(path))
        compact_moves.append((path, archived_path))
        if delete_now:
            compact_delete_after_sync.append(archived_path)

    if not archive_root.exists():
        return
    for path in sorted(archive_root.glob("*.md")):
        if _age_days(path, current_time) < delete_days:
            continue
        result["affected_paths"].append(str(path))
        result["compact_files_deleted"] += 1
        if not dry_run:
            path.unlink(missing_ok=True)


def _rollback_filesystem_changes(
    *,
    durable_backups: list,
    compact_moves: list,
    runtime_worker,
) -> None:
    restored_paths = []
    for path, backup_path in durable_backups:
        shutil.copy2(backup_path, path)
        restored_paths.append(str(path))
    for original_path, archived_path in reversed(compact_moves):
        if archived_path.exists() and not original_path.exists():
            os.replace(archived_path, original_path)
        if original_path.exists():
            restored_paths.append(str(original_path))
    if restored_paths:
        try:
            runtime_worker.sync_memory_files(
                upsert_paths=restored_paths,
                delete_paths=[],
            )
        except Exception:
            pass


def _is_fallback_block(block: Dict[str, Any], text: str) -> bool:
    memory_json = dict(block.get("meta", {}).get("memory_json") or {})
    aliases = {str(item) for item in memory_json.get("aliases", [])}
    if FALLBACK_ALIAS in aliases:
        return True
    block_text = text[int(block["start_offset"]) : int(block["end_offset"])]
    return FALLBACK_MARKER in block_text


def _remove_blocks(text: str, blocks: Iterable[Dict[str, Any]]) -> str:
    new_text = text
    for block in sorted(blocks, key=lambda item: int(item["start_offset"]), reverse=True):
        start = int(block["start_offset"])
        end = int(block["end_offset"])
        while end < len(new_text) and new_text[end] == "\n":
            end += 1
        new_text = new_text[:start] + new_text[end:]
    return new_text


def _age_days(path: Path, current_time: dt.datetime) -> float:
    created_text = parse_compact_created_at(path)
    created_at = _parse_iso(created_text)
    if created_at is None:
        created_at = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    return max(0.0, (current_time.timestamp() - created_at.timestamp()) / 86400.0)


def _parse_iso(value: str) -> Optional[dt.datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def main() -> None:
    raise SystemExit("maintenance_adapter.py is a library module; use memory_maintenance.py")


if __name__ == "__main__":
    main()
