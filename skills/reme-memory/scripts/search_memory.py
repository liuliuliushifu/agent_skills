#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import sys

from memory_retrieval import rerank_search_results
from memory_request_store import is_daemon_active_for_workdir
from reme_runtime import (
    DEFAULT_CANDIDATE_MULTIPLIER,
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_MIN_SCORE,
    DEFAULT_RECENCY_HALF_LIFE_DAYS,
    DEFAULT_RECENCY_WEIGHT,
    DEFAULT_RERANK_MULTIPLIER,
    DEFAULT_VECTOR_WEIGHT,
    close_local_store,
    get_compact_memory_dir,
    get_indexable_memory_files,
    get_memory_dir,
    init_local_store,
)


async def search_memory(
    query: str,
    max_results: int,
    min_score: float,
    vector_weight: float,
    candidate_multiplier: float,
):
    from reme.core.enumeration import MemorySource
    from reme.core.schema import FileMetadata
    from reme.core.utils.chunking_utils import chunk_markdown
    from reme.core.utils.common_utils import hash_text

    embedding_model, file_store = await init_local_store()

    try:
        for path in get_indexable_memory_files():
            content = path.read_text(encoding="utf-8")
            stat = path.stat()
            file_meta = FileMetadata(
                hash=hash_text(content),
                mtime_ms=stat.st_mtime * 1000,
                size=stat.st_size,
                path=str(path.absolute()),
                content=content,
            )
            existing = await file_store.get_file_metadata(file_meta.path, MemorySource.MEMORY)
            if (
                existing is not None
                and existing.hash == file_meta.hash
                and existing.mtime_ms == file_meta.mtime_ms
                and existing.size == file_meta.size
            ):
                continue
            chunks = chunk_markdown(
                content,
                file_meta.path or str(path.absolute()),
                MemorySource.MEMORY,
                DEFAULT_CHUNK_TOKENS,
                DEFAULT_CHUNK_OVERLAP,
            )
            file_meta.chunk_count = len(chunks)
            if chunks:
                await file_store.upsert_file(file_meta, MemorySource.MEMORY, chunks)

        candidate_limit = min(
            200,
            max(max_results, int(max_results * DEFAULT_RERANK_MULTIPLIER)),
        )
        results = await file_store.hybrid_search(
            query=query,
            limit=candidate_limit,
            sources=[MemorySource.MEMORY],
            vector_weight=vector_weight,
            candidate_multiplier=candidate_multiplier,
        )
        return rerank_search_results(
            results,
            max_results=max_results,
            min_score=min_score,
            memory_dir=get_memory_dir(),
            compact_memory_dir=get_compact_memory_dir(),
            recency_weight=DEFAULT_RECENCY_WEIGHT,
            recency_half_life_days=DEFAULT_RECENCY_HALF_LIFE_DAYS,
        )
    finally:
        await close_local_store(embedding_model, file_store)


def main() -> None:
    parser = argparse.ArgumentParser(description="Search local ReMe memory with hybrid vector + FTS retrieval.")
    parser.add_argument("--query", required=True, help="Memory search query")
    parser.add_argument("--max-results", type=int, default=5, help="Maximum number of results")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE, help="Minimum merged score threshold")
    parser.add_argument(
        "--vector-weight",
        type=float,
        default=DEFAULT_VECTOR_WEIGHT,
        help="Weight assigned to vector search when using hybrid retrieval",
    )
    parser.add_argument(
        "--candidate-multiplier",
        type=float,
        default=DEFAULT_CANDIDATE_MULTIPLIER,
        help="Candidate pool multiplier before hybrid merge",
    )
    args = parser.parse_args()
    from reme_runtime import REME_WORKDIR

    current_workdir = os.environ.get("REME_WORKDIR", REME_WORKDIR)
    if (
        os.environ.get("REME_ALLOW_LIVE_STORE_SCRIPTS", "").strip().lower() not in {"1", "true", "yes", "on"}
        and is_daemon_active_for_workdir(current_workdir)
    ):
        print(
            json.dumps(
                {
                    "status": "error",
                    "reason": "daemon_active_conflict",
                    "error": "search_memory.py refuses to read a live daemon-owned store",
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)

    try:
        results = asyncio.run(
            search_memory(
                query=args.query,
                max_results=args.max_results,
                min_score=args.min_score,
                vector_weight=args.vector_weight,
                candidate_multiplier=args.candidate_multiplier,
            )
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "reason": "search_failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
