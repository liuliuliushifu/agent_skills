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

from transcript_context_extractor import (
    extract_context_from_transcript,
    parse_entry_performance_lines,
    parse_markdown_table,
    parse_metric_lines,
    split_transcript,
)


SCRIPT_DIR = Path(__file__).resolve().parents[1]
EXTRACTOR = SCRIPT_DIR / "transcript_context_extractor.py"
PYTHON = sys.executable


class TranscriptContextExtractorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.test_root = Path(tempfile.mkdtemp(prefix="reme_transcript_extract_", dir="/tmp"))
        self.transcript = self.test_root / "packet_rx_transcript.txt"
        self.transcript.write_text(
            "\n".join(
                [
                    "user: Packet RX性能测试, baseline commit abc1234, compare def5678.",
                    "",
                    "assistant: Codex整理后的性能矩阵如下:",
                    "| version | commit | pkt_size | queues | rx_mpps | drop |",
                    "| --- | --- | --- | --- | --- | --- |",
                    "| v1 | abc1234 | 64 | 8 | 120.5 | 0 |",
                    "| v2 | def5678 | 64 | 8 | 108.2 | 0.1% |",
                    "",
                    "assistant: 结论: v1作为baseline, v2在64B RX下降约10.2%。",
                    "user: 后续需要复测128B和256B。",
                    "assistant: 关联待办 TRK-20260519-1, 不是commit。",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_split_transcript_keeps_table_source_range(self) -> None:
        source_lines = [
            {"line_no": idx, "text": line}
            for idx, line in enumerate(self.transcript.read_text(encoding="utf-8").splitlines(), start=1)
        ]
        chunks = split_transcript(source_lines)
        table_chunks = [chunk for chunk in chunks if chunk["chunk_type"] == "markdown_table"]

        self.assertEqual(len(table_chunks), 1)
        self.assertEqual(table_chunks[0]["line_range"], [4, 7])

    def test_parse_markdown_table_normalizes_performance_fields(self) -> None:
        table = "\n".join(
            [
                "| version | commit | pkt_size | queues | rx_mpps | drop |",
                "| --- | --- | --- | --- | --- | --- |",
                "| v1 | abc1234 | 64 | 8 | 120.5 | 0 |",
                "| v2 | def5678 | 64 | 8 | 108.2 | 0.1% |",
            ]
        )
        records = parse_markdown_table(table)

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["version"], "v1")
        self.assertEqual(records[0]["commit"], "abc1234")
        self.assertEqual(records[0]["packet_size"], "64")
        self.assertEqual(records[0]["queues"], "8")
        self.assertEqual(records[0]["throughput"], "120.5")
        self.assertEqual(records[1]["drop"], "0.1%")
        self.assertEqual(records[1]["raw"]["rx_mpps"], "108.2")

    def test_ambiguous_pkt_field_is_not_treated_as_packet_size(self) -> None:
        table = "\n".join(
            [
                "| pkt | version | commit | rx_mpps | drop |",
                "| --- | --- | --- | --- | --- |",
                "| 2 | v20260518 | a1b2c3d4 | 121.9 | 0 |",
                "| 8 | v20260518 | a1b2c3d4 | 118.4 | 0 |",
            ]
        )
        records = parse_markdown_table(table)

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["pkt"], "2")
        self.assertEqual(records[0]["throughput"], "121.9")
        self.assertNotIn("packet_size", records[0])
        self.assertNotIn("latency", records[0])
        self.assertNotIn("cpu", records[0])

    def test_parse_metric_lines_preserves_percent_without_extra_space(self) -> None:
        records = parse_metric_lines("output: rx_mpps=110.4 drop=0.2% latency_us=10.7 cpu_pct=81.5")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["rx_mpps"], "110.4")
        self.assertEqual(records[0]["drop"], "0.2%")
        self.assertEqual(records[0]["latency_us"], "10.7")
        self.assertEqual(records[0]["cpu_pct"], "81.5")

    def test_parse_entry_performance_lines_from_real_session_style(self) -> None:
        records = parse_entry_performance_lines(
            "\n".join(
                [
                    "下一步建议先做一个 2-entry 回归，确认小包还保持 `1.37Mpps` 附近。",
                    "2entry是1.39mpps,我测试过了.没有下降.",
                    "2entry是1.39mpps,我测试过了.没有下降.",
                ]
            )
        )

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["entry_count"], "2")
        self.assertEqual(records[0]["throughput"], "1.37 Mpps")
        self.assertEqual(records[0]["status"], "reference_or_target")
        self.assertEqual(records[1]["entry_count"], "2")
        self.assertEqual(records[1]["throughput"], "1.39 mpps")
        self.assertEqual(records[1]["status"], "measured")

    def test_parse_entry_performance_lines_with_chinese_entry_words(self) -> None:
        records = parse_entry_performance_lines(
            "\n".join(
                [
                    "实测 8个条目 连续压测吞吐 886,327 pps，没有下降。",
                    "建议 64 表项 baseline 保持 105,764 pps 附近。",
                    "入口 2 的配置不是性能数据。",
                ]
            )
        )

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["entry_count"], "8")
        self.assertEqual(records[0]["throughput"], "886,327 pps")
        self.assertEqual(records[0]["status"], "measured")
        self.assertEqual(records[1]["entry_count"], "64")
        self.assertEqual(records[1]["throughput"], "105,764 pps")
        self.assertEqual(records[1]["status"], "reference_or_target")

    def test_source_fixture_performance_strings_are_not_artifacts(self) -> None:
        transcript = self.test_root / "source_fixture.txt"
        transcript.write_text(
            "\n".join(
                [
                    "    def test_runner_emits_standard_exit_and_outputs(self) -> None:",
                    "        transcript = \"\\n\".join(",
                    "            [",
                    "                \"assistant: Packet RX A3.8 performance result.\",",
                    "                \"| case | A3.7 pps | A3.8 pps | change |\",",
                    "                \"| --- | --- | --- | --- |\",",
                    "                \"| 8-entry 连续 | 646,355 pps | 886,327 pps | +37.1% |\",",
                    "                \"| 64-entry 连续 | 80,451 pps | 105,764 pps | +31.5% |\",",
                    "                \"user: 2entry是1.39mpps,我测试过了.没有下降.\",",
                    "            ]",
                    "        )",
                    "        self.assertGreaterEqual(payload[\"artifact_count\"], 2)",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        extraction = extract_context_from_transcript(
            transcript_path=transcript,
            project="cling_packet",
            task="Avoid extracting benchmark values from unit-test fixtures",
            scenario="Codex PreCompact",
            thread_id="thread-source-fixture",
            artifact_root=self.test_root / "source_fixture_artifacts",
        )

        self.assertEqual(extraction["artifacts"], [])

    def test_patch_diff_source_fixture_is_not_artifact(self) -> None:
        transcript = self.test_root / "patch_fixture.txt"
        transcript.write_text(
            "\n".join(
                [
                    "*** Begin Patch",
                    "+    def test_value_pattern_selection_skips_test_source_output(self) -> None:",
                    "+        source_transcript = self.root / \"source_fixture_session.jsonl\"",
                    "+        rows = [",
                    "+            {",
                    "+                \"output\": \"\\n\".join(",
                    "+                    [",
                    "+                        \"| 8-entry 连续 | 646,355 pps | 886,327 pps | +37.1% |\",",
                    "+                        \"user: 2entry是1.39mpps,我测试过了.没有下降.\",",
                    "+                    ]",
                    "+                ),",
                    "+            },",
                    "+        ]",
                    "*** End Patch",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        extraction = extract_context_from_transcript(
            transcript_path=transcript,
            project="cling_packet",
            task="Avoid extracting benchmark values from patch diffs",
            scenario="Codex PreCompact",
            thread_id="thread-patch-fixture",
            artifact_root=self.test_root / "patch_fixture_artifacts",
        )

        self.assertEqual(extraction["artifacts"], [])

    def test_file_line_references_are_not_performance_artifacts(self) -> None:
        transcript = self.test_root / "review_finding.txt"
        transcript.write_text(
            "\n".join(
                [
                    "subagent 找到问题: entry_count > 128 时需要 fallback.",
                    "clx_packet_provision.c:501 returns -EINVAL.",
                    "clx_packet_protocol.c:1127 treats it as parse error.",
                    "payload above 512B, up to 1400 / 350 entries.",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        extraction = extract_context_from_transcript(
            transcript_path=transcript,
            project="cling_packet",
            task="Review finding extraction",
            scenario="packet-processing A3.8 review finding",
            thread_id="thread-review",
            artifact_root=self.test_root / "review_artifacts",
        )

        self.assertEqual(extraction["artifacts"], [])

    def test_extract_context_creates_stable_packet_rx_artifact(self) -> None:
        first = extract_context_from_transcript(
            transcript_path=self.transcript,
            project="cling_packet",
            task="Capture Packet RX performance matrix",
            scenario="Packet RX performance",
            thread_id="thread-001",
            artifact_root=self.test_root / "artifacts",
        )
        second = extract_context_from_transcript(
            transcript_path=self.transcript,
            project="cling_packet",
            task="Capture Packet RX performance matrix",
            scenario="Packet RX performance",
            thread_id="thread-001",
            artifact_root=self.test_root / "artifacts",
        )

        self.assertEqual(len(first["artifacts"]), 1)
        artifact = first["artifacts"][0]
        self.assertEqual(artifact["artifact_type"], "benchmark_matrix")
        self.assertEqual(artifact["benchmark_family"], "packet-rx")
        self.assertEqual(artifact["source"]["line_range"], [4, 7])
        self.assertEqual(len(artifact["records"]), 2)
        self.assertEqual(artifact["records"][0]["throughput"], "120.5")
        self.assertTrue(artifact["source"]["hash"].startswith("sha256:"))
        self.assertEqual(first["artifacts"][0]["artifact_id"], second["artifacts"][0]["artifact_id"])

        capture = first["capture"]
        self.assertEqual(capture["thread_id"], "thread-001")
        self.assertEqual(len(capture["benchmarks"]), 1)
        self.assertIn("artifact:", capture["benchmarks"][0]["notes"])
        self.assertTrue(any("baseline" in item["decision"] for item in capture["decisions"]))
        self.assertTrue(any("复测128B" in item for item in capture["next_steps"]))
        self.assertIn("TRK-20260519-1", capture["symbols"])
        self.assertNotIn("TRK-20260519", capture["symbols"])
        self.assertNotIn("20260519", capture["symbols"])

    def test_duplicate_artifact_chunks_are_deduped(self) -> None:
        duplicate = self.test_root / "duplicate_tables.txt"
        table = "\n".join(
            [
                "| case | A3.7 pps | A3.8 pps | change |",
                "| --- | --- | --- | --- |",
                "| 8-entry 连续 | 646,355 pps | 886,327 pps | +37.1% |",
            ]
        )
        duplicate.write_text(table + "\n\n" + table + "\n", encoding="utf-8")

        extraction = extract_context_from_transcript(
            transcript_path=duplicate,
            project="cling_packet",
            task="Capture Packet RX performance matrix",
            scenario="Packet RX performance",
            thread_id="thread-dedupe",
            artifact_root=self.test_root / "dedupe_artifacts",
        )

        self.assertEqual(len(extraction["artifacts"]), 1)
        self.assertEqual(extraction["artifacts"][0]["records"][0]["case"], "8-entry 连续")

    def test_cli_writes_capture_artifact_and_handoff(self) -> None:
        artifact_root = self.test_root / "out" / "artifacts"
        handoff_root = self.test_root / "out" / "handoffs"
        capture_json = self.test_root / "out" / "capture.json"

        result = subprocess.run(
            [
                PYTHON,
                str(EXTRACTOR),
                "--transcript",
                str(self.transcript),
                "--project",
                "cling_packet",
                "--task",
                "Capture Packet RX performance matrix",
                "--scenario",
                "Packet RX performance",
                "--thread-id",
                "thread-001",
                "--artifact-root",
                str(artifact_root),
                "--handoff-root",
                str(handoff_root),
                "--capture-json-out",
                str(capture_json),
                "--write-handoff",
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["artifact_count"], 1)
        self.assertTrue(capture_json.exists())
        self.assertEqual(len(list((artifact_root / "benchmarks" / "packet-rx").glob("*.json"))), 1)
        self.assertEqual(len(list((artifact_root / "benchmarks" / "packet-rx").glob("*.md"))), 1)
        self.assertEqual(len(list(handoff_root.glob("*.json"))), 1)
        self.assertEqual(len(list(handoff_root.glob("*.md"))), 1)

    def test_cli_can_read_transcript_from_stdin(self) -> None:
        artifact_root = self.test_root / "stdin_out" / "artifacts"
        capture_json = self.test_root / "stdin_out" / "capture.json"

        result = subprocess.run(
            [
                PYTHON,
                str(EXTRACTOR),
                "--transcript",
                "-",
                "--project",
                "cling_packet",
                "--task",
                "Capture Packet RX performance matrix",
                "--scenario",
                "Packet RX performance",
                "--thread-id",
                "thread-stdin",
                "--artifact-root",
                str(artifact_root),
                "--capture-json-out",
                str(capture_json),
            ],
            input=self.transcript.read_text(encoding="utf-8"),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["artifact_count"], 1)
        capture = json.loads(capture_json.read_text(encoding="utf-8"))
        self.assertEqual(capture["thread_id"], "thread-stdin")
        artifact_json = next((artifact_root / "benchmarks" / "packet-rx").glob("*.json"))
        artifact = json.loads(artifact_json.read_text(encoding="utf-8"))
        self.assertEqual(artifact["source"]["path"], "<stdin>")

    def test_jsonl_loader_extracts_nested_codex_payload_output(self) -> None:
        transcript = self.test_root / "codex_session.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "timestamp": "2026-05-21T00:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "output": "\n".join(
                            [
                                "| case | A3.7 pps | A3.8 pps | change |",
                                "| --- | --- | --- | --- |",
                                "| 8-entry 连续 | 646,355 pps | 886,327 pps | +37.1% |",
                            ]
                        ),
                        "encrypted_content": "ignore-this",
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        extraction = extract_context_from_transcript(
            transcript_path=transcript,
            project="cling_packet",
            task="Capture Packet RX performance matrix",
            scenario="Packet RX performance",
            thread_id="thread-jsonl",
            artifact_root=self.test_root / "jsonl_artifacts",
        )

        self.assertEqual(len(extraction["artifacts"]), 1)
        self.assertEqual(extraction["artifacts"][0]["records"][0]["case"], "8-entry 连续")
        self.assertFalse(any("ignore-this" in chunk["text"] for chunk in extraction["chunks"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
