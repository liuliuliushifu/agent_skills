#!/usr/bin/env python3
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


SCRIPT_DIR = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
DEFAULT_BUS_ROOT = str(CODEX_HOME / "memories/reme-memory/bus")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")


@dataclass(frozen=True)
class MemoryBusPaths:
    root: Path
    inbox: Path
    inbox_write: Path
    inbox_capture: Path
    inbox_refine: Path
    inbox_query: Path
    inbox_status: Path
    inbox_flush: Path
    processing: Path
    processing_write: Path
    processing_capture: Path
    processing_refine: Path
    processing_query: Path
    async_root: Path
    async_refine: Path
    async_refine_queued: Path
    async_refine_running: Path
    async_refine_tmp: Path
    async_refine_logs: Path
    result: Path
    archive: Path
    archive_raw: Path
    archive_completed: Path
    deadletter: Path
    lock: Path


def get_bus_root() -> Path:
    return Path(os.environ.get("REME_BUS_ROOT", DEFAULT_BUS_ROOT)).expanduser().resolve()


def build_bus_paths(root: Optional[Path] = None) -> MemoryBusPaths:
    bus_root = (root or get_bus_root()).resolve()
    inbox = bus_root / "inbox"
    processing = bus_root / "processing"
    archive = bus_root / "archive"
    return MemoryBusPaths(
        root=bus_root,
        inbox=inbox,
        inbox_write=inbox / "write",
        inbox_capture=inbox / "capture",
        inbox_refine=inbox / "refine",
        inbox_query=inbox / "query",
        inbox_status=inbox / "status",
        inbox_flush=inbox / "flush",
        processing=processing,
        processing_write=processing / "write",
        processing_capture=processing / "capture",
        processing_refine=processing / "refine",
        processing_query=processing / "query",
        async_root=bus_root / "async",
        async_refine=bus_root / "async" / "refine",
        async_refine_queued=bus_root / "async" / "refine" / "queued",
        async_refine_running=bus_root / "async" / "refine" / "running",
        async_refine_tmp=bus_root / "async" / "refine" / "tmp",
        async_refine_logs=bus_root / "async" / "refine" / "logs",
        result=bus_root / "result",
        archive=archive,
        archive_raw=archive / "raw",
        archive_completed=archive / "completed",
        deadletter=bus_root / "deadletter",
        lock=bus_root / "lock",
    )


def ensure_bus_dirs(paths: MemoryBusPaths) -> None:
    for path in (
        paths.root,
        paths.inbox,
        paths.inbox_write,
        paths.inbox_capture,
        paths.inbox_refine,
        paths.inbox_query,
        paths.inbox_status,
        paths.inbox_flush,
        paths.processing,
        paths.processing_write,
        paths.processing_capture,
        paths.processing_refine,
        paths.processing_query,
        paths.async_root,
        paths.async_refine,
        paths.async_refine_queued,
        paths.async_refine_running,
        paths.async_refine_tmp,
        paths.async_refine_logs,
        paths.result,
        paths.archive,
        paths.archive_raw,
        paths.archive_completed,
        paths.deadletter,
        paths.lock,
    ):
        path.mkdir(parents=True, exist_ok=True)


def validate_request_id(request_id: str) -> str:
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise ValueError(f"invalid request_id: {request_id}")
    return request_id
