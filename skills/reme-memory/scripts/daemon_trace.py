#!/usr/bin/env python3
import contextlib
import copy
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from diagnostics_utils import (
    diagnostics_hmac_short,
    ensure_safe_error_record,
)
from memory_bus_io import atomic_write_json, read_json
from reme_runtime import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_TOKENS,
    get_embedding_config,
    get_file_store_config,
    get_llm_config,
)


TRACE_VERSION = 1
TERMINAL_STATES = {"stored", "captured", "answered", "accepted_async", "failed", "deadletter"}
SCRIPT_DIR = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
DEFAULT_LOG_ROOT = Path(os.environ.get("REME_DAEMON_LOG_ROOT", str(CODEX_HOME / "memories/reme-memory/daemon/logs")))
DEFAULT_ATTENTION_THRESHOLD_MS = int(os.environ.get("REME_DIAGNOSTICS_ATTENTION_THRESHOLD_MS", "60000"))
RECENT_RETENTION_DAYS = 7
ATTENTION_RETENTION_DAYS = 30
SUMMARY_RETENTION_DAYS = 14
RECENT_LIMIT_PER_TYPE = 3
ATTENTION_LIMIT_TOTAL = 10
SUMMARY_LIMIT = 500
_ACTIVE_TRACE: Optional[Dict[str, Any]] = None


def logs_root() -> Path:
    configured = os.environ.get("REME_DIAGNOSTICS_LOG_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    bus_root = os.environ.get("REME_BUS_ROOT")
    if bus_root:
        resolved_bus_root = Path(bus_root).expanduser().resolve()
        if resolved_bus_root.name == "bus":
            return resolved_bus_root.parent / "daemon" / "logs"
        return resolved_bus_root / "_diagnostics_logs"
    return DEFAULT_LOG_ROOT.expanduser().resolve()


def build_runtime_identity() -> Dict[str, Any]:
    manifest_path = Path(
        os.environ.get(
            "REME_DEPLOY_MANIFEST_PATH",
            str(CODEX_HOME / "memories/reme-memory/daemon/deploy_manifest.json"),
        )
    )
    manifest_hash = ""
    deployed_at = ""
    if manifest_path.exists():
        content = manifest_path.read_text(encoding="utf-8")
        manifest_hash = diagnostics_hmac_short(content, length=12)
        try:
            manifest = json.loads(content)
            deployed_at = str(manifest.get("deployed_at", ""))
        except Exception:
            deployed_at = ""

    llm_config = get_llm_config()
    embedding_config = get_embedding_config()
    file_store_config = get_file_store_config()
    return {
        "daemon_script_deployed_at": deployed_at,
        "deploy_manifest_hash": manifest_hash,
        "runtime_label": os.environ.get("REME_RUNTIME_LABEL", "default"),
        "llm_model_name": llm_config["model_name"],
        "fts_enabled": bool(file_store_config["fts_enabled"]),
        "vector_enabled": bool(file_store_config["vector_enabled"]),
        "chunk_tokens": DEFAULT_CHUNK_TOKENS,
        "chunk_overlap": DEFAULT_CHUNK_OVERLAP,
        "embedding_model_name": embedding_config["model_name"],
        "embedding_cache_enabled": bool(embedding_config["enable_cache"]),
        "embedding_max_batch_size": int(embedding_config["max_batch_size"]),
    }


def start_trace(request, status_snapshot: Dict[str, Any], runtime_identity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    diag_id = diagnostics_hmac_short(request.request_id, length=16)
    path = _trace_path(request.request_type, diag_id)
    trace = {}
    if path.exists():
        try:
            trace = read_json(path)
        except Exception:
            trace = {}
    if not trace:
        trace = {
            "trace_version": TRACE_VERSION,
            "diag_id": diag_id,
            "request_type": request.request_type,
            "runtime_identity": runtime_identity or build_runtime_identity(),
            "started_at": _now_iso(),
            "completed_at": "",
            "queue_age_ms": _queue_age_ms(request.created_at),
            "total_elapsed_ms": 0,
            "final_state": "",
            "final_phase": "",
            "fallback_used": False,
            "retry_scheduled": False,
            "error": {},
            "diagnostic_sizes": {},
            "daemon_attempts": [],
            "steps": [],
        }
    else:
        trace["runtime_identity"] = runtime_identity or trace.get("runtime_identity", {})
    trace["_trace_path"] = str(path)
    trace["_terminal"] = False
    trace["_request_id"] = request.request_id
    trace["_latest_status_snapshot"] = copy.deepcopy(status_snapshot or {})
    return trace


def start_attempt(trace: Dict[str, Any], *, daemon_id: str, status_snapshot: Dict[str, Any]) -> None:
    attempt = {
        "attempt_index": len(trace.get("daemon_attempts", [])),
        "daemon_id": daemon_id,
        "claimed_at": _now_iso(),
        "handler_started_at": _now_iso(),
        "handler_ended_at": "",
        "elapsed_ms": 0,
        "phase_at_claim": status_snapshot.get("phase", "queued"),
        "phase_at_end": "",
        "scheduled_retry_delay_ms": 0,
        "actual_retry_wait_ms": _actual_retry_wait_ms(status_snapshot),
        "recovery_reason": _recovery_reason(status_snapshot),
        "error_kind": "",
    }
    trace.setdefault("daemon_attempts", []).append(attempt)
    flush_trace_snapshot(trace)


def finish_attempt(
    trace: Dict[str, Any],
    *,
    final_state: str,
    final_phase: str,
    error: Any = None,
) -> None:
    attempts = trace.get("daemon_attempts", [])
    if not attempts:
        return
    current = attempts[-1]
    current["handler_ended_at"] = _now_iso()
    current["elapsed_ms"] = _elapsed_ms(current["handler_started_at"], current["handler_ended_at"])
    current["phase_at_end"] = final_phase
    pending_retry = trace.pop("_pending_retry", None)
    if pending_retry:
        current["scheduled_retry_delay_ms"] = int(pending_retry.get("scheduled_retry_delay_ms", 0))
        current["error_kind"] = str(pending_retry.get("error_kind", ""))
    elif error:
        current["error_kind"] = ensure_safe_error_record(error).kind
    if final_state in TERMINAL_STATES:
        trace["_terminal"] = True
    flush_trace_snapshot(trace)


def mark_retry_scheduled(trace: Dict[str, Any], *, delay_seconds: int, error: Any) -> None:
    if trace is None:
        return
    safe_error = ensure_safe_error_record(error)
    trace["retry_scheduled"] = True
    trace["_pending_retry"] = {
        "scheduled_retry_delay_ms": int(delay_seconds * 1000),
        "error_kind": safe_error.kind,
    }
    record_event(
        trace,
        "schedule_retry",
        status="ok",
        scheduled_delay_seconds=int(delay_seconds),
        error_kind=safe_error.kind,
        error_type=safe_error.error_type,
    )


def mark_fallback_used(trace: Dict[str, Any]) -> None:
    if trace is None:
        return
    trace["fallback_used"] = True


def record_diagnostic_sizes(trace: Dict[str, Any], sizes: Dict[str, Any]) -> None:
    if trace is None:
        return
    trace["diagnostic_sizes"] = copy.deepcopy(sizes)
    flush_trace_snapshot(trace)


def record_event(trace: Dict[str, Any], name: str, *, status: str = "ok", **details: Any) -> None:
    if trace is None:
        return
    now = _now_iso()
    trace.setdefault("steps", []).append(
        {
            "name": name,
            "status": status,
            "started_at": now,
            "ended_at": now,
            "elapsed_ms": 0,
            "phase_before": trace.get("_latest_status_snapshot", {}).get("phase", ""),
            "phase_after": trace.get("_latest_status_snapshot", {}).get("phase", ""),
            "details": copy.deepcopy(details),
        }
    )
    flush_trace_snapshot(trace)


@contextlib.contextmanager
def step(trace: Optional[Dict[str, Any]], name: str, **details: Any) -> Iterator[Optional[Dict[str, Any]]]:
    if trace is None:
        yield None
        return
    start = _now_iso()
    phase_before = trace.get("_latest_status_snapshot", {}).get("phase", "")
    entry = {
        "name": name,
        "status": "ok",
        "started_at": start,
        "ended_at": "",
        "elapsed_ms": 0,
        "phase_before": phase_before,
        "phase_after": phase_before,
        "details": copy.deepcopy(details),
    }
    trace.setdefault("steps", []).append(entry)
    flush_trace_snapshot(trace)
    try:
        yield entry
    except Exception as exc:
        safe_error = ensure_safe_error_record(exc)
        entry["status"] = "error"
        entry["details"]["error_kind"] = safe_error.kind
        entry["details"]["error_type"] = safe_error.error_type
        raise
    finally:
        entry["ended_at"] = _now_iso()
        entry["elapsed_ms"] = _elapsed_ms(entry["started_at"], entry["ended_at"])
        entry["phase_after"] = trace.get("_latest_status_snapshot", {}).get("phase", "")
        flush_trace_snapshot(trace)


def record_exception(trace: Optional[Dict[str, Any]], exc: BaseException, *, context: str = "") -> None:
    if trace is None:
        return
    safe_error = ensure_safe_error_record(exc)
    trace["error"] = safe_error.to_dict()
    record_event(
        trace,
        "exception" if not context else f"exception:{context}",
        status="error",
        error_kind=safe_error.kind,
        error_type=safe_error.error_type,
    )


def update_latest_status(trace: Optional[Dict[str, Any]], status_snapshot: Optional[Dict[str, Any]]) -> None:
    if trace is None:
        return
    trace["_latest_status_snapshot"] = copy.deepcopy(status_snapshot or {})


def flush_trace_snapshot(trace: Optional[Dict[str, Any]]) -> None:
    if trace is None:
        return
    try:
        atomic_write_json(Path(trace["_trace_path"]), _public_trace(trace))
    except Exception:
        return


def finish_trace(
    trace: Optional[Dict[str, Any]],
    *,
    final_state: str,
    final_phase: str,
    error: Any = None,
) -> None:
    if trace is None:
        return
    trace["completed_at"] = _now_iso()
    trace["total_elapsed_ms"] = _elapsed_ms(trace["started_at"], trace["completed_at"])
    trace["final_state"] = final_state
    trace["final_phase"] = final_phase
    if error is not None:
        trace["error"] = ensure_safe_error_record(error).to_dict()
    flush_trace_snapshot(trace)
    if final_state in TERMINAL_STATES:
        _append_summary(trace)
        _write_attention(trace)
        prune_logs()


@contextlib.contextmanager
def activate_trace(trace: Optional[Dict[str, Any]]) -> Iterator[Optional[Dict[str, Any]]]:
    global _ACTIVE_TRACE
    previous = _ACTIVE_TRACE
    _ACTIVE_TRACE = trace
    try:
        yield trace
    finally:
        _ACTIVE_TRACE = previous


def current_trace() -> Optional[Dict[str, Any]]:
    return _ACTIVE_TRACE


def prune_logs() -> None:
    try:
        _prune_recent()
        _prune_attention()
        _prune_summary()
    except Exception:
        return


def _trace_path(request_type: str, diag_id: str) -> Path:
    root = logs_root() / "recent" / request_type
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{diag_id}.json"


def _attention_path(request_type: str, diag_id: str) -> Path:
    root = logs_root() / "attention" / request_type
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{diag_id}.json"


def _summary_path() -> Path:
    path = logs_root() / "request_summary.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _public_trace(trace: Dict[str, Any]) -> Dict[str, Any]:
    payload = {key: value for key, value in trace.items() if not key.startswith("_")}
    return payload


def _append_summary(trace: Dict[str, Any]) -> None:
    summary = {
        "diag_id": trace["diag_id"],
        "request_type": trace["request_type"],
        "completed_at": trace["completed_at"],
        "runtime_identity": trace["runtime_identity"],
        "queue_age_ms": trace["queue_age_ms"],
        "total_elapsed_ms": trace["total_elapsed_ms"],
        "final_state": trace["final_state"],
        "final_phase": trace["final_phase"],
        "fallback_used": trace["fallback_used"],
        "retry_scheduled": trace["retry_scheduled"],
        "daemon_attempt_count": len(trace.get("daemon_attempts", [])),
        "error_kind": trace.get("error", {}).get("kind", ""),
        "approx_prompt_tokens_bucket": trace.get("diagnostic_sizes", {}).get("approx_prompt_tokens_bucket", 0),
        "step_elapsed_ms": _summary_step_elapsed(trace.get("steps", [])),
    }
    path = _summary_path()
    line = json.dumps(summary, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _summary_step_elapsed(steps: List[Dict[str, Any]]) -> Dict[str, int]:
    fields = {
        "memory_generation": 0,
        "fallback_generation": 0,
        "render_memory_block": 0,
        "index_upsert": 0,
        "index_rebuild": 0,
        "search_backend": 0,
        "store_backend": 0,
    }
    for step_entry in steps:
        name = step_entry.get("name", "")
        if name in fields:
            fields[name] += int(step_entry.get("elapsed_ms", 0))
    return fields


def _write_attention(trace: Dict[str, Any]) -> None:
    total_elapsed_ms = int(trace.get("total_elapsed_ms", 0))
    if (
        trace.get("final_state") not in {"deadletter", "failed"}
        and total_elapsed_ms < DEFAULT_ATTENTION_THRESHOLD_MS
        and not trace.get("fallback_used", False)
        and len(trace.get("daemon_attempts", [])) <= 1
    ):
        return
    try:
        atomic_write_json(_attention_path(trace["request_type"], trace["diag_id"]), _public_trace(trace))
    except Exception:
        return


def _prune_recent() -> None:
    root = logs_root() / "recent"
    if not root.exists():
        return
    cutoff = time.time() - RECENT_RETENTION_DAYS * 86400
    for request_dir in root.iterdir():
        if not request_dir.is_dir():
            continue
        items = sorted(request_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for index, path in enumerate(items):
            if index >= RECENT_LIMIT_PER_TYPE or path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)


def _prune_attention() -> None:
    root = logs_root() / "attention"
    if not root.exists():
        return
    cutoff = time.time() - ATTENTION_RETENTION_DAYS * 86400
    items = sorted(root.glob("*/*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for index, path in enumerate(items):
        if index >= ATTENTION_LIMIT_TOTAL or path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def _prune_summary() -> None:
    path = _summary_path()
    if not path.exists():
        return
    cutoff = time.time() - SUMMARY_RETENTION_DAYS * 86400
    kept = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
            completed_at = payload.get("completed_at", "")
            if _iso_to_epoch(completed_at) < cutoff:
                continue
            kept.append(payload)
        except Exception:
            continue
    kept = kept[-SUMMARY_LIMIT:]
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in kept), encoding="utf-8")


def _queue_age_ms(created_at: str) -> int:
    created_epoch = _iso_to_epoch(created_at)
    if created_epoch <= 0:
        return 0
    return max(0, int((time.time() - created_epoch) * 1000))


def _actual_retry_wait_ms(status_snapshot: Dict[str, Any]) -> int:
    retry_at = str(status_snapshot.get("next_retry_at", ""))
    if not retry_at:
        return 0
    retry_epoch = _iso_to_epoch(retry_at)
    if retry_epoch <= 0:
        return 0
    return max(0, int((time.time() - retry_epoch) * 1000))


def _recovery_reason(status_snapshot: Dict[str, Any]) -> str:
    if status_snapshot.get("state") == "retrying" and status_snapshot.get("next_retry_at"):
        return "scheduled_retry"
    if status_snapshot.get("state") == "processing" and status_snapshot.get("lease_expires_at"):
        return "lease_recovery"
    return "fresh_claim"


def _elapsed_ms(started_at: str, ended_at: str) -> int:
    start = _iso_to_epoch(started_at)
    end = _iso_to_epoch(ended_at)
    if start <= 0 or end <= 0:
        return 0
    return max(0, int((end - start) * 1000))


def _iso_to_epoch(text: str) -> float:
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
