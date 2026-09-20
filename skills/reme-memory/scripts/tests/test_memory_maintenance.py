#!/usr/bin/env python3
import datetime as dt
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from maintenance_adapter import apply_memory_maintenance
from memory_block_renderer import parse_memory_blocks, write_memory_block


class FakeRuntimeWorker:
    def __init__(self) -> None:
        self.upserts = []
        self.deletes = []

    def upsert_memory_file(self, path: str) -> dict:
        self.upserts.append(path)
        return {"indexed_files": 1}

    def delete_memory_file(self, path: str) -> dict:
        self.deletes.append(path)
        return {"deleted_files": 1}

    def sync_memory_files(self, *, upsert_paths: list[str], delete_paths: list[str]) -> dict:
        self.upserts.extend(upsert_paths)
        self.deletes.extend(delete_paths)
        return {
            "indexed_files": len(upsert_paths),
            "deleted_files": len(delete_paths),
        }


class MemoryMaintenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="reme_maintenance_", dir="/tmp"))
        self.memory_dir = self.root / "memory"
        self.compact_dir = self.root / "compact_memory"
        self.archive_dir = self.root / "compact_archive"
        self.backup_dir = self.root / "backups"
        self.memory_dir.mkdir()
        self.compact_dir.mkdir()
        self.worker = FakeRuntimeWorker()
        self.now = dt.datetime(2026, 7, 24, 12, 0, tzinfo=dt.timezone.utc)
        self._write_durable_samples()
        self._write_compact("2026-07-14-cap_active.md", "2026-07-14T12:00:00+00:00")
        self._write_compact("2026-06-14-cap_archive.md", "2026-06-14T12:00:00+00:00")
        self._write_compact("2026-04-15-cap_delete.md", "2026-04-15T12:00:00+00:00")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_durable_samples(self) -> None:
        base = {
            "applicability": "test",
            "root_cause_patterns": [],
            "solutions": [],
            "key_locations": {"files": [], "symbols": [], "errors": []},
            "benchmark_names": [],
            "confidence": "high",
            "evidence_hashes": [],
            "review_after": "",
            "supersedes": [],
        }
        write_memory_block(
            {
                **base,
                "topic": "fallback",
                "conclusions": ["[ReMe async refine fallback] noisy output"],
                "aliases": ["reme-refine", "reme-refine-fallback"],
                "retrieval_surface": "fallback",
                "source_capture_id": "cap_fallback",
                "evidence_at": "2026-07-20T12:00:00+00:00",
            },
            {
                "capture_id": "cap_fallback",
                "durable_idempotency_key": "write_fallback",
                "created_at": "2026-07-20T12:00:00+00:00",
            },
            memory_root=self.memory_dir,
        )
        write_memory_block(
            {
                **base,
                "topic": "good",
                "conclusions": ["keep this"],
                "aliases": ["good"],
                "retrieval_surface": "good",
                "source_capture_id": "cap_good",
                "evidence_at": "2026-07-20T12:00:00+00:00",
            },
            {
                "capture_id": "cap_good",
                "durable_idempotency_key": "write_good",
                "created_at": "2026-07-20T12:00:00+00:00",
            },
            memory_root=self.memory_dir,
        )

    def _write_compact(self, name: str, created_at: str) -> None:
        (self.compact_dir / name).write_text(
            f"# Compact Memory - {Path(name).stem}\n\nCreated At: {created_at}\n\ncontent\n",
            encoding="utf-8",
        )

    def _run(self, *, dry_run: bool, request_id: str) -> dict:
        payload = {
            "maintenance_date": "2026-07-24",
            "cleanup_fallback": True,
            "compact_retention": True,
            "compact_active_days": 30,
            "compact_delete_days": 90,
            "dry_run": dry_run,
        }
        with (
            patch("maintenance_adapter.get_memory_dir", return_value=self.memory_dir),
            patch("maintenance_adapter.get_compact_memory_dir", return_value=self.compact_dir),
            patch("maintenance_adapter.get_compact_archive_dir", return_value=self.archive_dir),
            patch.dict(os.environ, {"REME_MAINTENANCE_BACKUP_ROOT": str(self.backup_dir)}),
        ):
            return apply_memory_maintenance(
                payload,
                runtime_worker=self.worker,
                request_id=request_id,
                now=self.now,
            )

    def test_dry_run_reports_without_mutating(self) -> None:
        result = self._run(dry_run=True, request_id="dry-run")
        self.assertEqual(result["fallback_blocks_removed"], 1)
        self.assertEqual(result["compact_files_archived"], 1)
        self.assertEqual(result["compact_files_deleted"], 1)
        self.assertEqual(len(list(self.compact_dir.glob("*.md"))), 3)
        self.assertFalse(self.archive_dir.exists())
        self.assertEqual(self.worker.upserts, [])
        self.assertEqual(self.worker.deletes, [])

    def test_apply_cleans_fallback_and_enforces_30_90(self) -> None:
        result = self._run(dry_run=False, request_id="apply")
        durable_path = self.memory_dir / "2026-07-20.md"
        blocks = parse_memory_blocks(durable_path.read_text(encoding="utf-8"))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["durable_idempotency_key"], "write_good")
        self.assertTrue((self.backup_dir / "apply" / "durable-2026-07-20.md").is_file())
        self.assertTrue((self.compact_dir / "2026-07-14-cap_active.md").is_file())
        self.assertTrue((self.archive_dir / "2026-06-14-cap_archive.md").is_file())
        self.assertFalse((self.compact_dir / "2026-04-15-cap_delete.md").exists())
        self.assertFalse((self.archive_dir / "2026-04-15-cap_delete.md").exists())
        self.assertEqual(result["index_files_upserted"], 1)
        self.assertEqual(result["index_files_deleted"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
