#!/usr/bin/env python3
import datetime as _dt
import json
import os
import errno
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

from reme_runtime import CODEX_HOME, REME_WORKDIR


DAEMON_ROOT = Path(os.environ.get("REME_RUNTIME_ROOT", str(CODEX_HOME / "memories/reme-memory/daemon")))
START_DAEMON_SCRIPT = Path(os.environ.get("REME_START_DAEMON_SCRIPT", str(DAEMON_ROOT / "start_daemon.sh")))
LOCK_FILE = Path(
    os.environ.get(
        "REME_DAEMON_LOCK_FILE",
        str(Path(os.environ.get("REME_BUS_ROOT", str(CODEX_HOME / "memories/reme-memory/bus"))) / "lock/daemon.lock"),
    )
)


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def _parse_daemon_time(text: str) -> Optional[_dt.datetime]:
    if not text:
        return None
    normalized = text
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    if len(normalized) >= 5 and normalized[-5] in {"+", "-"} and normalized[-3] != ":":
        normalized = normalized[:-2] + ":" + normalized[-2:]
    try:
        return _dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _pid_alive(pid_value: Any) -> bool:
    try:
        pid = int(pid_value)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM


def _same_workdir(lock_workdir: str, target_workdir: str) -> bool:
    try:
        return Path(lock_workdir).expanduser().resolve() == Path(target_workdir).expanduser().resolve()
    except Exception:
        return False


def daemon_health_snapshot(*, stale_seconds: float = 120.0, target_workdir: Optional[str] = None) -> Dict[str, Any]:
    target = target_workdir or os.environ.get("REME_WORKDIR", REME_WORKDIR)
    payload: Dict[str, Any] = {}
    if LOCK_FILE.exists():
        try:
            payload = _read_json(LOCK_FILE)
        except Exception as exc:
            return {
                "healthy": False,
                "reason": "lock_unreadable",
                "error": f"{type(exc).__name__}: {exc}",
                "lock_file": str(LOCK_FILE),
            }
    else:
        return {
            "healthy": False,
            "reason": "lock_missing",
            "lock_file": str(LOCK_FILE),
        }

    heartbeat_at = str(payload.get("heartbeat_at", ""))
    heartbeat_time = _parse_daemon_time(heartbeat_at)
    heartbeat_age_seconds: Optional[float] = None
    if heartbeat_time is not None:
        now = _dt.datetime.now(tz=heartbeat_time.tzinfo or _dt.timezone.utc)
        heartbeat_age_seconds = max(0.0, (now - heartbeat_time).total_seconds())

    pid = payload.get("pid")
    pid_alive = _pid_alive(pid)
    lock_workdir = str(payload.get("reme_workdir") or target)
    workdir_match = _same_workdir(lock_workdir, target)

    reasons = []
    if heartbeat_time is None:
        reasons.append("heartbeat_missing")
    elif heartbeat_age_seconds is not None and heartbeat_age_seconds > stale_seconds:
        reasons.append("heartbeat_stale")
    if not workdir_match:
        reasons.append("workdir_mismatch")

    return {
        "healthy": not reasons,
        "reason": "ok" if not reasons else ",".join(reasons),
        "pid_check": "alive" if pid_alive else "unverified",
        "lock_file": str(LOCK_FILE),
        "daemon_id": payload.get("daemon_id", ""),
        "pid": pid,
        "pid_alive": pid_alive,
        "hostname": payload.get("hostname", ""),
        "started_at": payload.get("started_at", ""),
        "heartbeat_at": heartbeat_at,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "reme_workdir": lock_workdir,
        "target_workdir": target,
    }


def ensure_daemon_running(*, wait_seconds: float = 30.0, poll_interval_seconds: float = 0.5) -> Dict[str, Any]:
    before = daemon_health_snapshot()
    if before.get("healthy"):
        return {"healthy": True, "started": False, "before": before, "after": before}

    if not START_DAEMON_SCRIPT.exists():
        raise RuntimeError(f"ReMe daemon start script missing: {START_DAEMON_SCRIPT}")

    started_at = time.monotonic()
    process = subprocess.run(
        [str(START_DAEMON_SCRIPT)],
        text=True,
        capture_output=True,
        check=False,
        timeout=max(10.0, wait_seconds),
    )
    if process.returncode != 0:
        stderr = process.stderr.strip()
        stdout = process.stdout.strip()
        detail = stderr or stdout or f"exit={process.returncode}"
        raise RuntimeError(f"failed to start ReMe daemon: {detail}")

    deadline = time.monotonic() + wait_seconds
    last = before
    while time.monotonic() < deadline:
        last = daemon_health_snapshot()
        if last.get("healthy"):
            return {
                "healthy": True,
                "started": True,
                "before": before,
                "after": last,
                "start_stdout": process.stdout.strip(),
                "waited_seconds": round(time.monotonic() - started_at, 3),
            }
        time.sleep(poll_interval_seconds)

    raise RuntimeError(
        "ReMe daemon did not become healthy within "
        f"{wait_seconds:.1f}s; last_reason={last.get('reason', 'unknown')}"
    )
