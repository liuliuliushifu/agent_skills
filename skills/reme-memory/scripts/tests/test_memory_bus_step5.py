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
from unittest.mock import patch

from index_adapter import handle_index_request
from memory_bus_client import enqueue_index, enqueue_write
from memory_bus_paths import build_bus_paths, ensure_bus_dirs
from memory_request_store import MemoryRequestStore
from write_adapter import PartialWriteCommittedError, RetryableWriteError, handle_write_request


class MemoryBusStep5Test(unittest.TestCase):
    def setUp(self) -> None:
        self.test_root = Path(tempfile.mkdtemp(prefix="reme_bus_step5_", dir="/tmp"))
        self.paths = build_bus_paths(self.test_root)
        ensure_bus_dirs(self.paths)
        self.store = MemoryRequestStore(paths=self.paths, daemon_id="daemon-step5", lease_seconds=3)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_root, ignore_errors=True)

    def _claim_write(self, request_id: str, lesson: str = "lesson"):
        enqueue_write(lesson=lesson, paths=self.paths, request_id=request_id)
        claimed = self.store.claim_next("memory_write")
        self.assertIsNotNone(claimed)
        return claimed

    def test_stub_success_write(self) -> None:
        claimed = self._claim_write("req_step5_write_001")

        def backend(_payload):
            return {"committed": True, "message": "ok"}

        def index_backend():
            return {"indexed_files": 1}

        handle_write_request(claimed, self.store, backend=backend, index_backend=index_backend)
        commit_files = sorted(self.paths.archive_completed.glob("sha256_*.commit.json"))
        self.assertEqual(len(commit_files), 1)
        archived_requests = sorted(self.paths.archive_completed.glob("req_step5_write_001.json"))
        self.assertEqual(len(archived_requests), 1)
        raw_request = self.paths.archive_raw / "req_step5_write_001.json"
        self.assertTrue(raw_request.exists())
        status = json.loads((self.paths.result / "req_step5_write_001.status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["phase"], "indexed")

    def test_idempotency_deduplicates_second_write(self) -> None:
        claimed_first = self._claim_write("req_step5_write_002", lesson="same lesson")
        backend_calls = {"count": 0}
        index_calls = {"count": 0}

        def backend(_payload):
            backend_calls["count"] += 1
            return {"committed": True}

        def index_backend():
            index_calls["count"] += 1
            return {"indexed_files": 1}

        handle_write_request(claimed_first, self.store, backend=backend, index_backend=index_backend)

        claimed_second = self._claim_write("req_step5_write_003", lesson="same lesson")
        self.assertEqual(claimed_first.request.idempotency_key, claimed_second.request.idempotency_key)
        handle_write_request(claimed_second, self.store, backend=backend, index_backend=index_backend)
        self.assertEqual(backend_calls["count"], 1)
        self.assertEqual(index_calls["count"], 1)

    def test_retryable_error_requeues(self) -> None:
        claimed = self._claim_write("req_step5_write_004")

        def backend(_payload):
            raise RetryableWriteError("temporary overload")

        handle_write_request(claimed, self.store, backend=backend)
        self.assertTrue((self.paths.processing_write / "req_step5_write_004.json").exists())
        status = json.loads((self.paths.result / "req_step5_write_004.status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "retrying")
        self.assertEqual(status["attempt"], 1)
        self.assertEqual(status["last_error"], "unknown RetryableWriteError")
        self.assertEqual(status["last_error_meta"]["error_type"], "RetryableWriteError")
        self.assertTrue(status["last_error_meta"]["fingerprint"])

        recovered = self.store.recover_processing(now_epoch=10**10)
        self.assertEqual(recovered, 1)
        claimed_again = self.store.claim_next("memory_write")
        self.assertIsNotNone(claimed_again)
        handle_write_request(claimed_again, self.store, backend=backend)
        status = json.loads((self.paths.result / "req_step5_write_004.status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["attempt"], 2)

    def test_partial_write_committed(self) -> None:
        claimed = self._claim_write("req_step5_write_005")

        def backend(_payload):
            raise PartialWriteCommittedError("committed before timeout")

        def index_backend():
            return {"indexed_files": 1}

        handle_write_request(claimed, self.store, backend=backend, index_backend=index_backend)
        commit_files = sorted(self.paths.archive_completed.glob("sha256_*.commit.json"))
        self.assertEqual(len(commit_files), 1)
        archived_requests = sorted(self.paths.archive_completed.glob("req_step5_write_005.json"))
        self.assertEqual(len(archived_requests), 1)

    def test_index_retry_skips_second_write(self) -> None:
        claimed = self._claim_write("req_step5_write_006")
        backend_calls = {"count": 0}
        index_calls = {"count": 0}

        def backend(_payload):
            backend_calls["count"] += 1
            return {"committed": True}

        def failing_index_backend():
            index_calls["count"] += 1
            raise RetryableWriteError("index timeout")

        handle_write_request(claimed, self.store, backend=backend, index_backend=failing_index_backend)
        status = json.loads((self.paths.result / "req_step5_write_006.status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "retrying")
        self.assertEqual(status["phase"], "apply_committed")
        self.assertEqual(status["last_error"], "retryable_index_error RetryableWriteError")
        self.assertEqual(status["last_error_meta"]["kind"], "retryable_index_error")

        recovered = self.store.recover_processing(now_epoch=10**10)
        self.assertEqual(recovered, 1)
        claimed_again = self.store.claim_next("memory_write")
        self.assertIsNotNone(claimed_again)

        def success_index_backend():
            index_calls["count"] += 1
            return {"indexed_files": 1}

        handle_write_request(claimed_again, self.store, backend=backend, index_backend=success_index_backend)
        self.assertEqual(backend_calls["count"], 1)
        self.assertEqual(index_calls["count"], 2)
        status = json.loads((self.paths.result / "req_step5_write_006.status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["phase"], "indexed")

    def test_runtime_worker_indexes_only_committed_memory_file(self) -> None:
        claimed = self._claim_write("req_step5_write_007")
        indexed_paths = []

        class RuntimeWorkerStub:
            def upsert_memory_file(self, memory_path):
                indexed_paths.append(memory_path)
                return {
                    "operation": "incremental_upsert",
                    "indexed_files": 1,
                    "indexed_chunks": 2,
                }

            def full_rebuild(self):
                raise AssertionError("manual memory writes must not trigger full rebuild")

        self.store.runtime_worker = RuntimeWorkerStub()

        def backend(_payload):
            return {
                "committed": True,
                "indexed": False,
                "path": "/tmp/2026-07-23.md",
            }

        handle_write_request(claimed, self.store, backend=backend)
        self.assertEqual(indexed_paths, ["/tmp/2026-07-23.md"])
        status = json.loads((self.paths.result / "req_step5_write_007.status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "stored")
        self.assertEqual(status["phase"], "indexed")

    def test_index_request_upserts_only_requested_compact_file(self) -> None:
        compact_dir = self.test_root / "compact_memory"
        compact_dir.mkdir()
        compact_path = compact_dir / "2026-07-23-cap_test.md"
        compact_path.write_text("# Compact memory\n\nDurable fact.\n", encoding="utf-8")
        enqueue_index(
            path=str(compact_path),
            paths=self.paths,
            request_id="req_step5_index_001",
        )
        claimed = self.store.claim_next("memory_write")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.request.request_type, "memory_index")
        indexed_paths = []

        def index_backend(path):
            indexed_paths.append(path)
            return {"operation": "incremental_upsert", "indexed_files": 1, "indexed_chunks": 1}

        with patch("index_adapter.get_indexable_memory_dirs", return_value=[compact_dir]):
            handle_index_request(claimed, self.store, index_backend=index_backend)

        self.assertEqual(indexed_paths, [str(compact_path.resolve())])
        status = json.loads((self.paths.result / "req_step5_index_001.status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "stored")
        self.assertEqual(status["phase"], "indexed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
