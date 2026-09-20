#!/usr/bin/env python3
import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
WORKER_SCRIPT = SCRIPT_DIR / "runtime_worker_server.py"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
USER_ROOT = Path(os.environ.get("CODEX_USER_HOME", str(CODEX_HOME.parent)))
REME_HOME = Path(os.environ.get("REME_HOME", str(USER_ROOT / ".local/share/reme")))
REME_PYTHON = os.environ.get("REME_PYTHON", str(REME_HOME / ".venv/bin/python"))


class RuntimeWorkerError(Exception):
    pass


class RuntimeWorkerTimeout(RuntimeWorkerError):
    pass


class RuntimeWorkerClient:
    def __init__(self) -> None:
        self.process: Optional[subprocess.Popen] = None
        self._stdout_queue: "queue.Queue[dict]" = queue.Queue()
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stderr_lines: Deque[str] = deque(maxlen=20)
        self._submit_lock = threading.Lock()
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(
            [REME_PYTHON, str(WORKER_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._generation += 1
        self._stdout_queue = queue.Queue()
        self._stdout_thread = threading.Thread(target=self._stdout_loop, daemon=True)
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            try:
                self._send_request(
                    {"task_id": "shutdown", "op": "shutdown", "kwargs": {}},
                    timeout_seconds=timeout_seconds,
                )
            except Exception:
                pass
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=timeout_seconds)
            except Exception:
                process.kill()
                process.wait(timeout=timeout_seconds)
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, stream_name, None)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        self.process = None

    def search(self, *, query: str, max_results: int, min_score: float, vector_weight: float, candidate_multiplier: float) -> dict:
        result = self.submit(
            "search",
            timeout_seconds=float(os.environ.get("REME_RUNTIME_WORKER_SEARCH_TIMEOUT_SECONDS", "30")),
            query=query,
            max_results=max_results,
            min_score=min_score,
            vector_weight=vector_weight,
            candidate_multiplier=candidate_multiplier,
        )
        return result

    def upsert_memory_file(self, memory_path: str) -> dict:
        result = self.submit(
            "upsert_memory_file",
            timeout_seconds=float(os.environ.get("REME_RUNTIME_WORKER_UPSERT_TIMEOUT_SECONDS", "30")),
            memory_path=memory_path,
        )
        return result

    def delete_memory_file(self, memory_path: str) -> dict:
        result = self.submit(
            "delete_memory_file",
            timeout_seconds=float(os.environ.get("REME_RUNTIME_WORKER_DELETE_TIMEOUT_SECONDS", "30")),
            memory_path=memory_path,
        )
        return result

    def sync_memory_files(self, *, upsert_paths: list[str], delete_paths: list[str]) -> dict:
        return self.submit(
            "sync_memory_files",
            timeout_seconds=float(os.environ.get("REME_RUNTIME_WORKER_SYNC_TIMEOUT_SECONDS", "600")),
            upsert_paths=upsert_paths,
            delete_paths=delete_paths,
        )

    def health_snapshot(self) -> dict:
        return self.submit("health", timeout_seconds=5.0)

    def submit(self, op: str, *, timeout_seconds: float, **kwargs: Any) -> dict:
        with self._submit_lock:
            self.start()
            task_id = f"{op}:{time.monotonic_ns()}"
            started = time.monotonic()
            payload = self._send_request(
                {"task_id": task_id, "op": op, "kwargs": kwargs},
                timeout_seconds=timeout_seconds,
            )
            result = payload["result"]
            result["worker_generation"] = self._generation
            result["backend"] = "runtime_worker_process"
            result["exec_ms"] = int((time.monotonic() - started) * 1000)
            result["queue_wait_ms"] = 0
            return result

    def _send_request(self, request: dict, *, timeout_seconds: float) -> dict:
        process = self.process
        if process is None or process.poll() is not None:
            raise RuntimeWorkerError("worker process not running")
        stdin = process.stdin
        if stdin is None:
            raise RuntimeWorkerError("worker stdin unavailable")
        stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        stdin.flush()
        try:
            payload = self._stdout_queue.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            self._replace_unhealthy_worker(reason=f"timeout op={request.get('op')}")
            raise RuntimeWorkerTimeout(f"worker timeout op={request.get('op')}") from exc
        if payload.get("task_id") != request.get("task_id"):
            self._replace_unhealthy_worker(reason="protocol_mismatch")
            raise RuntimeWorkerError("worker protocol mismatch")
        if not payload.get("ok", False):
            raise RuntimeWorkerError(
                payload.get("error", payload.get("error_type", "worker request failed"))
            )
        return payload

    def _replace_unhealthy_worker(self, *, reason: str) -> None:
        self._stderr_lines.append(f"worker_replaced reason={reason}")
        self.stop(timeout_seconds=1.0)

    def _stdout_loop(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                payload = {
                    "task_id": "",
                    "ok": False,
                    "error_type": "ProtocolError",
                    "error": f"invalid worker json: {stripped[:200]}",
                }
            self._stdout_queue.put(payload)

    def _stderr_loop(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            stripped = line.rstrip()
            if stripped:
                self._stderr_lines.append(stripped)
