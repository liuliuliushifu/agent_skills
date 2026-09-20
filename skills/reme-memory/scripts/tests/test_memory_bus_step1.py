#!/usr/bin/env python3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import json
import multiprocessing
import shutil
import tempfile
import unittest
from pathlib import Path

from memory_bus_io import atomic_write_json, claim_file, file_mode, read_json
from memory_bus_paths import build_bus_paths, ensure_bus_dirs, validate_request_id
from memory_request_schema import MemoryRequest, MemoryRequestStatus, SCHEMA_VERSION, compute_idempotency_key


def _write_request(path: str, payload: dict) -> None:
    atomic_write_json(Path(path), payload)


class MemoryBusStep1Test(unittest.TestCase):
    def setUp(self) -> None:
        self.test_root = Path(tempfile.mkdtemp(prefix="reme_bus_step1_", dir="/tmp"))
        self.paths = build_bus_paths(self.test_root)
        ensure_bus_dirs(self.paths)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_request_roundtrip(self) -> None:
        request = MemoryRequest.new(
            request_id="req_20260420_00000001",
            request_type="memory_write",
            client_id="codex-main",
            project="cling_packet",
            language="zh",
            payload={
                "task": "调试写入",
                "outcome": "完成定位",
                "lesson": "要区分 sandbox 伪超时",
                "tags": ["reme", "timeout"],
                "durability": "high",
            },
        )
        payload = request.to_dict()
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        loaded = MemoryRequest.from_dict(payload)
        self.assertEqual(loaded.idempotency_key, request.idempotency_key)
        self.assertEqual(loaded.payload["lesson"], "要区分 sandbox 伪超时")

    def test_invalid_request_type(self) -> None:
        with self.assertRaises(ValueError):
            MemoryRequest.new(
                request_id="req_20260420_00000002",
                request_type="unknown",
                client_id="codex-main",
                project="cling_packet",
                language="zh",
                payload={},
            )

    def test_incremental_index_request_roundtrip(self) -> None:
        request = MemoryRequest.new(
            request_id="req_20260420_index_001",
            request_type="memory_index",
            client_id="codex-main",
            project="cling_packet",
            language="zh",
            payload={"path": "/tmp/memory/2026-04-20.md"},
        )
        loaded = MemoryRequest.from_dict(request.to_dict())
        self.assertEqual(loaded.request_type, "memory_index")
        self.assertEqual(loaded.payload["path"], "/tmp/memory/2026-04-20.md")

    def test_structured_reconcile_write_roundtrip(self) -> None:
        memory_json = {
            "topic": "packet-processing durable workflow",
            "applicability": "When packet-processing is tested",
            "conclusions": ["Use the durable workflow"],
            "root_cause_patterns": [],
            "solutions": ["Run the workflow"],
            "key_locations": {"files": [], "symbols": [], "errors": []},
            "aliases": ["packet"],
            "benchmark_names": [],
            "retrieval_surface": "packet-processing durable workflow",
            "confidence": "high",
            "source_capture_id": "cap_packet",
            "evidence_at": "2026-07-24T12:00:00+08:00",
            "evidence_hashes": ["sha256:evidence"],
            "review_after": "",
            "supersedes": [],
        }
        request = MemoryRequest.new(
            request_id="req_20260420_reconcile_001",
            request_type="memory_write",
            client_id="reme-refine",
            project="cling_packet",
            language="zh",
            payload={
                "lesson": "structured reconcile",
                "memory_json": memory_json,
                "reconcile": {
                    "action": "merge",
                    "target_path": "/tmp/memory/2026-07-23.md",
                    "target_durable_idempotency_key": "write_target",
                },
            },
        )
        loaded = MemoryRequest.from_dict(request.to_dict())
        self.assertEqual(loaded.payload["reconcile"]["action"], "merge")
        self.assertEqual(loaded.payload["memory_json"]["evidence_at"], "2026-07-24T12:00:00+08:00")

    def test_maintenance_request_roundtrip(self) -> None:
        request = MemoryRequest.new(
            request_id="req_20260420_maintenance_001",
            request_type="memory_maintenance",
            client_id="reme-maintenance",
            project="cling_packet",
            language="zh",
            payload={
                "maintenance_date": "2026-07-24",
                "cleanup_fallback": True,
                "compact_retention": True,
                "compact_active_days": 30,
                "compact_delete_days": 90,
                "dry_run": True,
            },
        )
        loaded = MemoryRequest.from_dict(request.to_dict())
        self.assertTrue(loaded.payload["dry_run"])
        self.assertEqual(loaded.payload["compact_delete_days"], 90)

    def test_request_id_validation(self) -> None:
        self.assertEqual(validate_request_id("req_20260420_00000003"), "req_20260420_00000003")
        with self.assertRaises(ValueError):
            validate_request_id("../bad")

    def test_schema_version_validation(self) -> None:
        request = MemoryRequest.new(
            request_id="req_20260420_00000004",
            request_type="memory_query",
            client_id="codex-main",
            project="cling_packet",
            language="zh",
            payload={"query": "packet-processing timeout"},
        ).to_dict()
        request["schema_version"] = 999
        with self.assertRaises(ValueError):
            MemoryRequest.from_dict(request)

    def test_idempotency_key_is_stable(self) -> None:
        payload_a = {
            "task": "调试写入",
            "outcome": "完成定位",
            "lesson": "要区分 sandbox 伪超时",
            "tags": ["reme", "timeout"],
            "durability": "high",
        }
        payload_b = {
            "durability": "high",
            "tags": ["reme", "timeout"],
            "lesson": "要区分 sandbox 伪超时",
            "outcome": "完成定位",
            "task": "调试写入",
        }
        key_a = compute_idempotency_key("memory_write", payload_a, "cling_packet", "zh")
        key_b = compute_idempotency_key("memory_write", payload_b, "cling_packet", "zh")
        self.assertEqual(key_a, key_b)

    def test_atomic_write_json(self) -> None:
        request = MemoryRequest.new(
            request_id="req_20260420_00000005",
            request_type="memory_status",
            client_id="codex-main",
            project="cling_packet",
            language="zh",
            payload={"target_request_id": "req_20260420_00000004"},
        ).to_dict()
        target = self.paths.inbox_status / "req_20260420_00000005.json"
        atomic_write_json(target, request)
        self.assertTrue(target.exists())
        self.assertEqual(read_json(target)["request_id"], "req_20260420_00000005")
        self.assertEqual(file_mode(target), 0o600)

    def test_claim_file(self) -> None:
        target = self.paths.inbox_query / "req_20260420_00000006.json"
        atomic_write_json(target, {"request_id": "req_20260420_00000006"})
        dst = self.paths.processing_query / target.name
        claim_file(target, dst)
        self.assertFalse(target.exists())
        self.assertTrue(dst.exists())

    def test_size_limit_and_unknown_field(self) -> None:
        with self.assertRaises(ValueError):
            MemoryRequest.new(
                request_id="req_20260420_00000007",
                request_type="memory_query",
                client_id="codex-main",
                project="cling_packet",
                language="zh",
                payload={"query": "a", "extra": "bad"},
            )

    def test_status_roundtrip(self) -> None:
        status = MemoryRequestStatus.new(
            request_id="req_20260420_00000008",
            state="processing",
            phase="apply_started",
            attempt=1,
            lease_owner="daemon-001",
        )
        status_json = json.dumps(status.to_dict(), ensure_ascii=False)
        loaded = json.loads(status_json)
        self.assertEqual(loaded["phase"], "apply_started")
        self.assertEqual(loaded["attempt"], 1)

    def test_concurrent_atomic_writes(self) -> None:
        processes = []
        for index in range(12):
            path = self.paths.inbox_write / f"req_20260420_0001_{index:02d}.json"
            payload = {"request_id": f"req_20260420_0001_{index:02d}", "index": index}
            process = multiprocessing.Process(target=_write_request, args=(str(path), payload))
            process.start()
            processes.append(process)
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        files = sorted(self.paths.inbox_write.glob("*.json"))
        self.assertEqual(len(files), 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
