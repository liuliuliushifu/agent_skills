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
from deploy_runtime import deploy_runtime
from flush_adapter import handle_flush_request
from memory_bus_client import enqueue_capture, enqueue_flush
from memory_daemon import MemoryDaemon
from memory_request_store import MemoryRequestStore


class MemoryCaptureStep2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.test_root = Path(tempfile.mkdtemp(prefix="reme_capture_step2_", dir="/tmp"))
        self.runtime_root = Path(tempfile.mkdtemp(prefix="reme_capture_runtime_", dir="/tmp"))
        self.old_bus_root = os.environ.get("REME_BUS_ROOT")
        os.environ["REME_BUS_ROOT"] = str(self.test_root)
        self.store = MemoryRequestStore(daemon_id="daemon-capture-step2")
        self.daemon = MemoryDaemon(store=self.store, poll_interval_seconds=0.01)
        self.daemon.register_handler("memory_capture", handle_capture_request)
        self.daemon.register_handler("memory_flush", handle_flush_request)

    def tearDown(self) -> None:
        try:
            self.store.release_daemon_lock()
        except Exception:
            pass
        if self.old_bus_root is None:
            os.environ.pop("REME_BUS_ROOT", None)
        else:
            os.environ["REME_BUS_ROOT"] = self.old_bus_root
        shutil.rmtree(self.test_root, ignore_errors=True)
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    def _sample_capture(self) -> dict:
        return normalize_capture_input(
            {
                "project": "cling_glb",
                "task": "Archive capture request",
                "session_summary": "capture request should be archived",
                "facts": [{"text": "capture payload already normalized"}],
                "decisions": [{"decision": "daemon archives capture payload"}],
            },
            thread_id_override="thread-step2",
        )

    def test_capture_request_is_archived_and_completed(self) -> None:
        enqueue_capture(
            capture=self._sample_capture(),
            request_id="req_capture_step2_001",
        )
        self.daemon.start()
        try:
            handled = self.daemon.process_once()
        finally:
            self.daemon.stop()
        self.assertTrue(handled)

        raw_request = self.test_root / "archive" / "raw" / "req_capture_step2_001.json"
        archived_request = self.test_root / "archive" / "completed" / "req_capture_step2_001.json"
        status_path = self.test_root / "result" / "req_capture_step2_001.status.json"
        self.assertTrue(raw_request.exists())
        self.assertTrue(archived_request.exists())
        status = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "captured")
        self.assertEqual(status["phase"], "capture_archived")

    def test_flush_searchable_counts_pending_captures(self) -> None:
        enqueue_capture(
            capture=self._sample_capture(),
            request_id="req_capture_step2_002",
        )
        flush_request = enqueue_flush(scope="searchable", request_id="req_capture_step2_flush_001")
        self.daemon.start()
        try:
            self.assertTrue(self.daemon.process_once())
            self.assertTrue(self.daemon.process_once())
        finally:
            self.daemon.stop()

        result_path = self.test_root / "result" / f"{flush_request.request_id}.json"
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["scope"], "searchable")
        self.assertEqual(payload["pending_captures"], 0)
        self.assertEqual(payload["pending_total"], 0)

    def test_deploy_runtime_copies_scripts_and_writes_manifest(self) -> None:
        result = deploy_runtime(self.runtime_root)
        manifest_path = Path(result["manifest_path"])
        self.assertTrue(manifest_path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(manifest["deployed_files"])
        self.assertTrue((self.runtime_root / "scripts" / "memory_daemon.py").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
