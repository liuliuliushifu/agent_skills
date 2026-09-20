#!/usr/bin/env python3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capture_schema import normalize_capture_input
from handoff_writer import render_handoff_markdown, write_handoff


SCRIPT_DIR = Path(__file__).resolve().parents[1]


class HandoffWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.test_root = Path(tempfile.mkdtemp(prefix="reme_handoff_", dir="/tmp"))

    def tearDown(self) -> None:
        shutil.rmtree(self.test_root, ignore_errors=True)

    def _sample_capture(self) -> dict:
        return {
            "project": "cling_glb",
            "task": "Implement capture_context",
            "scenario": "Write deterministic handoff files",
            "session_summary": "Phase 1 writes markdown and json directly.",
            "subsystem": ["reme-memory", "handoff"],
            "facts": [{"text": "handoff is direct-write", "evidence": "plan", "confidence": "high"}],
            "decisions": [{"decision": "use sidecar json", "rationale": "audit clean", "status": "active"}],
            "constraints": ["Do not touch daemon in Phase 1"],
            "open_issues": ["Need bus capture path in Phase 2"],
            "next_steps": ["Implement capture_schema.py"],
            "key_files": [str(SCRIPT_DIR / "handoff_writer.py")],
            "aliases": ["handoff", "structured memory"],
        }

    def test_stable_scope_overwrites_existing_pair(self) -> None:
        raw = self._sample_capture()
        first = normalize_capture_input(
            raw,
            thread_id_override="thread-001",
            created_at="2026-04-21T18:30:00+08:00",
        )
        second = normalize_capture_input(
            raw,
            thread_id_override="thread-001",
            created_at="2026-04-22T09:00:00+08:00",
        )

        first_write = write_handoff(first, handoff_root=self.test_root)
        second_write = write_handoff(second, handoff_root=self.test_root)

        md_files = sorted(self.test_root.glob("*.md"))
        json_files = sorted(self.test_root.glob("*.json"))
        self.assertEqual(len(md_files), 1)
        self.assertEqual(len(json_files), 1)
        self.assertFalse(first_write["overwrote"])
        self.assertTrue(second_write["overwrote"])
        payload = json.loads(json_files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["created_at"], "2026-04-22T09:00:00+08:00")
        self.assertTrue(json_files[0].name.startswith("2026-04-21-"))

    def test_no_scope_creates_append_only_files(self) -> None:
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

        write_handoff(first, handoff_root=self.test_root)
        write_handoff(second, handoff_root=self.test_root)
        self.assertEqual(len(list(self.test_root.glob("*.md"))), 2)
        self.assertEqual(len(list(self.test_root.glob("*.json"))), 2)

    def test_markdown_section_order_is_stable(self) -> None:
        capture = normalize_capture_input(self._sample_capture(), thread_id_override="thread-001")
        markdown = render_handoff_markdown(capture, "capture.json")
        expected_order = [
            "## Metadata",
            "## Task",
            "## Session Summary",
            "## Facts",
            "## Decisions",
            "## Constraints",
            "## Open Issues",
            "## Next Steps",
            "## Key Files",
            "## Aliases",
            "## Audit",
        ]
        offsets = [markdown.index(section) for section in expected_order]
        self.assertEqual(offsets, sorted(offsets))


if __name__ == "__main__":
    unittest.main(verbosity=2)
