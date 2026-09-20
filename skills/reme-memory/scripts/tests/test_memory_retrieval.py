#!/usr/bin/env python3
import datetime as dt
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from memory_block_renderer import write_memory_block
from memory_retrieval import rerank_search_results


class MemoryRetrievalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="reme_retrieval_", dir="/tmp"))
        self.memory_dir = self.root / "memory"
        self.compact_dir = self.root / "compact_memory"
        self.memory_dir.mkdir()
        self.compact_dir.mkdir()
        self.now = dt.datetime(2026, 7, 24, 12, 0, tzinfo=dt.timezone.utc)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _result(self, path: Path, score: float, snippet: str = "memory"):
        return SimpleNamespace(
            path=str(path),
            start_line=1,
            end_line=80,
            score=score,
            snippet=snippet,
            source=SimpleNamespace(value="memory"),
            metadata={},
            raw_metric=1.0 - score,
        )

    def test_recency_bonus_is_capped_at_ten_percent(self) -> None:
        old_path = self.memory_dir / "2025-01-01.md"
        old_path.write_text("# old\n\nstable exact match\n", encoding="utf-8")
        recent_path = self.compact_dir / "2026-07-24-cap_recent.md"
        recent_path.write_text(
            "# Compact Memory - cap_recent\n\nCreated At: 2026-07-24T12:00:00+00:00\n\nrecent near match\n",
            encoding="utf-8",
        )
        items = rerank_search_results(
            [
                self._result(old_path, 1.0, "old"),
                self._result(recent_path, 0.92, "recent"),
            ],
            max_results=2,
            min_score=0.1,
            memory_dir=self.memory_dir,
            compact_memory_dir=self.compact_dir,
            recency_weight=0.10,
            recency_half_life_days=30,
            now=self.now,
        )
        self.assertEqual(items[0]["path"], str(recent_path))
        self.assertAlmostEqual(items[0]["score"], 1.012, places=3)
        self.assertEqual(items[0]["match_score"], 0.92)

        items = rerank_search_results(
            [
                self._result(old_path, 1.0, "old"),
                self._result(recent_path, 0.89, "recent"),
            ],
            max_results=2,
            min_score=0.1,
            memory_dir=self.memory_dir,
            compact_memory_dir=self.compact_dir,
            recency_weight=0.10,
            recency_half_life_days=30,
            now=self.now,
        )
        self.assertEqual(items[0]["path"], str(old_path))

    def test_same_lineage_returns_durable_and_attaches_compact_evidence(self) -> None:
        capture = {
            "capture_id": "cap_same",
            "durable_idempotency_key": "write_same",
            "created_at": "2026-07-24T11:00:00+00:00",
        }
        memory_json = {
            "topic": "same fact",
            "applicability": "same scope",
            "conclusions": ["same conclusion"],
            "root_cause_patterns": [],
            "solutions": [],
            "key_locations": {"files": [], "symbols": [], "errors": []},
            "aliases": [],
            "benchmark_names": [],
            "retrieval_surface": "same fact",
            "confidence": "high",
            "source_capture_id": "cap_same",
            "evidence_at": "2026-07-24T11:00:00+00:00",
            "evidence_hashes": [],
            "review_after": "",
            "supersedes": [],
        }
        durable = write_memory_block(memory_json, capture, memory_root=self.memory_dir)
        compact_path = self.compact_dir / "2026-07-24-cap_same.md"
        compact_path.write_text(
            "# Compact Memory - cap_same\n\nCreated At: 2026-07-24T11:00:00+00:00\n\nsame conclusion\n",
            encoding="utf-8",
        )
        items = rerank_search_results(
            [
                self._result(compact_path, 0.95),
                self._result(Path(durable["path"]), 0.94),
            ],
            max_results=5,
            min_score=0.1,
            memory_dir=self.memory_dir,
            compact_memory_dir=self.compact_dir,
            recency_weight=0.10,
            recency_half_life_days=30,
            now=self.now,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source_kind"], "durable")
        self.assertEqual(items[0]["metadata"]["collapsed_evidence_path"], str(compact_path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
