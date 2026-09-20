#!/usr/bin/env python3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import json
import os
import shutil
import subprocess
import tempfile
import datetime as dt
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
MEMORY_WORKFLOW = SCRIPT_DIR / "memory_workflow.py"
PYTHON = sys.executable


class CaptureContextWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.test_root = Path(tempfile.mkdtemp(prefix="reme_capture_workflow_", dir="/tmp"))
        self.handoff_root = self.test_root / "handoffs"
        self.metrics_root = self.test_root / "metrics"
        self.bus_root = self.test_root / "bus"
        self.capture_json = self.test_root / "capture.json"
        self.capture_json.write_text(
            json.dumps(
                {
                    "project": "cling_glb",
                    "task": "Implement capture_context",
                    "scenario": "Write a deterministic handoff pair",
                    "session_summary": "Phase 1 should only write handoff files.",
                    "facts": [{"text": "capture_context writes markdown directly", "confidence": "high"}],
                    "decisions": [{"decision": "sidecar json", "status": "active"}],
                    "next_steps": ["Add bus capture path in Phase 2"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.test_root, ignore_errors=True)

    def _run_capture(self, *extra_args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        reme_root = self.test_root / "reme"
        env["REME_HANDOFF_DIR"] = str(self.handoff_root)
        env["REME_MEMORY_METRICS_DIR"] = str(self.metrics_root)
        env["REME_BUS_ROOT"] = str(self.bus_root)
        env["REME_WORKDIR"] = str(reme_root)
        env["REME_HOME"] = str(reme_root / "reme-home")
        env["REME_RUNTIME_ENV_FILE"] = str(reme_root / "test-runtime.env")
        lock_path = self.bus_root / "lock" / "daemon.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps(
                {
                    "daemon_id": "daemon-capture-test",
                    "pid": os.getpid(),
                    "hostname": "test",
                    "reme_workdir": str(reme_root),
                    "started_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
                    "heartbeat_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
                }
            ),
            encoding="utf-8",
        )
        return subprocess.run(
            [PYTHON, str(MEMORY_WORKFLOW), "capture_context", "--capture-json", str(self.capture_json), *extra_args],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_capture_context_overwrites_same_thread_scope(self) -> None:
        first = self._run_capture("--thread-id", "thread-123")
        second = self._run_capture("--thread-id", "thread-123")

        self.assertEqual(first.returncode, 0, msg=first.stderr)
        self.assertEqual(second.returncode, 0, msg=second.stderr)
        self.assertIn("Handoff written: capture_id=", first.stdout)
        self.assertEqual(len(list(self.handoff_root.glob("*.md"))), 1)
        self.assertEqual(len(list(self.handoff_root.glob("*.json"))), 1)

        payload = json.loads(next(self.handoff_root.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(payload["thread_id"], "thread-123")

    def test_capture_context_without_scope_is_append_only(self) -> None:
        first = self._run_capture()
        second = self._run_capture()

        self.assertEqual(first.returncode, 0, msg=first.stderr)
        self.assertEqual(second.returncode, 0, msg=second.stderr)
        self.assertEqual(len(list(self.handoff_root.glob("*.md"))), 2)
        self.assertEqual(len(list(self.handoff_root.glob("*.json"))), 2)

    def test_capture_context_can_enqueue_durable_request(self) -> None:
        result = self._run_capture("--thread-id", "thread-123", "--enqueue-durable")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("capture_request_id=", result.stdout)
        self.assertEqual(len(list((self.bus_root / "inbox" / "capture").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
