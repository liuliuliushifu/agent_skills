#!/usr/bin/env python3
import hashlib
import json
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


CAPTURE_SCHEMA_VERSION = 2
DEFAULT_CAPTURE_REASON = "compact_context"
DEFAULT_PROJECT = os.environ.get("REME_BUS_PROJECT", "cling_glb")
MAX_TEXT_FIELD_LENGTH = 16384
MAX_SHORT_FIELD_LENGTH = 4096
MAX_LIST_ITEMS = 128
_WHITESPACE_RE = re.compile(r"\s+")
REQUIRED_CAPTURE_FIELDS = {
    "schema_version",
    "capture_id",
    "handoff_idempotency_key",
    "durable_idempotency_key",
    "capture_reason",
    "project",
    "created_at",
    "task",
    "session_summary",
    "subsystem",
    "facts",
    "decisions",
    "constraints",
    "errors",
    "benchmarks",
    "key_files",
    "symbols",
    "aliases",
    "open_issues",
    "next_steps",
    "retrieval_surface",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_capture_json(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("capture json must be an object")
    return data


def has_stable_session_scope(capture: Dict[str, Any]) -> bool:
    return bool(capture.get("thread_id") or capture.get("session_scope_key"))


def normalize_capture_input(
    raw: Dict[str, Any],
    thread_id_override: str = "",
    session_scope_key_override: str = "",
    created_at: Optional[str] = None,
    random_suffix_factory=None,
) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("capture payload must be a dict")

    capture_reason = _optional_string(raw.get("capture_reason"), MAX_SHORT_FIELD_LENGTH) or DEFAULT_CAPTURE_REASON
    project = _optional_string(raw.get("project"), 128) or DEFAULT_PROJECT
    thread_id = _normalize_nullable_string(thread_id_override or raw.get("thread_id"), 256)
    session_scope_key = _normalize_nullable_string(
        session_scope_key_override or raw.get("session_scope_key"),
        256,
    )
    durable_identity = _normalize_nullable_string(raw.get("durable_identity"), MAX_SHORT_FIELD_LENGTH)
    durable_identity = normalize_durable_identity(durable_identity) if durable_identity else None

    task = _require_string("task", raw.get("task", ""), MAX_SHORT_FIELD_LENGTH)
    scenario = _optional_string(raw.get("scenario"), MAX_TEXT_FIELD_LENGTH)
    session_summary = _require_string("session_summary", raw.get("session_summary", ""), MAX_TEXT_FIELD_LENGTH)
    retrieval_surface = _optional_string(raw.get("retrieval_surface"), MAX_TEXT_FIELD_LENGTH)

    envelope_without_keys = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "capture_reason": capture_reason,
        "project": project,
        "thread_id": thread_id,
        "session_scope_key": session_scope_key,
        "durable_identity": durable_identity,
        "created_at": created_at or now_iso(),
        "task": task,
        "subsystem": _normalize_string_list(raw.get("subsystem"), "subsystem", 128),
        "scenario": scenario,
        "session_summary": session_summary,
        "facts": _normalize_fact_list(raw.get("facts")),
        "decisions": _normalize_decision_list(raw.get("decisions")),
        "constraints": _normalize_string_list(raw.get("constraints"), "constraint", MAX_TEXT_FIELD_LENGTH),
        "errors": _normalize_error_list(raw.get("errors")),
        "benchmarks": _normalize_benchmark_list(raw.get("benchmarks")),
        "key_files": _normalize_string_list(raw.get("key_files"), "key_file", MAX_TEXT_FIELD_LENGTH),
        "symbols": _normalize_string_list(raw.get("symbols"), "symbol", 512),
        "aliases": _normalize_string_list(raw.get("aliases"), "alias", 512),
        "open_issues": _normalize_string_list(raw.get("open_issues"), "open_issue", MAX_TEXT_FIELD_LENGTH),
        "next_steps": _normalize_string_list(raw.get("next_steps"), "next_step", MAX_TEXT_FIELD_LENGTH),
        "retrieval_surface": "",
    }
    envelope_without_keys["retrieval_surface"] = retrieval_surface or _build_retrieval_surface(envelope_without_keys)

    random_factory = random_suffix_factory or _default_random_suffix
    stable_scope = bool(thread_id or session_scope_key)
    scope_entropy = "" if stable_scope else random_factory()

    handoff_idempotency_key = compute_handoff_idempotency_key(
        envelope_without_keys,
        scope_entropy=scope_entropy,
    )
    capture_id = build_capture_id(handoff_idempotency_key, scope_entropy)
    durable_idempotency_key = compute_durable_idempotency_key(
        envelope_without_keys,
        capture_id=capture_id,
    )

    return {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "capture_id": capture_id,
        "handoff_idempotency_key": handoff_idempotency_key,
        "durable_idempotency_key": durable_idempotency_key,
        **envelope_without_keys,
    }


def validate_capture_envelope(capture: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(capture, dict):
        raise ValueError("capture envelope must be a dict")
    missing = sorted(REQUIRED_CAPTURE_FIELDS - set(capture.keys()))
    if missing:
        raise ValueError(f"capture envelope missing required fields: {missing}")
    if int(capture.get("schema_version")) != CAPTURE_SCHEMA_VERSION:
        raise ValueError(f"unsupported capture schema_version: {capture.get('schema_version')}")

    envelope_without_keys = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "capture_reason": _require_string("capture_reason", capture.get("capture_reason", ""), MAX_SHORT_FIELD_LENGTH),
        "project": _require_string("project", capture.get("project", ""), 128),
        "thread_id": _normalize_nullable_string(capture.get("thread_id"), 256),
        "session_scope_key": _normalize_nullable_string(capture.get("session_scope_key"), 256),
        "durable_identity": _normalize_nullable_string(capture.get("durable_identity"), MAX_SHORT_FIELD_LENGTH),
        "created_at": _require_string("created_at", capture.get("created_at", ""), 64),
        "task": _require_string("task", capture.get("task", ""), MAX_SHORT_FIELD_LENGTH),
        "subsystem": _normalize_string_list(capture.get("subsystem"), "subsystem", 128),
        "scenario": _optional_string(capture.get("scenario"), MAX_TEXT_FIELD_LENGTH),
        "session_summary": _require_string("session_summary", capture.get("session_summary", ""), MAX_TEXT_FIELD_LENGTH),
        "facts": _normalize_fact_list(capture.get("facts")),
        "decisions": _normalize_decision_list(capture.get("decisions")),
        "constraints": _normalize_string_list(capture.get("constraints"), "constraint", MAX_TEXT_FIELD_LENGTH),
        "errors": _normalize_error_list(capture.get("errors")),
        "benchmarks": _normalize_benchmark_list(capture.get("benchmarks")),
        "key_files": _normalize_string_list(capture.get("key_files"), "key_file", MAX_TEXT_FIELD_LENGTH),
        "symbols": _normalize_string_list(capture.get("symbols"), "symbol", 512),
        "aliases": _normalize_string_list(capture.get("aliases"), "alias", 512),
        "open_issues": _normalize_string_list(capture.get("open_issues"), "open_issue", MAX_TEXT_FIELD_LENGTH),
        "next_steps": _normalize_string_list(capture.get("next_steps"), "next_step", MAX_TEXT_FIELD_LENGTH),
        "retrieval_surface": _require_string("retrieval_surface", capture.get("retrieval_surface", ""), MAX_TEXT_FIELD_LENGTH),
    }
    durable_identity = envelope_without_keys["durable_identity"]
    if durable_identity:
        envelope_without_keys["durable_identity"] = normalize_durable_identity(durable_identity)

    capture_id = _require_string("capture_id", capture.get("capture_id", ""), 128)
    handoff_idempotency_key = _require_string(
        "handoff_idempotency_key",
        capture.get("handoff_idempotency_key", ""),
        128,
    )
    durable_idempotency_key = _require_string(
        "durable_idempotency_key",
        capture.get("durable_idempotency_key", ""),
        128,
    )

    expected_handoff = compute_handoff_idempotency_key(envelope_without_keys)
    stable_scope = bool(envelope_without_keys["thread_id"] or envelope_without_keys["session_scope_key"])
    if stable_scope and handoff_idempotency_key != expected_handoff:
        raise ValueError("handoff_idempotency_key does not match normalized capture envelope")
    if stable_scope and capture_id != build_capture_id(handoff_idempotency_key):
        raise ValueError("capture_id does not match stable handoff_idempotency_key")

    expected_durable = compute_durable_idempotency_key(envelope_without_keys, capture_id=capture_id)
    if durable_idempotency_key != expected_durable:
        raise ValueError("durable_idempotency_key does not match normalized capture envelope")

    return {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "capture_id": capture_id,
        "handoff_idempotency_key": handoff_idempotency_key,
        "durable_idempotency_key": durable_idempotency_key,
        **envelope_without_keys,
    }


def compute_handoff_idempotency_key(
    envelope_without_keys: Dict[str, Any],
    scope_entropy: str = "",
) -> str:
    base = {
        "capture_reason": envelope_without_keys["capture_reason"],
        "project": envelope_without_keys["project"],
        "thread_id": envelope_without_keys["thread_id"],
        "session_scope_key": envelope_without_keys["session_scope_key"],
        "durable_identity": envelope_without_keys["durable_identity"],
        "task": envelope_without_keys["task"],
        "subsystem": envelope_without_keys["subsystem"],
        "scenario": envelope_without_keys["scenario"],
        "session_summary": envelope_without_keys["session_summary"],
        "facts": envelope_without_keys["facts"],
        "decisions": envelope_without_keys["decisions"],
        "constraints": envelope_without_keys["constraints"],
        "errors": envelope_without_keys["errors"],
        "benchmarks": envelope_without_keys["benchmarks"],
        "key_files": envelope_without_keys["key_files"],
        "symbols": envelope_without_keys["symbols"],
        "aliases": envelope_without_keys["aliases"],
        "open_issues": envelope_without_keys["open_issues"],
        "next_steps": envelope_without_keys["next_steps"],
        "retrieval_surface": envelope_without_keys["retrieval_surface"],
    }
    if scope_entropy:
        base["scope_entropy"] = scope_entropy
    return _sha256_key(base)


def compute_durable_idempotency_key(
    envelope_without_keys: Dict[str, Any],
    capture_id: str,
) -> str:
    durable_identity = envelope_without_keys.get("durable_identity")
    if durable_identity:
        return _sha256_key(
            {
                "project": envelope_without_keys["project"],
                "durable_identity": durable_identity,
            }
        )

    return _sha256_key(
        {
            "project": envelope_without_keys["project"],
            "thread_id": envelope_without_keys["thread_id"],
            "session_scope_key": envelope_without_keys["session_scope_key"],
            "capture_id": capture_id,
        }
    )


def build_capture_id(handoff_idempotency_key: str, scope_entropy: str = "") -> str:
    digest = handoff_idempotency_key.split("sha256:", 1)[-1][:16]
    if scope_entropy:
        return f"cap_{digest}_{scope_entropy}"
    return f"cap_{digest}"


def normalize_durable_identity(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.strip().lower()
    return _WHITESPACE_RE.sub(" ", normalized)


def _default_random_suffix() -> str:
    return uuid.uuid4().hex[:8]


def _sha256_key(data: Dict[str, Any]) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def _normalize_nullable_string(value: Any, max_len: int) -> Optional[str]:
    text = _optional_string(value, max_len)
    return text or None


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


def _normalize_fact_list(value: Any) -> List[Dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("facts must be a list")
    items: List[Dict[str, str]] = []
    for raw_item in value[:MAX_LIST_ITEMS]:
        if isinstance(raw_item, str):
            text = _require_string("fact.text", raw_item, MAX_TEXT_FIELD_LENGTH)
            items.append({"text": text, "evidence": "", "confidence": ""})
            continue
        if not isinstance(raw_item, dict):
            raise ValueError("fact must be an object or string")
        text = _require_string("fact.text", raw_item.get("text", ""), MAX_TEXT_FIELD_LENGTH)
        evidence = _optional_string(raw_item.get("evidence"), MAX_TEXT_FIELD_LENGTH)
        confidence = _optional_string(raw_item.get("confidence"), 32)
        items.append({"text": text, "evidence": evidence, "confidence": confidence})
    return items


def _normalize_decision_list(value: Any) -> List[Dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("decisions must be a list")
    items: List[Dict[str, str]] = []
    for raw_item in value[:MAX_LIST_ITEMS]:
        if isinstance(raw_item, str):
            decision = _require_string("decision.decision", raw_item, MAX_TEXT_FIELD_LENGTH)
            items.append({"decision": decision, "rationale": "", "status": ""})
            continue
        if not isinstance(raw_item, dict):
            raise ValueError("decision must be an object or string")
        decision = _require_string("decision.decision", raw_item.get("decision", ""), MAX_TEXT_FIELD_LENGTH)
        rationale = _optional_string(raw_item.get("rationale"), MAX_TEXT_FIELD_LENGTH)
        status = _optional_string(raw_item.get("status"), 64)
        items.append({"decision": decision, "rationale": rationale, "status": status})
    return items


def _normalize_error_list(value: Any) -> List[Dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("errors must be a list")
    items: List[Dict[str, str]] = []
    for raw_item in value[:MAX_LIST_ITEMS]:
        if isinstance(raw_item, str):
            fingerprint = _require_string("error.fingerprint", raw_item, MAX_TEXT_FIELD_LENGTH)
            items.append({"fingerprint": fingerprint, "root_cause": "", "workaround": ""})
            continue
        if not isinstance(raw_item, dict):
            raise ValueError("error must be an object or string")
        fingerprint = _require_string("error.fingerprint", raw_item.get("fingerprint", ""), MAX_TEXT_FIELD_LENGTH)
        root_cause = _optional_string(raw_item.get("root_cause"), MAX_TEXT_FIELD_LENGTH)
        workaround = _optional_string(raw_item.get("workaround"), MAX_TEXT_FIELD_LENGTH)
        items.append({"fingerprint": fingerprint, "root_cause": root_cause, "workaround": workaround})
    return items


def _normalize_benchmark_list(value: Any) -> List[Dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("benchmarks must be a list")
    items: List[Dict[str, str]] = []
    for raw_item in value[:MAX_LIST_ITEMS]:
        if isinstance(raw_item, str):
            name = _require_string("benchmark.name", raw_item, MAX_SHORT_FIELD_LENGTH)
            items.append({"name": name, "value": "", "notes": ""})
            continue
        if not isinstance(raw_item, dict):
            raise ValueError("benchmark must be an object or string")
        name = _require_string("benchmark.name", raw_item.get("name", ""), MAX_SHORT_FIELD_LENGTH)
        value_text = _optional_string(raw_item.get("value"), 256)
        notes = _optional_string(raw_item.get("notes"), MAX_TEXT_FIELD_LENGTH)
        items.append({"name": name, "value": value_text, "notes": notes})
    return items


def _build_retrieval_surface(envelope_without_keys: Dict[str, Any]) -> str:
    terms: List[str] = []
    terms.extend(envelope_without_keys["subsystem"])
    terms.extend(envelope_without_keys["aliases"])
    terms.extend(envelope_without_keys["symbols"])
    terms.extend(envelope_without_keys["key_files"])
    for error in envelope_without_keys["errors"]:
        terms.append(error["fingerprint"])
    if envelope_without_keys["durable_identity"]:
        terms.append(envelope_without_keys["durable_identity"])
    terms.append(envelope_without_keys["task"])
    if envelope_without_keys["scenario"]:
        terms.append(envelope_without_keys["scenario"])
    if envelope_without_keys["session_summary"]:
        terms.append(envelope_without_keys["session_summary"])

    cleaned: List[str] = []
    for term in terms:
        text = _WHITESPACE_RE.sub(" ", term.strip())
        if text and text not in cleaned:
            cleaned.append(text)
    return " ".join(cleaned)
