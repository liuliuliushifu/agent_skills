---
name: temp-files-mgr
description: Scan, clean, and audit configured temporary-file roots. Use for temporary-file size, stale-file cleanup, cleanup history, disk usage, or scheduled cleanup of build, deploy, and test scratch data.
---

# Temporary Files Manager

Use the deterministic manager for every operation:

```bash
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
MGR="$CODEX_HOME/skills/temp-files-mgr/scripts/temp_files_mgr.py"
python3 "$MGR" scan
python3 "$MGR" clean
python3 "$MGR" history --limit 10
python3 "$MGR" history --id RUN_ID
```

## Configuration

Configure the runtime with environment variables. The managed user path is always assembled as:

```text
TEMP_FILES_BASE_DIR / TEMP_FILES_USER / TEMP_FILES_USER_TMP_SUBDIR
```

- `TEMP_FILES_USER`: target account; defaults to the invoking account.
- `TEMP_FILES_BASE_DIR`: base directory that contains user homes; defaults to the parent of the
  invoking account's home.
- `TEMP_FILES_USER_TMP_SUBDIR`: relative temporary directory below the assembled user home;
  defaults to `.tmp` and must not be absolute or contain `..`.
- `TEMP_FILES_SYSTEM_TMP_DIR`: system temporary root; defaults to `/tmp`.
- `TEMP_FILES_NESTED_SUBDIR`: managed child below the user temporary root whose direct children
  are scanned independently; defaults to `codex`.
- `TEMP_FILES_ALLOWED_DESCENDANT_USERS`: comma-separated additional allowed descendant owners;
  defaults to `nobody`.
- `TEMP_FILES_STATE_DIR`: audit and lock directory; defaults to
  `$CODEX_HOME/temp-files-mgr`.
- `TEMP_FILES_RETENTION_DAYS`: ordinary retention; defaults to `7`.
- `TEMP_FILES_SCRIPT_RETENTION_DAYS`: retention for script-like files; defaults to `14`.

## Workflow

1. Run `scan` before an interactive cleanup and report candidate count and size.
2. Run `clean` only on explicit request or through a configured scheduled job.
3. Run `history` when asked what was removed or whether scheduled cleanup ran.
4. Report deleted count, released size, skipped reasons, errors, and the history record path.

## Safety Policy

- Run only as `TEMP_FILES_USER` and delete only complete top-level entries below configured roots.
- Inspect directories recursively without following symlinks or crossing mounted filesystems.
- Require the top-level entry to be owned by the configured user. Skip the complete entry when
  a descendant has an owner outside the configured allowlist.
- Preflight write and traversal access on every directory. A permission failure is recorded and
  skipped; the manager never invokes Docker or a force-removal helper automatically.
- Retain ordinary entries for the configured ordinary period.
- Retain `.sh`, `.bash`, `.py`, `.json`, `.jsonl`, and `.sftp` files for the configured script
  period. A containing directory remains protected until all such files expire.
- Skip hidden top-level entries and active-runtime namespaces such as `tmux-*`, `ssh-*`, and
  `codex-bwrap-*`.
- Keep audit records outside all cleanup roots.

Do not replace these checks with a broad recursive deletion command.

## Optional Permission-Recovery Guidance

If cleanup reports a real host permission error, stop and report the exact target. Docker-based
removal is a separate, optional operator action; it is never part of scheduled cleanup. Proceed
only after the user explicitly authorizes deletion of that exact target.

Before using Docker, resolve the target, reject cleanup roots themselves, symlinks, ambiguous
paths, and nested mount points, and mount only the target's direct parent. A suitable command
shape is:

```bash
TARGET="/absolute/exact/target"
TARGET_PARENT="$(dirname -- "$TARGET")"
TARGET_NAME="$(basename -- "$TARGET")"
CLEANUP_IMAGE="${TEMP_FILES_DOCKER_IMAGE:-alpine:3.20}"

docker run --rm --network none --read-only --cap-drop ALL \
  --mount "type=bind,src=$TARGET_PARENT,dst=/cleanup" \
  "$CLEANUP_IMAGE" rm -rf -- "/cleanup/$TARGET_NAME"
```

Verify that the exact target is gone. Never widen the mount or deletion target, and do not use
Docker to bypass a sandbox or approval denial.
