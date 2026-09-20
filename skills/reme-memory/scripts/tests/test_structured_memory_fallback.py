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
from memory_request_store import MemoryRequestStore
from structured_memory_llm import StructuredMemoryFallbackRequired


class StructuredMemoryFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bus_root = Path(tempfile.mkdtemp(prefix="reme_fallback_bus_", dir="/tmp"))
        self.reme_root = Path(tempfile.mkdtemp(prefix="reme_fallback_reme_", dir="/tmp"))
        self.old_bus_root = os.environ.get("REME_BUS_ROOT")
        self.old_workdir = os.environ.get("REME_WORKDIR")
        os.environ["REME_BUS_ROOT"] = str(self.bus_root)
        os.environ["REME_WORKDIR"] = str(self.reme_root)
        self.store = MemoryRequestStore(daemon_id="daemon-fallback")

    def tearDown(self) -> None:
        try:
            self.store.release_daemon_lock()
        except Exception:
            pass
        if self.old_bus_root is None:
            os.environ.pop("REME_BUS_ROOT", None)
        else:
            os.environ["REME_BUS_ROOT"] = self.old_bus_root
        if self.old_workdir is None:
            os.environ.pop("REME_WORKDIR", None)
        else:
            os.environ["REME_WORKDIR"] = self.old_workdir
        shutil.rmtree(self.bus_root, ignore_errors=True)
        shutil.rmtree(self.reme_root, ignore_errors=True)

    def test_fallback_writes_memory_file_and_marks_indexed(self) -> None:
        capture = normalize_capture_input(
            {
                "project": "cling_glb",
                "task": "Fallback structured write",
                "session_summary": "daemon should fallback when llm output fails",
                "facts": [{"text": "structured fallback is deterministic"}],
                "decisions": [{"decision": "fallback writes memory block"}],
                "durable_identity": "structured fallback write",
                "aliases": ["fallback"],
            },
            thread_id_override="thread-fallback",
        )
        enqueue_capture(capture=capture, request_id="req_fallback_001")
        claimed = self.store.claim_next("memory_capture")
        self.assertIsNotNone(claimed)

        def failing_llm(_capture):
            raise StructuredMemoryFallbackRequired("mock llm failure")

        handle_capture_request(
            claimed,
            self.store,
            llm_backend=failing_llm,
            index_backend=lambda: {"indexed_files": 1},
            enable_durable_write=True,
        )

        status_path = self.bus_root / "result" / "req_fallback_001.status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "stored")
        self.assertEqual(status["phase"], "memory_indexed")

        memory_files = list((self.reme_root / "memory").glob("*.md"))
        self.assertEqual(len(memory_files), 1)
        content = memory_files[0].read_text(encoding="utf-8")
        self.assertIn("Durable Idempotency Key", content)
        self.assertIn("### Topic:", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
