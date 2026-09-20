#!/usr/bin/env python3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import shutil
import tempfile
import unittest
from pathlib import Path

from capture_schema import normalize_capture_input
from memory_bus_client import enqueue_capture, enqueue_flush
from memory_bus_io import read_json
from memory_bus_paths import build_bus_paths, ensure_bus_dirs


class MemoryCaptureStep1Test(unittest.TestCase):
    def setUp(self) -> None:
        self.test_root = Path(tempfile.mkdtemp(prefix="reme_capture_step1_", dir="/tmp"))
        self.paths = build_bus_paths(self.test_root)
        ensure_bus_dirs(self.paths)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_root, ignore_errors=True)

    def _sample_capture(self) -> dict:
        return normalize_capture_input(
            {
                "project": "cling_glb",
                "task": "Archive capture request",
                "session_summary": "capture request should go through the bus",
                "facts": [{"text": "capture payload already normalized"}],
                "decisions": [{"decision": "memory_capture is a separate request type"}],
            },
            thread_id_override="thread-step1",
        )

    def test_enqueue_capture(self) -> None:
        capture = self._sample_capture()
        accepted = enqueue_capture(
            capture=capture,
            paths=self.paths,
            request_id="req_capture_step1_001",
        )
        payload = read_json(accepted.request_path)
        self.assertEqual(payload["request_type"], "memory_capture")
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["payload"]["capture_id"], capture["capture_id"])

    def test_enqueue_flush_supports_capture_scopes(self) -> None:
        flush_request = enqueue_flush(
            scope="captures",
            paths=self.paths,
            request_id="req_capture_flush_001",
        )
        payload = read_json(flush_request.request_path)
        self.assertEqual(payload["payload"]["scope"], "captures")


if __name__ == "__main__":
    unittest.main(verbosity=2)
