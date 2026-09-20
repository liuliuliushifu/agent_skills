---
name: usage-mgr
description: Track and query local Codex token usage by date, session, owner session, or registered agent. Use when the user asks how many Codex tokens were used on this machine, wants daily per-session/per-agent usage reports, asks questions like "skill agent 昨天消耗了多少 token", wants to inspect or install Stop/SubagentStop hook usage accounting, or wants to test whether agent/subagent hooks expose transcript token data.
---

# Usage Mgr

## Overview

Manage the local design and scripts for Codex token-usage accounting on this machine.

## When Used

- Inspect or explain the per-day, per-session token usage plan.
- Query usage by date, session, owner session, cwd, model, or co-agent registered agent.
- Test Codex `Stop` hook payloads and transcript token data.
- Build or maintain scripts that write usage state and daily usage ledgers.
- Feed daily-report workflows with local Codex usage summaries.

## Entry Points

- Design: `references/design.md`
- Hook setup: `scripts/hook_setup.py`
- Production hook/backfill: `scripts/usage_hook.py`
- Record external Codex usage: `scripts/record_external_usage.py`
- Query usage: `scripts/query_usage.py`
- Stop-hook probe: `scripts/tests/test_stop_hook.py`

## Configuration

- `CODEX_HOME`: Codex state root; defaults to `~/.codex`.
- `CODEX_USAGE_ROOT`: usage state and ledger root; defaults to `$CODEX_HOME/usage`.
- `CODEX_SESSIONS_ROOT`: transcript root; defaults to `$CODEX_HOME/sessions`.
- `COAGENT_REGISTRY`: optional co-agent registry used for display attribution; defaults to
  `$CODEX_HOME/coagents/agents.json`.
- `CODEX_USAGE_TEST_ROOT`: optional Stop-hook probe output directory.

Command-line path arguments override these defaults where supported.

## Operating Notes

- Prefer official Codex transcript `token_count` data from `transcript_path`.
- For Codex runs without a persisted transcript, record official `codex exec --json` usage with `record_external_usage.py --agent ReMe --event-id <stable-id>`.
- The production design is hook-driven: `Stop`/`SubagentStop` run `scripts/usage_hook.py`, read the latest cumulative usage, and record the delta.
- Usage records are keyed by real session/cwd facts: `raw_session_id`, `owner_session_id`, `raw_cwd`, `owner_cwd`, and `usage_kind`. Agent names are display-layer data, not ledger keys.
- Scheduled or manual `$ai-daily-report` `codex exec` sessions are displayed as `Daily Report`, not as the cwd-matched `skill agent`.
- Subagent and permission-approval usage should be recorded with the real raw session id and attributed to the parent owner session when transcript metadata exposes a parent thread.
- On the first subagent ingestion, exclude cumulative `token_count` records inherited from the parent transcript before the child's first real turn. Keep zero as the baseline when the child transcript starts without inherited token records.
- For user questions, run `scripts/query_usage.py` instead of manually reading ledger JSONL.
