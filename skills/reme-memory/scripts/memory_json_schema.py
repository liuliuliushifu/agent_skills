#!/usr/bin/env python3
from typing import Any, Dict, List


MAX_LIST_ITEMS = 128
CONFIDENCE_VALUES = {"high", "medium", "low"}


def normalize_memory_json(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("memory json must be a dict")

    topic = _require_string("topic", payload.get("topic", ""), 1024)
    applicability = _require_string("applicability", payload.get("applicability", ""), 4096)
    source_capture_id = _require_string("source_capture_id", payload.get("source_capture_id", ""), 128)
    retrieval_surface = _require_string("retrieval_surface", payload.get("retrieval_surface", ""), 16384)
    confidence = _require_string("confidence", payload.get("confidence", ""), 16).lower()
    if confidence not in CONFIDENCE_VALUES:
        raise ValueError(f"unsupported confidence: {confidence}")

    key_locations = payload.get("key_locations", {})
    if not isinstance(key_locations, dict):
        raise ValueError("key_locations must be a dict")

    return {
        "topic": topic,
        "applicability": applicability,
        "conclusions": _normalize_string_list(payload.get("conclusions"), "conclusion", 4096),
        "root_cause_patterns": _normalize_string_list(payload.get("root_cause_patterns"), "root_cause_pattern", 4096),
        "solutions": _normalize_string_list(payload.get("solutions"), "solution", 4096),
        "key_locations": {
            "files": _normalize_string_list(key_locations.get("files"), "key_file", 16384),
            "symbols": _normalize_string_list(key_locations.get("symbols"), "symbol", 512),
            "errors": _normalize_string_list(key_locations.get("errors"), "error", 4096),
        },
        "aliases": _normalize_string_list(payload.get("aliases"), "alias", 512),
        "benchmark_names": _normalize_string_list(payload.get("benchmark_names"), "benchmark_name", 512),
        "retrieval_surface": retrieval_surface,
        "confidence": confidence,
        "source_capture_id": source_capture_id,
        "evidence_at": _optional_string(payload.get("evidence_at"), 128),
        "evidence_hashes": _normalize_string_list(payload.get("evidence_hashes"), "evidence_hash", 128),
        "review_after": _optional_string(payload.get("review_after"), 128),
        "supersedes": _normalize_string_list(payload.get("supersedes"), "supersede", 128),
    }


def _require_string(name: str, value: Any, max_len: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    if len(text) > max_len:
        raise ValueError(f"{name} exceeds max length {max_len}")
    return text


def _optional_string(value: Any, max_len: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("optional field must be a string")
    text = value.strip()
    if len(text) > max_len:
        raise ValueError(f"optional field exceeds max length {max_len}")
    return text


def _normalize_string_list(value: Any, item_name: str, max_len: int) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{item_name} list must be a list")
    if len(value) > MAX_LIST_ITEMS:
        raise ValueError(f"{item_name} list exceeds max items {MAX_LIST_ITEMS}")
    items: List[str] = []
    for raw_item in value:
        item = _require_string(item_name, raw_item, max_len)
        if item not in items:
            items.append(item)
    return items
