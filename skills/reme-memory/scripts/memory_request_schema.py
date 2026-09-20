#!/usr/bin/env python3
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from capture_schema import validate_capture_envelope
from memory_bus_paths import validate_request_id
from memory_json_schema import normalize_memory_json


SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {1, 2}
MAX_QUERY_LENGTH = 4096
MAX_TEXT_FIELD_LENGTH = 16384
MAX_TAGS = 32
MAX_TAG_LENGTH = 64
MAX_REQUEST_BYTES = 64 * 1024
MAX_REFINE_EVIDENCE_ITEMS = 24
MAX_REFINE_EVIDENCE_TEXT_LENGTH = 8192

REQUEST_TYPES = {
    "memory_write",
    "memory_capture",
    "memory_index",
    "memory_refine",
    "memory_maintenance",
    "memory_query",
    "memory_status",
    "memory_flush",
}
PRIORITIES = {"low", "normal", "high"}
CLIENT_STATES = {"queued", "processing", "retrying", "accepted_async", "captured", "stored", "answered", "failed", "deadletter"}
PHASES = {
    "queued",
    "raw_archived",
    "apply_started",
    "apply_committed",
    "indexed",
    "async_enqueued",
    "capture_received",
    "capture_archived",
    "memory_generation_started",
    "memory_generated",
    "memory_index_started",
    "memory_indexed",
    "maintenance_started",
    "maintenance_completed",
    "completed",
}

WRITE_ALLOWED_FIELDS = {
    "task",
    "outcome",
    "lesson",
    "tags",
    "durability",
    "source_thread",
    "memory_json",
    "reconcile",
}
INDEX_ALLOWED_FIELDS = {"path"}
CAPTURE_ALLOWED_FIELDS = None
REFINE_ALLOWED_FIELDS = {
    "parent_capture_id",
    "capture_id",
    "thread_id",
    "owner_session_id",
    "source_excerpt_hash",
    "source_excerpt_path",
    "source_transcript_path",
    "compact_memory_path",
    "evidence_at",
    "task",
    "scenario",
    "evidence",
    "tags",
    "max_attempts",
}
MAINTENANCE_ALLOWED_FIELDS = {
    "maintenance_date",
    "cleanup_fallback",
    "compact_retention",
    "compact_active_days",
    "compact_delete_days",
    "dry_run",
}
QUERY_ALLOWED_FIELDS = {"query", "max_results", "min_score", "vector_weight", "candidate_multiplier"}
STATUS_ALLOWED_FIELDS = {"target_request_id"}
FLUSH_ALLOWED_FIELDS = {"scope"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _require_string(name: str, value: Any, max_len: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    if max_len < len(text):
        raise ValueError(f"{name} exceeds max length {max_len}")
    return text


def _normalize_tags(tags: Any) -> List[str]:
    if tags is None:
        return []
    if not isinstance(tags, list):
        raise ValueError("tags must be a list")
    if MAX_TAGS < len(tags):
        raise ValueError(f"too many tags: {len(tags)}")
    normalized = []
    for tag in tags:
        normalized_tag = _require_string("tag", tag, MAX_TAG_LENGTH)
        normalized.append(normalized_tag)
    return normalized


def _reject_unknown_fields(payload: Dict[str, Any], allowed_fields: set) -> None:
    unknown = sorted(set(payload.keys()) - allowed_fields)
    if unknown:
        raise ValueError(f"unknown payload fields: {unknown}")


def normalize_write_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("write payload must be a dict")
    _reject_unknown_fields(payload, WRITE_ALLOWED_FIELDS)
    lesson = _require_string("lesson", payload.get("lesson", ""), MAX_TEXT_FIELD_LENGTH)
    normalized = {
        "task": payload.get("task", "").strip(),
        "outcome": payload.get("outcome", "").strip(),
        "lesson": lesson,
        "tags": _normalize_tags(payload.get("tags")),
        "durability": payload.get("durability", "high"),
        "source_thread": payload.get("source_thread", "").strip(),
    }
    for key in ("task", "outcome", "source_thread"):
        if normalized[key] and MAX_TEXT_FIELD_LENGTH < len(normalized[key]):
            raise ValueError(f"{key} exceeds max length {MAX_TEXT_FIELD_LENGTH}")
    raw_memory_json = payload.get("memory_json")
    if raw_memory_json is not None:
        normalized["memory_json"] = normalize_memory_json(raw_memory_json)
    raw_reconcile = payload.get("reconcile")
    if raw_reconcile is not None:
        if not isinstance(raw_reconcile, dict):
            raise ValueError("reconcile must be a dict")
        _reject_unknown_fields(
            raw_reconcile,
            {"action", "target_path", "target_durable_idempotency_key"},
        )
        action = _optional_string("reconcile.action", raw_reconcile.get("action"), 32) or "create"
        if action not in {"create", "overwrite", "merge", "keep_both"}:
            raise ValueError(f"unsupported reconcile action: {action}")
        target_path = _optional_string(
            "reconcile.target_path",
            raw_reconcile.get("target_path"),
            MAX_TEXT_FIELD_LENGTH,
        )
        target_key = _optional_string(
            "reconcile.target_durable_idempotency_key",
            raw_reconcile.get("target_durable_idempotency_key"),
            256,
        )
        if action in {"overwrite", "merge"} and (not target_path or not target_key):
            raise ValueError(f"{action} requires reconcile target_path and target_durable_idempotency_key")
        normalized["reconcile"] = {
            "action": action,
            "target_path": target_path,
            "target_durable_idempotency_key": target_key,
        }
    return normalized


def normalize_capture_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("capture payload must be a dict")
    return validate_capture_envelope(payload)


def normalize_index_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("index payload must be a dict")
    _reject_unknown_fields(payload, INDEX_ALLOWED_FIELDS)
    return {"path": _require_string("path", payload.get("path", ""), MAX_TEXT_FIELD_LENGTH)}


def _optional_string(name: str, value: Any, max_len: int = MAX_TEXT_FIELD_LENGTH) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = value.strip()
    if max_len < len(text):
        raise ValueError(f"{name} exceeds max length {max_len}")
    return text


def _stable_evidence_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_line_range(value: Any) -> List[int]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) not in {1, 2}:
        raise ValueError("evidence.line_range must be a list of one or two integers")
    result = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError) as exc:
            raise ValueError("evidence.line_range must contain integers") from exc
        if number < 0:
            raise ValueError("evidence.line_range values must be >= 0")
        result.append(number)
    return result


def _normalize_refine_evidence(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("evidence must be a list")
    if not value:
        raise ValueError("evidence must not be empty")
    if MAX_REFINE_EVIDENCE_ITEMS < len(value):
        raise ValueError(f"too many evidence items: {len(value)}")
    normalized = []
    seen_hashes = set()
    for idx, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValueError("evidence item must be a dict")
        text = _require_string("evidence.text", item.get("text", ""), MAX_REFINE_EVIDENCE_TEXT_LENGTH)
        evidence_hash = _optional_string("evidence.evidence_hash", item.get("evidence_hash"), 128)
        if not evidence_hash:
            evidence_hash = _stable_evidence_hash(text)
        if evidence_hash in seen_hashes:
            continue
        seen_hashes.add(evidence_hash)
        normalized.append(
            {
                "evidence_id": _optional_string("evidence.evidence_id", item.get("evidence_id"), 128) or f"ev{idx:03d}",
                "text": text,
                "reason": _optional_string("evidence.reason", item.get("reason"), 128),
                "line_range": _normalize_line_range(item.get("line_range")),
                "evidence_hash": evidence_hash,
            }
        )
    if not normalized:
        raise ValueError("evidence normalized to empty list")
    return normalized


def normalize_refine_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("refine payload must be a dict")
    _reject_unknown_fields(payload, REFINE_ALLOWED_FIELDS)
    max_attempts = int(payload.get("max_attempts", 3))
    if max_attempts < 1 or max_attempts > 3:
        raise ValueError("max_attempts must be in [1, 3]")
    return {
        "parent_capture_id": _optional_string("parent_capture_id", payload.get("parent_capture_id"), 128)
        or _optional_string("capture_id", payload.get("capture_id"), 128),
        "capture_id": _optional_string("capture_id", payload.get("capture_id"), 128),
        "thread_id": _optional_string("thread_id", payload.get("thread_id"), 128),
        "owner_session_id": _optional_string("owner_session_id", payload.get("owner_session_id"), 128),
        "source_excerpt_hash": _optional_string("source_excerpt_hash", payload.get("source_excerpt_hash"), 128),
        "source_excerpt_path": _optional_string("source_excerpt_path", payload.get("source_excerpt_path"), MAX_TEXT_FIELD_LENGTH),
        "source_transcript_path": _optional_string("source_transcript_path", payload.get("source_transcript_path"), MAX_TEXT_FIELD_LENGTH),
        "compact_memory_path": _optional_string(
            "compact_memory_path",
            payload.get("compact_memory_path"),
            MAX_TEXT_FIELD_LENGTH,
        ),
        "evidence_at": _optional_string("evidence_at", payload.get("evidence_at"), 128),
        "task": _optional_string("task", payload.get("task"), MAX_TEXT_FIELD_LENGTH),
        "scenario": _optional_string("scenario", payload.get("scenario"), MAX_TEXT_FIELD_LENGTH),
        "evidence": _normalize_refine_evidence(payload.get("evidence")),
        "tags": _normalize_tags(payload.get("tags")),
        "max_attempts": max_attempts,
    }


def normalize_maintenance_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("maintenance payload must be a dict")
    _reject_unknown_fields(payload, MAINTENANCE_ALLOWED_FIELDS)
    active_days = int(payload.get("compact_active_days", 30))
    delete_days = int(payload.get("compact_delete_days", 90))
    if active_days < 1:
        raise ValueError("compact_active_days must be >= 1")
    if delete_days <= active_days:
        raise ValueError("compact_delete_days must be greater than compact_active_days")
    maintenance_date = _require_string(
        "maintenance_date",
        payload.get("maintenance_date", ""),
        32,
    )
    return {
        "maintenance_date": maintenance_date,
        "cleanup_fallback": bool(payload.get("cleanup_fallback", True)),
        "compact_retention": bool(payload.get("compact_retention", True)),
        "compact_active_days": active_days,
        "compact_delete_days": delete_days,
        "dry_run": bool(payload.get("dry_run", False)),
    }


def normalize_query_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("query payload must be a dict")
    _reject_unknown_fields(payload, QUERY_ALLOWED_FIELDS)
    query = _require_string("query", payload.get("query", ""), MAX_QUERY_LENGTH)
    max_results = int(payload.get("max_results", 5))
    if 1 > max_results or 20 < max_results:
        raise ValueError("max_results must be in [1, 20]")
    return {
        "query": query,
        "max_results": max_results,
        "min_score": float(payload.get("min_score", 0.1)),
        "vector_weight": float(payload.get("vector_weight", 0.7)),
        "candidate_multiplier": float(payload.get("candidate_multiplier", 3.0)),
    }


def normalize_status_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("status payload must be a dict")
    _reject_unknown_fields(payload, STATUS_ALLOWED_FIELDS)
    target_request_id = _require_string("target_request_id", payload.get("target_request_id", ""), 128)
    validate_request_id(target_request_id)
    return {"target_request_id": target_request_id}


def normalize_flush_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("flush payload must be a dict")
    _reject_unknown_fields(payload, FLUSH_ALLOWED_FIELDS)
    scope = payload.get("scope", "writes")
    if scope not in {"writes", "captures", "searchable"}:
        raise ValueError(f"unsupported flush scope: {scope}")
    return {"scope": scope}


def normalize_payload(request_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if "memory_write" == request_type:
        return normalize_write_payload(payload)
    if "memory_capture" == request_type:
        return normalize_capture_payload(payload)
    if "memory_index" == request_type:
        return normalize_index_payload(payload)
    if "memory_refine" == request_type:
        return normalize_refine_payload(payload)
    if "memory_maintenance" == request_type:
        return normalize_maintenance_payload(payload)
    if "memory_query" == request_type:
        return normalize_query_payload(payload)
    if "memory_status" == request_type:
        return normalize_status_payload(payload)
    if "memory_flush" == request_type:
        return normalize_flush_payload(payload)
    raise ValueError(f"unsupported request_type: {request_type}")


def compute_idempotency_key(request_type: str, payload: Dict[str, Any], project: str, language: str) -> str:
    base = {
        "request_type": request_type,
        "project": project,
        "language": language,
        "payload": payload,
    }
    canonical = json.dumps(base, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MemoryRequest:
    schema_version: int
    request_id: str
    idempotency_key: str
    request_type: str
    created_at: str
    client_id: str
    project: str
    language: str
    priority: str
    payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if MAX_REQUEST_BYTES < len(encoded):
            raise ValueError(f"request too large: {len(encoded)} bytes")
        return data

    @classmethod
    def new(
        cls,
        request_id: str,
        request_type: str,
        client_id: str,
        project: str,
        language: str,
        payload: Dict[str, Any],
        priority: str = "normal",
        idempotency_key: Optional[str] = None,
    ) -> "MemoryRequest":
        validate_request_id(request_id)
        if request_type not in REQUEST_TYPES:
            raise ValueError(f"unsupported request_type: {request_type}")
        if priority not in PRIORITIES:
            raise ValueError(f"unsupported priority: {priority}")
        normalized_payload = normalize_payload(request_type, payload)
        normalized_client = _require_string("client_id", client_id, 128)
        normalized_project = _require_string("project", project, 128)
        normalized_language = _require_string("language", language, 16)
        stable_key = idempotency_key or compute_idempotency_key(
            request_type=request_type,
            payload=normalized_payload,
            project=normalized_project,
            language=normalized_language,
        )
        return cls(
            schema_version=SCHEMA_VERSION,
            request_id=request_id,
            idempotency_key=stable_key,
            request_type=request_type,
            created_at=now_iso(),
            client_id=normalized_client,
            project=normalized_project,
            language=normalized_language,
            priority=priority,
            payload=normalized_payload,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryRequest":
        if not isinstance(data, dict):
            raise ValueError("request must be a dict")
        if data.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported schema_version: {data.get('schema_version')}")
        return cls.new(
            request_id=data["request_id"],
            request_type=data["request_type"],
            client_id=data["client_id"],
            project=data["project"],
            language=data["language"],
            payload=data.get("payload", {}),
            priority=data.get("priority", "normal"),
            idempotency_key=data.get("idempotency_key"),
        )


@dataclass(frozen=True)
class MemoryRequestStatus:
    request_id: str
    state: str
    phase: str
    attempt: int
    lease_owner: str
    lease_expires_at: str
    heartbeat_at: str
    next_retry_at: str
    updated_at: str
    last_error: str
    last_error_meta: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def new(
        cls,
        request_id: str,
        state: str = "queued",
        phase: str = "queued",
        attempt: int = 0,
        lease_owner: str = "",
        lease_expires_at: str = "",
        heartbeat_at: str = "",
        next_retry_at: str = "",
        last_error: str = "",
        last_error_meta: Optional[Dict[str, Any]] = None,
    ) -> "MemoryRequestStatus":
        validate_request_id(request_id)
        if state not in CLIENT_STATES:
            raise ValueError(f"unsupported state: {state}")
        if phase not in PHASES:
            raise ValueError(f"unsupported phase: {phase}")
        if 0 > attempt:
            raise ValueError("attempt must be >= 0")
        return cls(
            request_id=request_id,
            state=state,
            phase=phase,
            attempt=attempt,
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
            heartbeat_at=heartbeat_at,
            next_retry_at=next_retry_at,
            updated_at=now_iso(),
            last_error=last_error,
            last_error_meta=dict(last_error_meta or {}),
        )
