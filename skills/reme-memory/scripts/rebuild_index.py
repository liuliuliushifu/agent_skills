#!/usr/bin/env python3
import time

PROCESS_BOOT_STARTED = time.monotonic()

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

from index_rebuild_guard import (
    RebuildProtectionError,
    begin_protected_rebuild,
    commit_protected_rebuild,
    rollback_protected_rebuild,
)
from memory_request_store import is_daemon_active_for_workdir

REME_RUNTIME_IMPORT_STARTED = time.monotonic()
from reme_runtime import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_STORE_NAME,
    close_local_store_with_metrics,
    get_embedding_cache_dir,
    get_embedding_config,
    get_file_store_dir,
    get_indexable_memory_dirs,
    get_indexable_memory_files,
    get_runtime_summary,
    init_local_store_with_metrics,
)
REME_RUNTIME_IMPORT_MS = int((time.monotonic() - REME_RUNTIME_IMPORT_STARTED) * 1000)
DEFAULT_MAX_UNCACHED_EMBEDDING_BYTES = int(
    os.environ.get("REME_FULL_REBUILD_MAX_UNCACHED_BYTES", "1000000")
)


def _acquire_rebuild_lock(workdir: str):
    lock_path = Path(workdir).expanduser().resolve() / "rebuild_index.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.seek(0)
        owner = lock_handle.read(2048).strip()
        lock_handle.close()
        return None, lock_path, owner

    lock_handle.seek(0)
    lock_handle.truncate()
    json.dump(
        {
            "pid": os.getpid(),
            "started_at_epoch": time.time(),
            "workdir": str(Path(workdir).expanduser().resolve()),
        },
        lock_handle,
        ensure_ascii=False,
        sort_keys=True,
    )
    lock_handle.write("\n")
    lock_handle.flush()
    return lock_handle, lock_path, ""


def _release_rebuild_lock(lock_handle) -> None:
    if lock_handle is None:
        return
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    finally:
        lock_handle.close()


def _build_file_metadata(path: Path):
    from reme.core.schema import FileMetadata
    from reme.core.utils.common_utils import hash_text

    started = time.monotonic()
    read_started = time.monotonic()
    content = path.read_text(encoding="utf-8")
    read_text_ms = int((time.monotonic() - read_started) * 1000)
    stat_started = time.monotonic()
    stat = path.stat()
    stat_ms = int((time.monotonic() - stat_started) * 1000)
    hash_started = time.monotonic()
    content_hash = hash_text(content)
    hash_text_ms = int((time.monotonic() - hash_started) * 1000)
    return FileMetadata(
        hash=content_hash,
        mtime_ms=stat.st_mtime * 1000,
        size=stat.st_size,
        path=str(path.absolute()),
        content=content,
    ), {
        "file_name": path.name,
        "size_bytes": stat.st_size,
        "read_text_ms": read_text_ms,
        "stat_ms": stat_ms,
        "hash_text_ms": hash_text_ms,
        "build_metadata_ms": int((time.monotonic() - started) * 1000),
    }


def _load_valid_embedding_cache_keys(*, dimensions: int) -> set:
    cache_path = get_embedding_cache_dir() / "embedding_cache.jsonl"
    if not cache_path.is_file():
        return set()
    keys = set()
    with cache_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or len(payload) != 1:
                continue
            cache_key, embedding = next(iter(payload.items()))
            if (
                isinstance(cache_key, str)
                and isinstance(embedding, list)
                and len(embedding) == dimensions
                and any(float(value) != 0.0 for value in embedding)
            ):
                keys.add(cache_key)
    return keys


def _load_reusable_active_index_keys(
    *,
    dimensions: int,
    model_name: str,
    max_input_length: int,
) -> set:
    chunks_path = get_file_store_dir() / f"{DEFAULT_STORE_NAME}_chunks.jsonl"
    if not chunks_path.is_file():
        return set()
    keys = set()
    with chunks_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            embedding = chunk.get("embedding")
            text = str(chunk.get("text") or "")[:max_input_length]
            if (
                text
                and isinstance(embedding, list)
                and len(embedding) == dimensions
                and any(float(value) != 0.0 for value in embedding)
            ):
                keys.add(
                    hashlib.sha256(
                        f"{text}|{model_name}|{dimensions}".encode("utf-8")
                    ).hexdigest()
                )
    return keys


def _estimate_embedding_budget(
    *,
    chunk_tokens: int,
    chunk_overlap: int,
    limit: Optional[int],
) -> Dict[str, object]:
    embedding_config, file_store_config = get_runtime_summary()
    files = get_indexable_memory_files()
    if limit is not None:
        files = files[:limit]
    if not file_store_config["vector_enabled"]:
        return {
            "vector_enabled": False,
            "source_files": len(files),
            "total_chunks": 0,
            "cached_chunks": 0,
            "uncached_chunks": 0,
            "uncached_input_chars": 0,
            "uncached_input_bytes": 0,
            "estimated_api_batches": 0,
            "max_batch_size": int(embedding_config["max_batch_size"]),
            "max_retries": int(embedding_config["max_retries"]),
            "worst_case_api_input_bytes": 0,
            "cache_entries": 0,
            "max_cache_size": int(embedding_config["max_cache_size"]),
        }

    from reme.core.enumeration import MemorySource
    from reme.core.utils.chunking_utils import chunk_markdown

    dimensions = int(embedding_config["dimensions"])
    model_name = str(embedding_config["model_name"])
    max_input_length = int(embedding_config["max_input_length"])
    cache_keys = _load_valid_embedding_cache_keys(dimensions=dimensions)
    active_index_keys = _load_reusable_active_index_keys(
        dimensions=dimensions,
        model_name=model_name,
        max_input_length=max_input_length,
    )
    available_keys = cache_keys | active_index_keys
    initial_cache_entries = len(cache_keys)
    total_chunks = 0
    cached_chunks = 0
    uncached_chunks = 0
    uncached_chars = 0
    uncached_bytes = 0
    for path in files:
        content = path.read_text(encoding="utf-8")
        chunks = chunk_markdown(
            content,
            str(path.absolute()),
            MemorySource.MEMORY,
            chunk_tokens,
            chunk_overlap,
        )
        for chunk in chunks:
            total_chunks += 1
            text = str(chunk.text or "")[:max_input_length]
            cache_key = hashlib.sha256(
                f"{text}|{model_name}|{dimensions}".encode("utf-8")
            ).hexdigest()
            if cache_key in available_keys:
                cached_chunks += 1
                continue
            available_keys.add(cache_key)
            uncached_chunks += 1
            uncached_chars += len(text)
            uncached_bytes += len(text.encode("utf-8"))

    max_batch_size = max(1, int(embedding_config["max_batch_size"]))
    max_retries = max(1, int(embedding_config["max_retries"]))
    return {
        "vector_enabled": True,
        "source_files": len(files),
        "total_chunks": total_chunks,
        "cached_chunks": cached_chunks,
        "uncached_chunks": uncached_chunks,
        "uncached_input_chars": uncached_chars,
        "uncached_input_bytes": uncached_bytes,
        "estimated_api_batches": (uncached_chunks + max_batch_size - 1) // max_batch_size,
        "max_batch_size": max_batch_size,
        "max_retries": max_retries,
        "worst_case_api_input_bytes": uncached_bytes * max_retries,
        "cache_entries": initial_cache_entries,
        "active_index_reusable_entries": len(active_index_keys),
        "max_cache_size": int(embedding_config["max_cache_size"]),
    }


def _validate_rebuilt_index(
    *,
    index_dir: Path,
    expected_files: int,
    expected_chunks: int,
    vector_enabled: bool,
    embedding_dimensions: int,
) -> Dict[str, object]:
    chunks_path = index_dir / f"{DEFAULT_STORE_NAME}_chunks.jsonl"
    metadata_path = index_dir / f"{DEFAULT_STORE_NAME}_file_metadata.json"
    if not chunks_path.is_file() or not metadata_path.is_file():
        raise RuntimeError(
            f"rebuilt index files are missing: chunks={chunks_path.exists()} metadata={metadata_path.exists()}"
        )

    metadata_payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    memory_metadata = metadata_payload.get("memory", {})
    if not isinstance(memory_metadata, dict):
        raise RuntimeError("rebuilt index metadata is missing the memory mapping")
    if len(memory_metadata) != expected_files:
        raise RuntimeError(
            f"rebuilt index file count mismatch: expected={expected_files} actual={len(memory_metadata)}"
        )

    metadata_chunks = sum(int(item.get("chunk_count", 0)) for item in memory_metadata.values())
    if metadata_chunks != expected_chunks:
        raise RuntimeError(
            f"rebuilt metadata chunk count mismatch: expected={expected_chunks} actual={metadata_chunks}"
        )
    if expected_files <= 0 or expected_chunks <= 0:
        raise RuntimeError(
            f"rebuilt index must not be empty: files={expected_files} chunks={expected_chunks}"
        )

    chunk_count = 0
    zero_embeddings = 0
    invalid_dimensions = 0
    indexed_paths = set(memory_metadata)
    with chunks_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid rebuilt chunk JSON at line {line_number}: {exc}") from exc
            chunk_count += 1
            if str(chunk.get("path", "")) not in indexed_paths:
                raise RuntimeError(f"rebuilt chunk references unknown file at line {line_number}")
            if vector_enabled:
                embedding = chunk.get("embedding")
                if not isinstance(embedding, list) or len(embedding) != embedding_dimensions:
                    invalid_dimensions += 1
                elif not any(float(value) != 0.0 for value in embedding):
                    zero_embeddings += 1

    if chunk_count != expected_chunks:
        raise RuntimeError(f"rebuilt chunk count mismatch: expected={expected_chunks} actual={chunk_count}")
    if vector_enabled and (invalid_dimensions or zero_embeddings):
        raise RuntimeError(
            "rebuilt vector validation failed: "
            f"invalid_dimensions={invalid_dimensions} zero_embeddings={zero_embeddings}"
        )

    return {
        "status": "ok",
        "indexed_files": len(memory_metadata),
        "indexed_chunks": chunk_count,
        "metadata_chunks": metadata_chunks,
        "zero_embeddings": zero_embeddings,
        "invalid_embedding_dimensions": invalid_dimensions,
        "chunks_path": str(chunks_path),
        "metadata_path": str(metadata_path),
    }


async def _run(
    chunk_tokens: int,
    chunk_overlap: int,
    limit: Optional[int],
) -> Tuple[int, Dict[str, object]]:
    run_started = time.monotonic()
    import_started = time.monotonic()
    from reme.core.enumeration import MemorySource
    memory_source_import_ms = int((time.monotonic() - import_started) * 1000)
    import_started = time.monotonic()
    from reme.core.utils.chunking_utils import chunk_markdown
    chunking_utils_import_ms = int((time.monotonic() - import_started) * 1000)
    init_started = time.monotonic()
    embedding_model, file_store, init_metrics = await init_local_store_with_metrics()
    init_local_store_ms = int((time.monotonic() - init_started) * 1000)
    memory_dirs = get_indexable_memory_dirs()
    enumerate_started = time.monotonic()
    files = get_indexable_memory_files()

    if limit is not None:
        files = files[:limit]
    enumerate_memory_files_ms = int((time.monotonic() - enumerate_started) * 1000)

    if not any(path.exists() for path in memory_dirs):
        payload = {
            "status": "error",
            "reason": "memory_dirs_missing",
            "memory_dirs": [str(path) for path in memory_dirs],
            "init_local_store_ms": init_local_store_ms,
            "memory_source_import_ms": memory_source_import_ms,
            "chunking_utils_import_ms": chunking_utils_import_ms,
            "reme_runtime_import_ms": REME_RUNTIME_IMPORT_MS,
            "process_bootstrap_ms": int((time.monotonic() - PROCESS_BOOT_STARTED) * 1000),
            **init_metrics,
            "enumerate_memory_files_ms": enumerate_memory_files_ms,
        }
        await close_local_store_with_metrics(embedding_model, file_store)
        return 1, payload

    indexed_files = 0
    indexed_chunks = 0
    memory_total_bytes = 0
    build_metadata_ms_total = 0
    chunking_ms_total = 0
    upsert_ms_total = 0
    file_timings = []

    try:
        for path in files:
            file_started = time.monotonic()
            file_meta, metadata_metrics = _build_file_metadata(path)
            memory_total_bytes += metadata_metrics["size_bytes"]
            build_metadata_ms_total += metadata_metrics["build_metadata_ms"]
            chunking_started = time.monotonic()
            chunks = chunk_markdown(
                file_meta.content or "",
                file_meta.path or str(path.absolute()),
                MemorySource.MEMORY,
                chunk_tokens,
                chunk_overlap,
            )
            chunking_ms = int((time.monotonic() - chunking_started) * 1000)
            chunking_ms_total += chunking_ms
            file_meta.chunk_count = len(chunks)

            upsert_ms = 0
            if chunks:
                upsert_started = time.monotonic()
                await file_store.upsert_file(file_meta, MemorySource.MEMORY, chunks)
                upsert_ms = int((time.monotonic() - upsert_started) * 1000)
                upsert_ms_total += upsert_ms
                indexed_files += 1
                indexed_chunks += len(chunks)

            file_timings.append(
                {
                    **metadata_metrics,
                    "chunk_count": len(chunks),
                    "chunking_ms": chunking_ms,
                    "upsert_ms": upsert_ms,
                    "total_ms": int((time.monotonic() - file_started) * 1000),
                }
            )

        embedding_config, file_store_config = get_runtime_summary()
        close_started = time.monotonic()
        close_metrics = await close_local_store_with_metrics(embedding_model, file_store)
        close_local_store_ms = int((time.monotonic() - close_started) * 1000)
        embedding_model = None
        file_store = None
        run_wall_ms = int((time.monotonic() - run_started) * 1000)
        slowest_files = sorted(file_timings, key=lambda item: item["total_ms"], reverse=True)[:10]
        validation = _validate_rebuilt_index(
            index_dir=get_file_store_dir(),
            expected_files=indexed_files,
            expected_chunks=indexed_chunks,
            vector_enabled=bool(file_store_config["vector_enabled"]),
            embedding_dimensions=int(embedding_config["dimensions"]),
        )
        return 0, {
            "status": "ok",
            "memory_dirs": [str(path) for path in memory_dirs],
            "memory_dir": str(memory_dirs[0]) if memory_dirs else "",
            "source_files": len(files),
            "indexed_files": indexed_files,
            "indexed_chunks": indexed_chunks,
            "memory_total_bytes": memory_total_bytes,
            "embedding_model": embedding_config["model_name"],
            "embedding_dimensions": embedding_config["dimensions"],
            "embedding_cache_enabled": embedding_config["enable_cache"],
            "embedding_max_batch_size": embedding_config["max_batch_size"],
            "embedding_max_retries": embedding_config["max_retries"],
            "vector_enabled": file_store_config["vector_enabled"],
            "fts_enabled": file_store_config["fts_enabled"],
            "chunk_tokens": chunk_tokens,
            "chunk_overlap": chunk_overlap,
            "run_wall_ms": run_wall_ms,
            "memory_source_import_ms": memory_source_import_ms,
            "chunking_utils_import_ms": chunking_utils_import_ms,
            "reme_runtime_import_ms": REME_RUNTIME_IMPORT_MS,
            "process_bootstrap_ms": int((time.monotonic() - PROCESS_BOOT_STARTED) * 1000),
            "init_local_store_ms": init_local_store_ms,
            **init_metrics,
            "clear_all_ms": 0,
            "enumerate_memory_files_ms": enumerate_memory_files_ms,
            "build_metadata_ms_total": build_metadata_ms_total,
            "chunking_ms_total": chunking_ms_total,
            "upsert_ms_total": upsert_ms_total,
            "close_local_store_ms": close_local_store_ms,
            **close_metrics,
            "file_timings": file_timings,
            "slowest_files": slowest_files,
            "validation": validation,
        }
    finally:
        if embedding_model is not None or file_store is not None:
            await close_local_store_with_metrics(embedding_model, file_store)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild local ReMe memory file-store index.")
    parser.add_argument(
        "--confirm-full-rebuild",
        action="store_true",
        help="Confirm that the user explicitly requested or approved this standalone full rebuild.",
    )
    parser.add_argument("--chunk-tokens", type=int, default=DEFAULT_CHUNK_TOKENS, help="Chunk token budget")
    parser.add_argument("--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP, help="Chunk overlap budget")
    parser.add_argument("--limit", type=int, default=None, help="Only rebuild the first N markdown files")
    parser.add_argument(
        "--estimate-embedding-budget",
        action="store_true",
        help="Estimate cache misses and embedding input without stopping the daemon or rebuilding.",
    )
    parser.add_argument(
        "--max-uncached-bytes",
        type=int,
        default=DEFAULT_MAX_UNCACHED_EMBEDDING_BYTES,
        help="Reject before API calls when worst-case uncached input, including configured retries, exceeds this value.",
    )
    parser.add_argument(
        "--allow-embedding-budget-overrun",
        action="store_true",
        help="Proceed above the embedding budget only after the user explicitly approves the reported estimate.",
    )
    args = parser.parse_args()
    from reme_runtime import REME_WORKDIR

    if args.estimate_embedding_budget:
        estimate = _estimate_embedding_budget(
            chunk_tokens=args.chunk_tokens,
            chunk_overlap=args.chunk_overlap,
            limit=args.limit,
        )
        print(json.dumps({"status": "ok", "embedding_budget": estimate}, ensure_ascii=False, indent=2))
        raise SystemExit(0)

    if not args.confirm_full_rebuild:
        print(
            json.dumps(
                {
                    "status": "error",
                    "reason": "explicit_confirmation_required",
                    "error": "full rebuild requires --confirm-full-rebuild after explicit user approval",
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)

    current_workdir = os.environ.get("REME_WORKDIR", REME_WORKDIR)
    lock_handle, lock_path, lock_owner = _acquire_rebuild_lock(current_workdir)
    if lock_handle is None:
        print(
            json.dumps(
                {
                    "status": "error",
                    "reason": "rebuild_in_progress",
                    "error": "another full rebuild already holds the rebuild lock",
                    "lock_path": str(lock_path),
                    "lock_owner": lock_owner,
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        raise SystemExit(3)

    protection_active = False
    try:
        if is_daemon_active_for_workdir(current_workdir):
            print(
                json.dumps(
                    {
                        "status": "error",
                        "reason": "daemon_active_conflict",
                        "error": "stop the ReMe daemon before running the standalone full rebuild",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            raise SystemExit(2)

        try:
            embedding_cache_refresh = {"status": "not_needed"}
            _embedding_config, _file_store_config = get_runtime_summary()
            if _file_store_config["vector_enabled"] and get_file_store_dir().is_dir():
                try:
                    from refresh_embedding_cache import refresh_embedding_cache

                    embedding_cache_refresh = refresh_embedding_cache()
                except Exception as exc:
                    embedding_cache_refresh = {
                        "status": "skipped",
                        "reason": "active_index_not_reusable",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
            embedding_budget = _estimate_embedding_budget(
                chunk_tokens=args.chunk_tokens,
                chunk_overlap=args.chunk_overlap,
                limit=args.limit,
            )
            if (
                int(embedding_budget["worst_case_api_input_bytes"]) > args.max_uncached_bytes
                and not args.allow_embedding_budget_overrun
            ):
                print(
                    json.dumps(
                        {
                            "status": "error",
                            "reason": "embedding_budget_exceeded",
                            "error": (
                                "estimated uncached embedding input exceeds the rebuild budget; "
                                "show this estimate to the user and require explicit approval before overriding"
                            ),
                            "max_uncached_bytes": args.max_uncached_bytes,
                            "embedding_cache_refresh": embedding_cache_refresh,
                            "embedding_budget": embedding_budget,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    file=sys.stderr,
                )
                raise SystemExit(4)

            protection = begin_protected_rebuild(current_workdir)
            protection_active = True
            rc, payload = asyncio.run(
                _run(
                    chunk_tokens=args.chunk_tokens,
                    chunk_overlap=args.chunk_overlap,
                    limit=args.limit,
                )
            )
            if rc != 0:
                rollback = rollback_protected_rebuild(current_workdir)
                protection_active = False
                payload["rebuild_protection"] = protection
                payload["rollback"] = rollback
                print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
                raise SystemExit(rc)

            commit = commit_protected_rebuild(current_workdir)
            protection_active = False
            payload["rebuild_protection"] = protection
            payload["embedding_cache_refresh"] = embedding_cache_refresh
            payload["embedding_budget"] = embedding_budget
            payload["commit"] = commit
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            raise SystemExit(0)
        except SystemExit:
            raise
        except BaseException as exc:
            rollback = None
            rollback_error = ""
            if protection_active:
                try:
                    rollback = rollback_protected_rebuild(current_workdir)
                    protection_active = False
                except BaseException as rollback_exc:
                    rollback_error = f"{type(rollback_exc).__name__}: {rollback_exc}"
            reason = exc.reason if isinstance(exc, RebuildProtectionError) else "rebuild_failed"
            print(
                json.dumps(
                    {
                        "status": "error",
                        "reason": reason,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "rollback": rollback,
                        "rollback_error": rollback_error,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            raise SystemExit(1) from exc
    finally:
        if protection_active:
            try:
                rollback_protected_rebuild(current_workdir)
            except BaseException as exc:
                print(
                    json.dumps(
                        {
                            "status": "error",
                            "reason": "rollback_failed",
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    file=sys.stderr,
                )
        _release_rebuild_lock(lock_handle)


if __name__ == "__main__":
    main()
