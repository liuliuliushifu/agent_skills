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

from memory_block_renderer import parse_memory_blocks, reconcile_memory_block, write_memory_block


class MemoryBlockRendererTest(unittest.TestCase):
    def setUp(self) -> None:
        self.test_root = Path(tempfile.mkdtemp(prefix="reme_block_renderer_", dir="/tmp"))

    def tearDown(self) -> None:
        shutil.rmtree(self.test_root, ignore_errors=True)

    def _capture(self, capture_id: str) -> dict:
        return {
            "capture_id": capture_id,
            "durable_idempotency_key": "sha256:durable-test",
            "created_at": "2026-04-21T18:30:00+08:00",
        }

    def _memory_json(self, aliases, files, errors) -> dict:
        return {
            "topic": "Structured capture architecture",
            "applicability": "When Codex needs durable memory",
            "conclusions": ["Use structured writer"],
            "root_cause_patterns": ["soft prompts are not enough"],
            "solutions": ["Use deterministic renderer"],
            "key_locations": {
                "files": files,
                "symbols": ["capture_context"],
                "errors": errors,
            },
            "aliases": aliases,
            "benchmark_names": ["capture-latency"],
            "retrieval_surface": "capture_context structured writer",
            "confidence": "high",
            "source_capture_id": "cap_test",
            "review_after": "",
            "supersedes": [],
        }

    def test_renderer_appends_and_then_merges_retrieval_fields(self) -> None:
        first = write_memory_block(
            self._memory_json(["handoff"], ["/tmp/first.py"], ["summary timeout"]),
            self._capture("cap_first"),
            memory_root=self.test_root,
        )
        second = write_memory_block(
            self._memory_json(["structured memory"], ["/tmp/second.py"], ["index timeout"]),
            self._capture("cap_second"),
            memory_root=self.test_root,
        )

        target = Path(first["path"])
        blocks = parse_memory_blocks(target.read_text(encoding="utf-8"))
        self.assertEqual(len(blocks), 1)
        meta_memory = blocks[0]["meta"]["memory_json"]
        self.assertTrue(second["replaced"])
        self.assertEqual(meta_memory["aliases"], ["handoff", "structured memory"])
        self.assertEqual(meta_memory["key_locations"]["files"], ["/tmp/first.py", "/tmp/second.py"])
        self.assertEqual(meta_memory["key_locations"]["errors"], ["index timeout", "summary timeout"])

    def test_reconcile_merge_updates_existing_block_and_latest_evidence_time(self) -> None:
        first_memory = self._memory_json(["manual"], ["/tmp/first.py"], [])
        first_memory["evidence_at"] = "2026-04-20T10:00:00+08:00"
        first_memory["evidence_hashes"] = ["sha256:old"]
        first = write_memory_block(
            first_memory,
            self._capture("cap_manual"),
            memory_root=self.test_root,
        )
        new_memory = self._memory_json(["refined"], ["/tmp/second.py"], [])
        new_memory["conclusions"] = ["Use structured writer with reconciliation"]
        new_memory["evidence_at"] = "2026-04-22T10:00:00+08:00"
        new_memory["evidence_hashes"] = ["sha256:new"]
        result = reconcile_memory_block(
            new_memory,
            {
                "capture_id": "cap_refined",
                "durable_idempotency_key": "write_new",
                "created_at": "2026-04-22T10:00:00+08:00",
            },
            action="merge",
            target_path=first["path"],
            target_durable_idempotency_key="sha256:durable-test",
            memory_root=self.test_root,
        )
        text = Path(result["path"]).read_text(encoding="utf-8")
        blocks = parse_memory_blocks(text)
        self.assertEqual(len(blocks), 1)
        memory_json = blocks[0]["meta"]["memory_json"]
        self.assertEqual(
            memory_json["conclusions"],
            ["Use structured writer", "Use structured writer with reconciliation"],
        )
        self.assertEqual(memory_json["evidence_hashes"], ["sha256:new", "sha256:old"])
        self.assertEqual(memory_json["evidence_at"], "2026-04-22T10:00:00+08:00")
        self.assertEqual(
            blocks[0]["meta"]["source_capture_history"],
            ["cap_manual", "cap_refined"],
        )
        self.assertIn("Updated At: 2026-04-22T10:00:00+08:00", text)

    def test_reconcile_overwrite_replaces_conclusion_but_keeps_evidence_history(self) -> None:
        first_memory = self._memory_json(["manual"], [], [])
        first_memory["evidence_at"] = "2026-04-20T10:00:00+08:00"
        first_memory["evidence_hashes"] = ["sha256:old"]
        first = write_memory_block(
            first_memory,
            self._capture("cap_manual"),
            memory_root=self.test_root,
        )
        new_memory = self._memory_json(["refined"], [], [])
        new_memory["conclusions"] = ["New authoritative conclusion"]
        new_memory["evidence_at"] = "2026-04-23T10:00:00+08:00"
        new_memory["evidence_hashes"] = ["sha256:new"]
        reconcile_memory_block(
            new_memory,
            {
                "capture_id": "cap_refined",
                "durable_idempotency_key": "write_new",
                "created_at": "2026-04-23T10:00:00+08:00",
            },
            action="overwrite",
            target_path=first["path"],
            target_durable_idempotency_key="sha256:durable-test",
            memory_root=self.test_root,
        )
        blocks = parse_memory_blocks(Path(first["path"]).read_text(encoding="utf-8"))
        memory_json = blocks[0]["meta"]["memory_json"]
        self.assertEqual(memory_json["conclusions"], ["New authoritative conclusion"])
        self.assertEqual(memory_json["evidence_hashes"], ["sha256:new", "sha256:old"])
        self.assertEqual(memory_json["evidence_at"], "2026-04-23T10:00:00+08:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
