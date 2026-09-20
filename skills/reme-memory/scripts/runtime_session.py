#!/usr/bin/env python3
import json
import time
from pathlib import Path
from typing import Any, Dict, List

from memory_bus_io import atomic_write_json, atomic_write_text
from memory_retrieval import rerank_search_results
from reme_runtime import (
    DEFAULT_CANDIDATE_MULTIPLIER,
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_MIN_SCORE,
    DEFAULT_RECENCY_HALF_LIFE_DAYS,
    DEFAULT_RECENCY_WEIGHT,
    DEFAULT_RERANK_MULTIPLIER,
    DEFAULT_VECTOR_WEIGHT,
    close_local_store_with_metrics,
    get_compact_memory_dir,
    get_memory_dir,
    init_local_store_with_metrics,
)


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


class RuntimeSession:
    def __init__(self) -> None:
        self.embedding_model = None
        self.file_store = None
        self.started = False
        self.start_count = 0

    async def ensure_started(self) -> Dict[str, Any]:
        if self.started:
            return {"worker_warm": True, "worker_started": False}

        started = time.monotonic()
        self.embedding_model, self.file_store, init_metrics = await init_local_store_with_metrics()
        self.started = True
        self.start_count += 1
        return {
            "worker_warm": False,
            "worker_started": True,
            "worker_start_ms": int((time.monotonic() - started) * 1000),
            "bootstrap_rebuilt": False,
            "bootstrap_indexed_files": 0,
            "bootstrap_indexed_chunks": 0,
            **init_metrics,
        }

    async def close(self) -> Dict[str, Any]:
        if not self.started:
            return {"closed": False}
        metrics = await close_local_store_with_metrics(self.embedding_model, self.file_store)
        self.embedding_model = None
        self.file_store = None
        self.started = False
        return {"closed": True, **metrics}

    async def health_snapshot(self) -> Dict[str, Any]:
        ready = await self.ensure_started()
        return {
            "status": "ok",
            "started": self.started,
            "start_count": self.start_count,
            **ready,
        }

    async def upsert_memory_file(
        self,
        memory_path: str,
        *,
        chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> Dict[str, Any]:
        from reme.core.enumeration import MemorySource
        from reme.core.utils.chunking_utils import chunk_markdown

        ready = await self.ensure_started()
        path = Path(memory_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(path)

        total_started = time.monotonic()
        file_meta, metadata_metrics = _build_file_metadata(path)
        chunk_started = time.monotonic()
        chunks = chunk_markdown(
            file_meta.content or "",
            file_meta.path or str(path.absolute()),
            MemorySource.MEMORY,
            chunk_tokens,
            chunk_overlap,
        )
        chunking_ms = int((time.monotonic() - chunk_started) * 1000)
        file_meta.chunk_count = len(chunks)

        upsert_started = time.monotonic()
        if chunks:
            await self.file_store.upsert_file(file_meta, MemorySource.MEMORY, chunks)
        else:
            await self.file_store.delete_file(str(path), MemorySource.MEMORY)
        upsert_ms = int((time.monotonic() - upsert_started) * 1000)
        flush_metrics = await self._flush_store()
        return {
            "status": "ok",
            "operation": "incremental_upsert",
            "backend": "runtime_worker_process",
            "indexed_files": 1 if chunks else 0,
            "indexed_chunks": len(chunks),
            "memory_total_bytes": metadata_metrics["size_bytes"],
            "build_metadata_ms_total": metadata_metrics["build_metadata_ms"],
            "chunking_ms_total": chunking_ms,
            "upsert_ms_total": upsert_ms,
            "flush_store_ms": int(flush_metrics.get("flush_store_ms", 0)),
            "file_timings": [
                {
                    **metadata_metrics,
                    "chunk_count": len(chunks),
                    "chunking_ms": chunking_ms,
                    "upsert_ms": upsert_ms,
                    "total_ms": int((time.monotonic() - total_started) * 1000),
                }
            ],
            "slowest_files": [
                {
                    **metadata_metrics,
                    "chunk_count": len(chunks),
                    "chunking_ms": chunking_ms,
                    "upsert_ms": upsert_ms,
                    "total_ms": int((time.monotonic() - total_started) * 1000),
                }
            ],
            **flush_metrics,
            **ready,
        }

    async def delete_memory_file(self, memory_path: str) -> Dict[str, Any]:
        from reme.core.enumeration import MemorySource

        ready = await self.ensure_started()
        path = str(Path(memory_path).expanduser().resolve())
        delete_started = time.monotonic()
        await self.file_store.delete_file(path, MemorySource.MEMORY)
        delete_ms = int((time.monotonic() - delete_started) * 1000)
        flush_metrics = await self._flush_store()
        return {
            "status": "ok",
            "operation": "incremental_delete",
            "backend": "runtime_worker_process",
            "deleted_files": 1,
            "path": path,
            "delete_ms": delete_ms,
            **flush_metrics,
            **ready,
        }

    async def sync_memory_files(
        self,
        *,
        upsert_paths: List[str],
        delete_paths: List[str],
        chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> Dict[str, Any]:
        from reme.core.enumeration import MemorySource
        from reme.core.utils.chunking_utils import chunk_markdown

        ready = await self.ensure_started()
        started = time.monotonic()
        indexed_files = 0
        indexed_chunks = 0
        deleted_files = 0
        for raw_path in upsert_paths:
            path = Path(raw_path).expanduser().resolve()
            if not path.exists():
                raise FileNotFoundError(path)
            file_meta, _metadata_metrics = _build_file_metadata(path)
            chunks = chunk_markdown(
                file_meta.content or "",
                file_meta.path or str(path.absolute()),
                MemorySource.MEMORY,
                chunk_tokens,
                chunk_overlap,
            )
            file_meta.chunk_count = len(chunks)
            if chunks:
                await self.file_store.upsert_file(file_meta, MemorySource.MEMORY, chunks)
                indexed_files += 1
                indexed_chunks += len(chunks)
            else:
                await self.file_store.delete_file(str(path), MemorySource.MEMORY)
                deleted_files += 1

        for raw_path in delete_paths:
            path = str(Path(raw_path).expanduser().resolve())
            await self.file_store.delete_file(path, MemorySource.MEMORY)
            deleted_files += 1

        flush_metrics = await self._flush_store()
        return {
            "status": "ok",
            "operation": "incremental_batch_sync",
            "backend": "runtime_worker_process",
            "indexed_files": indexed_files,
            "indexed_chunks": indexed_chunks,
            "deleted_files": deleted_files,
            "sync_ms": int((time.monotonic() - started) * 1000),
            **flush_metrics,
            **ready,
        }

    async def search(
        self,
        *,
        query: str,
        max_results: int,
        min_score: float = DEFAULT_MIN_SCORE,
        vector_weight: float = DEFAULT_VECTOR_WEIGHT,
        candidate_multiplier: float = DEFAULT_CANDIDATE_MULTIPLIER,
    ) -> Dict[str, Any]:
        from reme.core.enumeration import MemorySource

        ready = await self.ensure_started()
        search_started = time.monotonic()
        candidate_limit = min(
            200,
            max(max_results, int(max_results * DEFAULT_RERANK_MULTIPLIER)),
        )
        results = await self.file_store.hybrid_search(
            query=query,
            limit=candidate_limit,
            sources=[MemorySource.MEMORY],
            vector_weight=vector_weight,
            candidate_multiplier=candidate_multiplier,
        )
        search_ms = int((time.monotonic() - search_started) * 1000)
        items = rerank_search_results(
            results,
            max_results=max_results,
            min_score=min_score,
            memory_dir=get_memory_dir(),
            compact_memory_dir=get_compact_memory_dir(),
            recency_weight=DEFAULT_RECENCY_WEIGHT,
            recency_half_life_days=DEFAULT_RECENCY_HALF_LIFE_DAYS,
        )
        return {
            "status": "answered",
            "backend": "runtime_worker_process",
            "items": items,
            "search_ms": search_ms,
            "auto_sync_files": 0,
            "auto_sync_chunks": 0,
            "auto_sync_deleted_files": 0,
            "auto_sync_ms": 0,
            **ready,
        }

    async def _flush_store(self) -> Dict[str, Any]:
        if self.file_store is None:
            return {"flush_store_ms": 0}

        started = time.monotonic()
        chunks_file = getattr(self.file_store, "_chunks_file", None)
        metadata_file = getattr(self.file_store, "_metadata_file", None)
        chunks = getattr(self.file_store, "_chunks", None)
        files = getattr(self.file_store, "_files", None)
        if chunks_file is None or metadata_file is None or chunks is None or files is None:
            return {"flush_store_ms": 0}

        lines = []
        for chunk in chunks.values():
            chunk_dict = chunk.model_dump(mode="json")
            lines.append(json.dumps(chunk_dict, ensure_ascii=False))
        atomic_write_text(Path(chunks_file), "\n".join(lines))

        metadata_payload: Dict[str, Any] = {}
        for source, per_source in files.items():
            metadata_payload[source] = {
                path: {
                    "path": meta.path,
                    "hash": meta.hash,
                    "mtime_ms": meta.mtime_ms,
                    "size": meta.size,
                    "chunk_count": meta.chunk_count,
                }
                for path, meta in per_source.items()
            }
        atomic_write_json(Path(metadata_file), metadata_payload)
        return {"flush_store_ms": int((time.monotonic() - started) * 1000)}
