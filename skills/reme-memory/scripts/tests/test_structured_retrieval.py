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
from pathlib import Path

from capture_adapter import handle_capture_request
from capture_schema import normalize_capture_input
from memory_block_renderer import parse_memory_blocks
from memory_bus_client import enqueue_capture, enqueue_query, wait_for_result
from memory_daemon import MemoryDaemon
from memory_request_store import MemoryRequestStore
from query_adapter import handle_query_request
from status_adapter import handle_status_request
from structured_memory_llm import StructuredMemoryFallbackRequired


class StructuredRetrievalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bus_root = Path(tempfile.mkdtemp(prefix="reme_structured_bus_", dir="/tmp"))
        self.reme_root = Path(tempfile.mkdtemp(prefix="reme_structured_reme_", dir="/tmp"))
        self.saved_env = os.environ.copy()
        os.environ["REME_BUS_ROOT"] = str(self.bus_root)
        os.environ["REME_WORKDIR"] = str(self.reme_root)
        os.environ["REME_VECTOR_ENABLED"] = "false"
        os.environ["REME_FTS_ENABLED"] = "true"
        self.store = MemoryRequestStore(daemon_id="daemon-structured-e2e")
        self.daemon = MemoryDaemon(store=self.store, poll_interval_seconds=0.02)
        self.daemon.register_handler(
            "memory_capture",
            lambda claimed, store: handle_capture_request(
                claimed,
                store,
                llm_backend=self._failing_llm,
                enable_durable_write=True,
            ),
        )
        self.daemon.register_handler("memory_query", handle_query_request)
        self.daemon.register_handler("memory_status", handle_status_request)
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

    def _failing_llm(self, _capture):
        raise StructuredMemoryFallbackRequired("force fallback for deterministic test")

    def _enqueue_capture_and_wait(self, capture: dict, request_id: str) -> None:
        enqueue_capture(capture=capture, request_id=request_id)
        deadline = time.time() + 20
        status_path = self.bus_root / "result" / f"{request_id}.status.json"
        while time.time() < deadline:
            if status_path.exists():
                payload = json.loads(status_path.read_text(encoding="utf-8"))
                if payload.get("state") == "stored" and payload.get("phase") == "memory_indexed":
                    return
            time.sleep(0.05)
        raise TimeoutError(request_id)

    def _block_overlap_hit(self, query: str, expected_key: str) -> bool:
        accepted = enqueue_query(query=query, max_results=3)
        result = wait_for_result(accepted.request_id, timeout_seconds=20.0)
        if not result.get("items"):
            return False

        memory_file = next((self.reme_root / "memory").glob("*.md"))
        block_ranges = {
            block["durable_idempotency_key"]: (block["start_line"], block["end_line"])
            for block in parse_memory_blocks(memory_file.read_text(encoding="utf-8"))
        }
        expected_range = block_ranges[expected_key]
        for item in result["items"][:3]:
            if not item["path"].endswith(memory_file.name):
                continue
            if _ranges_overlap(
                expected_range[0],
                expected_range[1],
                int(item["start_line"]),
                int(item["end_line"]),
            ):
                return True
        return False

    def test_structured_fallback_memory_is_queryable(self) -> None:
        capture_a = normalize_capture_input(
            {
                "project": "cling_glb",
                "task": "Need deterministic handoff path",
                "session_summary": "capture_context should write deterministic handoffs",
                "facts": [{"text": "summary_memory is too soft"}],
                "aliases": ["handoff"],
                "durable_identity": "structured handoff path",
            },
            thread_id_override="thread-a",
        )
        capture_b = normalize_capture_input(
            {
                "project": "cling_glb",
                "task": "Need durable block renderer",
                "session_summary": "renderer should preserve retrieval fields on overwrite",
                "facts": [{"text": "renderer must merge aliases and files"}],
                "aliases": ["renderer"],
                "durable_identity": "structured block renderer",
            },
            thread_id_override="thread-b",
        )

        self._enqueue_capture_and_wait(capture_a, "req_structured_a")
        self._enqueue_capture_and_wait(capture_b, "req_structured_b")

        self.assertTrue(self._block_overlap_hit("handoff", capture_a["durable_idempotency_key"]))
        self.assertTrue(self._block_overlap_hit("renderer", capture_b["durable_idempotency_key"]))


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return not (a_end < b_start or b_end < a_start)


if __name__ == "__main__":
    unittest.main(verbosity=2)
