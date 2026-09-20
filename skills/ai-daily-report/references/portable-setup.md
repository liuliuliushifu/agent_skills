# Portable Setup

This skill is designed so the `ai-daily-report` folder can be copied to another
Codex agent and run with minimal local setup.

## Runtime Directories

By default scripts use:

```text
CODEX_HOME=$HOME/.codex
AI_DAILY_REPORT_DIR=$CODEX_HOME/daily_report
```

Runtime state stays outside the skill folder:

```text
$AI_DAILY_REPORT_DIR/report_files/YYYYMM/YYYY-MM-DD.md
$AI_DAILY_REPORT_DIR/logs/
$AI_DAILY_REPORT_DIR/status.json
$AI_DAILY_REPORT_DIR/plan_state.json
```

## Direct Run

Generate and sync the previous calendar day's report:

```bash
AI_DAILY_REPORT_DIR="$HOME/.codex/daily_report" \
  "$HOME/.codex/skills/ai-daily-report/scripts/run_ai_daily_report.sh"
```

Generate only a local report without Obsidian sync:

```bash
AI_DAILY_SYNC=0 \
AI_DAILY_REPORT_DIR="$HOME/.codex/daily_report" \
  "$HOME/.codex/skills/ai-daily-report/scripts/run_ai_daily_report.sh" YYYY-MM-DD
```

Use `--today` for same-day testing, `--yesterday` for an explicit previous-day
run, or `AI_DAILY_TARGET_OFFSET_DAYS=N` to change the default offset.

For a historical report that may read and update existing ledger items, pass
each allowed ID explicitly:

```bash
"$HOME/.codex/skills/ai-daily-report/scripts/run_ai_daily_report.sh" \
  YYYY-MM-DD --plan-id YYYYMMDD-N --plan-id TRK-YYYYMMDD-N
```

## Important Environment Variables

- `CODEX_HOME`: Codex data directory. Defaults to `$HOME/.codex`.
- `CODEX_USER_HOME`: account home used for optional user-local tools. Defaults to `$HOME`.
- `AI_DAILY_REPORT_DIR`: runtime state directory. Defaults to `$CODEX_HOME/daily_report`.
- `AI_DAILY_TIMEZONE`: report timezone. Defaults to the `timezone` config value, then `UTC`.
- `CODEX_BIN`: Codex CLI executable. If unset, the wrapper tries `CODEX_JS` plus `NODE_BIN`, then `codex` from `PATH`.
- `NODE_BIN`: Node executable for a JavaScript Codex entrypoint.
- `CODEX_JS`: JavaScript Codex entrypoint.
- `AI_DAILY_SYNC`: set to `0` or `false` for local-only report generation.
- `AI_DAILY_TARGET_OFFSET_DAYS`: default target date offset for no-argument runs. Defaults to `1`, meaning previous calendar day.
- `AI_DAILY_REMOTE_USER`, `AI_DAILY_REMOTE_HOST`, `AI_DAILY_REMOTE_DIR`: Obsidian sync target. The remote directory is a base path; sync appends `YYYYMM/YYYY-MM-DD.md` and creates the month directory automatically.
- `ACLI_BIN`: Atlassian CLI path for Jira evidence collection. Defaults to `$HOME/.local/bin/acli`.
- `REME_MEMORY_ROOT`: ReMe markdown memory root. Defaults to `$HOME/.local/share/reme/light_data/memory`.
- `AI_DAILY_ALLOWED_REPO_PREFIXES`: `:`-separated repo path prefixes allowed for git evidence collection.
- `AI_DAILY_PROJECTS_FILE`: project registry JSON path.
- `AI_DAILY_SESSIONS_ROOT`, `AI_DAILY_MEMORIES_ROOT`, `AI_DAILY_JIRA_LEDGER`, and
  `AI_DAILY_PLAN_STATE`: optional data-source path overrides.

Proxy values are machine-owned environment settings and are intentionally not
stored in `scripts/.config.json`. Configure them in the shell, service, CI, or
cron environment. `BASH_ENV=/path/to/proxy.env` is suitable for Bash-based cron
jobs that should share the interactive environment.

## Compatibility Entrypoints

Optional compatibility wrappers may be installed under `$AI_DAILY_REPORT_DIR`. New logic should
remain in the skill's `scripts/` directory, and wrapper locations must come from configuration.
