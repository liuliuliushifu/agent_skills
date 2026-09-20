# Co-Agent Design

## Objective

Provide the `co-agent` skill: a script-driven handoff workflow for sequential Codex agents working in different directories. When a user says "把结果给 xxx agent", the current agent uses `scripts/coagent.sh` to create a strict handoff record and wake an explicitly selected target session with `codex exec resume`. The skill also provides conservative responsibility-based dispatch: when a registered agent receives work outside its ownership, it can ask the script to route the task to the best owner before creating a handoff.

The design uses human-friendly agent names and chat names, but keeps workflow state machine data script-owned and parseable.

## Non-Goals

- Do not implement free-form agent-to-agent chat.
- Do not let LLMs edit workflow ledgers directly.
- Do not silently infer a wake target when the caller omits session selection.
- Do not solve same-worktree concurrent editing in v1.
- Do not impose hard recursion limits in v1.

## Terms

- `agent`: A durable role identity, such as `build-agent` or `deploy-agent`.
- `goal`: A long-running human objective. One goal may contain many runs.
- `run`: One bounded co-agent collaboration round under a goal.
- `segment`: One concrete wake-up action from `from_agent` to `to_agent`.
- `attempt`: One execution try for a segment.
- `handoff`: Strict input artifact passed to the target agent.
- `result`: Strict output artifact written by the target agent.
- `top-from agent`: The immutable `created_by_agent` of the earliest run in a goal. It owns process cleanup for the whole goal.

## Source of Truth

`segments.jsonl` is the authoritative source of workflow state. All state changes are appended there by `coagent.sh` under a per-run lock.

Generated view files:

- `status-SEGNNN.json`: current status view for humans and monitor scripts.
- `result-SEGNNN-ATTNNN.md`: target agent result for one attempt.
- Goal-level `process-monitor.json`: current process-lifecycle view for every attempt in the goal.

Agents must not edit registry, goal, run, segment, or status files directly. They must use `coagent.sh`.

`run_id` is the primary grouping key. It appears in the run directory name, `run.json`, every `segments.jsonl` event, generated handoff files, generated prompts, and result filenames. A generated `run.md` is not part of v1 unless human-readable reporting becomes necessary later.

Agent name matching is case-insensitive and collapses repeated whitespace. Human-friendly names
such as `backend agent` are valid; scripts preserve the display name but resolve by normalized
name.

## Audit Requirements

The workflow must be explainable after a goal completes. For a question such as "我们都启动了哪些agent, 做了哪些配合", the script must be able to answer from stored state without LLM guessing.

Required audit data:

- `goal.json`: goal owner, title, lifecycle status, and created time.
- `run.json`: run id, previous run id, creator, reason, and lifecycle status.
- `segments.jsonl`: every handoff, retry, wake result, finish, return, cancel, and recovery event.
- Agent snapshots inside `segment_started`: `from_agent` and `to_agent` cwd, role, responsibilities, and aliases at the time the segment was created.
- Attempt records inside `attempt_started` and `wake_finished`: prompt file, result file, wake method, target cwd, requested chat name, resolved thread id if available, command exit code, and timestamps.
- Result summaries from `coagent.sh finish` or `coagent.sh return`.

The audit scope is co-agent orchestration. It records which agents were awakened, why, how they were connected, and what each segment reported. It does not automatically record every shell command executed inside an agent unless that agent includes the command list in its result.

The script should provide a query command for this instead of asking LLMs to inspect raw JSONL manually.

## Storage Layout

```text
$CODEX_HOME/coagents/
  agents.json
  goals/
    GOAL-YYYYMMDD-NNN/
      goal.json
      process-monitor.json
      .lock
      runs/
        RUN-YYYYMMDD-NNN/
          run.json
          segments.jsonl
          .lock
          handoff-SEGNNN.md
          prompt-SEGNNN-ATTNNN.md
          result-SEGNNN-ATTNNN.md
          status-SEGNNN.json
          wake-SEGNNN-ATTNNN.json
```

All generated JSON writes use temp-file-plus-rename. `segments.jsonl` appends and segment-id allocation happen while holding the run lock. Goal-level process monitoring uses the goal lock. Existing per-run `run.json` content and location are unchanged. The per-attempt wake receipt is a crash-recovery record: it is created as `wake_registration_pending` before launch and atomically updated with process identity immediately after launch; the ledger remains authoritative once `wake_finished` exists.

## Agent Registry

`agents.json` is the thin durable registry layer. It maps stable agent names to their cwd, aliases, and owned work. It does not store session-level titles or thread ids.

```json
{
  "version": 1,
  "agents": {
    "build-agent": {
      "cwd": "/path/A",
      "aliases": ["build"],
      "role": "Build, compile diagnostics, and artifact discovery.",
      "responsibilities": {
        "owns": ["Builds and compile diagnostics"],
        "not_for": ["production deployment ownership"]
      }
    },
    "deploy-agent": {
      "cwd": "/path/B",
      "role": "Deploy artifacts and verify production runtime state.",
      "aliases": ["deploy"]
    }
  }
}
```

Required fields:

- `cwd`: target directory for `codex exec --cd`.
- `role`: short role shown in prompts.

Optional fields:

- `aliases`: explicit short names for users, matched case-insensitively after whitespace normalization.
- `responsibilities`: structured routing hints with `owns` and `not_for` arrays. `handoff_when` is accepted for compatibility, but routing should normally be derived from `owns` across all registered agents.

## Chat Candidate Registry

`chat_candidates.json` is the shared session-level registry maintained by `chat_manager` and read by `co-agent`.

```json
{
  "version": 1,
  "updated_at": "2026-07-22T12:00:00+08:00",
  "cwd_map": {
    "/path/A": [
      {
        "thread_id": "019e...",
        "chat_name": "build mgr",
        "cwd": "/path/A",
        "renamed_at": "2026-07-22T12:00:00+08:00",
        "source": "chat_manager.rename-current"
      }
    ]
  }
}
```

Rules:

- `chat_manager rename` and `rename-current` upsert candidate records.
- `chat_manager archive`, `delete`, `archive-cwd`, `archive-old`, and `hide-resume` remove affected candidate records when they apply.
- `co-agent` never writes `chat_candidates.json`; it reads candidates by cwd, validates thread ids through `chat_manager`, filters archived or cwd-mismatched sessions, and sorts by real session `updated_at` descending.

Wake selection is explicit and mutually exclusive:

1. `--session <thread_id>` resumes a currently validated candidate belonging to the target agent cwd.
2. `--last` deliberately resumes the latest session under the target cwd.

Exactly one option is required. Omitting both fails before workflow state is written and prints the validated candidates. An unknown, archived, stale, or cwd-mismatched `--session` also fails; it never falls back to `--last`.

Every wake event records the resolved method, target cwd, resolved thread id if any, and command exit status.

## Required Chat Manager Interface

`chat_manager` should provide a stable id resolve API so co-agent scripts can validate candidate records without reading sqlite directly:

```bash
chat_manager.sh resolve --id <thread_id> --json
```

Expected JSON:

```json
{
  "thread_id": "019e...",
  "title": "deploy mgr",
  "cwd": "/path/B",
  "rollout_path": "...",
  "archived": false
}
```

If the id is missing, archived, or belongs to another cwd, `co-agent chat-candidates` skips it or reports it as stale.

## Goal Metadata

`goal.json` is created by script:

```json
{
  "version": 1,
  "goal_id": "GOAL-20260616-001",
  "title": "Improve deploy workflow",
  "owner_agent": "skill-agent",
  "created_at": "2026-06-16T10:00:00+08:00",
  "status": "active"
}
```

Valid goal statuses:

- `active`
- `finished`
- `blocked`
- `abandoned`

Goal status aggregation:

- `active`: any run is active.
- `blocked`: no run is active and at least one latest run is blocked.
- `finished`: all relevant runs are finished or abandoned.
- `abandoned`: set only by explicit command.

## Run Metadata

`run.json` is created by script:

```json
{
  "version": 1,
  "goal_id": "GOAL-20260616-001",
  "run_id": "RUN-20260616-001",
  "previous_run_id": "RUN-20260616-000",
  "created_by_agent": "build-agent",
  "created_at": "2026-06-16T10:30:00+08:00",
  "status": "active",
  "reason": "Second test round after code changes."
}
```

Valid run statuses:

- `active`
- `finished`
- `blocked`
- `failed`
- `cancelled`

Run status aggregation:

- `active`: any segment's latest attempt is active or woke successfully but is not terminal.
- `failed`: any latest required segment is failed and not superseded, returned, or retried.
- `blocked`: no active segments and at least one latest required segment is blocked.
- `finished`: all required branches are terminal and acknowledged.
- `cancelled`: set only by explicit command.

## Boundary Rules

Use the same `goal` when the human objective remains the same.

Start a new `run` when the main agent has consumed prior results and changed material state before asking for more agent work. Examples:

- First test delegation finds failures, main agent changes code, then delegates testing again: new run.
- Build agent delegates deploy, deploy delegates test, test returns deploy result: same run.
- Multiple agents analyze the same code state in parallel: same run.
- A target agent retries after a transient failure using the same handoff: same run, new attempt.

Script support:

- `coagent.sh start-run` or `coagent.sh handoff --new-run` generates a `run_id`.
- `coagent.sh handoff --run <run_id>` appends to that explicit run.
- If neither `--run` nor `--new-run` is provided and exactly one active run exists, append to it.
- If neither is provided and no active run exists, create a new run automatically.
- If multiple active runs exist, fail and ask the caller to choose.

Optional state snapshot:

- A state snapshot is not required for v1 correctness.
- If added later, it should be diagnostic only: record cwd, git head/status hash, and artifact ids so the script can warn when a caller appears to reuse an old run after material state changed.
- The snapshot must not replace explicit `run_id` ownership, and should not be a hard block by default.

## Segment and Attempt Ledger

`segments.jsonl` is append-only. Each line is one event object. Scripts are the only writer.

Segment start event:

```json
{
  "event": "segment_started",
  "segment_id": 1,
  "status": "active",
  "direction": "forward",
  "from_agent": "build-agent",
  "to_agent": "deploy-agent",
  "from_agent_snapshot": {
    "cwd": "/path/A",
    "role": "Build, compile diagnostics, and artifact discovery.",
    "responsibilities": {
      "owns": ["Builds and compile diagnostics"],
      "not_for": ["production deployment ownership"]
    }
  },
  "to_agent_snapshot": {
    "cwd": "/path/B",
    "role": "Deploy artifacts and verify production runtime state."
  },
  "parent_segment_id": null,
  "handoff_file": "handoff-SEG001.md",
  "status_file": "status-SEG001.json",
  "created_at": "2026-06-16T10:35:00+08:00",
  "reason": "Deploy the artifact and report runtime state."
}
```

Attempt start event:

```json
{
  "event": "attempt_started",
  "segment_id": 1,
  "attempt": 1,
  "status": "active",
  "prompt_file": "prompt-SEG001-ATT001.md",
  "result_file": "result-SEG001-ATT001.md",
  "wake_receipt_file": "wake-SEG001-ATT001.json",
  "wake_method": "last",
  "target_cwd": "/path/B",
  "requested_chat_name": "",
  "resolved_thread_id": "",
  "created_at": "2026-06-16T10:35:01+08:00",
  "idempotency_key": "sha256:..."
}
```

Wake result event:

```json
{
  "event": "wake_finished",
  "segment_id": 1,
  "attempt": 1,
  "status": "active",
  "exit_code": 0,
  "updated_at": "2026-06-16T10:35:05+08:00"
}
```

Attempt finish event:

```json
{
  "event": "attempt_finished",
  "segment_id": 1,
  "attempt": 1,
  "status": "finished",
  "result_file": "result-SEG001-ATT001.md",
  "updated_at": "2026-06-16T10:55:00+08:00"
}
```

Segment finish event:

```json
{
  "event": "segment_finished",
  "segment_id": 1,
  "status": "finished",
  "updated_at": "2026-06-16T10:55:01+08:00"
}
```

Valid segment statuses:

- `active`: target agent is expected to work.
- `finished`: target agent completed and no upstream continuation is needed.
- `returned`: target agent returned results to an upstream agent.
- `blocked`: target agent needs human input or unavailable external state.
- `failed`: target agent failed unexpectedly.
- `cancelled`: segment was explicitly cancelled.
- `superseded`: segment was replaced by a later segment.

Valid attempt statuses:

- `active`
- `finished`
- `blocked`
- `failed`
- `timed_out`
- `wake_failed`
- `wake_skipped`
- `cancelled`

Valid directions:

- `forward`: normal delegation.
- `return`: explicit return to an upstream agent.

Starting a later attempt reopens the same segment as `active`. Generated current-state views clear the prior attempt's summary and result reference; historical attempt events and result files remain unchanged.

## Return Semantics

Return is an atomic script operation:

1. The current segment's latest attempt is finished.
2. The current segment is marked `returned`.
3. A new `direction=return` segment is created.
4. The return target defaults to the immediate parent segment's `from_agent`.
5. The return segment is awakened and must later be acknowledged with `finish`.

Default return target should be the immediate upstream agent. Explicit `--to <agent>` can target another upstream agent only when that agent appears in the current run ancestry. Otherwise the script fails.

Return never edits free-form ledger text. It appends events.

## Status View

`status-SEGNNN.json` is a generated view of the authoritative ledger plus the latest result summary.

```json
{
  "version": 1,
  "segment_id": 1,
  "agent": "deploy-agent",
  "state": "finished",
  "latest_attempt": 1,
  "wake_status": "wake_detached",
  "wake_exit_code": null,
  "wake_pid": 12345,
  "wake_tracking": "detached",
  "wake_note": "codex wake was started in background; wake_exit_code is intentionally not tracked.",
  "summary": "Deployment completed and syncd/swss are running.",
  "result_file": "result-SEG001-ATT001.md",
  "result_excerpt": "Deployment completed...",
  "changed_files": [],
  "verification": [
    "deploy_result.json passed"
  ],
  "needs_followup": false,
  "return_to": "",
  "return_handoff_file": ""
}
```

The script updates `status-SEGNNN.json` atomically whenever it appends terminal events. If ledger and status disagree, `coagent.sh reconcile` regenerates status views from `segments.jsonl` and result metadata.

## Process Lifecycle Monitor

Business-result propagation remains hierarchical: each from-agent waits for downstream results only when it needs them to produce its own result. OS-process cleanup is centralized under the top-from agent.

Every wake records process identity in the run ledger and refreshes the goal-level `process-monitor.json`. The unique process-attempt key is:

```text
<run_id>:SEG<segment_id>:ATT<attempt>
```

The monitor view contains the immutable top-from agent and one record for every attempt across every run in the goal. Each record includes the run/segment/attempt key, from/to agents, business state, terminal time, PID, process group, session id, Linux process start ticks, uid, observed command-line hash, requested command hash, last inspection, grace deadline state, and cleanup result.

One-shot monitor command:

```bash
coagent.sh process-monitor --goal <goal_id> --from <top-from-agent>
```

Rules:

1. Only the top-from agent may call the command. The script derives it from the earliest run's `created_by_agent` and persists it in `process-monitor.json`.
2. Each invocation rebuilds the attempt list from all run ledgers. It never relies only on a segment's latest attempt.
3. An attempt becomes business-terminal at its own `attempt_finished`, at a later attempt start, or at a terminal segment event that occurs after that attempt starts. Earlier terminal segment events never terminate a retry attempt. The 60-second natural-exit grace period starts from the selected event time.
4. Derive the monitoring phase from all recorded attempts: any attempt without a business-terminal event means `BUSINESS_ACTIVE`; when all attempts are business-terminal but some processes remain, enter `FINAL_MONITORING`; when no processes remain, enter `ALL_CLEARED`.
5. Never clean a process during `BUSINESS_ACTIVE`, including processes belonging to targets that returned earlier. Process cleanup begins only in `FINAL_MONITORING`.
6. Before signalling, match the recorded PID, process group, process start ticks, uid, and observed command-line hash. A missing or mismatched identity remains `WAITING` and is never killed automatically.
7. Signal the verified process group so descendants are included. Send TERM to all due groups, wait up to five seconds once, then KILL only the groups still alive.
8. Treat an attempt without a completed wake registration as `WAKE_REGISTRATION_PENDING`; it is never cleared. If `wake_finished` is absent but the attempt receipt contains completed process identity, recover monitoring from the receipt.
9. Once a process is reliably observed exited or successfully cleaned, keep that attempt cleared without reinspecting its historical PID.
10. Write the monitor view atomically after every scan. Repeated scans replace state instead of adding periodic noise to `segments.jsonl`.
11. Print `ALL CLEARED` only when every recorded attempt is skipped, foreground-complete, naturally exited, or successfully cleaned. Otherwise print `WAITING` and advise a 30-second next check.

The LLM does not implement process inspection itself. Existing hierarchical result handling ensures that all descendants have returned before the top-from agent enters final monitoring. While `process-monitor` prints `WAITING`, the top-from agent does not advance task reasoning or create another handoff; it only waits 30 seconds and invokes the command again. No separate handoff lock or closing flag is required.

## Handoff Template

The script renders `handoff-SEGNNN.md` from strict fields. LLMs provide content only for bounded fields such as `context`, `requested_outcome`, `constraints`, and `artifacts`.

```markdown
# Co-Agent Handoff

run_id: RUN-20260616-001
segment_id: 1
direction: forward
from_agent: build-agent
to_agent: deploy-agent
parent_segment_id:
created_at: 2026-06-16T10:35:00+08:00

## From Agent Role
Build, compile diagnostics, and artifact discovery.

## From Agent Responsibilities
- owns: Builds and compile diagnostics
- not_for: production deployment ownership

## To Agent Role
Deploy artifacts and verify production runtime state.

## To Agent Responsibilities
- owns: production deployment and runtime validation
- not_for: Build system ownership

## Context
<bounded free text, max length enforced by script>

## Requested Outcome
<bounded free text, must be imperative and testable>

## Artifacts
- <path or identifier>

## Constraints
- <constraint>

## Return Rules
- If the task is complete, use `coagent.sh finish`.
- If upstream work is needed, use `coagent.sh return`.
- If another agent is needed before return, use `coagent.sh handoff --run <run_id>` with yourself as `from_agent`.
```

## Prompt Template

The script renders `prompt-SEGNNN-ATTNNN.md`; the command line prompt should only tell Codex to read this file.

```markdown
You are the target Codex agent for a co-agent handoff.

Stable identity:
- agent: {{to_agent}}
- role: {{to_role}}
- cwd: {{to_cwd}}
- responsibilities: {{to_responsibilities}}

Current segment:
- goal_id: {{goal_id}}
- run_id: {{run_id}}
- segment_id: {{segment_id}}
- attempt: {{attempt}}
- direction: {{direction}}
- from_agent: {{from_agent}}
- to_agent: {{to_agent}}
- parent_segment_id: {{parent_segment_id}}

Required files:
- handoff: {{handoff_file}}
- result: {{result_file}}
- status view: {{status_file}}
- ledger: {{segments_file}}

Instructions:
1. Read the handoff file first.
2. Treat your stable identity as {{to_agent}}. Do not inherit the identity of {{from_agent}}.
3. Continue the work in the current directory only unless the handoff says otherwise.
4. Do not edit workflow ledger or status files manually. Use `coagent.sh` commands for status, return, retry, or further handoff.
5. If you delegate to a third agent, you become `from_agent` for the new segment.
6. When done, write the result with `coagent.sh finish` or return with `coagent.sh return`.
```

Wake command when the caller explicitly chooses `--last`:

```bash
codex exec --cd "$to_cwd" resume --last "Read $prompt_file and execute the co-agent handoff."
```

Wake command when the caller chooses a validated session id:

```bash
codex exec --cd "$to_cwd" resume "$session_id" "Read $prompt_file and execute the co-agent handoff."
```

## Script API

All state writes must go through `coagent.sh`.

### Register Agent

```bash
coagent.sh register <agent> --cwd <dir> [--role <text>] \
  [--alias <short-name>] [--owns <text>] [--not-for <text>] [--update]
```

Validation:

- `agent` must be non-empty after trimming whitespace.
- Agent names may contain spaces for human readability.
- Agent and alias resolution is case-insensitive and collapses repeated whitespace.
- Short-name matching uses explicit aliases only; do not use substring guessing.
- `cwd` must exist.
- `role` must be non-empty.
- `owns` and `not_for` are structured routing hints; repeat the flag for multiple values. A task outside the current agent's `owns` should be routed by inspecting other agents' `owns`.
- Duplicate normalized agent names are rejected unless `--update`.
- Duplicate normalized aliases across agents are ambiguous and should be avoided.

### Resolve Agent

```bash
coagent.sh resolve <agent> [--json]
```

Returns cwd, role, aliases, and responsibilities.

### Chat Candidates

```bash
coagent.sh chat-candidates [--agent <agent>|--cwd <dir>] [--limit N] [--json]
```

Returns chat_manager-renamed sessions for the target cwd, validated by thread id and sorted by session `updated_at` descending.

### Whoami

```bash
coagent.sh whoami [--from <agent>] [--json]
```

Resolution order:

1. Explicit `--from`.
2. Validate `--from` against `COAGENT_NAME` when both are present.
3. Current cwd unique match in `agents.json`.
4. Current chat name unique match through `chat_manager resolve`.
5. Failure with candidate list.

This identity is stable. `from_agent` and `to_agent` are per-segment relations.

### Route Task

```bash
coagent.sh route [--from <agent>] --task <original-user-request> [--json]
```

Behavior:

- Resolves the current agent from `--from`, `COAGENT_NAME`, or cwd when possible.
- Scores registered agents using explicit aliases/names, `responsibilities.owns`, `responsibilities.not_for`, `handoff_when`, and role text.
- Returns `action=handoff` with `recommended_agent` only when another owner is a clear match.
- Returns `action=handle_here` when the current agent is the best owner.
- Returns `action=ask` when ownership is weak or ambiguous.
- Does not create goals, runs, segments, handoff files, or wake-ups. It is a read-only dispatch helper used before `handoff`.

### Start Goal

```bash
coagent.sh start-goal --title <title> --owner <agent>
```

Creates `GOAL-YYYYMMDD-NNN`.

### Start Run

```bash
coagent.sh start-run --goal <goal_id> --from <agent> --reason <text> [--previous-run <run_id>]
```

Creates run metadata only. It does not create a segment. The canonical way to create a segment is `coagent.sh handoff`.

### Handoff

```bash
coagent.sh handoff --goal <goal_id> --to <agent> --from <agent> \
  --context <file> --requested-outcome <text> \
  (--session <thread_id> | --last) [--run <run_id>] [--new-run] [--foreground]
```

Behavior:

- With `--run`, append a segment to the existing run.
- With `--new-run`, create a new run under the goal and append its first segment.
- If neither is provided, append to the only active run or create a new run when none exists.
- If multiple active runs exist, fail closed and require `--run` or `--new-run`.
- Require exactly one of `--session` or `--last` before creating a run or writing segment state.
- Validate `--session` against `chat-candidates` for the resolved target agent; print candidates and fail on missing or invalid selection.
- Script renders handoff and prompt files.
- Script appends segment and attempt events.
- Script wakes target agent in the background by default and immediately prints run id, segment id, handoff file, prompt file, result file, and status file.
- With `--foreground`, script waits for `codex exec` itself to exit. This does not mean the remote agent segment is finished.
- With `--no-wake`, script writes the same evidence but records `wake_skipped` instead of starting Codex; use this only for local skill tests.

### Retry

```bash
coagent.sh retry --run <run_id> --segment <id> \
  (--session <thread_id> | --last) [--reason <text>] [--foreground]
```

Creates a new attempt for the same segment. It never overwrites old prompt or result files.

### Finish Segment

```bash
coagent.sh finish --run <run_id> --segment <id> \
  --state finished|blocked|failed --summary <text> [--result <file>]
```

`finish` cannot set `returned`; use `coagent.sh return`.

### Return

```bash
coagent.sh return --run <run_id> --segment <id> --summary <text> \
  (--session <thread_id> | --last) [--result <file>] [--to <upstream-agent>]
```

Atomically marks the current segment returned and creates a new return segment.

### Recovery

```bash
coagent.sh rewake --run <run_id> --segment <id> [--attempt <n>]
coagent.sh cancel --run <run_id> --segment <id> --reason <text>
coagent.sh supersede --run <run_id> --segment <id> --by-segment <id> --reason <text>
coagent.sh unblock --run <run_id> --segment <id> --reason <text> \
  (--session <thread_id> | --last)
coagent.sh reconcile --run <run_id>
coagent.sh process-monitor --goal <goal_id> --from <top-from-agent>
```

Recovery behavior:

- `rewake`: reruns the wake command for an active or wake-failed attempt.
- `cancel`: terminally cancels a segment.
- `supersede`: marks one segment replaced by another.
- `unblock`: converts a blocked segment into a retryable active state by starting a new attempt.
- `reconcile`: regenerates status views from the ledger.

### Monitor

```bash
coagent.sh status <goal_id|run_id> [--json]
coagent.sh result <run_id> --segment <id> [--json] [--excerpt <chars>]
coagent.sh wait --run <run_id> [--segment <id>] [--timeout 3600]
coagent.sh monitor <run_id> [--interval 10] [--timeout 3600]
```

`result` prints the latest result file path, summary, and excerpt. `wait` waits for a run or segment to leave `active` without mutating state, polling every 300 seconds by default.

`monitor` marks stale active attempts as `timed_out` only when `--mark-timeout` is provided. Otherwise it reports timeout without mutating state.

### History and Audit

```bash
coagent.sh history <goal_id|run_id> [--json] [--timeline] [--tree]
```

Behavior:

- Reads `goal.json`, `run.json`, and all relevant `segments.jsonl` files.
- Lists unique agents that participated as owner, creator, `from_agent`, or `to_agent`.
- Lists every segment with direction, parent segment, from/to agents, reason, handoff file, latest status, result summary, and timestamps.
- Lists every attempt with wake method, target cwd, requested chat name, resolved thread id, wake exit code, result file, and final attempt state.
- Shows return chains and downstream delegation through `parent_segment_id`.
- Uses agent snapshots from segment events for historical cwd/role/responsibilities/chat values; current `agents.json` is used only to annotate if an agent has changed since the run.
- Reports incomplete data explicitly, such as active segments without finish events or wake attempts without `wake_finished`.

## LLM Responsibilities

LLM may:

- Decide whether the user asked for co-agent handoff.
- Use `coagent.sh route` to choose a target agent when the current registered agent receives work that may be outside its responsibilities.
- Choose target agent by explicit user name or by inspecting registered `owns`/`not_for` responsibilities when `route` is insufficient.
- Inspect the resolved target agent's validated chat candidates and explicitly choose `--session` or `--last` for every wake-producing command.
- Provide bounded handoff content.
- Decide `--new-run` vs existing run using boundary rules.
- Use `coagent.sh history` or `coagent.sh status` to answer audit questions about completed goals.
- Summarize final script results to the user.
- As the top-from agent, enter final monitoring after every started target has returned through the hierarchy; poll `process-monitor` every 30 seconds and do no further task work or handoff until `ALL CLEARED`.

LLM must not:

- Edit registry, goal, run, segment, status, prompt, or handoff files directly.
- Guess session ids from sqlite.
- Omit wake-target selection or rely on an implicit `--last` fallback.
- Answer audit questions by manually inferring from partial files when `coagent.sh history` is available.
- Manually construct `codex exec` commands when `coagent.sh handoff` can do it.
- Treat every current agent as `from_agent` without resolving identity.
- Ask intermediate agents to own or perform descendant process cleanup.
- Hand off solely on a weak or ambiguous route result; ask the user when `route` returns `action=ask`.

## New Run Decision

Use a new run when:

- The main agent has changed code/config/data since prior run.
- The main agent has consumed a prior result and is asking for a fresh verification round.
- The target agent set is materially different because the task state changed.
- A human explicitly says "new round", "再跑一轮", or equivalent.

Continue the same run when:

- Delegating downstream before the current segment returns.
- Returning results upstream.
- Running sibling agents for the same state and same collaboration objective.
- Retrying the same segment after transient failure.

If multiple active runs make the target run ambiguous, `coagent.sh handoff` fails closed and asks the caller to specify `--new-run` or `--run`. If no active run exists, it creates a new run automatically.

## Review and Safety Constraints

- Different directories are assumed in v1.
- If source and target cwd are identical, warn but do not block.
- Worktree support is reserved for v2.
- No hard recursion limit in v1; record the chain for audit.
- The script warns when the same `(from_agent, to_agent)` pair repeats more than once in an active run.
- Every wake-up must record `reason`.
- Every final state must include `summary`.
- Wake failures must create `wake_finished` with `status=wake_failed` and nonzero exit code.
- Every wake-producing command must explicitly select one validated `--session` or `--last` before state mutation.
- Local no-wake tests must create `wake_finished` with `status=wake_skipped` and exit code 0.
- Parallel sibling segments are allowed only through the script lock and idempotency key path.

## Implementation Order

1. Add `chat_manager resolve`.
2. Implement `coagent.sh register`, `resolve`, and `whoami`.
3. Implement goal/run/segment ledger with lock, strict JSON schemas, generated status views, and historical agent snapshots.
4. Implement handoff/prompt rendering.
5. Implement wake-up by explicitly selected session id or `--last`.
6. Implement finish, return, retry, and recovery commands.
7. Implement `status`, `history`, `result`, `wait`, and `monitor`.
8. Require explicit session-id or `--last` targeting and validate candidate session ids.
9. Add optional diagnostic state snapshot only if stale-run mistakes happen in real use.

## Open Decisions

- Whether `coagent.sh handoff` should accept inline context, file context only, or both.
- Whether return segments should always target the immediate parent `from_agent` or allow any upstream agent by default. Current recommendation: default to immediate parent, allow explicit upstream only if it appears in ancestry.
