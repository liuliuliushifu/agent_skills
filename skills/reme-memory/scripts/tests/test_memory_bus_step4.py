#!/usr/bin/env python3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from memory_bus_client import enqueue_query, result_path
from memory_bus_paths import build_bus_paths, ensure_bus_dirs
from memory_request_store import MemoryRequestStore, is_daemon_active_for_workdir
from index_rebuild_guard import (
    begin_protected_rebuild,
    commit_protected_rebuild,
    rebuild_paths,
    recover_interrupted_rebuild,
    rollback_protected_rebuild,
)
from query_adapter import handle_query_request
from rebuild_index import _acquire_rebuild_lock, _release_rebuild_lock


SCRIPT_DIR = Path(__file__).resolve().parents[1]
REBUILD_SCRIPT = SCRIPT_DIR / "rebuild_index.py"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
USER_ROOT = Path(os.environ.get("CODEX_USER_HOME", str(CODEX_HOME.parent)))
REME_HOME = Path(os.environ.get("REME_HOME", str(USER_ROOT / ".local/share/reme")))
REME_PYTHON = os.environ.get("REME_PYTHON", str(REME_HOME / ".venv/bin/python"))


def _write_memory_sample(memory_dir: Path) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "2026-04-20.md").write_text(
        """# Memory

- packet-processing HostSync timeout 调试：
  出现确认竞争时，先补时间日志，再调整重试。
""",
        encoding="utf-8",
    )


class MemoryBusStep4Test(unittest.TestCase):
    def setUp(self) -> None:
        self.saved_env = os.environ.copy()
        self.bus_root = Path(tempfile.mkdtemp(prefix="reme_bus_step4_bus_", dir="/tmp"))
        self.reme_root = Path(tempfile.mkdtemp(prefix="reme_bus_step4_reme_", dir="/tmp"))
        os.environ.update(
            {
                "REME_WORKDIR": str(self.reme_root),
                "REME_HOME": str(self.reme_root / "reme-home"),
                "REME_RUNTIME_ENV_FILE": str(self.reme_root / "test-runtime.env"),
                "REME_VECTOR_ENABLED": "false",
                "REME_FTS_ENABLED": "true",
            }
        )
        self.paths = build_bus_paths(self.bus_root)
        ensure_bus_dirs(self.paths)
        self.store = MemoryRequestStore(paths=self.paths, daemon_id="daemon-step4", lease_seconds=3)
        _write_memory_sample(self.reme_root / "memory")

        env = os.environ.copy()
        env.update(
            {
                "REME_WORKDIR": str(self.reme_root),
                "REME_HOME": str(self.reme_root / "reme-home"),
                "REME_RUNTIME_ENV_FILE": str(self.reme_root / "test-runtime.env"),
                "REME_VECTOR_ENABLED": "false",
                "REME_FTS_ENABLED": "true",
            }
        )
        process = subprocess.run(
            [REME_PYTHON, str(REBUILD_SCRIPT), "--confirm-full-rebuild"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(process.stderr or process.stdout)
        self.rebuild_payload = json.loads(process.stdout)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.saved_env)
        shutil.rmtree(self.bus_root, ignore_errors=True)
        shutil.rmtree(self.reme_root, ignore_errors=True)

    def test_stub_backend_query(self) -> None:
        enqueue_query(query="packet-processing timeout", paths=self.paths, request_id="req_step4_query_001")
        claimed = self.store.claim_next("memory_query")
        self.assertIsNotNone(claimed)

        def backend(_payload):
            return {"status": "answered", "items": [{"path": "/tmp/stub.md", "score": 0.9}]}

        handle_query_request(claimed, self.store, backend=backend)
        result = json.loads(result_path(self.paths, claimed.request.request_id).read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["items"][0]["path"], "/tmp/stub.md")

    def test_real_temp_workdir_query(self) -> None:
        old_workdir = os.environ.get("REME_WORKDIR")
        old_vector = os.environ.get("REME_VECTOR_ENABLED")
        old_fts = os.environ.get("REME_FTS_ENABLED")
        os.environ["REME_WORKDIR"] = str(self.reme_root)
        os.environ["REME_VECTOR_ENABLED"] = "false"
        os.environ["REME_FTS_ENABLED"] = "true"
        try:
            enqueue_query(query="HostSync", paths=self.paths, request_id="req_step4_query_002")
            claimed = self.store.claim_next("memory_query")
            self.assertIsNotNone(claimed)
            handle_query_request(claimed, self.store)
        finally:
            _restore_env("REME_WORKDIR", old_workdir)
            _restore_env("REME_VECTOR_ENABLED", old_vector)
            _restore_env("REME_FTS_ENABLED", old_fts)

        result = json.loads(result_path(self.paths, "req_step4_query_002").read_text(encoding="utf-8"))
        self.assertTrue(result["items"])
        self.assertTrue(result["items"][0]["path"].endswith("2026-04-20.md"))

    def test_two_concurrent_stub_queries(self) -> None:
        request_ids = ["req_step4_query_003", "req_step4_query_004"]
        for request_id in request_ids:
            enqueue_query(query="packet-processing timeout", paths=self.paths, request_id=request_id)

        claimed_items = [self.store.claim_next("memory_query"), self.store.claim_next("memory_query")]
        self.assertTrue(all(item is not None for item in claimed_items))

        def worker(claimed):
            def backend(_payload):
                return {"status": "answered", "items": [{"path": f"/tmp/{claimed.request.request_id}.md", "score": 0.8}]}

            handle_query_request(claimed, self.store, backend=backend)

        threads = [threading.Thread(target=worker, args=(claimed,)) for claimed in claimed_items]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        for request_id in request_ids:
            result = json.loads(result_path(self.paths, request_id).read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "answered")

    def test_rebuild_index_emits_diagnostics_fields(self) -> None:
        self.assertEqual(self.rebuild_payload["status"], "ok")
        self.assertIn("run_wall_ms", self.rebuild_payload)
        self.assertIn("init_local_store_ms", self.rebuild_payload)
        self.assertIn("load_runtime_env_ms", self.rebuild_payload)
        self.assertIn("file_store_construct_ms", self.rebuild_payload)
        self.assertIn("file_store_start_ms", self.rebuild_payload)
        self.assertIn("build_metadata_ms_total", self.rebuild_payload)
        self.assertIn("chunking_ms_total", self.rebuild_payload)
        self.assertIn("upsert_ms_total", self.rebuild_payload)
        self.assertIn("close_local_store_ms", self.rebuild_payload)
        self.assertIn("close_file_store_ms", self.rebuild_payload)
        self.assertIn("file_timings", self.rebuild_payload)
        self.assertIn("slowest_files", self.rebuild_payload)
        self.assertIn("embedding_cache_enabled", self.rebuild_payload)
        self.assertIn("embedding_max_batch_size", self.rebuild_payload)
        self.assertEqual(self.rebuild_payload["validation"]["status"], "ok")
        self.assertTrue(self.rebuild_payload["commit"]["committed"])
        self.assertTrue(self.rebuild_payload["commit"]["backup_removed"])
        self.assertTrue(self.rebuild_payload["file_timings"])

    def test_rebuild_requires_explicit_confirmation(self) -> None:
        env = os.environ.copy()
        env["REME_WORKDIR"] = str(self.reme_root)
        env["REME_VECTOR_ENABLED"] = "false"
        env["REME_FTS_ENABLED"] = "true"

        completed = subprocess.run(
            [REME_PYTHON, str(REBUILD_SCRIPT)],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("explicit_confirmation_required", completed.stderr)

    def test_rebuild_estimates_embedding_budget_without_api_call(self) -> None:
        env = os.environ.copy()
        env["REME_WORKDIR"] = str(self.reme_root)
        env["REME_VECTOR_ENABLED"] = "true"
        env["REME_FTS_ENABLED"] = "true"
        env["EMBEDDING_API_KEY"] = "test-only"
        env["EMBEDDING_BASE_URL"] = "https://embedding.invalid/v1"
        env["REME_EMBEDDING_ENABLE_CACHE"] = "false"

        completed = subprocess.run(
            [REME_PYTHON, str(REBUILD_SCRIPT), "--estimate-embedding-budget"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        estimate = payload["embedding_budget"]
        self.assertTrue(estimate["vector_enabled"])
        self.assertEqual(estimate["uncached_chunks"], 1)
        self.assertGreater(estimate["uncached_input_bytes"], 0)

    def test_rebuild_budget_rejection_preserves_active_index(self) -> None:
        active_dir, backup_dir, marker_path = rebuild_paths(str(self.reme_root))
        chunks_path = active_dir / "reme_local_chunks.jsonl"
        metadata_path = active_dir / "reme_local_file_metadata.json"
        chunks_before = chunks_path.read_bytes()
        metadata_before = metadata_path.read_bytes()
        env = os.environ.copy()
        env["REME_WORKDIR"] = str(self.reme_root)
        env["REME_VECTOR_ENABLED"] = "true"
        env["REME_FTS_ENABLED"] = "true"
        env["EMBEDDING_API_KEY"] = "test-only"
        env["EMBEDDING_BASE_URL"] = "https://embedding.invalid/v1"
        env["REME_EMBEDDING_ENABLE_CACHE"] = "false"

        completed = subprocess.run(
            [
                REME_PYTHON,
                str(REBUILD_SCRIPT),
                "--confirm-full-rebuild",
                "--max-uncached-bytes",
                "0",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 4)
        self.assertIn("embedding_budget_exceeded", completed.stderr)
        self.assertEqual(chunks_path.read_bytes(), chunks_before)
        self.assertEqual(metadata_path.read_bytes(), metadata_before)
        self.assertFalse(backup_dir.exists())
        self.assertFalse(marker_path.exists())

    def test_rebuild_script_refuses_live_daemon_store(self) -> None:
        old_bus_root = os.environ.get("REME_BUS_ROOT")
        old_workdir = os.environ.get("REME_WORKDIR")
        os.environ["REME_BUS_ROOT"] = str(self.bus_root)
        os.environ["REME_WORKDIR"] = str(self.reme_root)
        try:
            self.store.acquire_daemon_lock()
            completed = subprocess.run(
                [REME_PYTHON, str(REBUILD_SCRIPT), "--confirm-full-rebuild"],
                text=True,
                capture_output=True,
                env=os.environ.copy(),
                check=False,
            )
        finally:
            self.store.release_daemon_lock()
            _restore_env("REME_BUS_ROOT", old_bus_root)
            _restore_env("REME_WORKDIR", old_workdir)

        self.assertEqual(completed.returncode, 2)
        self.assertIn("daemon_active_conflict", completed.stderr)

    def test_recent_daemon_lock_with_dead_pid_is_not_active(self) -> None:
        lock_path = self.paths.lock / "daemon.lock"
        lock_path.write_text(
            json.dumps(
                {
                    "daemon_id": "stopped-daemon",
                    "pid": 999999999,
                    "reme_workdir": str(self.reme_root),
                    "heartbeat_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
                }
            ),
            encoding="utf-8",
        )

        self.assertFalse(
            is_daemon_active_for_workdir(
                str(self.reme_root),
                paths=self.paths,
            )
        )

    def test_rebuild_lock_rejects_second_rebuild(self) -> None:
        first_handle, lock_path, owner = _acquire_rebuild_lock(str(self.reme_root))
        self.assertIsNotNone(first_handle)
        self.assertEqual(owner, "")
        try:
            second_handle, second_path, second_owner = _acquire_rebuild_lock(str(self.reme_root))
            self.assertIsNone(second_handle)
            self.assertEqual(second_path, lock_path)
            self.assertIn('"pid"', second_owner)
        finally:
            _release_rebuild_lock(first_handle)

    def test_failed_rebuild_restores_previous_index(self) -> None:
        active_dir, backup_dir, marker_path = rebuild_paths(str(self.reme_root))
        chunks_path = active_dir / "reme_local_chunks.jsonl"
        metadata_path = active_dir / "reme_local_file_metadata.json"
        chunks_before = chunks_path.read_bytes()
        metadata_before = metadata_path.read_bytes()

        env = os.environ.copy()
        env["REME_WORKDIR"] = str(self.reme_root)
        env["REME_VECTOR_ENABLED"] = "false"
        env["REME_FTS_ENABLED"] = "true"
        completed = subprocess.run(
            [REME_PYTHON, str(REBUILD_SCRIPT), "--confirm-full-rebuild", "--limit", "0"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("rebuild_failed", completed.stderr)
        self.assertEqual(chunks_path.read_bytes(), chunks_before)
        self.assertEqual(metadata_path.read_bytes(), metadata_before)
        self.assertFalse(backup_dir.exists())
        self.assertFalse(marker_path.exists())

    def test_interrupted_rebuild_recovery_restores_previous_index(self) -> None:
        active_dir, backup_dir, marker_path = rebuild_paths(str(self.reme_root))
        metadata_path = active_dir / "reme_local_file_metadata.json"
        metadata_before = metadata_path.read_bytes()

        protection = begin_protected_rebuild(str(self.reme_root))
        self.assertTrue(protection["had_previous_index"])
        active_dir.mkdir(parents=True)
        (active_dir / "partial").write_text("partial", encoding="utf-8")

        recovery = recover_interrupted_rebuild(str(self.reme_root))
        self.assertTrue(recovery["recovered"])
        self.assertEqual(recovery["action"], "restored_backup")
        self.assertEqual(metadata_path.read_bytes(), metadata_before)
        self.assertFalse(backup_dir.exists())
        self.assertFalse(marker_path.exists())

    def test_successful_guard_commit_keeps_new_index(self) -> None:
        active_dir, backup_dir, marker_path = rebuild_paths(str(self.reme_root))
        begin_protected_rebuild(str(self.reme_root))
        active_dir.mkdir(parents=True)
        (active_dir / "new-index").write_text("complete", encoding="utf-8")

        commit = commit_protected_rebuild(str(self.reme_root))
        self.assertTrue(commit["committed"])
        self.assertEqual((active_dir / "new-index").read_text(encoding="utf-8"), "complete")
        self.assertFalse(backup_dir.exists())
        self.assertFalse(marker_path.exists())

    def test_explicit_rollback_restores_previous_index(self) -> None:
        active_dir, backup_dir, marker_path = rebuild_paths(str(self.reme_root))
        metadata_path = active_dir / "reme_local_file_metadata.json"
        metadata_before = metadata_path.read_bytes()
        begin_protected_rebuild(str(self.reme_root))
        active_dir.mkdir(parents=True)
        (active_dir / "partial").write_text("partial", encoding="utf-8")

        rollback = rollback_protected_rebuild(str(self.reme_root))
        self.assertTrue(rollback["rolled_back"])
        self.assertEqual(rollback["action"], "restored_backup")
        self.assertEqual(metadata_path.read_bytes(), metadata_before)
        self.assertFalse(backup_dir.exists())
        self.assertFalse(marker_path.exists())

    def test_search_script_refuses_live_daemon_store(self) -> None:
        old_bus_root = os.environ.get("REME_BUS_ROOT")
        old_workdir = os.environ.get("REME_WORKDIR")
        os.environ["REME_BUS_ROOT"] = str(self.bus_root)
        os.environ["REME_WORKDIR"] = str(self.reme_root)
        try:
            self.store.acquire_daemon_lock()
            completed = subprocess.run(
                [REME_PYTHON, str(SCRIPT_DIR / "search_memory.py"), "--query", "HostSync"],
                text=True,
                capture_output=True,
                env=os.environ.copy(),
                check=False,
            )
        finally:
            self.store.release_daemon_lock()
            _restore_env("REME_BUS_ROOT", old_bus_root)
            _restore_env("REME_WORKDIR", old_workdir)

        self.assertEqual(completed.returncode, 2)
        self.assertIn("daemon_active_conflict", completed.stderr)


def _restore_env(name: str, value) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


if __name__ == "__main__":
    unittest.main(verbosity=2)
