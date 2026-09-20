#!/usr/bin/env python3

import json
import os
from pathlib import Path

from daemon_trace import (
    current_trace,
    mark_fallback_used,
    mark_retry_scheduled,
    record_diagnostic_sizes,
    record_event,
    step,
    update_latest_status,
)
from diagnostics_utils import bucket_count, bucket_size
from memory_block_renderer import write_memory_block
from runtime_worker import RuntimeWorkerTimeout
from structured_memory_llm import StructuredMemoryFallbackRequired, build_fallback_memory_json, generate_structured_memory_json
from write_adapter import RetryableWriteError


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def handle_capture_request(
    claimed,
    store,
    *,
    llm_backend=None,
    renderer_backend=None,
    index_backend=None,
    enable_durable_write=None,
) -> None:
    request = claimed.request
    trace = current_trace()
    current_status = store.read_status(request.request_id) or {}
    current_attempt = int(current_status.get("attempt", 0))
    with step(trace, "archive_raw_request"):
        store.archive_raw_request(request)
    store.update_phase(
        request.request_id,
        state="processing",
        phase="capture_archived",
        attempt=current_attempt,
    )
    update_latest_status(trace, store.read_status(request.request_id) or {})
    durable_enabled = _env_bool("REME_CAPTURE_DURABLE_ENABLED", False) if enable_durable_write is None else enable_durable_write
    record_diagnostic_sizes(trace, _capture_diagnostic_sizes(request.payload))
    record_event(trace, "durable_gate_decision", durable_enabled=bool(durable_enabled))
    if not durable_enabled:
        store.complete_request(claimed, state="captured", phase="capture_archived")
        update_latest_status(trace, store.read_status(request.request_id) or {})
        return

    capture = request.payload
    llm_fn = llm_backend or generate_structured_memory_json
    render_fn = renderer_backend or write_memory_block
    index_fn = index_backend

    store.update_phase(
        request.request_id,
        state="processing",
        phase="memory_generation_started",
        attempt=current_attempt,
    )
    update_latest_status(trace, store.read_status(request.request_id) or {})
    try:
        with step(trace, "memory_generation"):
            memory_json = llm_fn(capture)
    except StructuredMemoryFallbackRequired:
        mark_fallback_used(trace)
        with step(trace, "fallback_generation"):
            memory_json = build_fallback_memory_json(capture)

    store.update_phase(
        request.request_id,
        state="processing",
        phase="memory_generated",
        attempt=current_attempt,
    )
    update_latest_status(trace, store.read_status(request.request_id) or {})
    with step(trace, "render_memory_block") as render_step:
        render_result = render_fn(memory_json, capture)
        if render_step is not None and isinstance(render_result, dict):
            render_step["details"].update(
                {
                    "target_memory_day": Path(render_result.get("path", "")).name,
                    "replaced_existing_block": bool(render_result.get("replaced", False)),
                    "rendered_block_bytes_bucket": bucket_size(render_result.get("rendered_block_bytes", 0)),
                    "target_file_bytes_before_bucket": bucket_size(render_result.get("target_file_bytes_before", 0)),
                    "target_file_bytes_after_bucket": bucket_size(render_result.get("target_file_bytes_after", 0)),
                    "existing_block_count_bucket": bucket_count(render_result.get("existing_block_count", 0)),
                }
            )
    store.update_phase(
        request.request_id,
        state="processing",
        phase="memory_index_started",
        attempt=current_attempt,
    )
    update_latest_status(trace, store.read_status(request.request_id) or {})
    try:
        with step(trace, "index_upsert") as index_step:
            index_result = _run_capture_index_backend(
                store=store,
                render_result=render_result,
                index_backend=index_fn,
            )
            if index_step is not None and isinstance(index_result, dict):
                index_step["details"].update(_safe_index_step_details(index_result))
    except RetryableWriteError as exc:
        mark_retry_scheduled(trace, delay_seconds=30, error=exc)
        store.schedule_retry(
            claimed,
            attempt=current_attempt + 1,
            delay_seconds=30,
            last_error=exc,
            phase="memory_index_started",
        )
        update_latest_status(trace, store.read_status(request.request_id) or {})
        return

    store.complete_request(claimed, state="stored", phase="memory_indexed")
    update_latest_status(trace, store.read_status(request.request_id) or {})


def main() -> None:
    raise SystemExit("capture_adapter.py is a library module; use memory_daemon.py to drive it.")


if __name__ == "__main__":
    main()


def _capture_diagnostic_sizes(capture: dict) -> dict:
    serialized = json.dumps(capture, ensure_ascii=False, indent=2)
    field_sizes = {
        "retrieval_surface": bucket_size(len(capture.get("retrieval_surface", ""))),
        "session_summary": bucket_size(len(capture.get("session_summary", ""))),
        "facts": bucket_size(sum(len(item.get("text", "")) for item in capture.get("facts", []))),
        "decisions": bucket_size(sum(len(item.get("decision", "")) for item in capture.get("decisions", []))),
        "constraints": bucket_size(sum(len(item) for item in capture.get("constraints", []))),
        "errors": bucket_size(sum(len(item.get("fingerprint", "")) for item in capture.get("errors", []))),
        "benchmarks": bucket_size(sum(len(item.get("name", "")) for item in capture.get("benchmarks", []))),
        "key_files": bucket_size(sum(len(item) for item in capture.get("key_files", []))),
    }
    field_counts = {
        "facts": bucket_count(len(capture.get("facts", []))),
        "decisions": bucket_count(len(capture.get("decisions", []))),
        "errors": bucket_count(len(capture.get("errors", []))),
        "benchmarks": bucket_count(len(capture.get("benchmarks", []))),
        "key_files": bucket_count(len(capture.get("key_files", []))),
        "symbols": bucket_count(len(capture.get("symbols", []))),
        "aliases": bucket_count(len(capture.get("aliases", []))),
    }
    prompt_chars = len(serialized)
    prompt_tokens = round(prompt_chars / 4)
    return {
        "request_payload_bytes_bucket": bucket_size(len(serialized.encode("utf-8"))),
        "capture_json_bytes_bucket": bucket_size(len(serialized.encode("utf-8"))),
        "approx_prompt_chars_bucket": bucket_size(prompt_chars),
        "approx_prompt_tokens_bucket": bucket_size(prompt_tokens),
        "approx_token_method": "chars_div_4",
        "field_size_buckets": field_sizes,
        "field_count_buckets": field_counts,
    }


def _safe_index_step_details(index_result: dict) -> dict:
    return {
        "backend": str(index_result.get("backend", "")),
        "operation": str(index_result.get("operation", "")),
        "worker_generation": int(index_result.get("worker_generation", 0)),
        "worker_warm": bool(index_result.get("worker_warm", False)),
        "worker_started": bool(index_result.get("worker_started", False)),
        "worker_start_ms": int(index_result.get("worker_start_ms", 0)),
        "queue_wait_ms": int(index_result.get("queue_wait_ms", 0)),
        "exec_ms": int(index_result.get("exec_ms", 0)),
        "flush_store_ms": int(index_result.get("flush_store_ms", 0)),
        "indexed_file_count_bucket": bucket_count(index_result.get("indexed_files", 0)),
        "indexed_chunk_count_bucket": bucket_count(index_result.get("indexed_chunks", 0)),
        "memory_total_bytes_bucket": bucket_size(index_result.get("memory_total_bytes", 0)),
        "run_wall_ms": int(index_result.get("run_wall_ms", 0)),
        "subprocess_wall_ms": int(index_result.get("subprocess_wall_ms", 0)),
        "subprocess_overhead_ms": int(index_result.get("subprocess_overhead_ms", 0)),
        "stdout_parse_ms": int(index_result.get("stdout_parse_ms", 0)),
        "stdout_bytes_bucket": bucket_size(index_result.get("stdout_bytes", 0)),
        "stderr_bytes_bucket": bucket_size(index_result.get("stderr_bytes", 0)),
        "process_bootstrap_ms": int(index_result.get("process_bootstrap_ms", 0)),
        "reme_runtime_import_ms": int(index_result.get("reme_runtime_import_ms", 0)),
        "memory_source_import_ms": int(index_result.get("memory_source_import_ms", 0)),
        "chunking_utils_import_ms": int(index_result.get("chunking_utils_import_ms", 0)),
        "init_local_store_ms": int(index_result.get("init_local_store_ms", 0)),
        "load_runtime_env_ms": int(index_result.get("load_runtime_env_ms", 0)),
        "embedding_model_import_ms": int(index_result.get("embedding_model_import_ms", 0)),
        "embedding_model_construct_ms": int(index_result.get("embedding_model_construct_ms", 0)),
        "embedding_model_start_ms": int(index_result.get("embedding_model_start_ms", 0)),
        "file_store_import_ms": int(index_result.get("file_store_import_ms", 0)),
        "file_store_construct_ms": int(index_result.get("file_store_construct_ms", 0)),
        "file_store_start_ms": int(index_result.get("file_store_start_ms", 0)),
        "clear_all_ms": int(index_result.get("clear_all_ms", 0)),
        "enumerate_memory_files_ms": int(index_result.get("enumerate_memory_files_ms", 0)),
        "build_metadata_ms_total": int(index_result.get("build_metadata_ms_total", 0)),
        "chunking_ms_total": int(index_result.get("chunking_ms_total", 0)),
        "upsert_ms_total": int(index_result.get("upsert_ms_total", 0)),
        "close_local_store_ms": int(index_result.get("close_local_store_ms", 0)),
        "close_file_store_ms": int(index_result.get("close_file_store_ms", 0)),
        "close_embedding_model_ms": int(index_result.get("close_embedding_model_ms", 0)),
        "vector_enabled": bool(index_result.get("vector_enabled", False)),
        "fts_enabled": bool(index_result.get("fts_enabled", False)),
        "chunk_tokens": int(index_result.get("chunk_tokens", 0)),
        "chunk_overlap": int(index_result.get("chunk_overlap", 0)),
        "embedding_model_name": str(index_result.get("embedding_model", "")),
        "embedding_cache_enabled": bool(index_result.get("embedding_cache_enabled", False)),
        "embedding_max_batch_size": int(index_result.get("embedding_max_batch_size", 0)),
        "slowest_files": [
            {
                "file_name": str(item.get("file_name", "")),
                "size_bytes_bucket": bucket_size(item.get("size_bytes", 0)),
                "chunk_count_bucket": bucket_count(item.get("chunk_count", 0)),
                "build_metadata_ms": int(item.get("build_metadata_ms", 0)),
                "read_text_ms": int(item.get("read_text_ms", 0)),
                "stat_ms": int(item.get("stat_ms", 0)),
                "hash_text_ms": int(item.get("hash_text_ms", 0)),
                "chunking_ms": int(item.get("chunking_ms", 0)),
                "upsert_ms": int(item.get("upsert_ms", 0)),
                "total_ms": int(item.get("total_ms", 0)),
            }
            for item in index_result.get("slowest_files", [])
        ],
    }


def _run_capture_index_backend(*, store, render_result: dict, index_backend=None) -> dict:
    if index_backend is not None:
        return index_backend()
    runtime_worker = getattr(store, "runtime_worker", None)
    if runtime_worker is not None and render_result.get("path"):
        try:
            return runtime_worker.upsert_memory_file(str(render_result["path"]))
        except RuntimeWorkerTimeout as exc:
            raise RetryableWriteError(str(exc)) from exc
    if not render_result.get("path"):
        raise RuntimeError("memory capture completed without a target path for incremental indexing")
    raise RuntimeError("runtime worker unavailable for incremental memory indexing")
