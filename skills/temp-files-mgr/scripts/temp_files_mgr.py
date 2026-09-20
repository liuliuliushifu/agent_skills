#!/usr/bin/env python3
"""Safely scan and clean a configured user's top-level temporary entries."""

import argparse
import collections
import datetime as dt
import fcntl
import json
import os
import pwd
import stat
import sys
import time
from pathlib import Path


def configured_absolute_path(name, default):
    path = Path(os.environ.get(name, str(default))).expanduser()
    if not path.is_absolute():
        raise ValueError("{} must be an absolute path".format(name))
    return path


TARGET_USER = os.environ.get("TEMP_FILES_USER") or pwd.getpwuid(os.getuid()).pw_name
USER_BASE_DIR = configured_absolute_path("TEMP_FILES_BASE_DIR", Path.home().parent)
USER_TEMP_SUBDIR = Path(os.environ.get("TEMP_FILES_USER_TMP_SUBDIR", ".tmp"))
if USER_TEMP_SUBDIR.is_absolute() or ".." in USER_TEMP_SUBDIR.parts:
    raise ValueError("TEMP_FILES_USER_TMP_SUBDIR must be a safe relative path")
USER_TEMP_ROOT = USER_BASE_DIR / TARGET_USER / USER_TEMP_SUBDIR
SYSTEM_TEMP_ROOT = configured_absolute_path("TEMP_FILES_SYSTEM_TMP_DIR", "/tmp")
ALLOWED_DESCENDANT_USERS = tuple(
    dict.fromkeys(
        [TARGET_USER]
        + [
            item.strip()
            for item in os.environ.get("TEMP_FILES_ALLOWED_DESCENDANT_USERS", "nobody").split(",")
            if item.strip()
        ]
    )
)
ROOTS = (SYSTEM_TEMP_ROOT, USER_TEMP_ROOT)
NESTED_SUBDIR_VALUE = os.environ.get("TEMP_FILES_NESTED_SUBDIR", "codex").strip()
NESTED_SUBDIR = Path(NESTED_SUBDIR_VALUE) if NESTED_SUBDIR_VALUE else None
if NESTED_SUBDIR and (NESTED_SUBDIR.is_absolute() or ".." in NESTED_SUBDIR.parts):
    raise ValueError("TEMP_FILES_NESTED_SUBDIR must be a safe relative path")
NESTED_CONTAINER_ROOTS = (USER_TEMP_ROOT / NESTED_SUBDIR,) if NESTED_SUBDIR else ()
SCAN_ROOTS = ROOTS + NESTED_CONTAINER_ROOTS
CODEX_HOME = configured_absolute_path("CODEX_HOME", Path.home() / ".codex")
STATE_DIR = configured_absolute_path(
    "TEMP_FILES_STATE_DIR", CODEX_HOME / "temp-files-mgr"
)
HISTORY_DIR = STATE_DIR / "history"
LOCK_FILE = STATE_DIR / "manager.lock"
BASE_RETENTION_DAYS = int(os.environ.get("TEMP_FILES_RETENTION_DAYS", "7"))
SCRIPT_RETENTION_DAYS = int(os.environ.get("TEMP_FILES_SCRIPT_RETENTION_DAYS", "14"))
SCRIPT_SUFFIXES = {".sh", ".bash", ".py", ".json", ".jsonl", ".sftp"}
CANDIDATE_REASONS = {"candidate"}
RUNTIME_PREFIXES = (
    "tmux-",
    "ssh-",
    "systemd-private-",
    "snap-private-tmp",
    "codex-bwrap-",
)


def iso_time(timestamp):
    return dt.datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def human_bytes(value):
    size = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    unit = units[0]
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            break
        size /= 1024.0
    return "{:.1f} {}".format(size, unit)


def ensure_runtime(uid):
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (STATE_DIR, HISTORY_DIR):
        info = path.stat()
        if info.st_uid != uid:
            raise RuntimeError("runtime path is not owned by {}: {}".format(TARGET_USER, path))
        os.chmod(str(path), 0o700)


def target_uid():
    try:
        return pwd.getpwnam(TARGET_USER).pw_uid
    except KeyError:
        raise RuntimeError("required user does not exist: {}".format(TARGET_USER))


def resolve_user_uids(usernames):
    resolved = set()
    for name in usernames:
        try:
            resolved.add(pwd.getpwnam(name).pw_uid)
        except KeyError:
            raise RuntimeError("required allowed owner does not exist: {}".format(name))
    return resolved


def allowed_descendant_uids():
    return resolve_user_uids(ALLOWED_DESCENDANT_USERS)


def check_identity(uid):
    if os.geteuid() != uid:
        raise RuntimeError(
            "refusing to run as uid {}; required {} ({})".format(
                os.geteuid(), TARGET_USER, uid
            )
        )


def is_script_like(path):
    return path.suffix.lower() in SCRIPT_SUFFIXES


def allocated_bytes(info):
    blocks = getattr(info, "st_blocks", 0)
    return int(blocks) * 512 if blocks else int(info.st_size)


def empty_analysis(path):
    return {
        "path": str(path),
        "kind": "unknown",
        "device": None,
        "inode": None,
        "top_owned": False,
        "owned": True,
        "deletable": True,
        "mountpoint": False,
        "entry_count": 0,
        "allocated_bytes": 0,
        "latest_mtime": 0.0,
        "script_count": 0,
        "latest_script_mtime": 0.0,
        "errors": [],
    }


def include_stat(
    result,
    path,
    info,
    allowed_uids,
    seen_inodes,
    count_script=True,
):
    result["entry_count"] += 1
    if info.st_uid not in allowed_uids:
        result["owned"] = False
    result["latest_mtime"] = max(result["latest_mtime"], info.st_mtime)
    inode_key = (info.st_dev, info.st_ino)
    if inode_key not in seen_inodes:
        result["allocated_bytes"] += allocated_bytes(info)
        seen_inodes.add(inode_key)
    if count_script and is_script_like(path) and not stat.S_ISDIR(info.st_mode):
        result["script_count"] += 1
        result["latest_script_mtime"] = max(
            result["latest_script_mtime"], info.st_mtime
        )


def has_directory_delete_access(path):
    try:
        return os.access(str(path), os.W_OK | os.X_OK, effective_ids=True)
    except TypeError:
        return os.access(str(path), os.W_OK | os.X_OK)


def analyze_entry(path, uid, allowed_uids=None):
    allowed_uids = allowed_uids or {uid}
    result = empty_analysis(path)
    seen_inodes = set()
    try:
        top_info = path.lstat()
    except (FileNotFoundError, PermissionError, OSError) as exc:
        result["errors"].append("lstat: {}".format(exc))
        return result

    result["device"] = top_info.st_dev
    result["inode"] = top_info.st_ino
    result["top_owned"] = top_info.st_uid == uid
    if stat.S_ISLNK(top_info.st_mode):
        result["kind"] = "symlink"
    elif stat.S_ISDIR(top_info.st_mode):
        result["kind"] = "directory"
    elif stat.S_ISREG(top_info.st_mode):
        result["kind"] = "file"
    else:
        result["kind"] = "other"

    include_stat(
        result,
        path,
        top_info,
        allowed_uids,
        seen_inodes,
    )
    if result["kind"] != "directory":
        return result
    if not has_directory_delete_access(path):
        result["deletable"] = False

    try:
        result["mountpoint"] = os.path.ismount(str(path))
    except OSError as exc:
        result["errors"].append("mountpoint: {}".format(exc))
        return result
    if result["mountpoint"]:
        return result

    def on_walk_error(exc):
        result["errors"].append("walk: {}".format(exc))

    try:
        for current, dirnames, filenames in os.walk(
            str(path), topdown=True, onerror=on_walk_error, followlinks=False
        ):
            current_path = Path(current)
            for name in dirnames + filenames:
                child = current_path / name
                try:
                    info = child.lstat()
                    include_stat(
                        result,
                        child,
                        info,
                        allowed_uids,
                        seen_inodes,
                    )
                    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                        if not has_directory_delete_access(child):
                            result["deletable"] = False
                except (FileNotFoundError, PermissionError, OSError) as exc:
                    result["errors"].append("{}: {}".format(child, exc))
    except (PermissionError, OSError) as exc:
        result["errors"].append("walk root: {}".format(exc))
    return result


def excluded_reason(path):
    name = path.name
    if name.startswith("."):
        return "hidden"
    if any(name.startswith(prefix) for prefix in RUNTIME_PREFIXES):
        return "runtime"
    return None


def classify(result, now):
    path = Path(result["path"])
    excluded = excluded_reason(path)
    if excluded:
        return "excluded_{}".format(excluded)
    if result["errors"]:
        return "scan_error"
    if not result["top_owned"]:
        return "other_owner"
    if result["mountpoint"]:
        return "mountpoint"
    if not result["owned"]:
        return "other_owner"
    if result["latest_mtime"] > now - BASE_RETENTION_DAYS * 86400:
        return "recent"
    if (
        result["latest_script_mtime"]
        and result["latest_script_mtime"] > now - SCRIPT_RETENTION_DAYS * 86400
    ):
        return "script_recent"
    if not result["deletable"]:
        return "not_writable"
    return "candidate"


def safe_top_level(path):
    absolute = Path(os.path.abspath(str(path)))
    return any(absolute.parent == root and absolute.name for root in SCAN_ROOTS)


def delete_owned_tree(path, uid, allowed_uids=None):
    """Delete without following symlinks, checking ownership at every step."""
    allowed_uids = allowed_uids or {uid}
    if not safe_top_level(path):
        raise RuntimeError("path is outside fixed top-level cleanup scope: {}".format(path))
    info = path.lstat()
    if info.st_uid != uid:
        raise RuntimeError("owner changed before deletion: {}".format(path))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        path.unlink()
        return
    if os.path.ismount(str(path)):
        raise RuntimeError("refusing to remove mountpoint: {}".format(path))
    with os.scandir(str(path)) as entries:
        children = [Path(entry.path) for entry in entries]
    for child in children:
        child_info = child.lstat()
        if child_info.st_uid not in allowed_uids:
            raise RuntimeError("non-allowed descendant owner appeared: {}".format(child))
        if stat.S_ISDIR(child_info.st_mode) and not stat.S_ISLNK(child_info.st_mode):
            if os.path.ismount(str(child)):
                raise RuntimeError("nested mountpoint appeared: {}".format(child))
            delete_owned_tree_inner(child, allowed_uids)
        else:
            child.unlink()
    path.rmdir()


def delete_owned_tree_inner(path, allowed_uids):
    info = path.lstat()
    if info.st_uid not in allowed_uids:
        raise RuntimeError("non-allowed directory owner appeared: {}".format(path))
    if os.path.ismount(str(path)):
        raise RuntimeError("nested mountpoint appeared: {}".format(path))
    with os.scandir(str(path)) as entries:
        children = [Path(entry.path) for entry in entries]
    for child in children:
        child_info = child.lstat()
        if child_info.st_uid not in allowed_uids:
            raise RuntimeError("non-allowed descendant owner appeared: {}".format(child))
        if stat.S_ISDIR(child_info.st_mode) and not stat.S_ISLNK(child_info.st_mode):
            delete_owned_tree_inner(child, allowed_uids)
        else:
            child.unlink()
    path.rmdir()


def scan_roots(uid, allowed_uids, now):
    results = []
    root_errors = []
    for root in SCAN_ROOTS:
        if not root.is_dir():
            if root in ROOTS:
                root_errors.append("missing root: {}".format(root))
            continue
        try:
            with os.scandir(str(root)) as entries:
                paths = sorted((Path(entry.path) for entry in entries), key=lambda p: p.name)
        except (PermissionError, OSError) as exc:
            root_errors.append("{}: {}".format(root, exc))
            continue
        for path in paths:
            if root in ROOTS and path in NESTED_CONTAINER_ROOTS:
                continue
            result = analyze_entry(path, uid, allowed_uids)
            result["reason"] = classify(result, now)
            results.append(result)
    return results, root_errors


def compact_entry(result):
    return {
        "path": result["path"],
        "kind": result["kind"],
        "allocated_bytes": result["allocated_bytes"],
        "latest_mtime": iso_time(result["latest_mtime"])
        if result["latest_mtime"]
        else None,
        "script_count": result["script_count"],
        "latest_script_mtime": iso_time(result["latest_script_mtime"])
        if result["latest_script_mtime"]
        else None,
        "reason": result["reason"],
        "errors": result["errors"],
    }


def summarize(results):
    reasons = collections.Counter(item["reason"] for item in results)
    candidates = [item for item in results if item["reason"] in CANDIDATE_REASONS]
    return {
        "scanned_top_level": len(results),
        "candidate_count": len(candidates),
        "candidate_bytes": sum(item["allocated_bytes"] for item in candidates),
        "reasons": dict(sorted(reasons.items())),
    }


def write_record(action, started_at, summary, details, root_errors):
    run_id = "{}-{}-{}".format(
        time.strftime("%Y%m%dT%H%M%S", time.localtime(started_at)), action, os.getpid()
    )
    record_path = HISTORY_DIR / "{}.json".format(run_id)
    record = {
        "run_id": run_id,
        "action": action,
        "started_at": iso_time(started_at),
        "finished_at": iso_time(time.time()),
        "user": TARGET_USER,
        "roots": [str(root) for root in ROOTS],
        "scan_roots": [str(root) for root in SCAN_ROOTS],
        "policy": {
            "allowed_descendant_users": list(ALLOWED_DESCENDANT_USERS),
            "ordinary_retention_days": BASE_RETENTION_DAYS,
            "script_retention_days": SCRIPT_RETENTION_DAYS,
            "script_suffixes": sorted(SCRIPT_SUFFIXES),
        },
        "summary": summary,
        "root_errors": root_errors,
        "entries": details,
    }
    temporary = record_path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    os.chmod(str(temporary), 0o600)
    os.replace(str(temporary), str(record_path))
    return run_id, record_path


def print_summary(label, summary, record_path):
    print("RESULT={}".format(label))
    print("SCANNED_TOP_LEVEL={}".format(summary.get("scanned_top_level", 0)))
    print("CANDIDATES={}".format(summary.get("candidate_count", 0)))
    print("CANDIDATE_BYTES={}".format(human_bytes(summary.get("candidate_bytes", 0))))
    if "deleted_count" in summary:
        print("DELETED={}".format(summary["deleted_count"]))
        print("RELEASED_BYTES={}".format(human_bytes(summary["deleted_bytes"])))
        print("DELETE_ERRORS={}".format(summary["delete_error_count"]))
    skipped = dict(summary.get("reasons", {}))
    skipped.pop("candidate", None)
    print("SKIPPED={}".format(json.dumps(skipped, sort_keys=True)))
    print("HISTORY={}".format(record_path))


def command_scan(uid, allowed_uids):
    started = time.time()
    results, root_errors = scan_roots(uid, allowed_uids, started)
    summary = summarize(results)
    candidates = [
        compact_entry(item) for item in results if item["reason"] in CANDIDATE_REASONS
    ]
    _, record_path = write_record("scan", started, summary, candidates, root_errors)
    print_summary("SCAN_OK", summary, record_path)
    return 0 if not root_errors else 1


def command_clean(uid, allowed_uids):
    started = time.time()
    results, root_errors = scan_roots(uid, allowed_uids, started)
    summary = summarize(results)
    deleted = []
    delete_errors = []
    for original in results:
        if original["reason"] not in CANDIDATE_REASONS:
            continue
        path = Path(original["path"])
        try:
            current = analyze_entry(path, uid, allowed_uids)
            current["reason"] = classify(current, time.time())
            if current["reason"] not in CANDIDATE_REASONS:
                delete_errors.append(
                    {"path": str(path), "error": "revalidation: {}".format(current["reason"])}
                )
                continue
            if current["device"] != original["device"] or current["inode"] != original["inode"]:
                delete_errors.append({"path": str(path), "error": "entry identity changed"})
                continue
            delete_owned_tree(path, uid, allowed_uids)
            deleted_entry = compact_entry(current)
            deleted_entry["deletion_method"] = "safe_delete"
            deleted.append(deleted_entry)
        except FileNotFoundError:
            delete_errors.append({"path": str(path), "error": "entry disappeared"})
        except (PermissionError, OSError, RuntimeError) as exc:
            delete_errors.append({"path": str(path), "error": str(exc)})

    summary["deleted_count"] = len(deleted)
    summary["deleted_bytes"] = sum(item["allocated_bytes"] for item in deleted)
    summary["delete_error_count"] = len(delete_errors)
    details = {"deleted": deleted, "delete_errors": delete_errors}
    _, record_path = write_record("clean", started, summary, details, root_errors)
    print_summary("CLEAN_OK" if not delete_errors and not root_errors else "CLEAN_PARTIAL", summary, record_path)
    return 0 if not delete_errors and not root_errors else 1


def load_history_record(path):
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def command_history(args):
    if args.id:
        path = HISTORY_DIR / "{}.json".format(args.id)
        if not path.is_file():
            print("history record not found: {}".format(args.id), file=sys.stderr)
            return 1
        print(json.dumps(load_history_record(path), ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    paths = sorted(HISTORY_DIR.glob("*.json"), reverse=True)[: args.limit]
    if not paths:
        print("NO_HISTORY")
        return 0
    for path in paths:
        try:
            record = load_history_record(path)
            summary = record.get("summary", {})
            print(
                "{} action={} candidates={} deleted={} released={} result_errors={}".format(
                    record.get("run_id", path.stem),
                    record.get("action", "unknown"),
                    summary.get("candidate_count", 0),
                    summary.get("deleted_count", 0),
                    human_bytes(summary.get("deleted_bytes", 0)),
                    summary.get("delete_error_count", 0) + len(record.get("root_errors", [])),
                )
            )
        except (OSError, ValueError, TypeError) as exc:
            print("{} unreadable={}".format(path.name, exc))
    return 0


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Scan and clean a configured user's temporary files using fixed safety rules."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("scan", help="scan fixed temporary roots and record candidates")
    subparsers.add_parser("clean", help="delete expired candidates and record the result")
    history = subparsers.add_parser("history", help="show prior scan and cleanup records")
    history.add_argument("--limit", type=int, default=10)
    history.add_argument("--id", help="show one complete record by run id")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        uid = target_uid()
        allowed_uids = allowed_descendant_uids()
        check_identity(uid)
        ensure_runtime(uid)
        if args.command == "history":
            return command_history(args)
        with LOCK_FILE.open("a+", encoding="utf-8") as lock_stream:
            try:
                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("RESULT=BUSY", file=sys.stderr)
                return 2
            if args.command == "scan":
                return command_scan(uid, allowed_uids)
            return command_clean(uid, allowed_uids)
    except (OSError, RuntimeError, ValueError) as exc:
        print("ERROR={}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
