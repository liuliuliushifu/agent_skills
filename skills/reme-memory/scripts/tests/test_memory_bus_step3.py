#!/usr/bin/env python3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from memory_bus_client import enqueue_query, enqueue_write
from memory_bus_paths import build_bus_paths, ensure_bus_dirs
from memory_daemon import MemoryDaemon
from memory_request_store import MemoryRequestStore


def _query_handler(claimed, store):
    store.complete_request(claimed, state="answered")


def _write_handler(claimed, store):
    store.update_phase(claimed.request.request_id, state="processing", phase="apply_started", attempt=0)
    store.update_phase(claimed.request.request_id, state="processing", phase="apply_committed", attempt=0)
    store.complete_request(claimed, state="stored")


def _failing_query_handler(_claimed, _store):
    raise RuntimeError("query backend boom")


class MemoryBusStep3Test(unittest.TestCase):
    def setUp(self) -> None:
        self.test_root = Path(tempfile.mkdtemp(prefix="reme_bus_step3_", dir="/tmp"))
        self.paths = build_bus_paths(self.test_root)
        ensure_bus_dirs(self.paths)
        self.store = MemoryRequestStore(paths=self.paths, daemon_id="daemon-test", lease_seconds=1)

    def tearDown(self) -> None:
        try:
            self.store.release_daemon_lock()
        except Exception:
            pass
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_daemon_double_start_rejected(self) -> None:
        self.store.acquire_daemon_lock()
        second = MemoryRequestStore(paths=self.paths, daemon_id="daemon-test-2", lease_seconds=1)
        with self.assertRaises(FileExistsError):
            second.acquire_daemon_lock()

    def test_stale_daemon_lock_can_be_recovered(self) -> None:
        self.store.acquire_daemon_lock()
        lock_path = self.paths.lock / "daemon.lock"
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        payload["heartbeat_at"] = "2000-01-01T00:00:00+0000"
        lock_path.write_text(json.dumps(payload), encoding="utf-8")

        second = MemoryRequestStore(
            paths=self.paths,
            daemon_id="daemon-test-2",
            lease_seconds=1,
            lock_stale_seconds=1,
        )
        second.acquire_daemon_lock()
        second.release_daemon_lock()

    def test_dead_pid_daemon_lock_can_be_recovered(self) -> None:
        self.store.acquire_daemon_lock()
        lock_path = self.paths.lock / "daemon.lock"
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        payload["pid"] = 999999999
        lock_path.write_text(json.dumps(payload), encoding="utf-8")

        second = MemoryRequestStore(
            paths=self.paths,
            daemon_id="daemon-test-2",
            lease_seconds=1,
            lock_stale_seconds=9999,
        )
        second.acquire_daemon_lock()
        second.release_daemon_lock()

    def test_release_daemon_lock_does_not_remove_other_owner_lock(self) -> None:
        owner = MemoryRequestStore(paths=self.paths, daemon_id="daemon-owner", lease_seconds=1)
        owner.acquire_daemon_lock()
        other = MemoryRequestStore(paths=self.paths, daemon_id="daemon-other", lease_seconds=1)
        other.release_daemon_lock()
        self.assertTrue((self.paths.lock / "daemon.lock").exists())
        owner.release_daemon_lock()

    def test_recover_processing_without_status(self) -> None:
        accepted = enqueue_write(
            lesson="recover me",
            paths=self.paths,
            request_id="req_step3_write_001",
        )
        claimed = self.store.claim_next("memory_write")
        self.assertIsNotNone(claimed)
        claimed.status_path.unlink()
        recovered = self.store.recover_processing(now_epoch=10**10)
        self.assertEqual(recovered, 1)
        self.assertTrue((self.paths.inbox_write / accepted.request_path.name).exists())

    def test_retry_and_recovery(self) -> None:
        enqueue_write(
            lesson="retry me",
            paths=self.paths,
            request_id="req_step3_write_002",
        )
        claimed = self.store.claim_next("memory_write")
        self.assertIsNotNone(claimed)
        self.store.schedule_retry(claimed, attempt=1, delay_seconds=0, last_error="temporary")
        claimed_again = self.store.claim_next("memory_write")
        self.assertIsNotNone(claimed_again)
        status = self.store.read_status(claimed_again.request.request_id)
        self.assertEqual(status["attempt"], 1)

    def test_delayed_retry_stays_in_processing_until_due(self) -> None:
        enqueue_write(
            lesson="retry later",
            paths=self.paths,
            request_id="req_step3_write_005",
        )
        claimed = self.store.claim_next("memory_write")
        self.assertIsNotNone(claimed)
        retry_path = self.store.schedule_retry(claimed, attempt=1, delay_seconds=60, last_error="temporary")
        self.assertEqual(retry_path, claimed.processing_path)
        self.assertTrue(claimed.processing_path.exists())
        self.assertFalse((self.paths.inbox_write / "req_step3_write_005.json").exists())
        recovered_early = self.store.recover_processing(now_epoch=time.time())
        self.assertEqual(recovered_early, 0)
        status = self.store.read_status(claimed.request.request_id)
        self.assertEqual(status["state"], "retrying")
        self.assertEqual(status["last_error"], "unknown Error")
        self.assertEqual(status["last_error_meta"]["kind"], "unknown")
        self.assertEqual(status["last_error_meta"]["error_type"], "Error")
        self.assertTrue(status["last_error_meta"]["fingerprint"])
        recovered_late = self.store.recover_processing(now_epoch=10**10)
        self.assertEqual(recovered_late, 1)
        self.assertTrue((self.paths.inbox_write / "req_step3_write_005.json").exists())

    def test_daemon_heartbeat_refreshes_lock(self) -> None:
        heartbeat_store = MemoryRequestStore(
            paths=self.paths,
            daemon_id="daemon-heartbeat",
            lease_seconds=1,
            lock_stale_seconds=2,
        )
        daemon = MemoryDaemon(
            store=heartbeat_store,
            poll_interval_seconds=0.05,
            heartbeat_interval_seconds=0.1,
        )
        daemon.start()
        try:
            time.sleep(1.3)
            second = MemoryRequestStore(
                paths=self.paths,
                daemon_id="daemon-second",
                lease_seconds=1,
                lock_stale_seconds=2,
            )
            with self.assertRaises(FileExistsError):
                second.acquire_daemon_lock()
        finally:
            daemon.stop()

    def test_complete_request_archives(self) -> None:
        enqueue_query(
            query="packet-processing timeout",
            paths=self.paths,
            request_id="req_step3_query_001",
        )
        claimed = self.store.claim_next("memory_query")
        self.assertIsNotNone(claimed)
        archive_path = self.store.complete_request(claimed, state="answered")
        self.assertTrue(archive_path.exists())
        self.assertFalse(claimed.processing_path.exists())

    def test_deadletter(self) -> None:
        enqueue_query(
            query="packet-processing timeout",
            paths=self.paths,
            request_id="req_step3_query_002",
        )
        claimed = self.store.claim_next("memory_query")
        self.assertIsNotNone(claimed)
        deadletter_path = self.store.fail_request(claimed, "boom")
        self.assertTrue(deadletter_path.exists())

    def test_daemon_process_once(self) -> None:
        daemon = MemoryDaemon(store=self.store, poll_interval_seconds=0.01)
        daemon.register_handler("memory_write", _write_handler)
        daemon.register_handler("memory_query", _query_handler)
        enqueue_write(
            lesson="process write",
            paths=self.paths,
            request_id="req_step3_write_003",
        )
        enqueue_query(
            query="packet-processing timeout",
            paths=self.paths,
            request_id="req_step3_query_003",
        )
        daemon.start()
        try:
            self.assertTrue(daemon.process_once())
            self.assertTrue(daemon.process_once())
            self.assertFalse(daemon.process_once())
        finally:
            daemon.stop()

    def test_handler_exception_is_isolated(self) -> None:
        daemon = MemoryDaemon(
            store=self.store,
            poll_interval_seconds=0.01,
            request_lease_interval_seconds=0.1,
        )
        daemon.register_handler("memory_query", _failing_query_handler)
        enqueue_query(
            query="packet-processing broken",
            paths=self.paths,
            request_id="req_step3_query_004",
        )
        daemon.start()
        try:
            self.assertTrue(daemon.process_once())
            self.assertTrue((self.paths.deadletter / "req_step3_query_004.json").exists())
            result = json.loads((self.paths.result / "req_step3_query_004.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"], "unknown RuntimeError")
            self.assertEqual(result["error_meta"]["kind"], "unknown")
            self.assertEqual(result["error_meta"]["error_type"], "RuntimeError")
            self.assertTrue(result["error_meta"]["fingerprint"])
        finally:
            daemon.stop()

    def test_committed_phase_is_persisted(self) -> None:
        enqueue_write(
            lesson="commit phase",
            paths=self.paths,
            request_id="req_step3_write_006",
        )
        claimed = self.store.claim_next("memory_write")
        self.assertIsNotNone(claimed)
        self.store.update_phase(claimed.request.request_id, state="processing", phase="apply_committed", attempt=0)
        status = (self.paths.result / f"{claimed.request.request_id}.status.json").read_text(encoding="utf-8")
        self.assertIn("apply_committed", status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
