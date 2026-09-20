# ai-daily-report

Generate Chinese AI/Codex daily reports, maintain a Task ID plan ledger, and optionally sync the report to an Obsidian vault.

## Configuration

Edit `scripts/.config.json` for local defaults:

- `timezone`: report timezone.
- `codex.node_bin` and `codex.codex_js`: explicit Codex runtime paths when `codex` is not on `PATH`.
- `remote`: Obsidian SSH/SFTP target. `remote.dir` is the base folder; reports sync to `YYYYMM/YYYY-MM-DD.md`, and the month folder is created automatically. Set `remote.sync` to `false` for local-only reports.
- `jira.projects` and `jira.done_status_names`: Jira keys/status names used by evidence collection.
- `git.allowed_repo_prefixes`: local repository prefixes that evidence collection may inspect.

The checked-in defaults are publication-safe: remote sync is disabled, remote identity and
directory values are empty, Jira projects are empty, and no repository prefixes are allowed.
Set `AI_DAILY_PROJECTS_FILE` to a local project registry or edit `scripts/projects.json` after
installation.

Environment variables override config values, including `CODEX_HOME`, `AI_DAILY_REPORT_DIR`, `AI_DAILY_REMOTE_USER`, `AI_DAILY_REMOTE_HOST`, `AI_DAILY_REMOTE_DIR`, and `AI_DAILY_SYNC`.

## Usage

```bash
bash scripts/run_ai_daily_report.sh
bash scripts/run_ai_daily_report.sh --yesterday
bash scripts/run_ai_daily_report.sh --today
bash scripts/run_ai_daily_report.sh YYYY-MM-DD
bash scripts/run_ai_daily_report.sh YYYY-MM-DD --plan-id YYYYMMDD-N --plan-id TRK-YYYYMMDD-N
```

With no argument, the scheduled wrapper defaults to the previous calendar day.
The workday checker receives the resolved target date.
Historical runs use only the explicitly listed existing ledger IDs, and the
same allowlist protects context extraction, evidence collection, and import.

Keep proxy settings in the machine environment rather than this portable
skill configuration. Bash cron jobs can use `BASH_ENV=/path/to/proxy.env`.

The local report is written to:

```text
$AI_DAILY_REPORT_DIR/report_files/YYYYMM/YYYY-MM-DD.md
```
