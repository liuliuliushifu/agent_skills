#!/usr/bin/env python3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import unittest
from pathlib import Path

from capture_schema import has_stable_session_scope, normalize_capture_input


SCRIPT_DIR = Path(__file__).resolve().parents[1]


class CaptureSchemaTest(unittest.TestCase):
    def _sample_capture(self) -> dict:
        return {
            "project": "cling_glb",
            "task": "Design structured capture path",
            "scenario": "Replace summary_memory with deterministic capture",
            "session_summary": "Phase 1 should write handoff files directly from Codex.",
            "subsystem": ["reme-memory", "handoff"],
            "facts": [{"text": "summary_memory is not deterministic", "confidence": "high"}],
            "decisions": [{"decision": "handoff is direct-write", "status": "active"}],
            "constraints": ["Do not modify upstream ReMe package"],
            "key_files": [str(SCRIPT_DIR / "memory_workflow.py")],
            "aliases": ["structured memory", "handoff"],
            "durable_identity": "ReMe structured capture architecture",
        }

    def test_thread_scoped_capture_is_stable_across_created_at(self) -> None:
        raw = self._sample_capture()
        first = normalize_capture_input(
            raw,
            thread_id_override="thread-123",
            created_at="2026-04-21T18:30:00+08:00",
        )
        second = normalize_capture_input(
            raw,
            thread_id_override="thread-123",
            created_at="2026-04-22T09:00:00+08:00",
        )

        self.assertTrue(has_stable_session_scope(first))
        self.assertEqual(first["capture_id"], second["capture_id"])
        self.assertEqual(first["handoff_idempotency_key"], second["handoff_idempotency_key"])
        self.assertEqual(first["durable_idempotency_key"], second["durable_idempotency_key"])

    def test_no_scope_capture_generates_unique_append_only_identity(self) -> None:
        raw = self._sample_capture()
        first = normalize_capture_input(
            raw,
            created_at="2026-04-21T18:30:00+08:00",
            random_suffix_factory=lambda: "aaaabbbb",
        )
        second = normalize_capture_input(
            raw,
            created_at="2026-04-21T18:31:00+08:00",
            random_suffix_factory=lambda: "ccccdddd",
        )

        self.assertFalse(has_stable_session_scope(first))
        self.assertNotEqual(first["capture_id"], second["capture_id"])
        self.assertNotEqual(first["handoff_idempotency_key"], second["handoff_idempotency_key"])
        self.assertIn("aaaabbbb", first["capture_id"])
        self.assertIn("ccccdddd", second["capture_id"])

    def test_durable_identity_normalization_stabilizes_durable_key(self) -> None:
        first = self._sample_capture()
        second = self._sample_capture()
        first["durable_identity"] = " ReMe   Structured Capture Architecture "
        second["durable_identity"] = "reme structured capture architecture"

        first_capture = normalize_capture_input(first, thread_id_override="thread-a")
        second_capture = normalize_capture_input(second, thread_id_override="thread-b")
        self.assertEqual(first_capture["durable_idempotency_key"], second_capture["durable_idempotency_key"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
