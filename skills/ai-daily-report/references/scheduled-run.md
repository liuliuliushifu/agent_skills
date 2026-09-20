# Scheduled Run

Use this reference when setting up or reviewing unattended `codex exec` daily report execution.

## Canonical Command

Run as the configured Codex account so Codex can read `$CODEX_HOME/auth.json`, sessions,
memories, rules, and any explicitly configured SSH keys. Set `CODEX_HOME` instead of embedding
an account name or home path in the schedule.

```bash
$AI_DAILY_REPORT_DIR/run_ai_daily_report.sh
```

This is a stable compatibility entrypoint for cron. It delegates to:

```bash
<skill-dir>/scripts/run_ai_daily_report.sh
```

The wrapper invokes:

```bash
"${CODEX_BIN:-codex}" \
  --ask-for-approval never \
  exec \
  --cd "$CODEX_HOME" \
  --sandbox read-only \
  --skip-git-repo-check \
  "使用 $ai-daily-report 生成目标日期的中文 Codex/AI 工作日报。最终答复必须是可直接保存为 Markdown 文件的日报正文；wrapper 负责落盘、脱敏检查和同步到 Obsidian。"
```

Portable deployments can set `CODEX_BIN` instead of using the pinned local
Node/Codex JavaScript entrypoint.

By default, scheduled runs generate the previous calendar day's report. The
workday checker receives that target date and skips weekends or holidays.
Manual runs can use an explicit date, `--yesterday`, or `--today`. Historical
ledger backfills repeat `--plan-id ID` for every existing Task/Track ID that
may be read and updated.

The wrapper then:

1. Resolves the report target date. With no argument, the default target is yesterday. Use `AI_DAILY_TARGET_OFFSET_DAYS=N`, `--yesterday`, `--today`, or an explicit `YYYY-MM-DD` to override it.
2. Runs `<skill-dir>/scripts/check_workday.py YYYY-MM-DD`
3. Updates `$AI_DAILY_REPORT_DIR/status.json` at each major stage
4. Skips before starting Codex when the target date is a weekend or holiday
5. For scheduled default/`--yesterday`/`--today` runs, writes compact Task ID context from `plan_state.py context --date YYYY-MM-DD` to `$AI_DAILY_REPORT_DIR/logs/plan-state-YYYY-MM-DD.md`. A scoped historical run adds one `--item-id ID` per wrapper `--plan-id`.
6. Collects structured evidence with `collect_evidence.py`, writing `$AI_DAILY_REPORT_DIR/logs/evidence-YYYY-MM-DD.json` and `.summary.txt`
7. Passes the evidence file path to Codex. Jira evidence is date-window based: `jira.created_on_target_date` and `jira.closed_on_target_date` are facts only. TRK updates come from plan context plus outcome evidence, not from automatic Jira key classification.
8. Copies `--output-last-message` into `$AI_DAILY_REPORT_DIR/report_files/YYYYMM/YYYY-MM-DD.md`
9. Runs a sensitive-pattern scan on the generated report
10. For scheduled default/`--yesterday`/`--today` runs, imports the report's `ai-daily-state.task_updates`, `next_long_tasks`, `tracking_updates`, and `next_tracking` back into `$AI_DAILY_REPORT_DIR/plan_state.json` with `plan_state.py import-report ... --replace-active`. Scoped historical runs add the same `--item-id` allowlist and reject unlisted IDs.
11. Writes `$CODEX_HOME/.tmp/obsidian-upload-YYYY-MM-DD.sftp`
12. Derives `YYYYMM` from the target date, creates the remote month directory, and performs fixed-shape `ssh` and `sftp` sync plus readback verification
13. Runs `<skill-dir>/scripts/cleanup_daily_report_run.sh YYYY-MM-DD` after successful SFTP sync and readback verification; failed runs keep temporary evidence for diagnostics

## Constraints

- Set `AI_DAILY_TIMEZONE` or `timezone` in `.config.json`; the safe default is `UTC`.
- Runtime state defaults to `$CODEX_HOME/daily_report`; override with `AI_DAILY_REPORT_DIR`.
- Set `AI_DAILY_SYNC=0` for local-only report generation without SSH/SFTP.
- Set `AI_DAILY_REMOTE_USER`, `AI_DAILY_REMOTE_HOST`, and `AI_DAILY_REMOTE_DIR` when syncing to a different Obsidian target. `AI_DAILY_REMOTE_DIR` is the base directory; the wrapper appends `YYYYMM/YYYY-MM-DD.md`.
- Do not rely on cron's default `PATH`; set `CODEX_BIN`, or set both `NODE_BIN` and `CODEX_JS`.
- Keep network proxy settings outside the portable skill. Interactive shells and cron should
  inherit the same operator-owned environment.
- Workday preflight uses the configured holiday API URL templates first, then cached API data,
  then the configured local calendar Python package, then a weekday/weekend fallback. Configure
  these through `calendar.*` in `.config.json` or the corresponding `AI_DAILY_CALENDAR_*`
  environment variables.
- `check_workday.py` attempts `python -m pip install --user -U chinesecalendar` after each decision; upgrade success or failure must not change the current run's decision.
- `status.json` is best-effort runtime metadata for quick diagnostics; logs remain the detailed source of truth.
- `plan_state.json` is the durable plan ledger. Completed or dropped tasks stay in the ledger with `completed_date`; use `plan_state.py context` for compact prompt input instead of reading the full JSON.
- Long-term/stage task records use `task_id` plus Chinese `text`; `task_id` must be `YYYYMMDD-N`, for example `20260507-1`.
- Daily unclosed tracking items use `track_id` plus Chinese `text`; `track_id` must be `TRK-YYYYMMDD-N`, for example `TRK-20260507-1`.
- By default `AI_DAILY_PLAN_LEDGER=auto`: the wrapper reads/writes the ledger for scheduled default-offset runs, `--yesterday`, `--today`, and explicit same-day runs. Explicit historical dates skip ledger mutation. To use historical ledger data, pass one or more `--plan-id ID` arguments; these IDs scope context, evidence, and import. `AI_DAILY_PLAN_LEDGER=0` disables unscoped automatic ledger use.
- `collect_evidence.py` runs before Codex so network-backed Jira status checks happen in the wrapper environment instead of inside the read-only Codex sandbox.
- `collect_evidence.py` is a wrapper-owned black box for scheduled runs. Codex should trust its output and should not execute or inspect evidence collection scripts, run `python -m py_compile`, call Jira CLI, or run git discovery/status/log commands. Missing or contradictory evidence should be reported as a risk or source limitation instead of being repaired by ad hoc commands.
- Workspace git evidence is collected only for repositories under configured prefixes. The
  default allowlist is empty. Runtime/tooling repositories should not be scanned as project
  repositories unless explicitly configured.
- Evidence files under `logs/evidence-YYYY-MM-DD.json` and `.summary.txt` are temporary prompt inputs. The cleanup script removes them only after the report has synced and readback verification has passed; keep them on failure.
- Use `AI_DAILY_FORCE=1` to bypass the workday check when manually backfilling a weekend or holiday report.
- Use `--sandbox read-only`; scheduled runs should let Codex read and summarize, while the wrapper performs local write and remote sync.
- Do not use `--ephemeral`; scheduled runs should leave session logs for audit.
- Do not use `--ignore-rules`; SSH/SFTP sync depends on user execpolicy rules.
- Use `--ask-for-approval never`; scheduled runs must fail fast instead of waiting for a prompt.
- Write logs under `$AI_DAILY_REPORT_DIR/logs/`.
- If remote sync fails, keep `$AI_DAILY_REPORT_DIR/report_files/YYYYMM/YYYY-MM-DD.md` and report the failure in logs.

## Cron Example

```cron
AI_DAILY_ENV_FILE=/path/to/ai-daily.env
15 1 * * * . "$AI_DAILY_ENV_FILE" && "$AI_DAILY_REPORT_DIR/run_ai_daily_report.sh" >> "$AI_DAILY_REPORT_DIR/logs/cron.log" 2>&1
```

The example runs at 01:15 and generates the previous calendar day's report.
