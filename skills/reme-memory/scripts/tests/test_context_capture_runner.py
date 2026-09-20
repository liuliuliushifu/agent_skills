#!/usr/bin/env python3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
RUNNER = SCRIPT_DIR / "context_capture_runner.py"
PYTHON = sys.executable


class ContextCaptureRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="reme_capture_runner_", dir="/tmp"))
        self.input_json = self.root / "entry.json"
        self.artifact_root = self.root / "artifacts"
        self.handoff_root = self.root / "handoffs"
        self.compact_memory_root = self.root / "compact_memory"
        self.capture_json = self.root / "capture.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_runner_emits_standard_exit_and_outputs(self) -> None:
        transcript = "\n".join(
            [
                "assistant: Packet RX A3.8 performance result.",
                "| case | A3.7 pps | A3.8 pps | change |",
                "| --- | --- | --- | --- |",
                "| 8-entry 连续 | 646,355 pps | 886,327 pps | +37.1% |",
                "| 64-entry 连续 | 80,451 pps | 105,764 pps | +31.5% |",
                "user: 2entry是1.39mpps,我测试过了.没有下降.",
            ]
        )
        self.input_json.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "test-run-001",
                    "project": "cling_packet",
                    "task": "Capture Packet RX performance matrix",
                    "scenario": "Packet RX performance",
                    "thread_id": "thread-runner",
                    "transcript_text": transcript,
                    "artifact_root": str(self.artifact_root),
                    "handoff_root": str(self.handoff_root),
                    "capture_json_out": str(self.capture_json),
                    "write_modes": ["capture_json", "artifacts", "handoff"],
                    "fail_open": False,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [PYTHON, str(RUNNER), "--input-json", str(self.input_json)],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["runner_version"], "0.1")
        self.assertEqual(payload["run_id"], "test-run-001")
        self.assertEqual(payload["state"], "ok")
        self.assertEqual(payload["capture_json_path"], str(self.capture_json))
        self.assertTrue(payload["handoff_md_path"].endswith(".md"))
        self.assertGreaterEqual(payload["artifact_count"], 2)
        self.assertTrue(self.capture_json.exists())
        self.assertTrue(Path(payload["handoff_md_path"]).exists())
        self.assertTrue(any(path.endswith(".json") for path in payload["artifact_paths"]))
        self.assertIn("generic:markdown-performance-table@0.1", self._rule_ids(payload))
        self.assertIn("generic:entry-performance-lines@0.1", self._rule_ids(payload))

        artifact_payloads = [
            json.loads(Path(path).read_text(encoding="utf-8"))
            for path in payload["artifact_paths"]
            if path.endswith(".json") and "benchmarks" in path
        ]
        records = [record for artifact in artifact_payloads for record in artifact["records"]]
        self.assertTrue(any(record.get("case") == "8-entry 连续" for record in records))
        self.assertTrue(any(record.get("entry_count") == "2" for record in records))
        self.assertFalse(any("latency" in record for record in records))
        self.assertFalse(any("cpu" in record for record in records))

    def test_runner_failure_uses_standard_exit_payload(self) -> None:
        self.input_json.write_text(
            json.dumps({"schema_version": 1, "project": "cling_packet", "fail_open": True}) + "\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [PYTHON, str(RUNNER), "--input-json", str(self.input_json)],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["artifact_paths"], [])
        self.assertTrue(payload["errors"])

    def test_runner_can_write_separate_compact_memory(self) -> None:
        self.input_json.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "test-run-compact-001",
                    "project": "reme",
                    "task": "PreCompact hook capture",
                    "scenario": "Codex PreCompact",
                    "thread_id": "thread-compact",
                    "transcript_text": "assistant: 确认 PreCompact 正式捕获 hook 已经落地。",
                    "artifact_root": str(self.artifact_root),
                    "handoff_root": str(self.handoff_root),
                    "compact_memory_root": str(self.compact_memory_root),
                    "capture_json_out": str(self.capture_json),
                    "write_modes": ["capture_json", "handoff", "compact_memory"],
                    "fail_open": False,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [PYTHON, str(RUNNER), "--input-json", str(self.input_json)],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        compact_path = Path(payload["compact_memory_md_path"])
        self.assertTrue(compact_path.exists())
        self.assertEqual(compact_path.parent, self.compact_memory_root)
        self.assertIn("PreCompact hook capture", compact_path.read_text(encoding="utf-8"))

    def _rule_ids(self, payload):
        return [rule["rule_id"] for rule in payload["rules_loaded"]]


if __name__ == "__main__":
    unittest.main(verbosity=2)
