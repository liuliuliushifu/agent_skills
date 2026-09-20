#!/usr/bin/env python3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

from flush_adapter import handle_flush_request
from memory_bus_client import enqueue_flush, wait_for_result
from memory_daemon import MemoryDaemon
from memory_request_store import MemoryRequestStore
from query_adapter import handle_query_request
from status_adapter import handle_status_request
from write_adapter import handle_write_request


SCRIPT_DIR = Path(__file__).resolve().parents[1]
MEMORY_WORKFLOW = SCRIPT_DIR / "memory_workflow.py"
REBUILD_SCRIPT = SCRIPT_DIR / "rebuild_index.py"
PYTHON = sys.executable
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
USER_ROOT = Path(os.environ.get("CODEX_USER_HOME", str(CODEX_HOME.parent)))
REME_HOME = Path(os.environ.get("REME_HOME", str(USER_ROOT / ".local/share/reme")))
REME_PYTHON = os.environ.get("REME_PYTHON", str(REME_HOME / ".venv/bin/python"))


def _write_initial_memory(memory_dir: Path) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "2026-04-20.md").write_text(
        """# Memory

- packet-processing HostSync timeout 调试：
  出现确认竞争时，先补时间日志，再调整重试。
""",
        encoding="utf-8",
    )


def _rebuild_fts_index(reme_root: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "REME_WORKDIR": str(reme_root),
            "REME_HOME": str(reme_root / "reme-home"),
            "REME_RUNTIME_ENV_FILE": str(reme_root / "test-runtime.env"),
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


class MemoryBusE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.saved_env = os.environ.copy()
        self.bus_root = Path(tempfile.mkdtemp(prefix="reme_bus_e2e_bus_", dir="/tmp"))
        self.reme_root = Path(tempfile.mkdtemp(prefix="reme_bus_e2e_reme_", dir="/tmp"))
        self.metrics_root = Path(tempfile.mkdtemp(prefix="reme_bus_e2e_metrics_", dir="/tmp"))
        _write_initial_memory(self.reme_root / "memory")
        _rebuild_fts_index(self.reme_root)

        os.environ["REME_BUS_ROOT"] = str(self.bus_root)
        os.environ["REME_WORKDIR"] = str(self.reme_root)
        os.environ["REME_HOME"] = str(self.reme_root / "reme-home")
        os.environ["REME_RUNTIME_ENV_FILE"] = str(self.reme_root / "test-runtime.env")
        os.environ["REME_VECTOR_ENABLED"] = "false"
        os.environ["REME_FTS_ENABLED"] = "true"
        os.environ["REME_BUS_CLIENT_ID"] = "codex-e2e"
        os.environ["REME_MEMORY_METRICS_DIR"] = str(self.metrics_root)
        os.environ["REME_QUERY_WAIT_TIMEOUT_SECONDS"] = "60"

        self.store = MemoryRequestStore(daemon_id="daemon-e2e")
        self.daemon = MemoryDaemon(store=self.store, poll_interval_seconds=0.05)
        self.daemon.register_handler("memory_query", handle_query_request)
        self.daemon.register_handler("memory_status", handle_status_request)
        self.daemon.register_handler("memory_flush", handle_flush_request)
        self.daemon.register_handler("memory_write", self._handle_local_write)
        self.thread = threading.Thread(target=self.daemon.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        try:
            self.daemon.stop()
        except Exception:
            pass
        self.thread.join(timeout=2)
        os.environ.clear()
        os.environ.update(self.saved_env)
        shutil.rmtree(self.bus_root, ignore_errors=True)
        shutil.rmtree(self.reme_root, ignore_errors=True)
        shutil.rmtree(self.metrics_root, ignore_errors=True)

    def _handle_local_write(self, claimed, store) -> None:
        def backend(payload):
            memory_dir = self.reme_root / "memory"
            memory_dir.mkdir(parents=True, exist_ok=True)
            target = memory_dir / f"{datetime.now().strftime('%Y-%m-%d')}.md"
            if target.exists():
                content = target.read_text(encoding="utf-8")
            else:
                content = f"# Memory - {datetime.now().strftime('%Y-%m-%d')}\n\n"
            line = f"- {payload['lesson']}\n"
            if line not in content:
                content += line
                target.write_text(content, encoding="utf-8")
            return {
                "committed": True,
                "message": "local-write",
                "indexed": False,
                "path": str(target),
            }

        handle_write_request(claimed, store, backend=backend)

    def _run_workflow(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, str(MEMORY_WORKFLOW), *args],
            text=True,
            capture_output=True,
            env=os.environ.copy(),
            check=False,
        )

    def test_end_to_end_query_and_deduplicated_write(self) -> None:
        first_query = self._run_workflow("prepare", "--task", "HostSync timeout", "--max-results", "3")
        self.assertEqual(first_query.returncode, 0, msg=first_query.stderr)
        self.assertIn("2026-04-20.md", first_query.stdout)

        lesson = "daemon e2e write should become searchable"
        finalize_a = self._run_workflow("finalize", "--outcome", "e2e", "--lesson", lesson, "--language", "zh")
        finalize_b = self._run_workflow("finalize", "--outcome", "e2e", "--lesson", lesson, "--language", "zh")
        self.assertEqual(finalize_a.returncode, 0, msg=finalize_a.stderr)
        self.assertEqual(finalize_b.returncode, 0, msg=finalize_b.stderr)

        deadline = time.time() + 20
        while time.time() < deadline:
            commit_files = list((self.bus_root / "archive" / "completed").glob("sha256_*.commit.json"))
            if commit_files:
                break
            time.sleep(0.1)
        self.assertEqual(len(commit_files), 1)

        flush_request = enqueue_flush()
        flush_result = wait_for_result(flush_request.request_id, timeout_seconds=5.0)
        self.assertEqual(flush_result["status"], "answered")

        second_query = self._run_workflow("prepare", "--task", "searchable", "--max-results", "5")
        self.assertEqual(second_query.returncode, 0, msg=second_query.stderr)
        self.assertIn("daemon e2e write should become searchable", second_query.stdout)

    def test_two_parallel_prepare_requests(self) -> None:
        results = []

        def worker():
            results.append(self._run_workflow("prepare", "--task", "HostSync timeout", "--max-results", "3"))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive(), "parallel prepare worker did not finish in time")

        self.assertEqual(len(results), 2)
        for result in results:
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("ReMe brief", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
