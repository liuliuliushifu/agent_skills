#!/usr/bin/env python3
"""Compatibility layer for structured capture memory generation.

Current ReMe uses deterministic capture plus async Codex refine for LLM-quality
extraction. This module intentionally does not call external chat-completion
providers; it preserves the old function names so existing daemon code can keep
using the same import path.
"""

from __future__ import annotations

from typing import Any, Dict

from daemon_trace import current_trace, record_event, step
from memory_json_schema import normalize_memory_json


class StructuredMemoryLLMError(Exception):
    pass


class StructuredMemoryFallbackRequired(StructuredMemoryLLMError):
    pass


def generate_structured_memory_json(
    capture: Dict[str, Any],
    *,
    request_timeout_seconds: float | None = None,
    max_retries: int | None = None,
    http_backend=None,
) -> Dict[str, Any]:
    """Build structured memory locally.

    The timeout, retry, and http_backend parameters are accepted for backward
    compatibility only.
    """
    del request_timeout_seconds, max_retries, http_backend
    trace = current_trace()
    with step(trace, "local_structured_generation"):
        payload = build_fallback_memory_json(capture)
    record_event(trace, "llm_generation_result", result_source="local_deterministic")
    return payload


def build_fallback_memory_json(capture: Dict[str, Any]) -> Dict[str, Any]:
    conclusions = [item["decision"] for item in capture.get("decisions", []) if item.get("decision")]
    if not conclusions:
        conclusions = [item["text"] for item in capture.get("facts", [])[:5] if item.get("text")]

    root_causes = [item["root_cause"] for item in capture.get("errors", []) if item.get("root_cause")]
    solutions = [item["rationale"] for item in capture.get("decisions", []) if item.get("rationale")]
    if not solutions:
        solutions = list(capture.get("next_steps", []))

    payload = {
        "topic": capture.get("durable_identity") or capture.get("task") or "ReMe captured memory",
        "applicability": capture.get("scenario") or capture.get("capture_reason") or "",
        "conclusions": conclusions or [capture.get("session_summary") or capture.get("task") or "Captured ReMe memory."],
        "root_cause_patterns": root_causes,
        "solutions": solutions,
        "key_locations": {
            "files": list(capture.get("key_files", [])),
            "symbols": list(capture.get("symbols", [])),
            "errors": [item["fingerprint"] for item in capture.get("errors", []) if item.get("fingerprint")],
        },
        "aliases": list(capture.get("aliases", [])),
        "benchmark_names": [item["name"] for item in capture.get("benchmarks", []) if item.get("name")],
        "retrieval_surface": capture["retrieval_surface"],
        "confidence": "high" if capture.get("facts") or capture.get("decisions") else "medium",
        "source_capture_id": capture["capture_id"],
        "review_after": "",
        "supersedes": [],
    }
    return normalize_memory_json(payload)
