---
name: co-agent
description: Manage script-driven handoffs and responsibility-based dispatch between Codex agents across directories. Use when the user asks to pass results to another agent, wake another named agent, coordinate multi-agent work, return work upstream, inspect which agents participated in a goal, audit co-agent handoff history, or when the current registered agent is asked to do work outside its responsibilities and another registered agent appears to own that work.
---

# Co-Agent

Use the bundled script. Do not edit co-agent registry, goal, run, segment, prompt, handoff, result, or status files directly.

## LLM Role

- Resolve the current agent with `whoami --from <agent>` when the user gives an identity; otherwise use `whoami` only if the cwd is registered.
- Register or update named agents only when the user provides or confirms their cwd.
- Before substantive work in a registered cwd, if the request may be outside the current agent's responsibilities, run `route --task <original request> --json`.
- If `route` returns `action=handoff`, create a handoff to `recommended_agent`; if it returns `action=handle_here`, continue locally; if it returns `action=ask`, ask the user instead of guessing.
- When the target agent is not explicit and `route` is insufficient, inspect `list-agents --json` responsibilities before choosing who should own the work.
- Before `handoff`, `return`, `retry`, or `unblock`, run `chat-candidates` for the resolved target agent. Pass `--session <thread_id>` when one candidate clearly matches the work; otherwise explicitly pass `--last`.
- Start a goal or create a handoff through the script; let the script create `run_id`, `segment_id`, handoff files, prompt files, and JSONL evidence.
- After handoff, if this agent needs the target business result before continuing, run one `wait --timeout <realistic seconds>`; the script polls every 5 minutes by default, so do not use a 60-second process-exit timeout here.
- Do not manually poll `status`/`result` in a loop. If `wait` times out, inspect `status` once and decide whether to extend the wait or ask the user.
- The first `from_agent` in a goal is its top-from agent. After every started target in the current round has returned through the existing hierarchical result flow, the top-from agent enters final monitoring: call `process-monitor` every 30 seconds until it prints `ALL CLEARED`. The script gives each business-terminal process 60 seconds to exit naturally before cleanup. Do not continue task work or create another handoff while it prints `WAITING`. Intermediate agents do not perform this polling.
- Use `wait`, `result`, `history`, or `status` to answer completion and audit questions.
- Summarize script output to the user.

## Script Entry

```bash
COAGENT="${CODEX_HOME:-$HOME/.codex}/skills/co-agent/scripts/coagent.sh"
"$COAGENT" register "agent name" --cwd <dir> [--role <text>] [--alias "short name"] [--owns <text>] [--not-for <text>] [--update]
"$COAGENT" rename-agent "old agent name" "new agent name" [--alias "short name"]
"$COAGENT" list-agents [--json]
"$COAGENT" resolve "agent name" [--json]
"$COAGENT" resolve-cwd <cwd> [--json]
"$COAGENT" chat-candidates [--agent "agent name"|--cwd <dir>] [--limit N] [--json]
"$COAGENT" whoami [--from "agent name"] [--json]
"$COAGENT" route [--from "agent name"] --task <original request> [--json]
"$COAGENT" start-goal --title <title> --owner "agent name"
"$COAGENT" handoff --goal <goal_id> --from "agent A" --to "agent B" --context <file> --requested-outcome <text> (--session <thread_id>|--last) [--run <run_id>|--new-run] [--foreground] [--no-wake]
"$COAGENT" finish --run <run_id> --segment <n> --state finished|blocked|failed --summary <text> [--result <file>]
"$COAGENT" return --run <run_id> --segment <n> --summary <text> (--session <thread_id>|--last) [--to "upstream agent"]
"$COAGENT" retry --run <run_id> --segment <n> (--session <thread_id>|--last) [--reason <text>]
"$COAGENT" unblock --run <run_id> --segment <n> (--session <thread_id>|--last) [--reason <text>]
"$COAGENT" process-monitor --goal <goal_id> --from "top-from agent"
"$COAGENT" wait --run <run_id> [--segment <n>] [--timeout <seconds>]
"$COAGENT" result <run_id> --segment <n> [--json]
"$COAGENT" status <goal_id|run_id> [--json]
"$COAGENT" history <goal_id|run_id> [--json] [--tree]
```

## Behavior

- Agent name matching is case-insensitive and ignores repeated whitespace.
- `rename-agent` migrates the registry key and display name while preserving cwd, role, and responsibilities. If `--alias` is supplied, it replaces the old alias list.
- `resolve-cwd` returns the registered agent with the longest cwd prefix match; callers should fall back to the original cwd when there is no match.
- Short names are explicit aliases. Prefer role-based aliases such as `backend agent`,
  `documentation agent`, or `test agent`; do not rely on substring guessing.
- Agent responsibilities are routed by `owns` and `not_for`: if a task is outside the current agent's `owns`, inspect other agents and hand off to the owner. Handoff prompts and history use the snapshot recorded when the segment was created.
- `route` is a conservative dispatch helper over registered aliases, `owns`, `not_for`, and role text. Treat `action=handoff` as permission to hand off; treat `action=ask` as ambiguous ownership.
- `agents.json` records agent-level identity only: name, aliases, cwd, role, and responsibilities. Session-level title/thread data lives in `${CODEX_HOME}/coagents/chat_candidates.json`.
- `chat-candidates` reads chat_manager-maintained candidates for an agent cwd, validates thread ids against chat_manager, filters archived/mismatched sessions, and sorts by session `updated_at` descending.
- Every `handoff`, `return`, `retry`, and `unblock` must explicitly pass exactly one of `--session <thread_id>` or `--last`, including `--no-wake` tests.
- `--session` accepts only a currently validated candidate for the target agent. Missing selection or an invalid session fails before workflow state is written and prints the valid candidates; there is no implicit fallback to `--last`.
- If `handoff` has no active run and no `--run` or `--new-run`, it creates a run automatically.
- `handoff` wakes the explicitly selected target session in the background, prints run/segment/result/status paths, and returns. Use `--foreground` only when the caller explicitly needs to wait for `codex exec` itself to exit.
- Use `wait --run ... --segment ...` to wait for segment completion, and `result <run_id> --segment <n>` to read the latest result directly.
- `status --json` includes the latest summary, result path, result excerpt, and wake notes.
- `--no-wake` is for local skill testing: it writes the same evidence files but records `wake_skipped` instead of starting Codex.
- `segments.jsonl` is the authoritative evidence ledger. `status-SEGNNN.json` is a generated view.
- State is written under `${CODEX_HOME:-<skill-root-parent>}/coagents` unless `COAGENT_HOME` is set. In restricted Codex sandboxes, the first write may require an approved escalation or an approved `coagent.sh` command prefix.
- `COAGENT_SEED_REGISTRY` may point to an optional initial registry. No personal or project
  registry is bundled with the skill.

Read `references/design.md` only when changing the skill implementation or resolving a state-machine ambiguity.
