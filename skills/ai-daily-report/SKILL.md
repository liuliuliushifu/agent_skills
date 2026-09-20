---
name: ai-daily-report
description: Generate and sync Chinese Codex/AI work daily reports for the user's "daily by AI" Obsidian workflow. Use for Codex 工作日报, AI 工作日报, daily by AI 同步, Codex 当天工作总结, or scheduled Codex-generated daily report execution.
---

# AI Daily Report

Generate a daily engineering summary with Codex participation and optionally sync it to a
configured remote directory.

## When Triggered

Use this skill when the user asks to:

- 生成 Codex/AI 工作日报、写 AI 日报、总结 Codex 当天工作
- 同步到 Obsidian 的 `daily by AI`
- 设置或执行 Codex 定时日报
- Review what Codex agents did today

Do not use this skill for generic non-Codex daily reports unless the user explicitly says they should use the `daily by AI` workflow.

## Core Rules

- Write in Chinese. Keep necessary English technical terms, commands, paths, identifiers, and error messages unchanged.
- Summarize; do not dump raw chat logs.
- Use an Obsidian-friendly layout: a top `summary` callout, outcome-focused sections, a priority table for risks, and a folded `info` callout for sources.
- The required sections are `项目进展`, `跟踪事项`, `做了哪些事`, `学习到了哪些事`, `token使用`, `风险与阻塞`, and `来源`. `跟踪事项` must appear after `项目进展`. `token使用` must appear after `学习到了哪些事` and before `风险与阻塞`. Do not output separate `前次计划回顾` or `下一步计划` sections.
- Append a folded `ai-daily-state` callout with machine-readable `task_updates`, `next_long_tasks`, `tracking_updates`, and `next_tracking` JSON entries.
- Long-running or stage task records must use `task_id` plus Chinese `text`. `task_id` format is `YYYYMMDD-N`, for example `20260507-1`.
- Daily unclosed tracking items must use `track_id` plus Chinese `text`. `track_id` format is `TRK-YYYYMMDD-N`, for example `TRK-20260507-1`.
- `项目进展` must be organized only by registered active projects from `$AI_DAILY_REPORT_DIR/projects.json` or `scripts/projects.json`; do not invent new project rows from incidental daily activity.
- Long-running tasks may include `tracking.mode=long_term` metadata with keywords, Jira keys, and repo paths. Use `evidence.long_tasks` for targeted tracking, and summarize only outcome/status progress in one or two sentences.
- Treat `$AI_DAILY_REPORT_DIR/plan_state.json` as the durable plan ledger. Use `scripts/plan_state.py context` for compact Task ID context instead of loading the full ledger into the prompt. Historical backfills must pass explicit `--plan-id` values so context extraction and report import remain limited to those existing Task/Track IDs. `$AI_DAILY_REPORT_DIR/plan_state.py` is only a compatibility wrapper when such wrapper is installed.
- Never write plaintext passwords, tokens, private keys, cookies, or full auth headers to the report.
- If sensitive credentials appeared in a conversation, describe only the outcome, for example: `已配置临时凭据` or `已建立 SSH 免密登录`.

`references/report-rules.md` is the canonical source of truth for report rules. Read it before generating or changing a report.

For the Markdown output skeleton, read `references/report-template.md`.

For scheduled `codex exec` usage, read `references/scheduled-run.md`.

For copying this skill to another agent or running it outside this machine, read `references/portable-setup.md`.

## Script Entry Points

- Full setup/reconcile: `scripts/setup.py`
- Hook setup: `scripts/hook_setup.py`
- Turn bundle extractor: `scripts/extract_turn_bundle.py`
- Stop-hook turn capture: `scripts/outcome_stop_hook.py`
- Nightly outcome input preparation: `scripts/outcome_pipeline.py --date YYYY-MM-DD`

The Stop hook path should only capture deterministic per-turn bundles. LLM/subagent outcome analysis belongs in the scheduled report workflow after `outcome_pipeline.py` has prepared bounded inputs.

## Data Sources

Collect relevant information for the target date, using the configured report timezone:

- Codex sessions: `$CODEX_HOME/sessions/YYYY/MM/DD/*.jsonl`
- Current conversation context if the current session has not fully landed in the session log yet
- Memory files:
  - `$CODEX_HOME/memories/*.md`
  - `$CODEX_HOME/memories/**/*.md`
  - `$CODEX_HOME/memories/reme-memory/memory_workflow_events.jsonl`
  - `$CODEX_HOME/memories/reme-memory/handoffs/*`
- Relevant git repositories mentioned by the sessions, memories, or current task. For scheduled wrapper runs, use the workspace repository status already captured in `$AI_DAILY_REPORT_DIR/logs/evidence-YYYY-MM-DD.json`; do not re-run git discovery or status commands.
- Existing local daily report artifacts in `$AI_DAILY_REPORT_DIR/report_files/YYYYMM/`
- Structured evidence from `$AI_DAILY_REPORT_DIR/logs/evidence-YYYY-MM-DD.json` when the wrapper has generated it
- Token usage by agent from `evidence.token_usage.by_agent` and summary from `evidence.token_usage.total` when present
- Long-task targeted evidence from `evidence.long_tasks[]`, when active ledger tasks define `tracking.mode=long_term`
- Project registry from `$AI_DAILY_REPORT_DIR/projects.json` or `scripts/projects.json`
- Plan ledger context from `scripts/plan_state.py context --date YYYY-MM-DD`

Default scope is Codex-only. Cursor transcripts are optional and must be included only when the user explicitly asks for cross-agent coverage or the run configuration enables Cursor inputs.

Treat tool/daemon bus JSON and status files as low-signal operational artifacts unless the target day's outcome, git evidence, long task, or project report profile makes that tool work relevant.

## Workflow

1. Determine the target date. In scheduled wrapper runs, the wrapper supplies the target date and defaults to the previous calendar day. In interactive/manual use, follow the user's requested date; if truly unspecified, use today's date in the configured timezone.
2. Read `references/report-rules.md` and `references/report-template.md`.
3. Read the plan ledger context from `scripts/plan_state.py context --date YYYY-MM-DD` when available. For a scoped historical backfill, append one `--item-id ID` per approved Task/Track ID. Use `Long-Term Tasks` for `项目进展` and `Tracking Items` for `跟踪事项`.
4. Read `$AI_DAILY_REPORT_DIR/logs/evidence-YYYY-MM-DD.json` when present. Jira evidence is limited to `jira.created_on_target_date` and `jira.closed_on_target_date`; Jira keys merely mentioned in dialogue are not daily evidence. Use `plan_state.active_tracking_items` and the plan context as historical TRK input, then decide updates from outcome evidence.
   For scheduled wrapper runs, treat this evidence file as the authoritative black-box collection result. Do not inspect or execute the collection scripts, run `python -m py_compile`, invoke Jira CLI, or run `git status`, `git log`, `git branch`, or `git rev-parse`; if data is missing, record the missing evidence in `风险与阻塞` or `来源`.
5. Find the most recent existing local daily report before the target date. Prefer its folded `ai-daily-state` JSON callout as fallback or supporting context. Do not use legacy hidden HTML comments unless the user explicitly asks for a one-off migration. Skip missing non-workday reports; do not assume the previous calendar day is the prior report.
6. For interactive/manual runs, gather the remaining data sources above. Prefer `rg`, `find`, `sed`, and existing daily report artifacts over ad hoc scanning. For scheduled wrapper runs, do not perform extra data collection beyond reading rule/template files, plan context, evidence files, and evidence-referenced local text sources. Record unreadable or intentionally skipped sources in `风险与阻塞`.
7. If the user provides an explicit project-progress summary, merge it into `项目进展` as first-class context. Do not reduce it to low-level action bullets.
8. For an interactive/manual run, write the report to `$AI_DAILY_REPORT_DIR/report_files/YYYYMM/YYYY-MM-DD.md`.
9. For scheduled `codex exec` runs driven by `scripts/run_ai_daily_report.sh` or a compatibility entry such as `$AI_DAILY_REPORT_DIR/run_ai_daily_report.sh`, return the final report body as the final Markdown answer only; the skill script wrapper will save it, scan it, import its `ai-daily-state.task_updates`, `next_long_tasks`, `tracking_updates`, and `next_tracking` back into the plan ledger, prepare the date-specific SFTP batch file, sync it, and verify it.
10. Scan the draft for sensitive patterns listed in `references/report-rules.md`; redact before syncing.
11. If remote sync is enabled, require non-empty configured user, host, and directory values.
12. Create the configured remote `YYYYMM` directory, upload the report, and verify it using the
    wrapper's SSH/SFTP workflow.

## Configuration

The checked-in `scripts/.config.json` is safe and local-only by default. Configure values there
or through environment variables:

- `timezone` or `AI_DAILY_TIMEZONE`: report timezone; default `UTC`.
- `codex.node_bin`, `codex.codex_js`, or `CODEX_BIN`: Codex executable configuration. When none
  is set, the wrapper resolves `codex` from `PATH`.
- `remote.sync` or `AI_DAILY_SYNC`: enable remote synchronization; default `false`.
- `remote.user`, `remote.host`, and `remote.dir`, or the corresponding
  `AI_DAILY_REMOTE_USER`, `AI_DAILY_REMOTE_HOST`, and `AI_DAILY_REMOTE_DIR`: SSH/SFTP target.
- `jira.projects` and `jira.done_status_names`: optional Jira evidence scope.
- `calendar.*` or the `AI_DAILY_CALENDAR_*` environment variables: workday API URL
  templates, HTTP user agent and timeouts, cache directory, and optional Python package name.
  URL templates may use `{year}` and `{date}` placeholders.
- `git.allowed_repo_prefixes` or `AI_DAILY_ALLOWED_REPO_PREFIXES`: explicit local repositories
  eligible for evidence collection. The default is empty.
- `AI_DAILY_PROJECTS_FILE`: project registry path. The bundled registry is empty.
- `CODEX_HOME`, `CODEX_USER_HOME`, `AI_DAILY_REPORT_DIR`, `AI_DAILY_SESSIONS_ROOT`,
  `AI_DAILY_MEMORIES_ROOT`, `REME_MEMORY_ROOT`, `AI_DAILY_JIRA_LEDGER`, and
  `AI_DAILY_PLAN_STATE`: local runtime paths.

Remote synchronization is optional. Do not infer a username, host, vault location, workspace,
or project from the current machine. When enabled, store each report below the configured remote
directory as `YYYYMM/YYYY-MM-DD.md`.

The SFTP batch file must contain exactly one `put` line for the target report and the configured
remote month directory.

Do not combine local preparation, SSH, SFTP, and verification into one command with pipes, shell separators, heredocs, redirection, or command substitution.

## Failure Handling

- If remote sync fails, keep the local report and state the sync failure clearly.
- If a source cannot be read, note the missing source in `风险与阻塞`.
- If no meaningful activity is found, still create a short report explaining that no useful activity was detected.
- For scheduled `codex exec` runs, avoid interactive prompts. Make conservative assumptions and leave unresolved decisions in `风险与阻塞`.
