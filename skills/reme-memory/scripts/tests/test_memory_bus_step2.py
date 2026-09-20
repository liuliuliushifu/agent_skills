#!/usr/bin/env python3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import multiprocessing
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from memory_bus_client import (
    enqueue_flush,
    enqueue_query,
    enqueue_status,
    enqueue_write,
    new_request_id,
    result_path,
    wait_for_result,
)
from memory_bus_io import atomic_write_json, read_json
from memory_bus_paths import build_bus_paths, ensure_bus_dirs


def _enqueue_write_worker(root: str, index: int) -> None:
    paths = build_bus_paths(Path(root))
    enqueue_write(
        lesson=f"lesson-{index}",
        task="step2",
        outcome="accepted",
        paths=paths,
        request_id=f"req_step2_{index:03d}",
    )


class MemoryBusStep2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.test_root = Path(tempfile.mkdtemp(prefix="reme_bus_step2_", dir="/tmp"))
        self.paths = build_bus_paths(self.test_root)
        ensure_bus_dirs(self.paths)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_enqueue_write(self) -> None:
        accepted = enqueue_write(
            lesson="后台写入记忆",
            task="step2",
            outcome="ok",
            paths=self.paths,
            request_id="req_step2_write_001",
        )
        payload = read_json(accepted.request_path)
        self.assertEqual(payload["request_type"], "memory_write")
        self.assertEqual(payload["payload"]["lesson"], "后台写入记忆")

    def test_enqueue_query(self) -> None:
        accepted = enqueue_query(
            query="packet-processing timeout",
            max_results=3,
            paths=self.paths,
            request_id="req_step2_query_001",
        )
        payload = read_json(accepted.request_path)
        self.assertEqual(payload["request_type"], "memory_query")
        self.assertEqual(payload["payload"]["max_results"], 3)

    def test_enqueue_status_and_flush(self) -> None:
        status_request = enqueue_status(
            target_request_id="req_target_001",
            paths=self.paths,
            request_id="req_step2_status_001",
        )
        flush_request = enqueue_flush(paths=self.paths, request_id="req_step2_flush_001")
        self.assertTrue(status_request.request_path.exists())
        self.assertTrue(flush_request.request_path.exists())

    def test_new_request_id_uniqueness(self) -> None:
        ids = {new_request_id() for _ in range(32)}
        self.assertEqual(len(ids), 32)

    def test_wait_for_result(self) -> None:
        accepted = enqueue_query(
            query="packet-processing timeout",
            paths=self.paths,
            request_id="req_step2_query_002",
        )

        def _delayed_result() -> None:
            payload = {
                "request_id": accepted.request_id,
                "status": "answered",
                "items": [{"path": "/tmp/sample.md", "score": 0.88}],
            }
            atomic_write_json(result_path(self.paths, accepted.request_id), payload)

        timer = threading.Timer(0.2, _delayed_result)
        timer.start()
        try:
            result = wait_for_result(accepted.request_id, timeout_seconds=2.0, paths=self.paths)
            self.assertEqual(result["status"], "answered")
        finally:
            timer.cancel()

    def test_wait_for_result_timeout(self) -> None:
        accepted = enqueue_query(
            query="packet-processing timeout",
            paths=self.paths,
            request_id="req_step2_query_003",
        )
        with self.assertRaises(TimeoutError):
            wait_for_result(accepted.request_id, timeout_seconds=0.2, poll_interval_seconds=0.05, paths=self.paths)

    def test_concurrent_high_level_enqueues(self) -> None:
        processes = []
        for index in range(20):
            process = multiprocessing.Process(target=_enqueue_write_worker, args=(str(self.test_root), index))
            process.start()
            processes.append(process)
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        files = sorted(self.paths.inbox_write.glob("*.json"))
        self.assertEqual(len(files), 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
