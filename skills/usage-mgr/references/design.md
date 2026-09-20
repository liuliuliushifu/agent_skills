# Local Codex Usage Accounting Design

## Goal

Track token usage for sessions below the configured `CODEX_HOME`, grouped by day and
session/thread. The preferred source is Codex's own transcript `token_count` event, not a
locally re-tokenized estimate.

## Data Source

Codex hook payloads include `session_id`, `turn_id`, `cwd`, `model`, and `transcript_path`. The transcript JSONL contains `event_msg` records whose payload type is `token_count`.

The useful fields are:

- `total_token_usage`: cumulative usage for the transcript/session.
- `last_token_usage`: usage for the latest model turn when available.
- `session_meta.source.subagent.thread_spawn.parent_thread_id`: parent thread for subagent sessions when present.

## Hook Strategy

Install `scripts/usage_hook.py` as the handler for both `Stop` and `SubagentStop`. Normal agent turns use `Stop`; subagent turns may only emit `SubagentStop`.

For `Stop`, account from `transcript_path`.

For `SubagentStop`, account from `agent_transcript_path` when it is present. The hook payload's `transcript_path` can point to the parent transcript, so using it directly would double count parent-side tool/wait usage and miss the child transcript's own parent metadata.

On every matching hook:

1. Read the hook JSON from stdin.
2. Pick the accounting transcript path.
3. Open the accounting transcript.
4. Find the first `session_meta` record from the start of the transcript and the latest `token_count` record by scanning the transcript backward from the end.
5. Load persistent state for the real accounting session id. For subagents this is `session_meta.id` or `agent_id`, not `session_meta.session_id`, because the latter can be the parent thread id.
6. For a subagent with no persistent state, find its first real UUIDv7 `task_started` record. If copied parent `token_count` records appear before that boundary, use the last copied cumulative total as the initial baseline. If no copied token record exists, keep a zero baseline.
7. Compare the latest `total_token_usage` with the stored or inherited baseline.
8. Append only a positive delta to the daily ledger.
9. Update state even when no ledger row is appended, so repeated hook events are idempotent.

If a Stop/SubagentStop hook sees no token-count marker yet, or sees a token-count marker that has not advanced beyond state, it waits briefly and re-reads the transcript. If retries are exhausted, it writes only a warning diagnostic and does not mark the event as seen, so a later retry/backfill can still account for it.

For hook ingestion, skip older/equal token-count events when state already has a newer cumulative marker. Only use `last_token_usage` when the token-count marker is newer than state but the cumulative total regressed.

For backfill, compute the target date's window delta from transcript token counts:

```text
delta = latest total_token_usage inside target date
      - latest total_token_usage before target date
```

This prevents old long-running sessions from charging historical usage to the backfilled day.
On a subagent's spawn day, the inherited parent-history baseline is also considered; the newer of the day baseline and inherited baseline wins.

The hook must use a file lock around state and ledger updates because multiple Codex agents can finish at nearly the same time.

## External Usage Records

Some Codex runs do not expose a persisted transcript to hooks. For example, `codex exec --ephemeral --json` emits official per-turn usage in stdout JSONL, while the Stop hook payload can miss `transcript_path`. Account these runs through `scripts/record_external_usage.py` instead of transcript scanning.

External ingestion rules:

- Caller supplies an agent label such as `ReMe` and a usage JSON object from the official Codex event.
- Caller must supply a stable `--event-id`; without it the script refuses to write because retries cannot be made idempotent.
- External records must use `source: external`; put a more specific label such as `codex_exec_json` in `external_source`.
- The state and ledger update still happens under the global usage lock.
- External deltas are added to the stored cumulative total under the same lock, so concurrent external writers cannot overwrite or undercount each other.
- External records set `agent_locked: true`, so user-facing queries preserve the supplied agent label instead of remapping it by cwd through the co-agent registry.

`reme-memory/scripts/record_codex_exec_usage.py` is the ReMe-side bridge. It reads a single-turn `codex exec --json` event stream, requires `thread.started` unless an explicit event id is supplied, extracts `turn.completed.usage`, derives a stable event id from the thread id and event line, and records the usage as agent `ReMe`. Invalid JSON or multiple `turn.completed.usage` events are rejected instead of guessed.

## Storage Layout

```text
$CODEX_USAGE_ROOT/
  state.json
  ledger/
    YYYY/
      MM/
        YYYY-MM-DD.jsonl
```

`state.json` stores the latest cumulative total per real session id. Daily ledger rows are append-only JSONL records.

## Query Interface

Use `scripts/query_usage.py` for user-facing usage questions. It supports:

```bash
scripts/query_usage.py --date yesterday --agent "skill agent"
scripts/query_usage.py --date 2026-07-03 --session 019f25ba --group-by session
scripts/query_usage.py --since 2026-07-01 --until 2026-07-03 --group-by agent
scripts/query_usage.py --date yesterday --group-by cwd
scripts/query_usage.py --date today --source probe --group-by agent
```

Filters:

- `--date`, `--since`, `--until`
- `--agent` using co-agent `agents.json` names, aliases, and chat names
- `--session` for raw accounting session id prefix matching
- `--owner` for parent/owner session id prefix matching
- `--cwd` for cwd substring matching

The ledger stores stable facts first: session ids, cwd, transcript path, usage kind, and token deltas. Agent grouping maps cwd through the current co-agent registry unless a record has `agent_locked: true`. Scheduled daily reports should prefer `--group-by agent` and trust `query_usage.py` as the display-layer authority. External or system-owned records with `agent_locked: true` preserve the supplied label, such as `ReMe` and `Daily Report`.

Grouping:

- `none`
- `day`
- `agent`
- `session`
- `owner`
- `cwd`
- `model`

The script reads production ledgers by default and deduplicates ledger rows by `event_key`. In `--source auto` mode it falls back to the Stop-hook probe log when no ledger exists, which is useful during hook validation only. Scheduled daily reports must use `--source ledger`, never `auto`, so probe data cannot pollute production summaries.

## Ledger Row Shape

```json
{
  "recorded_at": "2026-07-03T14:30:00+08:00",
  "usage_date": "2026-07-03",
  "event_key": "hook:Stop:session-id:turn-id",
  "session_id": "hook-session-id",
  "raw_session_id": "real-accounting-session-id",
  "accounting_session_id": "real-accounting-session-id",
  "owner_session_id": "parent-or-self-session-id",
  "usage_kind": "owner",
  "is_subagent": false,
  "turn_id": "turn-id",
  "cwd": "$PROJECT_ROOT",
  "raw_cwd": "$PROJECT_ROOT",
  "owner_cwd": "$PROJECT_ROOT",
  "model": "gpt-5",
  "delta": {
    "input_tokens": 0,
    "cached_input_tokens": 0,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
    "total_tokens": 0
  },
  "total": {
    "input_tokens": 0,
    "cached_input_tokens": 0,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
    "total_tokens": 0
  },
  "transcript_path": "/path/to/transcript.jsonl"
}
```

## Subagent Attribution

Subagent sessions should remain visible as their own `raw_session_id` because that is the most accurate audit trail. For user-facing daily reports, aggregate by `owner_session_id` and display by `owner_cwd`:

- Normal agent: `owner_session_id == raw_session_id`.
- Subagent: `raw_session_id == session_meta.id` or `agent_id`; `owner_session_id == parent_thread_id` when transcript metadata exposes it.

If the parent id is missing, keep `owner_session_id == accounting_session_id` and mark `is_subagent` from whatever transcript source metadata is available.

Permission-approval guardian sessions are Codex internal subagents with metadata like `source.subagent.other == "guardian"`. They are real model usage and should be recorded, but they are not business/review/co-agent work. Store them with:

- `usage_category: permission_approval`
- `usage_kind: approval`
- `approval_session_id: <guardian raw session id>`
- `owner_cwd: <owner/main cwd>`

Default agent/session summaries must fold approval overhead into the owner cwd/session. Keep `usage_category` and `approval_session_id` only for audit and explicit category diagnostics.

## Edge Cases

- Duplicate Stop events: skip ledger append when cumulative total did not increase.
- State write failure after ledger append: roll back the just-appended ledger row before returning an error to the hook wrapper.
- Out-of-order cumulative totals: skip older/equal token-count markers.
- Reset cumulative totals: use `last_token_usage` only when the token-count marker is newer than state.
- Missing transcript or missing token_count: append no usage row; update a diagnostic log only.
- Context compaction: continue using the transcript supplied by the Stop event. If a new session id appears, initialize new state.
- Backfill: `scripts/usage_hook.py backfill --date ...` scans transcripts and writes date-window deltas only when the target date has no existing ledger row for that accounting session. If hook or backfill already recorded that session/day, backfill skips it to avoid inflating totals.
- Forked subagent history: copied parent `token_count` events before the child's first real UUIDv7 turn are a baseline, not new child usage. A child without copied token events remains zero-based.

## Test Hook

`scripts/tests/test_stop_hook.py` is a probe, not the production accountant. It records:

- Stop hook payload keys and core fields.
- Trigger transcript path readability.
- Accounting transcript path readability.
- First `session_meta` from the accounting transcript.
- Latest `token_count` from the accounting transcript.
- Whether the transcript appears to be a subagent and its parent thread id.

The test log is:

```text
$CODEX_USAGE_TEST_ROOT/stop-hook-events.jsonl
```

For production, install or update the global hooks with:

```bash
python3 scripts/hook_setup.py
```

The setup script uses `$CODEX_HOME` when set and otherwise falls back to `~/.codex`. Pass
`--codex-home` or `--hooks-file` for an explicit target.

The resulting `Stop` entry is:

```json
"Stop": [
  {
    "hooks": [
      {
        "type": "command",
        "command": "python3 $CODEX_HOME/skills/usage-mgr/scripts/usage_hook.py",
        "timeout": 30,
        "async": false,
        "statusMessage": "Recording Codex usage"
      }
    ]
  }
]
```

Register the same command under `SubagentStop` as well. `scripts/tests/test_stop_hook.py` remains only as a probe for diagnosing hook payload shape.
