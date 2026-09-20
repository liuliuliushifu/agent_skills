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

from memory_request_schema import MAX_REQUEST_BYTES, MemoryRequest


SCRIPT_DIR = Path(__file__).resolve().parents[1]
SESSION_EXCERPT = SCRIPT_DIR / "session_excerpt.py"
RUNNER = SCRIPT_DIR / "context_capture_runner.py"
PYTHON = sys.executable


class SessionExcerptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="reme_session_excerpt_", dir="/tmp"))
        self.transcript = self.root / "codex_session.jsonl"
        self.excerpt = self.root / "excerpt.txt"
        self.metadata = self.root / "excerpt.json"
        self.runner_input = self.root / "runner-entry.json"
        self.refine_json = self.root / "refine.json"
        self.artifact_root = self.root / "artifacts"
        self.handoff_root = self.root / "handoffs"
        self.capture_json = self.root / "capture.json"
        self._write_transcript()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_keyword_excerpt_filters_system_encrypted_and_unrelated_noise(self) -> None:
        result = subprocess.run(
            [
                PYTHON,
                str(SESSION_EXCERPT),
                "--transcript-path",
                str(self.transcript),
                "--keyword",
                "A3.8",
                "--keyword",
                "886,327",
                "--output",
                str(self.excerpt),
                "--metadata-out",
                str(self.metadata),
                "--runner-input-json-out",
                str(self.runner_input),
                "--project",
                "cling_packet",
                "--task",
                "Capture Packet RX performance matrix",
                "--scenario",
                "Packet RX performance",
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "ok")
        self.assertTrue(self.excerpt.exists())
        self.assertTrue(self.metadata.exists())
        self.assertTrue(self.runner_input.exists())

        text = self.excerpt.read_text(encoding="utf-8")
        self.assertIn("886,327 pps", text)
        self.assertIn("105,764 pps", text)
        self.assertIn("2entry是1.39mpps", text)
        self.assertNotIn("SHOULD_NOT_APPEAR", text)
        self.assertNotIn("UNRELATED_LOG_LINE", text)

        runner_payload = json.loads(self.runner_input.read_text(encoding="utf-8"))
        self.assertEqual(runner_payload["transcript_path"], str(self.excerpt.resolve()))
        self.assertEqual(runner_payload["source_transcript_path"], str(self.transcript.resolve()))
        self.assertEqual(runner_payload["project"], "cling_packet")

    def test_excerpt_can_write_evidence_only_refine_json(self) -> None:
        result = subprocess.run(
            [
                PYTHON,
                str(SESSION_EXCERPT),
                "--transcript-path",
                str(self.transcript),
                "--keyword",
                "A3.8",
                "--output",
                str(self.excerpt),
                "--metadata-out",
                str(self.metadata),
                "--refine-json-out",
                str(self.refine_json),
                "--project",
                "cling_packet",
                "--task",
                "Capture Packet RX performance matrix",
                "--scenario",
                "Packet RX performance",
                "--thread-id",
                "thread-refine",
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["refine_json_path"], str(self.refine_json.resolve()))
        refine = json.loads(self.refine_json.read_text(encoding="utf-8"))
        self.assertEqual(refine["thread_id"], "thread-refine")
        self.assertEqual(refine["source_transcript_path"], str(self.transcript.resolve()))
        self.assertEqual(refine["source_excerpt_path"], str(self.excerpt.resolve()))
        self.assertTrue(refine["evidence"])
        self.assertTrue(all(item["evidence_hash"].startswith("sha256:") for item in refine["evidence"]))
        self.assertTrue(all(item["line_range"] for item in refine["evidence"]))
        evidence_text = "\n".join(item["text"] for item in refine["evidence"])
        self.assertIn("A3.8", evidence_text)
        self.assertNotIn(str(self.transcript), evidence_text)

    def test_refine_json_stays_under_memory_bus_request_cap(self) -> None:
        large_transcript = self.root / "large_codex_session.jsonl"
        rows = [
            {
                "timestamp": "2026-05-21T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": "session-large-001", "cwd": "/workspace/example-codex-home"},
            }
        ]
        for idx in range(40):
            rows.append(
                {
                    "timestamp": "2026-05-21T00:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "A3.8 durable evidence {} ".format(idx) + ("x" * 6000),
                            }
                        ],
                    },
                }
            )
        large_transcript.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                PYTHON,
                str(SESSION_EXCERPT),
                "--transcript-path",
                str(large_transcript),
                "--keyword",
                "A3.8",
                "--output",
                str(self.excerpt),
                "--metadata-out",
                str(self.metadata),
                "--refine-json-out",
                str(self.refine_json),
                "--max-chars",
                "200000",
                "--per-record-max-chars",
                "12000",
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        refine = json.loads(self.refine_json.read_text(encoding="utf-8"))
        request = MemoryRequest.new(
            request_id="req_refine_size_001",
            request_type="memory_refine",
            client_id="test",
            project="cling_packet",
            language="zh",
            payload=refine,
        )
        request_bytes = len(json.dumps(request.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8"))
        self.assertLessEqual(request_bytes, MAX_REQUEST_BYTES)
        self.assertLessEqual(len(refine["evidence"]), 24)

    def test_empty_excerpt_skips_refine_json(self) -> None:
        empty_transcript = self.root / "empty.jsonl"
        empty_transcript.write_text("", encoding="utf-8")
        self.refine_json.write_text("stale", encoding="utf-8")

        result = subprocess.run(
            [
                PYTHON,
                str(SESSION_EXCERPT),
                "--transcript-path",
                str(empty_transcript),
                "--output",
                str(self.excerpt),
                "--metadata-out",
                str(self.metadata),
                "--refine-json-out",
                str(self.refine_json),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertIsNone(payload["refine_json_path"])
        self.assertFalse(self.refine_json.exists())
        self.assertIn("no evidence selected", "\n".join(payload["warnings"]))

    def test_fallback_tail_selection_skips_refine_json(self) -> None:
        low_value_transcript = self.root / "low_value.jsonl"
        rows = [
            {
                "timestamp": "2026-05-21T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": "session-low-value", "cwd": "/workspace/example-codex-home"},
            },
            {
                "timestamp": "2026-05-21T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ordinary low signal chat without durable markers"}],
                },
            },
        ]
        low_value_transcript.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                PYTHON,
                str(SESSION_EXCERPT),
                "--transcript-path",
                str(low_value_transcript),
                "--output",
                str(self.excerpt),
                "--metadata-out",
                str(self.metadata),
                "--refine-json-out",
                str(self.refine_json),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertIsNone(payload["refine_json_path"])
        self.assertFalse(self.refine_json.exists())
        metadata = json.loads(self.metadata.read_text(encoding="utf-8"))
        self.assertIn("fallback-tail", set(metadata["selection_reasons"].values()))

    def test_hook_json_input_writes_runner_payload(self) -> None:
        hook_json = self.root / "hook.json"
        hook_json.write_text(
            json.dumps(
                {
                    "session_id": "session-test-001",
                    "hook_event_name": "UserPromptSubmit",
                    "transcript_path": str(self.transcript),
                    "cwd": "/workspace/example-project",
                    "prompt": "确认 A3.8 性能",
                    "keywords": ["A3.8", "1.39mpps"],
                    "fail_open": True,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                PYTHON,
                str(SESSION_EXCERPT),
                "--input-json",
                str(hook_json),
                "--output",
                str(self.excerpt),
                "--metadata-out",
                str(self.metadata),
                "--runner-input-json-out",
                str(self.runner_input),
                "--project",
                "cling_packet",
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        runner_payload = json.loads(self.runner_input.read_text(encoding="utf-8"))
        self.assertEqual(runner_payload["run_reason"], "hook_userpromptsubmit")
        self.assertEqual(runner_payload["thread_id"], "session-test-001")
        self.assertTrue(runner_payload["fail_open"])

    def test_excerpt_can_feed_standard_capture_runner(self) -> None:
        excerpt_result = subprocess.run(
            [
                PYTHON,
                str(SESSION_EXCERPT),
                "--transcript-path",
                str(self.transcript),
                "--keyword",
                "A3.8",
                "--keyword",
                "1.39mpps",
                "--output",
                str(self.excerpt),
                "--metadata-out",
                str(self.metadata),
                "--runner-input-json-out",
                str(self.runner_input),
                "--project",
                "cling_packet",
                "--task",
                "Capture Packet RX performance matrix",
                "--scenario",
                "Packet RX performance",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(excerpt_result.returncode, 0, msg=excerpt_result.stderr)

        entry = json.loads(self.runner_input.read_text(encoding="utf-8"))
        entry["artifact_root"] = str(self.artifact_root)
        entry["handoff_root"] = str(self.handoff_root)
        entry["capture_json_out"] = str(self.capture_json)
        self.runner_input.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")

        runner_result = subprocess.run(
            [PYTHON, str(RUNNER), "--input-json", str(self.runner_input)],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(runner_result.returncode, 0, msg=runner_result.stderr)
        payload = json.loads(runner_result.stdout)
        self.assertEqual(payload["state"], "ok")
        self.assertGreaterEqual(payload["artifact_count"], 2)
        artifacts = [
            json.loads(Path(path).read_text(encoding="utf-8"))
            for path in payload["artifact_paths"]
            if path.endswith(".json") and "benchmarks" in path
        ]
        records = [record for artifact in artifacts for record in artifact["records"]]
        self.assertTrue(any(record.get("case") == "8-entry 连续" for record in records))
        self.assertTrue(any(record.get("entry_count") == "2" for record in records))
        self.assertFalse(any("latency" in record for record in records))
        self.assertFalse(any("cpu" in record for record in records))

    def test_value_pattern_selection_skips_test_source_output(self) -> None:
        source_transcript = self.root / "source_fixture_session.jsonl"
        rows = [
            {
                "timestamp": "2026-05-22T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": "session-source-fixture", "cwd": "/workspace/example-codex-home"},
            },
            {
                "timestamp": "2026-05-22T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "output": "\n".join(
                        [
                            "    def test_runner_emits_standard_exit_and_outputs(self) -> None:",
                            "        transcript = \"\\n\".join(",
                            "            [",
                            "                \"| 8-entry 连续 | 646,355 pps | 886,327 pps | +37.1% |\",",
                            "                \"user: 2entry是1.39mpps,我测试过了.没有下降.\",",
                            "            ]",
                            "        )",
                            "        self.assertGreaterEqual(payload[\"artifact_count\"], 2)",
                        ]
                    ),
                },
            },
            {
                "timestamp": "2026-05-22T00:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "确认 PreCompact hook 正式落地，下一步检查真实 compact 结果。",
                },
            },
        ]
        source_transcript.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                PYTHON,
                str(SESSION_EXCERPT),
                "--transcript-path",
                str(source_transcript),
                "--output",
                str(self.excerpt),
                "--metadata-out",
                str(self.metadata),
                "--max-chars",
                "12000",
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        text = self.excerpt.read_text(encoding="utf-8")
        self.assertIn("PreCompact hook 正式落地", text)
        self.assertNotIn("def test_runner_emits_standard_exit_and_outputs", text)
        self.assertNotIn("646,355 pps", text)

    def _write_transcript(self) -> None:
        rows = [
            {
                "timestamp": "2026-05-21T00:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "session-test-001",
                    "cwd": "/workspace/example-project",
                    "base_instructions": {"text": "SHOULD_NOT_APPEAR_BASE"},
                },
            },
            {
                "timestamp": "2026-05-21T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "SHOULD_NOT_APPEAR_DEVELOPER"}],
                },
            },
            {
                "timestamp": "2026-05-21T00:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "encrypted_content": "SHOULD_NOT_APPEAR_ENCRYPTED",
                },
            },
            {
                "timestamp": "2026-05-21T00:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-noise",
                    "output": "\n".join("UNRELATED_LOG_LINE {}".format(idx) for idx in range(200)),
                },
            },
            {
                "timestamp": "2026-05-21T00:00:04Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "请确认 A3.8 Packet RX 性能结果"}],
                },
            },
            {
                "timestamp": "2026-05-21T00:00:05Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Codex整理后的性能矩阵如下:",
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-05-21T00:00:06Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-table",
                    "output": "\n".join(
                        [
                            "| case | A3.7 pps | A3.8 pps | change |",
                            "| --- | --- | --- | --- |",
                            "| 8-entry 连续 | 646,355 pps | 886,327 pps | +37.1% |",
                            "| 64-entry 连续 | 80,451 pps | 105,764 pps | +31.5% |",
                        ]
                    ),
                },
            },
            {
                "timestamp": "2026-05-21T00:00:07Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "2entry是1.39mpps,我测试过了.没有下降."}],
                },
            },
        ]
        self.transcript.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
