#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DATE="${1:?usage: cleanup_daily_report_run.sh YYYY-MM-DD [--reset-run-artifacts]}"
MODE="${2:-evidence}"
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
DAILY_REPORT_DIR="${AI_DAILY_REPORT_DIR:-$CODEX_HOME_DIR/daily_report}"
LOG_DIR="$DAILY_REPORT_DIR/logs"
PYTHON_BIN="${PYTHON_BIN:-python3}"
REPORT_PATH_TOOL="$SCRIPT_DIR/report_paths.py"
if [[ -f "$REPORT_PATH_TOOL" ]]; then
  REPORT_FILE="$("$PYTHON_BIN" "$REPORT_PATH_TOOL" path "$TARGET_DATE")"
else
  REPORT_FILE="$DAILY_REPORT_DIR/report_files/${TARGET_DATE:0:4}${TARGET_DATE:5:2}/${TARGET_DATE}.md"
fi
STATUS_FILE="$DAILY_REPORT_DIR/status.json"
BATCH_FILE="$CODEX_HOME_DIR/.tmp/obsidian-upload-${TARGET_DATE}.sftp"

cleanup_evidence() {
  # Evidence files are temporary prompt inputs.
  rm -f \
    "$LOG_DIR/evidence-${TARGET_DATE}.json" \
    "$LOG_DIR/evidence-${TARGET_DATE}.summary.txt" \
    "$BATCH_FILE"
}

cleanup_run_artifacts() {
  cleanup_evidence
  rm -f \
    "$REPORT_FILE" \
    "$LOG_DIR/ai-daily-${TARGET_DATE}.log" \
    "$LOG_DIR/ai-daily-${TARGET_DATE}.last.md" \
    "$LOG_DIR/plan-state-${TARGET_DATE}.md" \
    "$BATCH_FILE"
  if [[ -f "$STATUS_FILE" ]]; then
    rm -f "$STATUS_FILE"
  fi
}

case "$MODE" in
  evidence)
    cleanup_evidence
    printf '[%s] cleaned temporary evidence for %s\n' "$(date '+%F %T %z')" "$TARGET_DATE"
    ;;
  --reset-run-artifacts)
    cleanup_run_artifacts
    printf '[%s] reset local run artifacts for %s\n' "$(date '+%F %T %z')" "$TARGET_DATE"
    ;;
  *)
    printf 'unknown cleanup mode: %s\n' "$MODE" >&2
    exit 2
    ;;
esac
