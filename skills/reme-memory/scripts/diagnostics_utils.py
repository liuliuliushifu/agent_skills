#!/usr/bin/env python3
import hashlib
import hmac
import os
import socket
import urllib.error
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple


ERROR_KIND_VALUES = {
    "timeout",
    "connection_error",
    "dns_error",
    "http_4xx",
    "http_5xx",
    "empty_response",
    "json_parse_error",
    "schema_validation_error",
    "subprocess_nonzero",
    "retryable_index_error",
    "unknown",
}


def _diagnostics_secret() -> str:
    return (
        os.environ.get("REME_DIAGNOSTICS_SECRET")
        or os.environ.get("REME_CAPTURE_DIAGNOSTICS_SECRET")
        or f"reme-local::{socket.gethostname()}::{os.getuid()}"
    )


def diagnostics_secret() -> str:
    return _diagnostics_secret()


def diagnostics_hmac_short(text: str, length: int = 16) -> str:
    digest = hmac.new(
        _diagnostics_secret().encode("utf-8"),
        str(text).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:length]


def _normalize_error_text(text: str) -> str:
    return " ".join(str(text or "").strip().split())


def _fingerprint(text: str) -> str:
    normalized = _normalize_error_text(text)
    payload = (_diagnostics_secret() + "::" + normalized).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def bucket_size(value: Any, quantum: int = 64) -> int:
    try:
        size = max(0, int(value))
    except (TypeError, ValueError):
        return 0
    if size == 0:
        return 0
    return ((size + quantum - 1) // quantum) * quantum


def bucket_count(value: Any) -> str:
    try:
        count = max(0, int(value))
    except (TypeError, ValueError):
        return "0"
    if count == 0:
        return "0"
    if count == 1:
        return "1"
    if count <= 3:
        return "2-3"
    if count <= 7:
        return "4-7"
    if count <= 15:
        return "8-15"
    return "16+"


@dataclass(frozen=True)
class SafeErrorRecord:
    kind: str
    error_type: str
    message_size_bytes: int
    fingerprint: str
    http_status: Optional[int] = None
    errno: Optional[int] = None
    returncode: Optional[int] = None

    def summary(self) -> str:
        parts = [self.kind, self.error_type]
        if self.returncode is not None:
            parts.append(f"rc={self.returncode}")
        if self.http_status is not None:
            parts.append(f"http={self.http_status}")
        if self.errno is not None:
            parts.append(f"errno={self.errno}")
        return " ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def safe_error_from_exception(exc: BaseException) -> SafeErrorRecord:
    error_type = exc.__class__.__name__
    text = str(exc) or error_type
    errno = getattr(exc, "errno", None)
    returncode = getattr(exc, "returncode", None)
    http_status = getattr(exc, "code", None) if isinstance(exc, urllib.error.HTTPError) else None
    return SafeErrorRecord(
        kind=_classify_error(text, error_type=error_type, errno=errno, returncode=returncode, http_status=http_status),
        error_type=error_type,
        message_size_bytes=len(text.encode("utf-8")),
        fingerprint=_fingerprint(text),
        http_status=http_status,
        errno=errno,
        returncode=returncode,
    )


def safe_error_from_text(
    text: str,
    *,
    error_type: str = "Error",
    errno: Optional[int] = None,
    returncode: Optional[int] = None,
    http_status: Optional[int] = None,
) -> SafeErrorRecord:
    normalized = str(text or "")
    return SafeErrorRecord(
        kind=_classify_error(normalized, error_type=error_type, errno=errno, returncode=returncode, http_status=http_status),
        error_type=error_type,
        message_size_bytes=len(normalized.encode("utf-8")),
        fingerprint=_fingerprint(normalized),
        http_status=http_status,
        errno=errno,
        returncode=returncode,
    )


def ensure_safe_error_record(value: Any, *, last_error_meta: Optional[Dict[str, Any]] = None) -> SafeErrorRecord:
    if isinstance(value, SafeErrorRecord):
        return value
    if isinstance(value, BaseException):
        return safe_error_from_exception(value)
    if last_error_meta:
        kind = str(last_error_meta.get("kind", "unknown"))
        if kind not in ERROR_KIND_VALUES:
            kind = "unknown"
        return SafeErrorRecord(
            kind=kind,
            error_type=str(last_error_meta.get("error_type", "Error")),
            message_size_bytes=int(last_error_meta.get("message_size_bytes", 0)),
            fingerprint=str(last_error_meta.get("fingerprint", "")),
            http_status=_coerce_int(last_error_meta.get("http_status")),
            errno=_coerce_int(last_error_meta.get("errno")),
            returncode=_coerce_int(last_error_meta.get("returncode")),
        )
    return safe_error_from_text(str(value or ""))


def persisted_error_fields(value: Any, *, last_error_meta: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any]]:
    if not value and not last_error_meta:
        return "", {}
    record = ensure_safe_error_record(value, last_error_meta=last_error_meta)
    return record.summary(), record.to_dict()


def _coerce_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _classify_error(
    text: str,
    *,
    error_type: str,
    errno: Optional[int],
    returncode: Optional[int],
    http_status: Optional[int],
) -> str:
    lowered = text.lower()
    if http_status is not None:
        if 400 <= http_status < 500:
            return "http_4xx"
        if 500 <= http_status < 600:
            return "http_5xx"
    if "error code: 4" in lowered or "余额不足" in text or "无可用资源包" in text:
        return "http_4xx"
    if "error code: 5" in lowered:
        return "http_5xx"
    if errno in {-2} or "name or service not known" in lowered or "temporary failure in name resolution" in lowered:
        return "dns_error"
    if "timeout" in lowered or "timed out" in lowered or error_type == "TimeoutError":
        if "index" in lowered:
            return "retryable_index_error"
        return "timeout"
    if "connection" in lowered or "connectionerror" in lowered or "connection reset" in lowered:
        return "connection_error"
    if "empty content" in lowered or "returned empty" in lowered or "empty response" in lowered:
        return "empty_response"
    if "json" in lowered and ("parse" in lowered or "decode" in lowered):
        return "json_parse_error"
    if "schema" in lowered or "validation" in lowered:
        return "schema_validation_error"
    if returncode is not None:
        return "subprocess_nonzero"
    return "unknown"
