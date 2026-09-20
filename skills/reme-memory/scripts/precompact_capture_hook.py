#!/usr/bin/env python3
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional


HOOK_ROOT = Path(__file__).resolve().parent
REME_ROOT = HOOK_ROOT.parent
CODEX_HOME = Path(
    os.environ.get("CODEX_HOME", str(REME_ROOT.parent.parent))
).expanduser()
REME_HOME = Path(
    os.environ.get("REME_HOME", str(Path.home() / ".local" / "share" / "reme"))
).expanduser()
SCRIPT_ROOT = CODEX_HOME / "skills/reme-memory/scripts"
SESSION_EXCERPT = SCRIPT_ROOT / "session_excerpt.py"
CONTEXT_CAPTURE_RUNNER = SCRIPT_ROOT / "context_capture_runner.py"
MEMORY_BUS_CLIENT = SCRIPT_ROOT / "memory_bus_client.py"
PYTHON = os.environ.get("REME_PYTHON", sys.executable)

AUDIT_ROOT = Path(os.environ.get("REME_PRECOMPACT_AUDIT_ROOT", str(REME_ROOT / "audit/precompact")))
TMP_ROOT = Path(os.environ.get("REME_PRECOMPACT_TMP_ROOT", str(REME_ROOT / "tmp/precompact")))

DEFAULT_EXCERPT_TIMEOUT_SECONDS = 8
DEFAULT_RUNNER_TIMEOUT_SECONDS = 18
DEFAULT_MAX_CHARS = 64000
DEFAULT_PER_RECORD_MAX_CHARS = 12000


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def today_text() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


def safe_str(value: Any, limit: int = 4096) -> str:
    text = "" if value is None else str(value)
    if len(text) > limit:
        return text[:limit] + "...<truncated>"
    return text


def stable_hash(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8", errors="replace")).hexdigest()[:16]


def parse_stdin() -> Dict[str, Any]:
    raw = sys.stdin.read()
    raw_bytes = raw.encode("utf-8", errors="replace")
    if not raw.strip():
        return {"_stdin_sha256": hashlib.sha256(raw_bytes).hexdigest(), "_stdin_bytes": len(raw_bytes), "_parse_error": "empty stdin"}
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        return {
            "_stdin_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "_stdin_bytes": len(raw_bytes),
            "_parse_error": "{}: {}".format(exc.__class__.__name__, exc),
        }
    if not isinstance(parsed, dict):
        return {
            "_stdin_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "_stdin_bytes": len(raw_bytes),
            "_parse_error": "stdin JSON is not an object",
        }
    parsed["_stdin_sha256"] = hashlib.sha256(raw_bytes).hexdigest()
    parsed["_stdin_bytes"] = len(raw_bytes)
    return parsed


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def enrich_refine_input(refine_input_path: Path, runner_payload: Dict[str, Any]) -> None:
    if not refine_input_path.is_file():
        return
    payload = json.loads(refine_input_path.read_text(encoding="utf-8"))
    capture_id = safe_str(runner_payload.get("capture_id"), 128)
    compact_memory_path = safe_str(runner_payload.get("compact_memory_md_path"))
    capture_json_path = Path(safe_str(runner_payload.get("capture_json_path"))).expanduser()
    evidence_at = ""
    if capture_json_path.is_file():
        try:
            capture_payload = json.loads(capture_json_path.read_text(encoding="utf-8"))
            evidence_at = safe_str(capture_payload.get("created_at"), 128)
        except (OSError, ValueError, TypeError):
            evidence_at = ""
    if capture_id:
        payload["capture_id"] = capture_id
    if compact_memory_path:
        payload["compact_memory_path"] = compact_memory_path
    if evidence_at:
        payload["evidence_at"] = evidence_at
    atomic_write_json(refine_input_path, payload)


def append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def finish_audit(audit: Dict[str, Any], audit_log_path: Path, audit_event_path: Path) -> None:
    atomic_write_json(audit_event_path, audit)
    append_jsonl(audit_log_path, audit)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def process_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def command_record(
    name: str,
    argv: List[str],
    timeout_seconds: int,
    stdout_path: Path,
    stderr_path: Path,
) -> Dict[str, Any]:
    started = time.monotonic()
    record: Dict[str, Any] = {
        "name": name,
        "argv": argv,
        "timeout_seconds": timeout_seconds,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }
    env = os.environ.copy()
    env.setdefault("CODEX_HOME", str(CODEX_HOME))
    env.setdefault("REME_WORKDIR", str(REME_HOME / "light_data"))
    env.setdefault("REME_BUS_ROOT", str(REME_ROOT / "bus"))
    try:
        result = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            env=env,
        )
        atomic_write_text(stdout_path, result.stdout)
        atomic_write_text(stderr_path, result.stderr)
        record.update(
            {
                "state": "ok" if result.returncode == 0 else "failed",
                "returncode": result.returncode,
                "stdout_bytes": len(result.stdout.encode("utf-8", errors="replace")),
                "stderr_bytes": len(result.stderr.encode("utf-8", errors="replace")),
            }
        )
        parsed = parse_json_output(result.stdout)
        if parsed is not None:
            record["json_state"] = parsed.get("state")
            record["json_warnings"] = parsed.get("warnings", [])
            record["json_errors"] = parsed.get("errors", [])
        return record
    except subprocess.TimeoutExpired as exc:
        stdout = process_output_text(exc.stdout)
        stderr = process_output_text(exc.stderr)
        atomic_write_text(stdout_path, stdout)
        atomic_write_text(stderr_path, stderr)
        record.update(
            {
                "state": "timeout",
                "returncode": None,
                "stdout_bytes": len(stdout.encode("utf-8", errors="replace")),
                "stderr_bytes": len(stderr.encode("utf-8", errors="replace")),
                "error": "timeout after {}s".format(timeout_seconds),
            }
        )
        return record
    except Exception as exc:
        record.update(
            {
                "state": "failed",
                "returncode": None,
                "error": "{}: {}".format(exc.__class__.__name__, exc),
                "traceback": traceback.format_exc(limit=8),
            }
        )
        return record
    finally:
        record["duration_ms"] = int((time.monotonic() - started) * 1000)


def parse_json_output(text: str) -> Optional[Dict[str, Any]]:
    if not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def build_run_id(payload: Dict[str, Any]) -> str:
    session_id = safe_str(payload.get("session_id"), 64)
    turn_id = safe_str(payload.get("turn_id"), 64)
    transcript_path = safe_str(payload.get("transcript_path"), 4096)
    digest = stable_hash(session_id, turn_id, transcript_path, now_iso())
    session_prefix = session_id[:8] if session_id else "nosess"
    return "precompact_{}_{}_{}".format(datetime.datetime.now().strftime("%Y%m%dT%H%M%S"), session_prefix, digest)


def main() -> int:
    payload = parse_stdin()
    event = safe_str(payload.get("hook_event_name") or os.environ.get("CODEX_HOOK_EVENT") or "unknown")
    run_id = build_run_id(payload)
    basename = "{}-{}".format(today_text(), run_id)
    audit_day_root = AUDIT_ROOT / today_text()
    audit_run_root = audit_day_root / run_id
    command_output_root = audit_run_root / "commands"
    tmp_run_root = TMP_ROOT / today_text() / run_id
    payload_path = audit_run_root / "payload.json"
    excerpt_path = audit_run_root / "excerpt.txt"
    metadata_path = audit_run_root / "excerpt-metadata.json"
    runner_input_path = audit_run_root / "runner-input.json"
    refine_input_path = tmp_run_root / "refine-input.json"
    audit_log_path = audit_day_root / "events.jsonl"
    audit_event_path = audit_run_root / "event.json"
    excerpt_stdout_path = command_output_root / "session_excerpt.stdout.json"
    excerpt_stderr_path = command_output_root / "session_excerpt.stderr.log"
    runner_stdout_path = command_output_root / "runner.stdout.json"
    runner_stderr_path = command_output_root / "runner.stderr.log"
    index_stdout_path = command_output_root / "index.stdout.json"
    index_stderr_path = command_output_root / "index.stderr.log"
    refine_stdout_path = command_output_root / "refine.stdout.json"
    refine_stderr_path = command_output_root / "refine.stderr.log"
    refine_enabled = env_bool("REME_PRECOMPACT_REFINE_ENABLED", True) and not env_bool("REME_REFINE_WORKER", False)

    audit: Dict[str, Any] = {
        "created_at": now_iso(),
        "event": event,
        "run_id": run_id,
        "session_id": safe_str(payload.get("session_id")),
        "turn_id": safe_str(payload.get("turn_id")),
        "transcript_path": safe_str(payload.get("transcript_path")),
        "cwd": safe_str(payload.get("cwd")),
        "model": safe_str(payload.get("model")),
        "payload_path": str(payload_path),
        "excerpt_path": str(excerpt_path),
        "metadata_path": str(metadata_path),
        "runner_input_path": str(runner_input_path),
        "audit_root": str(AUDIT_ROOT),
        "audit_run_root": str(audit_run_root),
        "audit_log_path": str(audit_log_path),
        "audit_event_path": str(audit_event_path),
        "tmp_run_root": str(tmp_run_root),
        "refine_enabled": refine_enabled,
        "refine_input_path": str(refine_input_path) if refine_enabled else "",
        "commands": [],
        "warnings": [],
        "errors": [],
    }

    try:
        payload.setdefault("schema_version", 1)
        payload.setdefault("run_reason", "precompact")
        payload.setdefault("fail_open", True)
        payload.setdefault("run_id", run_id)
        atomic_write_json(payload_path, payload)

        if payload.get("_parse_error"):
            audit["state"] = "skipped"
            audit["errors"].append(payload["_parse_error"])
            finish_audit(audit, audit_log_path, audit_event_path)
            return 0

        if event != "PreCompact":
            audit["state"] = "skipped"
            audit["warnings"].append("hook event is {}, not PreCompact".format(event))
            finish_audit(audit, audit_log_path, audit_event_path)
            return 0

        transcript_path = safe_str(payload.get("transcript_path"))
        if not transcript_path:
            audit["state"] = "skipped"
            audit["errors"].append("missing transcript_path")
            finish_audit(audit, audit_log_path, audit_event_path)
            return 0

        excerpt_timeout = env_int("REME_PRECOMPACT_EXCERPT_TIMEOUT_SECONDS", DEFAULT_EXCERPT_TIMEOUT_SECONDS)
        runner_timeout = env_int("REME_PRECOMPACT_RUNNER_TIMEOUT_SECONDS", DEFAULT_RUNNER_TIMEOUT_SECONDS)
        max_chars = env_int("REME_PRECOMPACT_MAX_CHARS", DEFAULT_MAX_CHARS)
        per_record_max_chars = env_int("REME_PRECOMPACT_PER_RECORD_MAX_CHARS", DEFAULT_PER_RECORD_MAX_CHARS)

        excerpt_argv = [
            PYTHON,
            str(SESSION_EXCERPT),
            "--input-json",
            str(payload_path),
            "--output",
            str(excerpt_path),
            "--metadata-out",
            str(metadata_path),
            "--runner-input-json-out",
            str(runner_input_path),
            "--run-reason",
            "precompact",
            "--thread-id",
            safe_str(payload.get("session_id")),
            "--session-scope-key",
            safe_str(payload.get("session_id")),
            "--task",
            "PreCompact ReMe context capture",
            "--scenario",
            "Codex PreCompact",
            "--max-chars",
            str(max_chars),
            "--per-record-max-chars",
            str(per_record_max_chars),
            "--fail-open",
        ]
        if refine_enabled:
            excerpt_argv.extend(["--refine-json-out", str(refine_input_path)])
        project = safe_str(payload.get("project") or os.environ.get("REME_BUS_PROJECT"))
        if project:
            excerpt_argv.extend(["--project", project])
        cwd = safe_str(payload.get("cwd"))
        if cwd:
            excerpt_argv.extend(["--cwd", cwd])

        excerpt_record = command_record(
            name="session_excerpt",
            argv=excerpt_argv,
            timeout_seconds=excerpt_timeout,
            stdout_path=excerpt_stdout_path,
            stderr_path=excerpt_stderr_path,
        )
        audit["commands"].append(excerpt_record)

        if excerpt_record.get("state") not in {"ok"}:
            audit["state"] = "partial"
            audit["errors"].append("session_excerpt did not complete successfully")
            finish_audit(audit, audit_log_path, audit_event_path)
            return 0

        runner_argv = [
            PYTHON,
            str(CONTEXT_CAPTURE_RUNNER),
            "--input-json",
            str(runner_input_path),
        ]
        runner_record = command_record(
            name="context_capture_runner",
            argv=runner_argv,
            timeout_seconds=runner_timeout,
            stdout_path=runner_stdout_path,
            stderr_path=runner_stderr_path,
        )
        audit["commands"].append(runner_record)

        runner_payload = parse_json_output(runner_stdout_path.read_text(encoding="utf-8", errors="replace"))
        if runner_payload:
            audit["capture_json_path"] = runner_payload.get("capture_json_path")
            audit["handoff_md_path"] = runner_payload.get("handoff_md_path")
            audit["compact_memory_md_path"] = runner_payload.get("compact_memory_md_path")
            audit["artifact_count"] = runner_payload.get("artifact_count")
            audit["artifact_paths"] = runner_payload.get("artifact_paths", [])[:20]
            audit["rules_loaded"] = runner_payload.get("rules_loaded", [])
            audit["warnings"].extend(runner_payload.get("warnings", []))
            audit["errors"].extend(runner_payload.get("errors", []))
            compact_memory_path = safe_str(runner_payload.get("compact_memory_md_path"))
            if compact_memory_path:
                index_record = command_record(
                    name="memory_index_enqueue",
                    argv=[
                        PYTHON,
                        str(MEMORY_BUS_CLIENT),
                        "index",
                        "--path",
                        compact_memory_path,
                    ],
                    timeout_seconds=env_int("REME_PRECOMPACT_INDEX_ENQUEUE_TIMEOUT_SECONDS", 5),
                    stdout_path=index_stdout_path,
                    stderr_path=index_stderr_path,
                )
                audit["commands"].append(index_record)
                index_payload = parse_json_output(index_stdout_path.read_text(encoding="utf-8", errors="replace"))
                if index_payload:
                    audit["index_request_id"] = index_payload.get("request_id", "")
                    audit["index_request_status"] = index_payload.get("status", "")
                if index_record.get("state") != "ok":
                    audit["errors"].append("memory_index_enqueue did not complete successfully")
            enrich_refine_input(refine_input_path, runner_payload)

        if refine_enabled and refine_input_path.exists():
            refine_record = command_record(
                name="memory_refine_enqueue",
                argv=[
                    PYTHON,
                    str(MEMORY_BUS_CLIENT),
                    "refine",
                    "--refine-json",
                    str(refine_input_path),
                ],
                timeout_seconds=env_int("REME_PRECOMPACT_REFINE_ENQUEUE_TIMEOUT_SECONDS", 5),
                stdout_path=refine_stdout_path,
                stderr_path=refine_stderr_path,
            )
            audit["commands"].append(refine_record)
            refine_payload = parse_json_output(refine_stdout_path.read_text(encoding="utf-8", errors="replace"))
            if refine_payload:
                audit["refine_request_id"] = refine_payload.get("request_id", "")
                audit["refine_request_status"] = refine_payload.get("status", "")
            if refine_record.get("state") != "ok":
                audit["errors"].append("memory_refine_enqueue did not complete successfully")
            refine_input_path.unlink(missing_ok=True)
            shutil.rmtree(tmp_run_root, ignore_errors=True)

        audit["state"] = "ok" if runner_record.get("state") == "ok" else "partial"
        finish_audit(audit, audit_log_path, audit_event_path)
        return 0
    except Exception as exc:
        audit["state"] = "failed"
        audit["errors"].append("{}: {}".format(exc.__class__.__name__, exc))
        audit["traceback"] = traceback.format_exc(limit=8)
        refine_input_path.unlink(missing_ok=True)
        shutil.rmtree(tmp_run_root, ignore_errors=True)
        finish_audit(audit, audit_log_path, audit_event_path)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
