#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from daemon_trace import current_trace, step
from memory_bus_client import result_path
from memory_bus_io import atomic_write_json
from runtime_worker import RuntimeWorkerError


SCRIPT_DIR = Path(__file__).resolve().parent
SEARCH_SCRIPT = SCRIPT_DIR / "search_memory.py"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
USER_ROOT = Path(os.environ.get("CODEX_USER_HOME", str(CODEX_HOME.parent)))
REME_HOME = Path(os.environ.get("REME_HOME", str(USER_ROOT / ".local/share/reme")))
REME_PYTHON = os.environ.get("REME_PYTHON", str(REME_HOME / ".venv/bin/python"))


def _run_search_backend(payload: dict, env: Optional[dict] = None) -> dict:
    cmd = [
        REME_PYTHON,
        str(SEARCH_SCRIPT),
        "--query",
        payload["query"],
        "--max-results",
        str(payload["max_results"]),
        "--min-score",
        str(payload["min_score"]),
        "--vector-weight",
        str(payload["vector_weight"]),
        "--candidate-multiplier",
        str(payload["candidate_multiplier"]),
    ]
    process = subprocess.run(
        cmd,
        env=env or os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip() or "search backend failed")
    return {
        "status": "answered",
        "backend": "search_subprocess",
        "items": json.loads(process.stdout.strip() or "[]"),
    }


def handle_query_request(claimed, store, backend: Optional[Callable[[dict], dict]] = None) -> None:
    payload = claimed.request.payload
    backend_fn = backend
    if backend_fn is None:
        runtime_worker = getattr(store, "runtime_worker", None)
        if runtime_worker is not None:
            backend_fn = lambda body: _run_worker_search_backend(runtime_worker, body)
        else:
            backend_fn = _run_search_backend
    trace = current_trace()
    with step(trace, "search_backend") as search_step:
        result = backend_fn(payload)
        if search_step is not None:
            search_step["details"].update(
                {
                    "backend": str(result.get("backend", "")),
                    "worker_generation": int(result.get("worker_generation", 0)),
                    "worker_warm": bool(result.get("worker_warm", False)),
                    "queue_wait_ms": int(result.get("queue_wait_ms", 0)),
                    "exec_ms": int(result.get("exec_ms", 0)),
                    "search_ms": int(result.get("search_ms", 0)),
                }
            )
    result_payload = {
        "request_id": claimed.request.request_id,
        "request_type": claimed.request.request_type,
        "status": result.get("status", "answered"),
        "items": result.get("items", []),
    }
    atomic_write_json(result_path(store.paths, claimed.request.request_id), result_payload)
    store.complete_request(claimed, state="answered")


def main() -> None:
    print("query_adapter.py is a library module; use memory_daemon.py to drive it.", file=sys.stderr)


if __name__ == "__main__":
    main()


def _run_worker_search_backend(runtime_worker, payload: dict) -> dict:
    try:
        return runtime_worker.search(
            query=payload["query"],
            max_results=int(payload["max_results"]),
            min_score=float(payload["min_score"]),
            vector_weight=float(payload["vector_weight"]),
            candidate_multiplier=float(payload["candidate_multiplier"]),
        )
    except RuntimeWorkerError:
        raise
