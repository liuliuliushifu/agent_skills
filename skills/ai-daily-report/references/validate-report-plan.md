# Validate Report Plan

This is a future upgrade plan. It is intentionally not wired into
`run_ai_daily_report.sh` yet.

## Goal

Add a local Markdown validator after `codex exec` writes
`$AI_DAILY_REPORT_DIR/report_files/YYYYMM/YYYY-MM-DD.md` and before SFTP sync.

The validator should prevent malformed or incomplete reports from being
uploaded to Obsidian while keeping the current generation flow unchanged until
there is evidence that validation is needed.

## Proposed Script

```text
scripts/validate_report.py
```

Proposed invocation:

```bash
python3 scripts/validate_report.py "$AI_DAILY_REPORT_DIR/report_files/YYYYMM/YYYY-MM-DD.md" --date YYYY-MM-DD
```

Return codes:

- `0`: validation passed
- `10`: hard validation failure; do not sync
- `20`: validator internal error; wrapper may conservatively keep local file and skip sync

## Hard Failures

These should block Obsidian sync:

- Missing frontmatter or frontmatter `date` does not match target date.
- Missing `# YYYY-MM-DD`.
- Missing required sections:
  - `## 跟踪事项`
  - `## 项目进展`
  - `## 做了哪些事`
  - `## 学习到了哪些事`
  - `## 风险与阻塞`
  - `## 来源`
- Report still contains deprecated sections:
  - `## 前次计划回顾`
  - `## 下一步计划`
- Report still contains template instruction text.
- Report is suspiciously small, for example under 1000 bytes.
- Sensitive patterns are detected. This duplicates the wrapper scan but gives
  one place for future structured validation output.
- Missing hidden `ai-daily-state` block, invalid JSON inside it, or mismatched
  `date`.

## Warnings

These should be recorded in `status.json` but should not block sync at first:

- `跟踪事项` says no tracked items were available even though active tracking items exist in the ledger.
- `风险与阻塞` table has no data rows.
- `来源` does not list any memory files.
- `tracking_updates` or `next_tracking` in `ai-daily-state` does not cover Track IDs shown in `跟踪事项`.
- Any long-term task item uses `id` or `title` instead of the required `task_id` and `text`.
- Any tracking item uses `id` or `title` instead of the required `track_id` and `text`.
- Any `task_id` does not match `YYYYMMDD-N`.
- Any `track_id` does not match `TRK-YYYYMMDD-N`.
- The report is very long, for example over 12000 bytes.

## Status Integration

If implemented, `validate_report.py` should emit compact JSON such as:

```json
{
  "passed": true,
  "errors": [],
  "warnings": ["sources_missing_memory_files"]
}
```

The wrapper should store this under `validation` in:

```text
$AI_DAILY_REPORT_DIR/status.json
```

## Rollout

1. Implement `validate_report.py` as a standalone script.
2. Test against existing local reports without changing sync behavior.
3. Enable warning-only mode for several runs.
4. Promote hard failures to sync blockers only after the validator has no false
   positives on normal reports.
