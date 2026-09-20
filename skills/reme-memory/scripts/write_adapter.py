#!/usr/bin/env python3
import os
import json
import subprocess
from pathlib import Path
from typing import Callable, Optional

from daemon_trace import current_trace, step
from runtime_worker import RuntimeWorkerTimeout

SCRIPT_DIR = Path(__file__).resolve().parent
STORE_SCRIPT = SCRIPT_DIR / "store_memory.py"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
USER_ROOT = Path(os.environ.get("CODEX_USER_HOME", str(CODEX_HOME.parent)))
REME_HOME = Path(os.environ.get("REME_HOME", str(USER_ROOT / ".local/share/reme")))
REME_PYTHON = os.environ.get("REME_PYTHON", str(REME_HOME / ".venv/bin/python"))


class RetryableWriteError(Exception):
    pass


class PartialWriteCommittedError(Exception):
    pass


def _store_error_is_retryable(error_text: str) -> bool:
    lowered = error_text.lower()
    if "error code: 429" in lowered or "余额不足" in error_text or "无可用资源包" in error_text:
        return False
    retryable_markers = (
        "overloaded_error",
        "TimeoutError",
        "APITimeoutError",
        "APIConnectionError",
        "summary_memory_timeout",
        "timed out",
    )
    return any(marker.lower() in lowered for marker in retryable_markers)


def _run_store_backend(payload: dict, env: Optional[dict] = None) -> dict:
    lesson = payload["lesson"]
    language = os.environ.get("REME_BUS_LANGUAGE", "zh")
    cmd = [
        REME_PYTHON,
        str(STORE_SCRIPT),
        "--note",
        lesson,
        "--language",
        language,
    ]
    task = str(payload.get("task") or "")
    outcome = str(payload.get("outcome") or "")
    source_thread = str(payload.get("source_thread") or "")
    if task:
        cmd.extend(["--task", task])
    if outcome:
        cmd.extend(["--outcome", outcome])
    if source_thread:
        cmd.extend(["--source-thread", source_thread])
    for tag in payload.get("tags") or []:
        cmd.extend(["--tag", str(tag)])
    memory_json = payload.get("memory_json")
    if memory_json is not None:
        cmd.extend(["--memory-json", json.dumps(memory_json, ensure_ascii=False, sort_keys=True)])
    reconcile = payload.get("reconcile") or {}
    action = str(reconcile.get("action") or "")
    if action:
        cmd.extend(["--reconcile-action", action])
    target_path = str(reconcile.get("target_path") or "")
    if target_path:
        cmd.extend(["--target-path", target_path])
    target_key = str(reconcile.get("target_durable_idempotency_key") or "")
    if target_key:
        cmd.extend(["--target-key", target_key])
    process = subprocess.run(
        cmd,
        env=env or os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode == 0:
        message = process.stdout.strip()
        parsed = {}
        if message:
            try:
                parsed = json.loads(message)
            except json.JSONDecodeError:
                parsed = {}
        return {
            **parsed,
            "committed": True,
            "backend": parsed.get("backend", "store_subprocess"),
            "message": message,
            "indexed": False,
        }

    stderr = process.stderr.strip()
    if _store_error_is_retryable(stderr):
        raise RetryableWriteError(stderr or process.stdout.strip())
    raise RuntimeError(stderr or process.stdout.strip() or "write backend failed")


def handle_write_request(
    claimed,
    store,
    backend: Optional[Callable[[dict], dict]] = None,
    index_backend: Optional[Callable[[], dict]] = None,
) -> None:
    request = claimed.request
    current_status = store.read_status(request.request_id) or {}
    current_attempt = int(current_status.get("attempt", 0))
    store.archive_raw_request(request)
    store.update_phase(request.request_id, state="processing", phase="raw_archived", attempt=current_attempt)

    commit_metadata = store.get_commit_metadata(request.idempotency_key)
    committed_result = commit_metadata.get("metadata", {}) if commit_metadata is not None else {}
    if commit_metadata is not None:
        indexed = bool(committed_result.get("indexed"))
        if indexed:
            store.complete_request(claimed, state="stored", phase="indexed")
            return
        store.update_phase(request.request_id, state="processing", phase="apply_committed", attempt=current_attempt)
    else:
        backend_fn = backend or _run_store_backend
        store.update_phase(request.request_id, state="processing", phase="apply_started", attempt=current_attempt)
        try:
            trace = current_trace()
            with step(trace, "store_backend") as store_step:
                result = backend_fn(request.payload)
                if store_step is not None and isinstance(result, dict):
                    store_step["details"].update(
                        {
                            "backend": str(result.get("backend", "store_subprocess")),
                            "indexed": bool(result.get("indexed", False)),
                        }
                    )
        except PartialWriteCommittedError as exc:
            committed_result = {"partial_error": str(exc), "indexed": False}
            store.mark_write_committed(request, committed_result)
            store.update_phase(request.request_id, state="processing", phase="apply_committed", attempt=current_attempt)
        except RetryableWriteError as exc:
            store.schedule_retry(
                claimed,
                attempt=current_attempt + 1,
                delay_seconds=30,
                last_error=exc,
                phase="apply_started",
            )
            return
        else:
            already_indexed = bool(result.get("indexed"))
            store.mark_write_committed(request, result)
            committed_result = result
            if already_indexed:
                store.update_phase(request.request_id, state="processing", phase="indexed", attempt=current_attempt)
                store.complete_request(claimed, state="stored", phase="indexed")
                return
            store.update_phase(request.request_id, state="processing", phase="apply_committed", attempt=current_attempt)

    index_backend_fn = index_backend
    try:
        trace = current_trace()
        with step(trace, "index_upsert") as index_step:
            index_result = _run_write_index_backend(
                store=store,
                write_result=committed_result,
                index_backend=index_backend_fn,
            )
            if index_step is not None and isinstance(index_result, dict):
                index_step["details"].update(
                    {
                        "backend": str(index_result.get("backend", "")),
                        "operation": str(index_result.get("operation", "")),
                        "worker_generation": int(index_result.get("worker_generation", 0)),
                        "worker_warm": bool(index_result.get("worker_warm", False)),
                        "queue_wait_ms": int(index_result.get("queue_wait_ms", 0)),
                        "exec_ms": int(index_result.get("exec_ms", 0)),
                        "indexed_file_count": int(index_result.get("indexed_files", 0)),
                        "indexed_chunk_count": int(index_result.get("indexed_chunks", 0)),
                    }
                )
    except RetryableWriteError as exc:
        store.schedule_retry(
            claimed,
            attempt=current_attempt + 1,
            delay_seconds=30,
            last_error=exc,
            phase="apply_committed",
        )
        return
    except Exception:
        raise

    store.mark_write_committed(
        request,
        {
            **committed_result,
            **index_result,
            "indexed": True,
        },
    )
    store.update_phase(request.request_id, state="processing", phase="indexed", attempt=current_attempt)
    store.complete_request(claimed, state="stored", phase="indexed")


def main() -> None:
    raise SystemExit("write_adapter.py is a library module; use memory_daemon.py to drive it.")


if __name__ == "__main__":
    main()


def _run_write_index_backend(*, store, write_result: dict, index_backend=None) -> dict:
    if index_backend is not None:
        return index_backend()
    runtime_worker = getattr(store, "runtime_worker", None)
    memory_path = str(write_result.get("path") or "").strip()
    if runtime_worker is not None and memory_path:
        try:
            return runtime_worker.upsert_memory_file(memory_path)
        except RuntimeWorkerTimeout as exc:
            raise RetryableWriteError(str(exc)) from exc
    if not memory_path:
        raise RuntimeError("memory write committed without a target path for incremental indexing")
    raise RuntimeError("runtime worker unavailable for incremental memory indexing")
