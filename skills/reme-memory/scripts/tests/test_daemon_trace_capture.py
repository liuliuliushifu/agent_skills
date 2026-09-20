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
import unittest
from pathlib import Path

from capture_adapter import handle_capture_request
from capture_schema import normalize_capture_input
from memory_bus_client import enqueue_capture
from memory_daemon import MemoryDaemon
from memory_request_store import MemoryRequestStore
from structured_memory_llm import StructuredMemoryFallbackRequired
from write_adapter import RetryableWriteError


class DaemonTraceCaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bus_root = Path(tempfile.mkdtemp(prefix="reme_trace_bus_", dir="/tmp"))
        self.reme_root = Path(tempfile.mkdtemp(prefix="reme_trace_reme_", dir="/tmp"))
        self.logs_root = Path(tempfile.mkdtemp(prefix="reme_trace_logs_", dir="/tmp"))
        self.saved_env = os.environ.copy()
        os.environ["REME_BUS_ROOT"] = str(self.bus_root)
        os.environ["REME_WORKDIR"] = str(self.reme_root)
        os.environ["REME_VECTOR_ENABLED"] = "false"
        os.environ["REME_FTS_ENABLED"] = "true"
        os.environ["REME_DIAGNOSTICS_LOG_ROOT"] = str(self.logs_root)
        os.environ["REME_RUNTIME_LABEL"] = "test"
        self.store = MemoryRequestStore(daemon_id="daemon-trace")
        self.daemon = MemoryDaemon(store=self.store, poll_interval_seconds=0.01)

    def tearDown(self) -> None:
        try:
            self.daemon.stop()
        except Exception:
            pass
        try:
            self.store.release_daemon_lock()
        except Exception:
            pass
        os.environ.clear()
        os.environ.update(self.saved_env)
        shutil.rmtree(self.bus_root, ignore_errors=True)
        shutil.rmtree(self.reme_root, ignore_errors=True)
        shutil.rmtree(self.logs_root, ignore_errors=True)

    def _sample_capture(self) -> dict:
        return normalize_capture_input(
            {
                "project": "cling_glb",
                "task": "Diagnose capture trace",
                "session_summary": "daemon should write safe diagnostics only",
                "facts": [{"text": "fallback path should still emit trace"}],
                "decisions": [{"decision": "trace should avoid raw capture text"}],
                "aliases": ["trace"],
                "durable_identity": "trace capture path",
            },
            thread_id_override="thread-trace",
        )

    def _single_recent_trace(self) -> dict:
        recent_dir = self.logs_root / "recent" / "memory_capture"
        traces = sorted(recent_dir.glob("*.json"))
        self.assertEqual(len(traces), 1)
        return json.loads(traces[0].read_text(encoding="utf-8"))

    def test_fallback_capture_writes_safe_trace_and_summary(self) -> None:
        capture = self._sample_capture()
        self.daemon.register_handler(
            "memory_capture",
            lambda claimed, store: handle_capture_request(
                claimed,
                store,
                llm_backend=lambda _capture: (_ for _ in ()).throw(StructuredMemoryFallbackRequired("mock llm failure")),
                index_backend=lambda: {
                    "indexed_files": 1,
                    "indexed_chunks": 2,
                    "memory_total_bytes": 128,
                    "init_local_store_ms": 1,
                    "clear_all_ms": 0,
                    "enumerate_memory_files_ms": 1,
                    "chunking_ms_total": 2,
                    "upsert_ms_total": 3,
                    "close_local_store_ms": 1,
                    "vector_enabled": False,
                    "fts_enabled": True,
                    "chunk_tokens": 400,
                    "chunk_overlap": 80,
                    "embedding_model": "embedding-3",
                    "embedding_cache_enabled": True,
                    "embedding_max_batch_size": 10,
                },
                enable_durable_write=True,
            ),
        )
        enqueue_capture(capture=capture, request_id="req_trace_capture_001")
        self.daemon.start()
        try:
            handled = self.daemon.process_once()
        finally:
            self.daemon.stop()
        self.assertTrue(handled)

        trace = self._single_recent_trace()
        self.assertEqual(trace["request_type"], "memory_capture")
        self.assertNotIn("request_id", trace)
        self.assertEqual(trace["final_state"], "stored")
        self.assertEqual(trace["final_phase"], "memory_indexed")
        self.assertTrue(trace["fallback_used"])
        self.assertEqual(len(trace["daemon_attempts"]), 1)
        self.assertIn("approx_prompt_tokens_bucket", trace["diagnostic_sizes"])
        step_names = [item["name"] for item in trace["steps"]]
        self.assertIn("memory_generation", step_names)
        self.assertIn("fallback_generation", step_names)
        self.assertIn("render_memory_block", step_names)
        self.assertIn("index_upsert", step_names)
        trace_text = json.dumps(trace, ensure_ascii=False)
        self.assertNotIn(capture["session_summary"], trace_text)
        self.assertNotIn(capture["task"], trace_text)

        summary_path = self.logs_root / "request_summary.jsonl"
        self.assertTrue(summary_path.exists())
        summary_rows = [json.loads(line) for line in summary_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(summary_rows), 1)
        self.assertEqual(summary_rows[0]["request_type"], "memory_capture")
        self.assertTrue(summary_rows[0]["fallback_used"])

        attention_dir = self.logs_root / "attention" / "memory_capture"
        attention_traces = sorted(attention_dir.glob("*.json"))
        self.assertEqual(len(attention_traces), 1)

    def test_trace_accumulates_daemon_attempts_across_retry(self) -> None:
        capture = self._sample_capture()
        attempts = {"count": 0}

        def flaky_index():
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RetryableWriteError("index timeout")
            return {
                "indexed_files": 1,
                "indexed_chunks": 1,
                "memory_total_bytes": 64,
                "init_local_store_ms": 1,
                "clear_all_ms": 0,
                "enumerate_memory_files_ms": 1,
                "chunking_ms_total": 1,
                "upsert_ms_total": 1,
                "close_local_store_ms": 1,
                "vector_enabled": False,
                "fts_enabled": True,
                "chunk_tokens": 400,
                "chunk_overlap": 80,
                "embedding_model": "embedding-3",
                "embedding_cache_enabled": True,
                "embedding_max_batch_size": 10,
            }

        self.daemon.register_handler(
            "memory_capture",
            lambda claimed, store: handle_capture_request(
                claimed,
                store,
                llm_backend=lambda current_capture: {
                    "topic": current_capture["task"],
                    "applicability": current_capture["capture_reason"],
                    "conclusions": ["ok"],
                    "root_cause_patterns": [],
                    "solutions": ["ok"],
                    "key_locations": {"files": [], "symbols": [], "errors": []},
                    "aliases": [],
                    "benchmark_names": [],
                    "retrieval_surface": current_capture["retrieval_surface"],
                    "confidence": "high",
                    "source_capture_id": current_capture["capture_id"],
                    "review_after": "",
                    "supersedes": [],
                },
                index_backend=flaky_index,
                enable_durable_write=True,
            ),
        )
        enqueue_capture(capture=capture, request_id="req_trace_capture_002")
        self.daemon.start()
        try:
            self.assertTrue(self.daemon.process_once())
            recovered = self.store.recover_processing(now_epoch=10**10)
            self.assertEqual(recovered, 1)
            self.assertTrue(self.daemon.process_once())
        finally:
            self.daemon.stop()

        trace = self._single_recent_trace()
        self.assertEqual(len(trace["daemon_attempts"]), 2)
        self.assertTrue(trace["retry_scheduled"])
        self.assertEqual(trace["final_state"], "stored")
        self.assertEqual(trace["daemon_attempts"][0]["scheduled_retry_delay_ms"], 30000)
        self.assertEqual(trace["daemon_attempts"][0]["error_kind"], "retryable_index_error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
