#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from memory_bus_paths import build_bus_paths
from reme_daemon_guard import daemon_health_snapshot
from reme_runtime import (
    ACTIVE_ENV_FILE,
    DEFAULT_CANDIDATE_MULTIPLIER,
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_MIN_SCORE,
    DEFAULT_VECTOR_WEIGHT,
    REME_HOME,
    REME_WORKDIR,
    get_embedding_config,
    get_file_store_config,
    get_indexable_memory_dirs,
    get_llm_config,
    load_runtime_env,
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _safe_run(args: List[str], timeout: float = 2.0) -> Dict[str, Any]:
    try:
        process = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": process.returncode == 0,
        "returncode": process.returncode,
        "stdout": process.stdout.strip(),
        "stderr": process.stderr.strip(),
    }


def _count_files(path: Path, suffix: str = "*.md") -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.glob(suffix))


def _count_dir_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.glob("*.json") if item.is_file())


def _embedding_nonzero(values: Any) -> bool:
    if not isinstance(values, list):
        return False
    for value in values:
        try:
            if abs(float(value)) > 1e-12:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _index_snapshot(workdir: Path, store_name: str) -> Dict[str, Any]:
    chunks_path = workdir / "file_store" / f"{store_name}_chunks.jsonl"
    metadata_path = workdir / "file_store" / f"{store_name}_file_metadata.json"
    chunk_count = 0
    embedding_present = 0
    embedding_nonzero = 0
    embedding_zero = 0
    observed_dimensions: Optional[int] = None
    first_error = ""

    if chunks_path.exists():
        with chunks_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                chunk_count += 1
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    if not first_error:
                        first_error = f"json_decode_error line={chunk_count}: {exc}"
                    continue
                embedding = payload.get("embedding")
                if isinstance(embedding, list):
                    embedding_present += 1
                    if observed_dimensions is None:
                        observed_dimensions = len(embedding)
                    if _embedding_nonzero(embedding):
                        embedding_nonzero += 1
                    else:
                        embedding_zero += 1

    indexed_file_count = 0
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            for source_payload in metadata.values():
                if isinstance(source_payload, dict):
                    indexed_file_count += len(source_payload)
        except Exception as exc:
            first_error = first_error or f"metadata_read_error: {type(exc).__name__}: {exc}"

    coverage = 0.0
    if chunk_count:
        coverage = embedding_nonzero / chunk_count
    return {
        "chunks_path": str(chunks_path),
        "metadata_path": str(metadata_path),
        "indexed_files": indexed_file_count,
        "indexed_chunks": chunk_count,
        "embedding_present_chunks": embedding_present,
        "embedding_nonzero_chunks": embedding_nonzero,
        "embedding_zero_chunks": embedding_zero,
        "embedding_coverage": coverage,
        "observed_embedding_dimensions": observed_dimensions,
        "error": first_error,
    }


def _bus_snapshot() -> Dict[str, Any]:
    paths = build_bus_paths()
    return {
        "root": str(paths.root),
        "inbox": {
            "write": _count_dir_files(paths.inbox_write),
            "capture": _count_dir_files(paths.inbox_capture),
            "refine": _count_dir_files(paths.inbox_refine),
            "query": _count_dir_files(paths.inbox_query),
            "status": _count_dir_files(paths.inbox_status),
            "flush": _count_dir_files(paths.inbox_flush),
        },
        "processing": {
            "write": _count_dir_files(paths.processing_write),
            "capture": _count_dir_files(paths.processing_capture),
            "refine": _count_dir_files(paths.processing_refine),
            "query": _count_dir_files(paths.processing_query),
        },
        "async_refine": {
            "queued": _count_dir_files(paths.async_refine_queued),
            "running": _count_dir_files(paths.async_refine_running),
        },
        "result_files": _count_dir_files(paths.result),
        "deadletter_files": _count_dir_files(paths.deadletter),
    }


def _recent_errors(limit: int = 5) -> List[Dict[str, Any]]:
    summary_path_value = os.environ.get("REME_DAEMON_SUMMARY_LOG", "").strip()
    if summary_path_value:
        summary_path = Path(summary_path_value)
    else:
        summary_path = Path(os.environ.get("CODEX_HOME", str(Path(__file__).resolve().parents[2]))) / "memories/reme-memory/daemon/logs/request_summary.jsonl"
    if not summary_path.exists():
        return []

    results: List[Dict[str, Any]] = []
    try:
        lines = summary_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return results
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        state = str(item.get("final_state", ""))
        error_kind = str(item.get("error_kind", ""))
        if error_kind or state in {"failed", "deadletter"}:
            results.append(
                {
                    "completed_at": item.get("completed_at", ""),
                    "request_type": item.get("request_type", ""),
                    "final_state": state,
                    "final_phase": item.get("final_phase", ""),
                    "error_kind": error_kind,
                    "diag_id": item.get("diag_id", ""),
                }
            )
        if len(results) >= limit:
            break
    return results


def _systemd_snapshot() -> Dict[str, Any]:
    unit_name = os.environ.get("REME_DAEMON_UNIT_NAME", "reme-memory-daemon") + ".service"
    active = _safe_run(["systemctl", "--user", "is-active", unit_name])
    show = _safe_run(
        [
            "systemctl",
            "--user",
            "show",
            unit_name,
            "--property=ActiveState",
            "--property=SubState",
            "--property=MainPID",
            "--property=ExecMainStatus",
        ]
    )
    properties: Dict[str, str] = {}
    if show.get("stdout"):
        for line in show["stdout"].splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                properties[key] = value
    return {
        "unit": unit_name,
        "active": active.get("stdout") if active.get("stdout") else "unknown",
        "active_check_ok": active.get("ok", False),
        "properties": properties,
        "error": active.get("stderr") or show.get("stderr") or active.get("error") or show.get("error") or "",
    }


def collect_status() -> Dict[str, Any]:
    load_runtime_env(override=True)
    embedding_config = get_embedding_config()
    file_store_config = get_file_store_config()
    llm_config = get_llm_config()
    workdir = Path(REME_WORKDIR)
    store_name = os.environ.get("REME_FILE_STORE_NAME", "reme_local")
    memory_dirs = get_indexable_memory_dirs()

    return {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "runtime": {
            "reme_home": str(REME_HOME),
            "reme_workdir": str(workdir),
            "active_env_file": str(ACTIVE_ENV_FILE),
            "active_env_exists": ACTIVE_ENV_FILE.exists(),
        },
        "daemon": {
            "systemd": _systemd_snapshot(),
            "health": daemon_health_snapshot(),
        },
        "retrieval": {
            "fts_enabled": file_store_config["fts_enabled"],
            "vector_requested": _env_bool("REME_VECTOR_ENABLED", True),
            "vector_enabled": file_store_config["vector_enabled"],
            "vector_weight": DEFAULT_VECTOR_WEIGHT,
            "min_score": DEFAULT_MIN_SCORE,
            "candidate_multiplier": DEFAULT_CANDIDATE_MULTIPLIER,
            "chunk_tokens": DEFAULT_CHUNK_TOKENS,
            "chunk_overlap": DEFAULT_CHUNK_OVERLAP,
        },
        "embedding": {
            "model": embedding_config["model_name"],
            "base_url": embedding_config["base_url"],
            "dimensions": embedding_config["dimensions"],
            "use_dimensions": embedding_config["use_dimensions"],
            "api_key_present": bool(embedding_config["api_key"]),
            "cache_enabled": embedding_config["enable_cache"],
            "cache_dir": str(workdir / "embedding_cache"),
            "max_batch_size": embedding_config["max_batch_size"],
            "max_input_length": embedding_config["max_input_length"],
        },
        "llm": {
            "backend": llm_config.get("backend", "codex_exec_refine"),
            "model": llm_config["model_name"],
            "env_file": llm_config["env_file"],
        },
        "memory": {
            "dirs": [str(path) for path in memory_dirs],
            "memory_files": _count_files(workdir / "memory"),
            "compact_memory_files": _count_files(workdir / "compact_memory"),
            "index": _index_snapshot(workdir, store_name),
        },
        "bus": _bus_snapshot(),
        "recent_errors": _recent_errors(),
    }


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _fmt_seconds(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.1f}s"
    except (TypeError, ValueError):
        return "n/a"


def _print_section(title: str, rows: Iterable[tuple]) -> None:
    print(title)
    for key, value in rows:
        print(f"  {key:<28} {value}")


def print_human(payload: Dict[str, Any]) -> None:
    daemon = payload["daemon"]
    health = daemon["health"]
    systemd = daemon["systemd"]
    retrieval = payload["retrieval"]
    embedding = payload["embedding"]
    llm = payload["llm"]
    memory = payload["memory"]
    index = memory["index"]
    bus = payload["bus"]

    print("ReMe status")
    print(f"Generated at: {payload['generated_at']}")
    print("")
    _print_section(
        "Runtime",
        [
            ("REME_HOME", payload["runtime"]["reme_home"]),
            ("REME_WORKDIR", payload["runtime"]["reme_workdir"]),
            ("active env", payload["runtime"]["active_env_file"]),
            ("active env exists", _yes_no(payload["runtime"]["active_env_exists"])),
        ],
    )
    print("")
    _print_section(
        "Daemon",
        [
            ("systemd unit", systemd["unit"]),
            ("systemd active", systemd["active"]),
            ("healthy", _yes_no(bool(health.get("healthy")))),
            ("reason", health.get("reason", "")),
            ("pid", health.get("pid", "")),
            ("pid check", health.get("pid_check", "alive" if health.get("pid_alive") else "unverified")),
            ("heartbeat", health.get("heartbeat_at", "")),
            ("heartbeat age", _fmt_seconds(health.get("heartbeat_age_seconds"))),
        ],
    )
    print("")
    _print_section(
        "Retrieval",
        [
            ("FTS enabled", _yes_no(bool(retrieval["fts_enabled"]))),
            ("vector requested", _yes_no(bool(retrieval["vector_requested"]))),
            ("vector enabled", _yes_no(bool(retrieval["vector_enabled"]))),
            ("vector weight", retrieval["vector_weight"]),
            ("min score", retrieval["min_score"]),
            ("candidate multiplier", retrieval["candidate_multiplier"]),
            ("chunk size", f"{retrieval['chunk_tokens']} tokens, overlap {retrieval['chunk_overlap']}"),
        ],
    )
    print("")
    _print_section(
        "Embedding",
        [
            ("model", embedding["model"]),
            ("base URL", embedding["base_url"]),
            ("dimensions", embedding["dimensions"]),
            ("use dimensions", _yes_no(bool(embedding["use_dimensions"]))),
            ("API key", "present" if embedding["api_key_present"] else "missing"),
            ("cache", "enabled" if embedding["cache_enabled"] else "disabled"),
            ("cache dir", embedding["cache_dir"]),
        ],
    )
    print("")
    _print_section(
        "Refine Backend",
        [
            ("backend", llm.get("backend", "codex_exec_refine")),
            ("model", llm["model"]),
            ("env file", llm["env_file"]),
        ],
    )
    print("")
    _print_section(
        "Memory Index",
        [
            ("memory files", memory["memory_files"]),
            ("compact files", memory["compact_memory_files"]),
            ("indexed files", index["indexed_files"]),
            ("indexed chunks", index["indexed_chunks"]),
            ("nonzero embeddings", index["embedding_nonzero_chunks"]),
            ("zero embeddings", index["embedding_zero_chunks"]),
            ("embedding coverage", f"{index['embedding_coverage'] * 100:.1f}%"),
            ("observed dimensions", index["observed_embedding_dimensions"] or "n/a"),
            ("index error", index["error"] or "none"),
        ],
    )
    print("")
    _print_section(
        "Bus",
        [
            ("root", bus["root"]),
            ("inbox", bus["inbox"]),
            ("processing", bus["processing"]),
            ("async refine", bus["async_refine"]),
            ("result files", bus["result_files"]),
            ("deadletter files", bus["deadletter_files"]),
        ],
    )
    if payload["recent_errors"]:
        print("")
        print("Recent Errors")
        for item in payload["recent_errors"]:
            print(
                "  {completed_at} {request_type} state={final_state} phase={final_phase} "
                "error={error_kind} diag={diag_id}".format(**item)
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Show local ReMe runtime, daemon, retrieval, and index status.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    payload = collect_status()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print_human(payload)


if __name__ == "__main__":
    main()
