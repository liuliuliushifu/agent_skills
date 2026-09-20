#!/usr/bin/env python3
import hashlib
import json
from collections import OrderedDict

from memory_bus_io import atomic_write_text
from reme_runtime import (
    DEFAULT_STORE_NAME,
    get_embedding_cache_dir,
    get_embedding_config,
    get_file_store_dir,
)


def refresh_embedding_cache() -> dict:
    config = get_embedding_config()
    dimensions = int(config["dimensions"])
    max_input_length = int(config["max_input_length"])
    max_cache_size = int(config["max_cache_size"])
    model_name = str(config["model_name"])
    chunks_path = get_file_store_dir() / f"{DEFAULT_STORE_NAME}_chunks.jsonl"
    if not chunks_path.is_file():
        raise FileNotFoundError(chunks_path)

    entries = OrderedDict()
    chunk_count = 0
    invalid_embeddings = 0
    zero_embeddings = 0
    with chunks_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            chunk = json.loads(line)
            chunk_count += 1
            text = str(chunk.get("text") or "")[:max_input_length]
            embedding = chunk.get("embedding")
            if not text or not isinstance(embedding, list) or len(embedding) != dimensions:
                invalid_embeddings += 1
                continue
            if not any(float(value) != 0.0 for value in embedding):
                zero_embeddings += 1
                continue
            key_text = f"{text}|{model_name}|{dimensions}"
            cache_key = hashlib.sha256(key_text.encode("utf-8")).hexdigest()
            entries.pop(cache_key, None)
            entries[cache_key] = embedding
            if max_cache_size < len(entries):
                entries.popitem(last=False)

    if chunk_count <= 0 or invalid_embeddings or zero_embeddings:
        raise RuntimeError(
            "refusing to refresh cache from an invalid index: "
            f"chunks={chunk_count} invalid_embeddings={invalid_embeddings} zero_embeddings={zero_embeddings}"
        )

    cache_path = get_embedding_cache_dir() / "embedding_cache.jsonl"
    lines = [json.dumps({key: embedding}, ensure_ascii=False) for key, embedding in entries.items()]
    atomic_write_text(cache_path, "\n".join(lines) + ("\n" if lines else ""))
    return {
        "status": "ok",
        "source_chunks": chunk_count,
        "cache_entries": len(entries),
        "max_cache_size": max_cache_size,
        "embedding_dimensions": dimensions,
        "cache_path": str(cache_path),
    }


def main() -> None:
    print(json.dumps(refresh_embedding_cache(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
