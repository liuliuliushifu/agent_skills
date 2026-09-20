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
from pathlib import Path

from memory_daemon import MemoryDaemon
from flush_adapter import handle_flush_request
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


def _write_memory_sample(memory_dir: Path) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "2026-04-20.md").write_text(
        """# Memory

- packet-processing HostSync timeout 调试：
  出现确认竞争时，先补时间日志，再调整重试。
""",
        encoding="utf-8",
    )


class MemoryBusStep6Test(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = os.environ.copy()
        self.bus_root = Path(tempfile.mkdtemp(prefix="reme_bus_step6_bus_", dir="/tmp"))
        self.reme_root = Path(tempfile.mkdtemp(prefix="reme_bus_step6_reme_", dir="/tmp"))
        self.metrics_root = Path(tempfile.mkdtemp(prefix="reme_bus_step6_metrics_", dir="/tmp"))
        _write_memory_sample(self.reme_root / "memory")
        test_env = os.environ.copy()
        test_env.update(
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
            env=test_env,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(process.stderr or process.stdout)

        os.environ["REME_BUS_ROOT"] = str(self.bus_root)
        os.environ["REME_HOME"] = str(self.reme_root / "reme-home")
        os.environ["REME_RUNTIME_ENV_FILE"] = str(self.reme_root / "test-runtime.env")
        self.store = MemoryRequestStore(daemon_id="daemon-step6")
        self.daemon = MemoryDaemon(store=self.store, poll_interval_seconds=0.05)
        self.daemon.register_handler("memory_query", handle_query_request)
        self.daemon.register_handler("memory_status", handle_status_request)
        self.daemon.register_handler("memory_flush", handle_flush_request)
        self.daemon.register_handler(
            "memory_write",
            lambda claimed, store: handle_write_request(
                claimed,
                store,
                backend=lambda _payload: {"committed": True, "message": "stub", "indexed": True},
            ),
        )
        self.thread = threading.Thread(target=self.daemon.serve_forever, daemon=True)
        os.environ["REME_WORKDIR"] = str(self.reme_root)
        os.environ["REME_VECTOR_ENABLED"] = "false"
        os.environ["REME_FTS_ENABLED"] = "true"
        os.environ["REME_BUS_ROOT"] = str(self.bus_root)
        os.environ["REME_BUS_CLIENT_ID"] = "codex-step6"
        self.old_metrics_dir = os.environ.get("REME_MEMORY_METRICS_DIR")
        os.environ["REME_MEMORY_METRICS_DIR"] = str(self.metrics_root)
        self.thread.start()

    def tearDown(self) -> None:
        try:
            self.daemon.stop()
        except Exception:
            pass
        self.thread.join(timeout=2)
        os.environ.clear()
        os.environ.update(self._saved_env)
        shutil.rmtree(self.bus_root, ignore_errors=True)
        shutil.rmtree(self.reme_root, ignore_errors=True)
        shutil.rmtree(self.metrics_root, ignore_errors=True)

    def test_prepare_via_bus(self) -> None:
        env = os.environ.copy()
        process = subprocess.run(
            [PYTHON, str(MEMORY_WORKFLOW), "prepare", "--task", "HostSync timeout", "--max-results", "3"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(process.returncode, 0, msg=process.stderr)
        self.assertIn("ReMe brief", process.stdout)
        self.assertIn("2026-04-20.md", process.stdout)

    def test_finalize_enqueues_write(self) -> None:
        env = os.environ.copy()
        process = subprocess.run(
            [
                PYTHON,
                str(MEMORY_WORKFLOW),
                "finalize",
                "--outcome",
                "step6 integration",
                "--lesson",
                "workflow finalize should enqueue write requests",
                "--language",
                "zh",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(process.returncode, 0, msg=process.stderr)
        self.assertIn("Memory write-back completed: request_id=", process.stdout)
        self.assertIn("state=stored phase=indexed", process.stdout)

        deadline = time.time() + 3
        while time.time() < deadline:
            archived = list((self.bus_root / "archive" / "completed").glob("req_*.json"))
            if archived:
                break
            time.sleep(0.1)
        self.assertTrue(archived)

    def test_status_and_flush_commands(self) -> None:
        env = os.environ.copy()
        flush_process = subprocess.run(
            [PYTHON, str(MEMORY_WORKFLOW), "flush"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(flush_process.returncode, 0, msg=flush_process.stderr)
        self.assertIn('"status": "answered"', flush_process.stdout)
        self.assertIn('"pending_writes"', flush_process.stdout)

        status_process = subprocess.run(
            [PYTHON, str(MEMORY_WORKFLOW), "status", "--target-request-id", "req_dummy_001"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(status_process.returncode, 0, msg=status_process.stderr)
        self.assertIn('"status": "answered"', status_process.stdout)
        self.assertIn('"target_request_id": "req_dummy_001"', status_process.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
