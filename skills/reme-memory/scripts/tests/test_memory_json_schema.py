#!/usr/bin/env python3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import unittest

from memory_json_schema import normalize_memory_json


class MemoryJsonSchemaTest(unittest.TestCase):
    def test_valid_memory_json(self) -> None:
        payload = normalize_memory_json(
            {
                "topic": "Structured capture architecture",
                "applicability": "When Codex needs deterministic handoff and durable memory",
                "conclusions": ["Use capture_context for handoff"],
                "root_cause_patterns": ["summary_memory drops stable fields"],
                "solutions": ["Use structured renderer"],
                "key_locations": {
                    "files": ["/tmp/example.py"],
                    "symbols": ["capture_context"],
                    "errors": ["summary timeout"],
                },
                "aliases": ["handoff"],
                "benchmark_names": ["capture-latency"],
                "retrieval_surface": "handoff capture_context summary timeout",
                "confidence": "high",
                "source_capture_id": "cap_test_001",
                "review_after": "",
                "supersedes": [],
            }
        )
        self.assertEqual(payload["topic"], "Structured capture architecture")
        self.assertEqual(payload["confidence"], "high")

    def test_invalid_confidence_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_memory_json(
                {
                    "topic": "Structured capture architecture",
                    "applicability": "When Codex needs deterministic handoff and durable memory",
                    "conclusions": ["Use capture_context for handoff"],
                    "root_cause_patterns": [],
                    "solutions": [],
                    "key_locations": {"files": [], "symbols": [], "errors": []},
                    "aliases": [],
                    "benchmark_names": [],
                    "retrieval_surface": "handoff capture_context",
                    "confidence": "certain",
                    "source_capture_id": "cap_test_001",
                    "review_after": "",
                    "supersedes": [],
                }
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
