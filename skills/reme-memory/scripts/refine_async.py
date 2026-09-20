#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from memory_bus_client import enqueue_write
from memory_bus_io import atomic_write_json, read_json
from memory_retrieval import build_durable_reconcile_candidates
from reme_runtime import get_memory_dir


SCRIPT_DIR = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
USAGE_RECORDER = SCRIPT_DIR / "record_codex_exec_usage.py"
DEFAULT_MAX_CONCURRENCY = int(os.environ.get("REME_REFINE_MAX_CONCURRENCY", "2"))
DEFAULT_POLL_INTERVAL_SECONDS = float(os.environ.get("REME_REFINE_POLL_INTERVAL_SECONDS", "0.2"))
DEFAULT_ATTEMPT_TIMEOUT_SECONDS = float(os.environ.get("REME_REFINE_ATTEMPT_TIMEOUT_SECONDS", "300"))
DEFAULT_KEEP_FAILED_ARTIFACTS = os.environ.get("REME_REFINE_KEEP_FAILED_ARTIFACTS", "").lower() in {"1", "true", "yes", "on"}
DEFAULT_RUNNING_STALE_SECONDS = int(os.environ.get("REME_REFINE_RUNNING_STALE_SECONDS", "900"))
MAX_REFINE_OUTPUT_MEMORIES = int(os.environ.get("REME_REFINE_OUTPUT_MAX_MEMORIES", "5"))


class RefineValidationError(Exception):
    pass


@dataclass(frozen=True)
class RefineAttemptResult:
    text: str
    events_jsonl: str = ""
    model: str = ""
    error: str = ""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def today_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d")


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return safe[:160] or "refine"


def _atomic_append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _claim_file(src: Path, dst: Path) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except FileNotFoundError:
        return False
    return True


def _iso_to_epoch(text: str) -> float:
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+0000"
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%S%z"))
    except ValueError:
        try:
            return dt.datetime.fromisoformat(text).timestamp()
        except ValueError:
            return 0.0


def _pid_exists(pid_value: Any) -> bool:
    try:
        pid = int(pid_value)
    except (TypeError, ValueError):
        return False
    return Path(f"/proc/{pid}").exists()


class RefineJobStore:
    def __init__(self, paths) -> None:
        self.paths = paths
        self._lock = threading.RLock()

    def enqueue_from_request(self, request: Any) -> Dict[str, Any]:
        payload = dict(request.payload)
        refine_id = _safe_name(request.request_id.replace("req_", "refine_", 1))
        job = {
            "schema_version": 1,
            "refine_id": refine_id,
            "request_id": request.request_id,
            "idempotency_key": request.idempotency_key,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "state": "queued",
            "attempt": 0,
            "max_attempts": int(payload.get("max_attempts", 3)),
            "project": request.project,
            "language": request.language,
            "client_id": request.client_id,
            "payload": payload,
            "write_request_ids": [],
            "last_error": "",
        }
        with self._lock:
            atomic_write_json(self.paths.async_refine_queued / f"{refine_id}.json", job)
        self.append_log({"event": "queued", "refine_id": refine_id, "request_id": request.request_id})
        return job

    def recover_running_jobs(self, stale_seconds: int = DEFAULT_RUNNING_STALE_SECONDS) -> None:
        with self._lock:
            for path in sorted(self.paths.async_refine_running.glob("*.json")):
                try:
                    job = read_json(path)
                except Exception:
                    continue
                heartbeat = str(job.get("worker_heartbeat_at") or job.get("updated_at") or "")
                age = time.time() - _iso_to_epoch(heartbeat)
                if _pid_exists(job.get("worker_pid")) and age < stale_seconds:
                    self.append_log(
                        {
                            "event": "running_job_still_owned",
                            "refine_id": job.get("refine_id", path.stem),
                            "worker_pid": job.get("worker_pid"),
                            "age_seconds": int(age),
                        }
                    )
                    continue
                job["state"] = "queued"
                job.pop("worker_pid", None)
                job.pop("worker_heartbeat_at", None)
                job["updated_at"] = now_iso()
                atomic_write_json(self.paths.async_refine_queued / path.name, job)
                path.unlink(missing_ok=True)
                self.append_log({"event": "recovered_running", "refine_id": job.get("refine_id", path.stem)})

    def claim_next(self) -> Optional[Tuple[Dict[str, Any], Path]]:
        with self._lock:
            for path in sorted(self.paths.async_refine_queued.glob("*.json")):
                target = self.paths.async_refine_running / path.name
                if not _claim_file(path, target):
                    continue
                job = read_json(target)
                job["state"] = "running"
                job["updated_at"] = now_iso()
                atomic_write_json(target, job)
                self.append_log({"event": "running", "refine_id": job["refine_id"]})
                return job, target
        return None

    def update_running(self, running_path: Path, job: Dict[str, Any], **updates: Any) -> None:
        with self._lock:
            job.update(updates)
            job["updated_at"] = now_iso()
            atomic_write_json(running_path, job)

    def finish(self, running_path: Path, job: Dict[str, Any], *, state: str, details: Optional[Dict[str, Any]] = None) -> None:
        details = details or {}
        refine_id = str(job.get("refine_id") or running_path.stem)
        self.write_async_result(job, state=state, details=details)
        self.append_log(
            {
                "event": "terminal",
                "state": state,
                "refine_id": refine_id,
                "request_id": job.get("request_id", ""),
                "attempt": job.get("attempt", 0),
                "write_request_ids": job.get("write_request_ids", []),
                "details": details,
            }
        )
        running_path.unlink(missing_ok=True)
        self.cleanup_tmp(refine_id)

    def write_async_result(self, job: Dict[str, Any], *, state: str, details: Optional[Dict[str, Any]] = None) -> None:
        request_id = str(job.get("request_id") or "")
        if not request_id:
            return
        payload = {
            "request_id": request_id,
            "refine_id": job.get("refine_id", ""),
            "state": state,
            "attempt": job.get("attempt", 0),
            "updated_at": now_iso(),
            "write_request_ids": job.get("write_request_ids", []),
            "details": details or {},
            "last_error": job.get("last_error", ""),
        }
        atomic_write_json(self.paths.result / f"{request_id}.async.json", payload)

    def cleanup_tmp(self, refine_id: str) -> None:
        tmp_root = self.paths.async_refine_tmp / _safe_name(refine_id)
        shutil.rmtree(tmp_root, ignore_errors=True)

    def append_log(self, payload: Dict[str, Any]) -> None:
        payload = {"created_at": now_iso(), **payload}
        with self._lock:
            _atomic_append_jsonl(self.paths.async_refine_logs / f"{today_text()}.jsonl", payload)


RunnerFn = Callable[[Dict[str, Any], int, str, Path], RefineAttemptResult]
UsageRecorderFn = Callable[[Dict[str, Any], int, str], Dict[str, Any]]


class AsyncRefineSupervisor:
    def __init__(
        self,
        store,
        *,
        runner: Optional[RunnerFn] = None,
        usage_recorder: Optional[UsageRecorderFn] = None,
        max_concurrency: Optional[int] = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self.store = store
        self.job_store = RefineJobStore(store.paths)
        self.runner = runner or run_codex_refine_attempt
        self.usage_recorder = usage_recorder or record_codex_exec_usage
        self.max_concurrency = max(1, int(max_concurrency or DEFAULT_MAX_CONCURRENCY))
        self.poll_interval_seconds = poll_interval_seconds
        self._executor: Optional[ThreadPoolExecutor] = None
        self._running: Dict[str, Tuple[Future, Path]] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

    def start(self) -> None:
        self.job_store.recover_running_jobs()
        self._executor = ThreadPoolExecutor(max_workers=self.max_concurrency, thread_name_prefix="reme-refine")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="reme-refine-supervisor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=False)
            self._executor = None

    def enqueue_request(self, request: Any) -> Dict[str, Any]:
        return self.job_store.enqueue_from_request(request)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            running = sorted(self._running.keys())
        return {
            "max_concurrency": self.max_concurrency,
            "running": running,
            "running_count": len(running),
            "queued_count": len(list(self.store.paths.async_refine_queued.glob("*.json"))),
            "running_cache_count": len(list(self.store.paths.async_refine_running.glob("*.json"))),
        }

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._reap_done()
            self._start_available()
            self._stop.wait(self.poll_interval_seconds)
        self._reap_done()

    def _start_available(self) -> None:
        with self._lock:
            capacity = self.max_concurrency - len(self._running)
        while capacity > 0 and not self._stop.is_set():
            claimed = self.job_store.claim_next()
            if claimed is None:
                return
            job, running_path = claimed
            if self._executor is None:
                return
            self.job_store.update_running(
                running_path,
                job,
                worker_pid=os.getpid(),
                worker_heartbeat_at=now_iso(),
            )
            future = self._executor.submit(self._run_job, job, running_path)
            with self._lock:
                self._running[job["refine_id"]] = (future, running_path)
                capacity = self.max_concurrency - len(self._running)

    def _reap_done(self) -> None:
        done: List[str] = []
        with self._lock:
            for refine_id, (future, _running_path) in self._running.items():
                if future.done():
                    done.append(refine_id)
        for refine_id in done:
            with self._lock:
                future, running_path = self._running.pop(refine_id)
            try:
                future.result()
            except Exception as exc:
                try:
                    job = read_json(running_path)
                except Exception:
                    job = {"refine_id": refine_id, "request_id": ""}
                self.job_store.write_async_result(
                    job,
                    state="failed",
                    details={"error": f"{exc.__class__.__name__}: {exc}"},
                )
                self.job_store.append_log(
                    {
                        "event": "worker_exception",
                        "state": "failed",
                        "refine_id": refine_id,
                        "error": f"{exc.__class__.__name__}: {exc}",
                        "traceback": traceback.format_exc(limit=8),
                    }
                )
                if not DEFAULT_KEEP_FAILED_ARTIFACTS:
                    running_path.unlink(missing_ok=True)
                    self.job_store.cleanup_tmp(refine_id)

    def _run_job(self, job: Dict[str, Any], running_path: Path) -> None:
        refine_id = job["refine_id"]
        max_attempts = int(job.get("max_attempts", 3))
        validation_error = ""
        valid_payload: Optional[Dict[str, Any]] = None
        tmp_root = self.store.paths.async_refine_tmp / _safe_name(refine_id)
        tmp_root.mkdir(parents=True, exist_ok=True)
        self._load_reconcile_candidates(job)

        for attempt in range(1, max_attempts + 1):
            self.job_store.update_running(
                running_path,
                job,
                state="running",
                attempt=attempt,
                worker_pid=os.getpid(),
                worker_heartbeat_at=now_iso(),
            )
            try:
                result = self.runner(job, attempt, validation_error, tmp_root)
            except Exception as exc:
                validation_error = f"{exc.__class__.__name__}: {exc}"
                job["last_error"] = validation_error
                self.job_store.append_log(
                    {
                        "event": "attempt_exception",
                        "refine_id": refine_id,
                        "attempt": attempt,
                        "error": validation_error,
                    }
                )
                continue
            if result.events_jsonl:
                self._record_usage(job, attempt, result.events_jsonl)
            if result.error:
                validation_error = result.error
                job["last_error"] = validation_error
                self.job_store.append_log({"event": "attempt_error", "refine_id": refine_id, "attempt": attempt, "error": result.error})
                continue
            try:
                valid_payload = validate_refine_output(job, result.text)
                break
            except RefineValidationError as exc:
                validation_error = str(exc)
                job["last_error"] = validation_error
                self.job_store.append_log({"event": "validation_failed", "refine_id": refine_id, "attempt": attempt, "error": validation_error})

        if valid_payload is None:
            self.job_store.finish(
                running_path,
                job,
                state="refine_failed",
                details={
                    "fallback_used": False,
                    "write_count": 0,
                    "error": validation_error or "refine produced no valid output",
                },
            )
            return

        try:
            write_request_ids = self._enqueue_writes(job, valid_payload)
        except Exception as exc:
            self.job_store.finish(
                running_path,
                job,
                state="failed",
                details={"fallback_used": False, "error": f"{exc.__class__.__name__}: {exc}"},
            )
            return
        job["write_request_ids"] = write_request_ids
        self.job_store.finish(
            running_path,
            job,
            state="write_enqueued" if write_request_ids else "no_store",
            details={"fallback_used": False, "write_count": len(write_request_ids)},
        )

    def _load_reconcile_candidates(self, job: Dict[str, Any]) -> None:
        if os.environ.get("REME_REFINE_RECONCILE_ENABLED", "true").strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            job["reconcile_candidates"] = []
            return
        runtime_worker = getattr(self.store, "runtime_worker", None)
        if runtime_worker is None:
            job["reconcile_candidates"] = []
            return
        query = build_reconcile_query(job)
        if not query:
            job["reconcile_candidates"] = []
            return
        try:
            search_result = runtime_worker.search(
                query=query,
                max_results=20,
                min_score=0.1,
                vector_weight=0.7,
                candidate_multiplier=5.0,
            )
            candidates = build_durable_reconcile_candidates(
                search_result.get("items", []),
                memory_dir=get_memory_dir(),
                max_candidates=5,
            )
        except Exception as exc:
            candidates = []
            self.job_store.append_log(
                {
                    "event": "reconcile_candidate_search_failed",
                    "refine_id": job.get("refine_id", ""),
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
        job["reconcile_candidates"] = candidates
        self.job_store.append_log(
            {
                "event": "reconcile_candidates_loaded",
                "refine_id": job.get("refine_id", ""),
                "candidate_count": len(candidates),
            }
        )

    def _record_usage(self, job: Dict[str, Any], attempt: int, events_jsonl: str) -> None:
        try:
            result = self.usage_recorder(job, attempt, events_jsonl)
            self.job_store.append_log(
                {
                    "event": "usage_recorded",
                    "refine_id": job.get("refine_id", ""),
                    "attempt": attempt,
                    "usage_result": result,
                }
            )
        except Exception as exc:
            self.job_store.append_log(
                {
                    "event": "usage_record_failed",
                    "refine_id": job.get("refine_id", ""),
                    "attempt": attempt,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )

    def _enqueue_writes(self, job: Dict[str, Any], payload: Dict[str, Any]) -> List[str]:
        request_ids: List[str] = []
        tags = list(job.get("payload", {}).get("tags") or [])
        tags.append("reme-refine")
        candidate_map = {
            str(candidate.get("candidate_id")): candidate
            for candidate in job.get("reconcile_candidates", [])
        }
        for item in payload.get("memories", []):
            if not item.get("should_store", False):
                continue
            confidence = str(item.get("confidence", "")).lower()
            if confidence == "low":
                continue
            action = str(item.get("action") or "create")
            target_candidate = candidate_map.get(str(item.get("target_candidate_id") or ""))
            reconcile = {"action": action}
            if action in {"overwrite", "merge"}:
                if target_candidate is None:
                    raise RefineValidationError(f"{action} selected without a valid target candidate")
                reconcile.update(
                    {
                        "target_path": target_candidate["path"],
                        "target_durable_idempotency_key": target_candidate["durable_idempotency_key"],
                    }
                )
            memory_json = build_structured_memory_json(job, item)
            lesson = render_memory_lesson(job, item, fallback_used=False)
            accepted = enqueue_write(
                lesson=lesson,
                task=str(job.get("payload", {}).get("task") or "ReMe async refine"),
                outcome=str(item.get("summary") or ""),
                tags=sorted(set(tags + list(item.get("tags", [])))),
                source_thread=str(job.get("payload", {}).get("owner_session_id") or job.get("payload", {}).get("thread_id") or ""),
                memory_json=memory_json,
                reconcile=reconcile,
                client_id="reme-refine",
                project=str(job.get("project") or "cling_glb"),
                language=str(job.get("language") or "zh"),
                paths=self.store.paths,
            )
            request_ids.append(accepted.request_id)
        return request_ids


def build_reconcile_query(job: Dict[str, Any]) -> str:
    payload = job.get("payload", {})
    parts = [
        str(payload.get("task") or ""),
        str(payload.get("scenario") or ""),
    ]
    parts.extend(str(item.get("text") or "") for item in payload.get("evidence", []))
    query = " ".join(" ".join(parts).split())
    return query[:4096]


def build_structured_memory_json(job: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    payload = job.get("payload", {})
    memory_type = str(item.get("memory_type") or "fact").strip().lower()
    summary = str(item.get("summary") or "").strip()
    subject = str(item.get("subject") or "").strip()
    reuse_condition = str(item.get("reuse_condition") or "").strip()
    tags = _normalize_tags(item.get("tags", []))
    aliases = _normalize_tags([memory_type, *tags])
    source_capture_id = str(
        payload.get("capture_id")
        or payload.get("parent_capture_id")
        or job.get("refine_id")
        or "reme-refine"
    )
    evidence_at = str(payload.get("evidence_at") or job.get("created_at") or now_iso())
    retrieval_surface = " ".join(
        part
        for part in [
            subject,
            summary,
            reuse_condition,
            memory_type,
            " ".join(aliases),
        ]
        if part
    )
    return {
        "topic": subject,
        "applicability": reuse_condition,
        "conclusions": [summary],
        "root_cause_patterns": [summary] if memory_type == "root_cause" else [],
        "solutions": [summary] if memory_type == "workflow" else [],
        "key_locations": {"files": [], "symbols": [], "errors": []},
        "aliases": aliases,
        "benchmark_names": [],
        "retrieval_surface": retrieval_surface,
        "confidence": str(item.get("confidence") or "medium"),
        "source_capture_id": source_capture_id,
        "evidence_at": evidence_at,
        "evidence_hashes": [str(value) for value in item.get("evidence_hashes", [])],
        "review_after": "",
        "supersedes": [],
    }


def build_refine_prompt(job: Dict[str, Any], attempt: int, previous_error: str) -> str:
    payload = job.get("payload", {})
    evidence = [
        {
            "evidence_id": item.get("evidence_id", ""),
            "reason": item.get("reason", ""),
            "line_range": item.get("line_range", []),
            "evidence_hash": item.get("evidence_hash", ""),
            "text": item.get("text", ""),
        }
        for item in payload.get("evidence", [])
    ]
    prompt = {
        "task": "Extract durable ReMe memories from evidence only.",
        "rules": [
            "Use only the supplied evidence text.",
            "Do not infer facts that are not directly supported.",
            "Return strict JSON only, with no markdown.",
            "Every stored memory must reference one or more supplied evidence_hash values.",
            "Skip transient logs, temporary guesses, and low-signal status messages.",
            "Compare each proposed memory with the supplied durable candidates.",
            "Use overwrite when the new memory replaces the same durable conclusion.",
            "Use merge when both memories describe the same durable conclusion and should be combined.",
            "Use keep_both only when scopes differ or both conclusions remain independently useful.",
            "Use create when no candidate is substantially similar.",
            "For overwrite or merge, target_candidate_id must name one supplied candidate.",
        ],
        "output_schema": {
            "schema_version": 1,
            "memories": [
                {
                    "should_store": True,
                    "memory_type": "decision|workflow|preference|root_cause|fact",
                    "subject": "short stable subject",
                    "summary": "one concise durable memory",
                    "reuse_condition": "when this memory should be reused",
                    "confidence": "high|medium|low",
                    "evidence_hashes": ["sha256:..."],
                    "tags": ["optional-short-tag"],
                    "action": "create|overwrite|merge|keep_both",
                    "target_candidate_id": "candidate_N or empty",
                }
            ],
            "no_store_reason": "",
        },
        "attempt": attempt,
        "previous_validation_error": previous_error,
        "context": {
            "parent_capture_id": payload.get("parent_capture_id", ""),
            "task": payload.get("task", ""),
            "scenario": payload.get("scenario", ""),
        },
        "evidence": evidence,
        "durable_candidates": job.get("reconcile_candidates", []),
    }
    return json.dumps(prompt, ensure_ascii=False, indent=2, sort_keys=True)


def run_codex_refine_attempt(job: Dict[str, Any], attempt: int, previous_error: str, tmp_root: Path) -> RefineAttemptResult:
    prompt = build_refine_prompt(job, attempt, previous_error)
    prompt_path = tmp_root / f"attempt-{attempt:02d}-prompt.json"
    events_path = tmp_root / f"attempt-{attempt:02d}-events.jsonl"
    prompt_path.write_text(prompt, encoding="utf-8")
    cmd = ["codex", "exec", "--json", "--ephemeral", prompt]
    env = os.environ.copy()
    env["REME_REFINE_WORKER"] = "1"
    try:
        process = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            check=False,
            timeout=DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
        events_path.write_text(stdout, encoding="utf-8")
        return RefineAttemptResult(text="", events_jsonl=str(events_path), error=f"codex exec timeout after {DEFAULT_ATTEMPT_TIMEOUT_SECONDS}s")
    events_path.write_text(process.stdout, encoding="utf-8")
    if process.returncode != 0:
        return RefineAttemptResult(text="", events_jsonl=str(events_path), error=process.stderr.strip() or "codex exec failed")
    text = extract_codex_exec_text(process.stdout)
    if not text:
        return RefineAttemptResult(text="", events_jsonl=str(events_path), error="codex exec produced no assistant text")
    return RefineAttemptResult(text=text, events_jsonl=str(events_path))


def extract_codex_exec_text(events_jsonl: str) -> str:
    candidates: List[str] = []
    for raw in events_jsonl.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        candidates.extend(_collect_possible_text(event))
    json_candidates = [item for item in candidates if "{" in item and "}" in item]
    return (json_candidates[-1] if json_candidates else (candidates[-1] if candidates else "")).strip()


def _collect_possible_text(value: Any) -> List[str]:
    results: List[str] = []
    if isinstance(value, str):
        if value.strip():
            results.append(value)
        return results
    if isinstance(value, list):
        for item in value:
            results.extend(_collect_possible_text(item))
        return results
    if not isinstance(value, dict):
        return results
    if value.get("type") == "turn.completed" and isinstance(value.get("usage"), dict):
        return results
    for key in ("message", "text", "output_text", "content", "response", "result", "summary"):
        if key in value:
            results.extend(_collect_possible_text(value[key]))
    return results


def validate_refine_output(job: Dict[str, Any], raw_text: str) -> Dict[str, Any]:
    payload = _parse_json_object(raw_text)
    try:
        schema_version = int(payload.get("schema_version", 0))
    except (TypeError, ValueError) as exc:
        raise RefineValidationError("schema_version must be integer 1") from exc
    if schema_version != 1:
        raise RefineValidationError("schema_version must be 1")
    memories = payload.get("memories")
    if not isinstance(memories, list):
        raise RefineValidationError("memories must be a list")
    if len(memories) > MAX_REFINE_OUTPUT_MEMORIES:
        raise RefineValidationError(f"memories exceeds max count {MAX_REFINE_OUTPUT_MEMORIES}")
    known_hashes = {item["evidence_hash"] for item in job.get("payload", {}).get("evidence", [])}
    known_candidate_ids = {
        str(item.get("candidate_id"))
        for item in job.get("reconcile_candidates", [])
        if item.get("candidate_id")
    }
    normalized = []
    for idx, item in enumerate(memories, start=1):
        if not isinstance(item, dict):
            raise RefineValidationError(f"memory[{idx}] must be an object")
        raw_should_store = item.get("should_store", False)
        if not isinstance(raw_should_store, bool):
            raise RefineValidationError(f"memory[{idx}].should_store must be boolean")
        should_store = raw_should_store
        evidence_hashes = item.get("evidence_hashes") or []
        action = str(item.get("action") or "create").strip().lower()
        target_candidate_id = str(item.get("target_candidate_id") or "").strip()
        if should_store:
            if "action" not in item:
                raise RefineValidationError(f"memory[{idx}].action is required")
            for key in ("memory_type", "subject", "summary", "reuse_condition", "confidence"):
                if not str(item.get(key, "")).strip():
                    raise RefineValidationError(f"memory[{idx}].{key} is required")
            if str(item.get("confidence", "")).strip().lower() not in {"high", "medium", "low"}:
                raise RefineValidationError(f"memory[{idx}].confidence must be high, medium, or low")
            if not isinstance(evidence_hashes, list) or not evidence_hashes:
                raise RefineValidationError(f"memory[{idx}].evidence_hashes must be a non-empty list")
            unknown = sorted(set(str(value) for value in evidence_hashes) - known_hashes)
            if unknown:
                raise RefineValidationError(f"memory[{idx}] references unknown evidence hashes: {unknown}")
            if action not in {"create", "overwrite", "merge", "keep_both"}:
                raise RefineValidationError(f"memory[{idx}].action is unsupported: {action}")
            if action in {"overwrite", "merge"} and target_candidate_id not in known_candidate_ids:
                raise RefineValidationError(
                    f"memory[{idx}].target_candidate_id must reference a supplied candidate for {action}"
                )
            if target_candidate_id and target_candidate_id not in known_candidate_ids:
                raise RefineValidationError(f"memory[{idx}] references unknown candidate: {target_candidate_id}")
        normalized.append(
            {
                "should_store": should_store,
                "memory_type": str(item.get("memory_type", "")).strip(),
                "subject": str(item.get("subject", "")).strip()[:200],
                "summary": str(item.get("summary", "")).strip()[:2000],
                "reuse_condition": str(item.get("reuse_condition", "")).strip()[:1000],
                "confidence": str(item.get("confidence", "")).strip().lower() or "medium",
                "evidence_hashes": [str(value) for value in evidence_hashes],
                "tags": _normalize_tags(item.get("tags", [])),
                "action": action,
                "target_candidate_id": target_candidate_id,
            }
        )
    return {"schema_version": 1, "memories": normalized, "no_store_reason": str(payload.get("no_store_reason", "")).strip()}


def _parse_json_object(raw_text: str) -> Dict[str, Any]:
    text = raw_text.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RefineValidationError(f"output JSON parse failed: {exc}") from exc
    if not isinstance(value, dict):
        raise RefineValidationError("output must be a JSON object")
    return value


def _normalize_tags(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    tags = []
    for item in value:
        text = str(item).strip()
        if text and len(text) <= 64 and text not in tags:
            tags.append(text)
    return tags[:16]


def build_fallback_refine_output(job: Dict[str, Any], last_error: str) -> Dict[str, Any]:
    evidence = job.get("payload", {}).get("evidence", [])
    if evidence and all(str(item.get("reason", "")) == "fallback-tail" for item in evidence):
        reason = "fallback-tail evidence is not durable; refine fallback is disabled"
    else:
        reason = f"refine failed without durable fallback: {last_error}"
    return {
        "schema_version": 1,
        "memories": [],
        "no_store_reason": reason[:500],
    }


def render_memory_lesson(job: Dict[str, Any], item: Dict[str, Any], *, fallback_used: bool) -> str:
    payload = job.get("payload", {})
    evidence_hashes = ", ".join(item.get("evidence_hashes", []))
    prefix = "[ReMe async refine{}]".format(" fallback" if fallback_used else "")
    lines = [
        f"{prefix} {item.get('subject', '').strip()}",
        f"- type: {item.get('memory_type', '')}",
        f"- summary: {item.get('summary', '')}",
        f"- reuse: {item.get('reuse_condition', '')}",
        f"- confidence: {item.get('confidence', '')}",
        f"- parent_capture_id: {payload.get('parent_capture_id', '')}",
        f"- evidence_hashes: {evidence_hashes}",
    ]
    return "\n".join(line for line in lines if line.strip())


def record_codex_exec_usage(job: Dict[str, Any], attempt: int, events_jsonl: str) -> Dict[str, Any]:
    if not Path(events_jsonl).is_file():
        return {"status": "skipped", "reason": "events_jsonl_missing"}
    if not USAGE_RECORDER.is_file():
        return {"status": "skipped", "reason": "usage_recorder_missing", "path": str(USAGE_RECORDER)}
    event_id = f"{job.get('refine_id', 'refine')}-attempt-{attempt}"
    cmd = [
        sys.executable,
        str(USAGE_RECORDER),
        "--events-jsonl",
        events_jsonl,
        "--event-id",
        event_id,
        "--cwd",
        str(CODEX_HOME),
        "--json",
    ]
    process = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip() or "usage recorder failed")
    if not process.stdout.strip():
        return {"status": "recorded"}
    return json.loads(process.stdout)
