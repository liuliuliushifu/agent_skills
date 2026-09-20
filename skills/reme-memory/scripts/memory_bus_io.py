#!/usr/bin/env python3
import json
import os
import stat
import tempfile
from pathlib import Path


DEFAULT_DIR_MODE = 0o700
DEFAULT_FILE_MODE = 0o600


def ensure_dir_mode(path: Path, mode: int = DEFAULT_DIR_MODE) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except PermissionError:
        pass


def atomic_write_json(path: Path, payload: dict, mode: int = DEFAULT_FILE_MODE) -> None:
    parent = path.parent
    ensure_dir_mode(parent)

    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp_path, mode)
        except PermissionError:
            pass
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def atomic_write_text(path: Path, content: str, mode: int = DEFAULT_FILE_MODE) -> None:
    parent = path.parent
    ensure_dir_mode(parent)

    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp_path, mode)
        except PermissionError:
            pass
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def claim_file(src: Path, dst: Path) -> None:
    ensure_dir_mode(dst.parent)
    os.replace(src, dst)


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)
