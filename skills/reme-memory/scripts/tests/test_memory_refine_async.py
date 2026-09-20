#!/usr/bin/env python3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flush_adapter import handle_flush_request
from memory_bus_client import enqueue_query, enqueue_refine, enqueue_status, enqueue_write, wait_for_result
from memory_bus_io import atomic_write_json
from memory_daemon import MemoryDaemon
from memory_request_store import MemoryRequestStore
from memory_bus_client import result_path
from refine_adapter import handle_refine_request
from refine_async import RefineAttemptResult, RefineValidationError, build_fallback_refine_output, validate_refine_output
from status_adapter import handle_status_request


SCRIPT_DIR = Path(__file__).resolve().parents[1]
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))


def _write_initial_memory(memory_dir: Path) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "2026-04-20.md").write_text(
        """# Memory

- packet-processing HostSync timeout 调试：
  出现确认竞争时，先补时间日志，再调整重试。
""",
        encoding="utf-8",
    )


def _wait_for(predicate, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition timed out")


class MemoryRefineAsyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bus_root = Path(tempfile.mkdtemp(prefix="reme_refine_bus_", dir="/tmp"))
        self.reme_root = Path(tempfile.mkdtemp(prefix="reme_refine_reme_", dir="/tmp"))
        self.metrics_root = Path(tempfile.mkdtemp(prefix="reme_refine_metrics_", dir="/tmp"))
        _write_initial_memory(self.reme_root / "memory")

        self.saved_env = os.environ.copy()
        os.environ["REME_BUS_ROOT"] = str(self.bus_root)
        os.environ["REME_WORKDIR"] = str(self.reme_root)
        os.environ["REME_HOME"] = str(self.reme_root / "reme-home")
        os.environ["REME_RUNTIME_ENV_FILE"] = str(self.reme_root / "test-runtime.env")
        os.environ["REME_VECTOR_ENABLED"] = "false"
        os.environ["REME_FTS_ENABLED"] = "true"
        os.environ["REME_REFINE_MAX_CONCURRENCY"] = "2"
        os.environ["REME_REFINE_RECONCILE_ENABLED"] = "false"
        os.environ["REME_MEMORY_METRICS_DIR"] = str(self.metrics_root)
        os.environ["REME_QUERY_WAIT_TIMEOUT_SECONDS"] = "60"

        self.usage_records = []
        self.store = MemoryRequestStore(daemon_id="daemon-refine-test")
        self._start_daemon()

    def _start_daemon(self) -> None:
        self.daemon = MemoryDaemon(store=self.store, poll_interval_seconds=0.03)
        self.daemon.refine_supervisor.runner = self._valid_runner
        self.daemon.refine_supervisor.usage_recorder = self._record_usage
        self.daemon.register_handler("memory_refine", handle_refine_request)
        self.daemon.register_handler("memory_write", self._handle_local_write)
        self.daemon.register_handler("memory_query", self._handle_local_query)
        self.daemon.register_handler("memory_status", handle_status_request)
        self.daemon.register_handler("memory_flush", handle_flush_request)
        self.thread = threading.Thread(target=self.daemon.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        try:
            self.daemon.stop()
        except Exception:
            pass
        self.thread.join(timeout=2)
        os.environ.clear()
        os.environ.update(self.saved_env)
        shutil.rmtree(self.bus_root, ignore_errors=True)
        shutil.rmtree(self.reme_root, ignore_errors=True)
        shutil.rmtree(self.metrics_root, ignore_errors=True)

    def _handle_local_write(self, claimed, store) -> None:
        payload = claimed.request.payload
        memory_dir = self.reme_root / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        target = memory_dir / f"{datetime.now().strftime('%Y-%m-%d')}.md"
        content = target.read_text(encoding="utf-8") if target.exists() else "# Memory\n\n"
        lesson = payload["lesson"]
        if lesson not in content:
            content += f"- {lesson}\n"
            target.write_text(content, encoding="utf-8")
        store.complete_request(claimed, state="stored", phase="indexed")

    def _handle_local_query(self, claimed, store) -> None:
        payload = {
            "request_id": claimed.request.request_id,
            "request_type": claimed.request.request_type,
            "status": "answered",
            "query": claimed.request.payload["query"],
            "items": [
                {
                    "path": str(self.reme_root / "memory/2026-04-20.md"),
                    "score": 1.0,
                    "snippet": "packet-processing HostSync timeout 调试",
                }
            ],
        }
        atomic_write_json(result_path(store.paths, claimed.request.request_id), payload)
        store.complete_request(claimed, state="answered")

    def _record_usage(self, job, attempt, events_jsonl):
        self.usage_records.append({"refine_id": job["refine_id"], "attempt": attempt, "events_jsonl": events_jsonl})
        return {"status": "recorded", "attempt": attempt}

    def _events_file(self, tmp_root: Path, attempt: int) -> str:
        path = tmp_root / f"attempt-{attempt:02d}-events.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": f"fake-{attempt}"}),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {
                                "input_tokens": 10,
                                "cached_input_tokens": 0,
                                "output_tokens": 5,
                                "reasoning_output_tokens": 0,
                                "total_tokens": 15,
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return str(path)

    def _valid_runner(self, job, attempt, previous_error, tmp_root):
        evidence_hash = job["payload"]["evidence"][0]["evidence_hash"]
        payload = {
            "schema_version": 1,
            "memories": [
                {
                    "should_store": True,
                    "memory_type": "workflow",
                    "subject": f"async refined {job['refine_id']}",
                    "summary": f"async refined memory for {job['refine_id']}",
                    "reuse_condition": "when async refine is tested",
                    "confidence": "high",
                    "evidence_hashes": [evidence_hash],
                    "tags": ["async-refine-test"],
                    "action": "create",
                    "target_candidate_id": "",
                }
            ],
            "no_store_reason": "",
        }
        return RefineAttemptResult(text=json.dumps(payload, ensure_ascii=False), events_jsonl=self._events_file(tmp_root, attempt))

    def _refine_payload(self, name: str = "one") -> dict:
        return {
            "parent_capture_id": f"cap-{name}",
            "thread_id": f"thread-{name}",
            "owner_session_id": f"owner-{name}",
            "task": f"Refine {name}",
            "scenario": "unit-test",
            "source_excerpt_hash": f"sha256:excerpt-{name}",
            "source_transcript_path": "/should/not/be/prompted.jsonl",
            "evidence": [
                {
                    "evidence_id": f"ev-{name}",
                    "reason": "decision",
                    "line_range": [10, 12],
                    "evidence_hash": f"sha256:evidence-{name}",
                    "text": f"用户确认 {name} 这个流程需要长期记忆。",
                }
            ],
        }

    def _memory_text(self) -> str:
        return "\n".join(path.read_text(encoding="utf-8") for path in (self.reme_root / "memory").glob("*.md"))

    def _async_result(self, request_id: str) -> dict:
        path = self.bus_root / "result" / f"{request_id}.async.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _assert_async_cache_clean(self) -> None:
        paths = self.store.paths
        self.assertFalse(list(paths.async_refine_queued.glob("*.json")))
        self.assertFalse(list(paths.async_refine_running.glob("*.json")))
        tmp_entries = [item for item in paths.async_refine_tmp.glob("*") if item.exists()]
        self.assertFalse(tmp_entries)

    def test_single_refine_enqueues_memory_write_and_records_usage(self) -> None:
        accepted = enqueue_refine(self._refine_payload("single"), request_id="req_refine_single_001")

        _wait_for(lambda: "async refined memory for refine_refine_single_001" in self._memory_text())

        status_path = self.bus_root / "result" / f"{accepted.request_id}.status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "accepted_async")
        self.assertEqual(status["phase"], "async_enqueued")
        status_request = enqueue_status(accepted.request_id)
        status_result = wait_for_result(status_request.request_id, timeout_seconds=5)
        self.assertEqual(status_result["async_result"]["state"], "write_enqueued")
        self.assertTrue(status_result["async_result"]["write_request_ids"])
        self.assertTrue(self.usage_records)
        self._assert_async_cache_clean()

    def test_two_refines_run_concurrently(self) -> None:
        active = 0
        max_active = 0
        lock = threading.Lock()

        def concurrent_runner(job, attempt, previous_error, tmp_root):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.4)
                return self._valid_runner(job, attempt, previous_error, tmp_root)
            finally:
                with lock:
                    active -= 1

        self.daemon.refine_supervisor.runner = concurrent_runner
        enqueue_refine(self._refine_payload("a"), request_id="req_refine_parallel_001")
        enqueue_refine(self._refine_payload("b"), request_id="req_refine_parallel_002")

        _wait_for(lambda: "async refined memory for refine_refine_parallel_001" in self._memory_text())
        _wait_for(lambda: "async refined memory for refine_refine_parallel_002" in self._memory_text())
        self.assertGreaterEqual(max_active, 2)
        self._assert_async_cache_clean()

    def test_search_is_not_blocked_while_refine_runs(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def blocking_runner(job, attempt, previous_error, tmp_root):
            started.set()
            release.wait(timeout=5)
            return self._valid_runner(job, attempt, previous_error, tmp_root)

        self.daemon.refine_supervisor.runner = blocking_runner
        enqueue_refine(self._refine_payload("search"), request_id="req_refine_search_001")
        self.assertTrue(started.wait(timeout=12))

        query = enqueue_query("HostSync timeout", max_results=3)
        result = wait_for_result(query.request_id, timeout_seconds=5)
        self.assertEqual(result["status"], "answered")
        self.assertTrue(result["items"])
        release.set()
        _wait_for(lambda: "async refined memory for refine_refine_search_001" in self._memory_text())

    def test_write_is_not_blocked_while_refine_runs(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def blocking_runner(job, attempt, previous_error, tmp_root):
            started.set()
            release.wait(timeout=5)
            return self._valid_runner(job, attempt, previous_error, tmp_root)

        self.daemon.refine_supervisor.runner = blocking_runner
        enqueue_refine(self._refine_payload("write"), request_id="req_refine_write_001")
        self.assertTrue(started.wait(timeout=12))

        enqueue_write("manual write during async refine", request_id="req_refine_manual_write_001")
        _wait_for(lambda: "manual write during async refine" in self._memory_text())
        release.set()
        _wait_for(lambda: "async refined memory for refine_refine_write_001" in self._memory_text())

    def test_invalid_json_retries_then_records_failure_without_fallback(self) -> None:
        attempts = []

        def invalid_runner(job, attempt, previous_error, tmp_root):
            attempts.append((attempt, previous_error))
            return RefineAttemptResult(text="not json", events_jsonl=self._events_file(tmp_root, attempt))

        self.daemon.refine_supervisor.runner = invalid_runner
        enqueue_refine(self._refine_payload("fallback"), request_id="req_refine_fallback_001")

        _wait_for(lambda: self._async_result("req_refine_fallback_001").get("state") == "refine_failed")
        self.assertEqual([item[0] for item in attempts], [1, 2, 3])
        self.assertNotIn("[ReMe async refine fallback]", self._memory_text())
        self._assert_async_cache_clean()

    def test_bad_schema_json_retries_then_records_failure_without_fallback(self) -> None:
        attempts = []

        def bad_schema_runner(job, attempt, previous_error, tmp_root):
            attempts.append((attempt, previous_error))
            return RefineAttemptResult(
                text=json.dumps({"schema_version": "bad", "memories": []}),
                events_jsonl=self._events_file(tmp_root, attempt),
            )

        self.daemon.refine_supervisor.runner = bad_schema_runner
        enqueue_refine(self._refine_payload("bad-schema"), request_id="req_refine_bad_schema_001")

        _wait_for(lambda: self._async_result("req_refine_bad_schema_001").get("state") == "refine_failed")
        self.assertEqual([item[0] for item in attempts], [1, 2, 3])
        self.assertNotIn("[ReMe async refine fallback]", self._memory_text())
        self._assert_async_cache_clean()

    def test_prose_wrapped_json_retries_then_records_failure_without_fallback(self) -> None:
        attempts = []

        def prose_runner(job, attempt, previous_error, tmp_root):
            attempts.append((attempt, previous_error))
            evidence_hash = job["payload"]["evidence"][0]["evidence_hash"]
            payload = {
                "schema_version": 1,
                "memories": [
                    {
                        "should_store": True,
                        "memory_type": "fact",
                        "subject": "wrapped",
                        "summary": "wrapped json should be rejected",
                        "reuse_condition": "never",
                        "confidence": "high",
                        "evidence_hashes": [evidence_hash],
                        "action": "create",
                        "target_candidate_id": "",
                    }
                ],
            }
            return RefineAttemptResult(text="Here is JSON:\n" + json.dumps(payload), events_jsonl=self._events_file(tmp_root, attempt))

        self.daemon.refine_supervisor.runner = prose_runner
        enqueue_refine(self._refine_payload("wrapped"), request_id="req_refine_wrapped_001")

        _wait_for(lambda: self._async_result("req_refine_wrapped_001").get("state") == "refine_failed")
        self.assertEqual([item[0] for item in attempts], [1, 2, 3])
        self.assertNotIn("wrapped json should be rejected", self._memory_text())
        self._assert_async_cache_clean()

    def test_runner_exception_retries_then_records_failure_without_fallback(self) -> None:
        attempts = []

        def raising_runner(job, attempt, previous_error, tmp_root):
            attempts.append((attempt, previous_error))
            raise TimeoutError("fake timeout")

        self.daemon.refine_supervisor.runner = raising_runner
        enqueue_refine(self._refine_payload("exception"), request_id="req_refine_exception_001")

        _wait_for(lambda: self._async_result("req_refine_exception_001").get("state") == "refine_failed")
        self.assertEqual([item[0] for item in attempts], [1, 2, 3])
        self.assertNotIn("[ReMe async refine fallback]", self._memory_text())
        self._assert_async_cache_clean()

    def test_write_back_uses_explicit_store_paths_not_current_env(self) -> None:
        wrong_bus = Path(tempfile.mkdtemp(prefix="reme_refine_wrong_bus_", dir="/tmp"))
        self.addCleanup(lambda: shutil.rmtree(wrong_bus, ignore_errors=True))
        os.environ["REME_BUS_ROOT"] = str(wrong_bus)

        enqueue_refine(
            self._refine_payload("explicit-paths"),
            request_id="req_refine_explicit_paths_001",
            paths=self.store.paths,
        )

        _wait_for(lambda: "async refined memory for refine_refine_explicit_paths_001" in self._memory_text())
        wrong_write_dir = wrong_bus / "inbox" / "write"
        self.assertFalse(list(wrong_write_dir.glob("*.json")) if wrong_write_dir.exists() else [])
        self._assert_async_cache_clean()

    def test_too_many_output_memories_retries_then_records_failure_without_fallback(self) -> None:
        attempts = []

        def fanout_runner(job, attempt, previous_error, tmp_root):
            attempts.append((attempt, previous_error))
            evidence_hash = job["payload"]["evidence"][0]["evidence_hash"]
            payload = {
                "schema_version": 1,
                "memories": [
                    {
                        "should_store": True,
                        "memory_type": "fact",
                        "subject": f"fanout-{idx}",
                        "summary": f"fanout memory {idx}",
                        "reuse_condition": "never",
                        "confidence": "high",
                        "evidence_hashes": [evidence_hash],
                        "action": "create",
                        "target_candidate_id": "",
                    }
                    for idx in range(8)
                ],
            }
            return RefineAttemptResult(text=json.dumps(payload), events_jsonl=self._events_file(tmp_root, attempt))

        self.daemon.refine_supervisor.runner = fanout_runner
        enqueue_refine(self._refine_payload("fanout"), request_id="req_refine_fanout_001")

        _wait_for(lambda: self._async_result("req_refine_fanout_001").get("state") == "refine_failed")
        self.assertEqual([item[0] for item in attempts], [1, 2, 3])
        self.assertNotIn("fanout memory", self._memory_text())
        self._assert_async_cache_clean()

    def test_fallback_tail_evidence_does_not_store_in_fallback(self) -> None:
        payload = build_fallback_refine_output(
            {
                "payload": {
                    "task": "low signal",
                    "evidence": [
                        {
                            "reason": "fallback-tail",
                            "text": "ordinary low-signal tail",
                            "evidence_hash": "sha256:tail",
                        }
                    ],
                }
            },
            "validation failed",
        )
        self.assertEqual(payload["memories"], [])
        self.assertIn("fallback-tail", payload["no_store_reason"])

    def test_reconcile_action_must_explicitly_select_known_candidate(self) -> None:
        job = {
            "payload": {
                "evidence": [{"evidence_hash": "sha256:evidence"}],
            },
            "reconcile_candidates": [{"candidate_id": "candidate_1"}],
        }
        item = {
            "should_store": True,
            "memory_type": "workflow",
            "subject": "same workflow",
            "summary": "merge the refined workflow",
            "reuse_condition": "when the workflow recurs",
            "confidence": "high",
            "evidence_hashes": ["sha256:evidence"],
            "action": "merge",
            "target_candidate_id": "candidate_1",
        }
        payload = validate_refine_output(
            job,
            json.dumps({"schema_version": 1, "memories": [item]}),
        )
        self.assertEqual(payload["memories"][0]["action"], "merge")
        del item["action"]
        with self.assertRaises(RefineValidationError):
            validate_refine_output(
                job,
                json.dumps({"schema_version": 1, "memories": [item]}),
            )

    def test_merge_action_enqueues_structured_targeted_write(self) -> None:
        job = {
            "refine_id": "refine_merge",
            "created_at": "2026-07-24T12:00:00+08:00",
            "project": "cling_packet",
            "language": "zh",
            "payload": {
                "capture_id": "cap_merge",
                "evidence_at": "2026-07-24T11:30:00+08:00",
                "task": "merge task",
                "thread_id": "thread-merge",
                "evidence": [{"evidence_hash": "sha256:evidence"}],
                "tags": [],
            },
            "reconcile_candidates": [
                {
                    "candidate_id": "candidate_1",
                    "path": "/tmp/memory/2026-07-23.md",
                    "durable_idempotency_key": "write_target",
                }
            ],
        }
        payload = {
            "memories": [
                {
                    "should_store": True,
                    "memory_type": "workflow",
                    "subject": "merge subject",
                    "summary": "merge summary",
                    "reuse_condition": "when merging",
                    "confidence": "high",
                    "evidence_hashes": ["sha256:evidence"],
                    "tags": [],
                    "action": "merge",
                    "target_candidate_id": "candidate_1",
                }
            ]
        }
        with patch("refine_async.enqueue_write", return_value=SimpleNamespace(request_id="req_merge")) as enqueue:
            request_ids = self.daemon.refine_supervisor._enqueue_writes(job, payload)
        self.assertEqual(request_ids, ["req_merge"])
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(kwargs["reconcile"]["action"], "merge")
        self.assertEqual(kwargs["reconcile"]["target_durable_idempotency_key"], "write_target")
        self.assertEqual(kwargs["memory_json"]["evidence_at"], "2026-07-24T11:30:00+08:00")

    def test_stop_restart_does_not_duplicate_running_refine(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def blocking_runner(job, attempt, previous_error, tmp_root):
            started.set()
            release.wait(timeout=5)
            return self._valid_runner(job, attempt, previous_error, tmp_root)

        self.daemon.refine_supervisor.runner = blocking_runner
        enqueue_refine(self._refine_payload("restart"), request_id="req_refine_restart_001")
        self.assertTrue(started.wait(timeout=12))

        self.daemon.stop()
        self.thread.join(timeout=2)
        self._start_daemon()
        release.set()

        _wait_for(lambda: "async refined memory for refine_refine_restart_001" in self._memory_text())
        text = self._memory_text()
        self.assertEqual(text.count("async refined memory for refine_refine_restart_001"), 1)
        self._assert_async_cache_clean()


if __name__ == "__main__":
    unittest.main(verbosity=2)
