---
name: reme-memory
description: Use when the task depends on prior project decisions, recurring debugging patterns, user preferences, or multi-session continuity. This skill retrieves relevant local ReMe memories before work and stores durable new learnings after important tasks, using local file memory with hybrid vector plus FTS retrieval.
---

# ReMe Memory

Use this skill when the current task may benefit from prior local memory.

## When to use

- The user refers to earlier sessions, prior fixes, past decisions, or "last time we did this".
- The task is part of a long-running stream of debugging or implementation work.
- The task touches recurring project patterns that should be reused consistently.
- You discover a durable new lesson worth reusing later.

## Do not use

- The task is one-off and has no likely reuse value.
- The information is sensitive and should not be persisted.
- The memory would be low-signal noise such as transient shell output or temporary experiments.

## Runtime assumptions

- `CODEX_HOME` defaults to `$HOME/.codex`.
- `REME_HOME` defaults to `$HOME/.local/share/reme`.
- `REME_WORKDIR` defaults to `$REME_HOME/light_data`.
- `REME_ENV_FILE` defaults to `$REME_HOME/.env`.
- `REME_PYTHON` defaults to `$REME_HOME/.venv/bin/python`.
- ReMe venv: `$REME_HOME/.venv`
- ReMe working dir: `$REME_WORKDIR`
- ReMe env file: `$REME_ENV_FILE`
- This skill uses local file memory with `fts_enabled=True` and `vector_enabled=True`.
- Default embedding env vars:
  - `EMBEDDING_API_KEY`
  - `EMBEDDING_BASE_URL`
  - optional `REME_EMBEDDING_MODEL` (default `embedding-3`)
  - optional `REME_EMBEDDING_DIMENSIONS` (default `2048`)
- Always run the bundled scripts with `$REME_PYTHON`.

Initialize the variables before using the command examples:

```bash
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
REME_HOME="${REME_HOME:-$HOME/.local/share/reme}"
REME_WORKDIR="${REME_WORKDIR:-$REME_HOME/light_data}"
REME_ENV_FILE="${REME_ENV_FILE:-$REME_HOME/.env}"
REME_PYTHON="${REME_PYTHON:-$REME_HOME/.venv/bin/python}"
```

## Workflow

Setup or reconcile an existing/new ReMe runtime:

```bash
python3 $CODEX_HOME/skills/reme-memory/scripts/setup.py
```

Standard read entry:

```bash
$REME_PYTHON $CODEX_HOME/skills/reme-memory/scripts/memory_workflow.py prepare --task "<task or issue>"
```

Runtime status:

```bash
$REME_PYTHON $CODEX_HOME/skills/reme-memory/scripts/reme_status.py
```

Record ReMe Codex backend usage after `codex exec --json`:

```bash
$REME_PYTHON $CODEX_HOME/skills/reme-memory/scripts/record_codex_exec_usage.py --events-jsonl <events.jsonl>
```

If the user explicitly wants to compress context or produce a handoff:

```bash
$REME_PYTHON $CODEX_HOME/skills/reme-memory/scripts/memory_workflow.py capture_context --capture-json "<capture.json>" [--thread-id "<thread-id>"] [--session-scope-key "<session-scope-key>"] [--enqueue-durable]
```

Evidence-only async refine entry:

```bash
$REME_PYTHON $CODEX_HOME/skills/reme-memory/scripts/memory_workflow.py refine --refine-json "<refine.json>"
```

Offline PreCompact standard runner, when the user wants to extract valuable data from a saved transcript without enabling hooks:

```bash
$REME_PYTHON $CODEX_HOME/skills/reme-memory/scripts/context_capture_runner.py --input-json "<entry.json>"
```

Minimal entry JSON:

```json
{
  "schema_version": 1,
  "run_reason": "offline",
  "project": "cling_packet",
  "task": "Capture Packet RX performance matrix",
  "scenario": "Packet RX performance",
  "transcript_path": "<transcript.txt-or-jsonl>",
  "write_modes": ["capture_json", "artifacts", "handoff"],
  "fail_open": false
}
```

The runner prints a standard exit JSON with `state`, `rules_loaded`, `capture_json_path`, `handoff_md_path`, `artifact_paths`, `warnings`, and `errors`. It writes exact benchmark artifacts under `$CODEX_HOME/memories/reme-memory/artifacts/` and handoff files under `$CODEX_HOME/memories/reme-memory/handoffs/` when `handoff` is in `write_modes`. It is an offline tool only; it does not enable Codex hooks or enqueue durable memory.

`transcript_context_extractor.py` remains the extractor engine and test/debug tool. Routine manual capture should go through `context_capture_runner.py` so project, global, and generic rules are loaded and audited consistently.

When the source is a full Codex rollout JSONL, create a bounded plaintext excerpt first:

```bash
$REME_PYTHON $CODEX_HOME/skills/reme-memory/scripts/session_excerpt.py \
  --input-json "<hook-or-entry.json>" \
  --keyword "<important-keyword>" \
  --output "<excerpt.txt>" \
  --metadata-out "<excerpt.json>" \
  --runner-input-json-out "<runner-entry.json>"
```

`session_excerpt.py` accepts Codex hook JSON or the same entry-style JSON as a JSON object. Hook stdin is JSON, not raw transcript JSONL. The script reads `transcript_path`, skips system/developer prompts and encrypted reasoning by default, selects keyword or high-value-pattern records, writes a plaintext excerpt, and can write a `context_capture_runner.py` input JSON whose `transcript_path` points to that excerpt. Use this before feeding large rollout files into the runner; direct full-session runner scans are only for debugging because they can preserve repeated tool outputs.

Rule lifecycle entry, when a rule should be listed, disabled, or retired:

```bash
$REME_PYTHON $CODEX_HOME/skills/reme-memory/scripts/rule_proposal_workflow.py list --project "<project>"
$REME_PYTHON $CODEX_HOME/skills/reme-memory/scripts/rule_proposal_workflow.py disable --rule-id "<scope:name@version>" --reason "<reason>" --project "<project>"
$REME_PYTHON $CODEX_HOME/skills/reme-memory/scripts/rule_proposal_workflow.py retire --rule-id "<scope:name@version>" --reason "<reason>" --replacement-rule-id "<replacement>"
```

Do not remove rule files directly. `disable` sets `enabled=false`; `retire` also sets `deprecated=true` and records the replacement when provided. Both commands write a tombstone next to the rule root and append an audit event to `$CODEX_HOME/memories/reme-memory/rule-audit.jsonl`. Built-in generic rules require `--allow-generic` before lifecycle commands modify them.

Standard write entry, after work and only if a durable lesson exists:

```bash
$REME_PYTHON $CODEX_HOME/skills/reme-memory/scripts/memory_workflow.py finalize --outcome "<result>" --lesson "<durable lesson>"
```

`finalize` is the normal durable-write interface. It enqueues a bus request for the ReMe daemon, waits until the daemon reports `state=stored` and `phase=indexed`, records workflow metrics, and exposes a request id for diagnosis. This can take several minutes; progress lines with `state=processing` and a fresh daemon heartbeat mean the write is still active and should not be interrupted unless the user explicitly asks.

Routine durable writes update only the memory file returned by the write backend. They must use incremental `upsert_memory_file`; never trigger a full index rebuild from `finalize`, capture, query, daemon startup, or recovery.

PreCompact compact-memory files must enqueue a `memory_index` request after the file is written. That request performs one incremental upsert through the daemon and may queue safely while the daemon is stopped for maintenance.

Async refine must never write a deterministic fallback memory after Codex refinement fails. Exhausted retries finish as `refine_failed`; the compact memory remains the short-term evidence copy. Valid refine output must explicitly choose `create`, `overwrite`, `merge`, or `keep_both` after comparing the proposed memory with durable search candidates. `overwrite` and `merge` update the selected durable block in place, preserve evidence/source history, set its evidence/update time to the newest evidence time, and incrementally upsert only that durable date file.

Do not call `store_memory.py` for routine writes. It is the low-level local deterministic write backend used by the daemon; direct use bypasses the bus request lifecycle, daemon status, retry handling, commit markers, and workflow metrics.

Detailed flow:

1. Before substantial work, decide whether prior memory is likely useful.
2. Run `memory_workflow.py prepare`.
3. Read the brief and keep only the relevant facts, decisions, and heuristics in working context.
4. Perform the task.
5. If the user asks to compress the current context for continuation, run `memory_workflow.py capture_context`.
6. If the task produced a durable lesson, run `memory_workflow.py finalize`.
7. If there is no durable lesson, skip write-back. Do not store noise just to satisfy the workflow.

## Metrics

- Every `prepare` call records one read event.
- Every `prepare` hit records whether memory matched and how many memory items were returned.
- Every `capture_context` call records one handoff write event.
- Every `finalize` call records whether write-back was skipped or succeeded.
- Metrics log path:

```bash
$CODEX_HOME/memories/reme-memory/memory_workflow_events.jsonl
```

- Handoff output path:

```bash
$CODEX_HOME/memories/reme-memory/handoffs/
```

- Weekly summary:

```bash
$REME_PYTHON $CODEX_HOME/skills/reme-memory/scripts/report_memory_metrics.py --days 7
```

## Memory quality

Store only reusable memory:

- Root cause patterns
- Stable workarounds
- Repeated user preferences
- Important decisions and rationale
- Project-specific conventions that will likely matter again

Do not store:

- Secrets, tokens, private keys
- Large raw logs
- Temporary guesses
- Low-confidence conclusions

## Retrieval guidance

- Search with concrete keywords: subsystem, error, root cause, workaround, protocol, file or module names.
- If the first query is weak, try one tighter query rather than broadening aggressively.
- Prefer the smallest memory subset that changes the plan.
- Normal search covers active durable memory and active compact memory together.
- Hybrid relevance remains the primary score. Search overfetches candidates, filters by the raw hybrid match score, then applies a bounded recency bonus:

```text
freshness = 2 ^ (-age_days / 30)
rank_score = match_score * (1 + 0.10 * freshness)
```

- Use compact `Created At` and durable block `Evidence At`/`Updated At`; never use a shared durable file mtime as the block time.
- Results from the same capture/evidence lineage collapse to one result. Prefer the durable result and attach the compact path as evidence.
- If needed, the lower-level search helper remains available:

```bash
$REME_PYTHON $CODEX_HOME/skills/reme-memory/scripts/search_memory.py --query "<task or issue>"
```

## Routine memory maintenance

Compact memory is short-term searchable evidence:

- 0-30 days: active, indexed, and included in normal search.
- 30-90 days: moved to `compact_archive` and removed from the normal index.
- Older than 90 days: the compact Markdown file is deleted.

The daemon enqueues routine maintenance every 24 hours. Run an explicit dry-run or immediate maintenance pass through the same daemon single-writer path:

```bash
$REME_PYTHON \
  $CODEX_HOME/skills/reme-memory/scripts/memory_maintenance.py \
  --dry-run

$REME_PYTHON \
  $CODEX_HOME/skills/reme-memory/scripts/memory_maintenance.py
```

Maintenance also removes legacy durable blocks tagged `reme-refine-fallback`. It backs up affected durable date files, updates or deletes only the affected index paths, and never starts a full rebuild.

## Full index rebuild

Treat a full index rebuild as a standalone maintenance operation.

- Run it only when the user explicitly requests or approves a full rebuild in the current conversation.
- Never invoke it automatically from another script, normal memory writes, capture, search, daemon startup, retry, recovery, or setup.
- Stop the daemon before rebuilding and restart it afterward, including after a failed rebuild attempt.
- Stop the daemon only for this standalone maintenance operation. Routine incremental writes keep the daemon running; requests arriving during maintenance remain queued until restart.
- Pass `--confirm-full-rebuild`; the interface rejects calls without this explicit confirmation.
- Do not retry when the interface returns `reason=rebuild_in_progress`; another rebuild already owns the non-blocking rebuild lock.
- Before stopping the daemon, run `rebuild_index.py --estimate-embedding-budget`. This read-only estimate reports cached/uncached chunks, uncached input bytes, estimated API batches, configured retries, and worst-case API input bytes.
- The rebuild rejects uncached embedding input above its configured budget before moving the old index or calling the API. Never pass `--allow-embedding-budget-overrun` unless the user has seen the estimate and explicitly approved that estimated spend.
- Preserve the current `file_store` as the single rebuild backup. Build a fresh index at the active path, commit it only after structural/vector validation, and restore the backup on any failure.
- On daemon startup, recover an interrupted rebuild marker by restoring the backup. Recovery must never start a rebuild.
- Keep the embedding cache large enough for the full index. `refresh_embedding_cache.py` can repopulate it locally from a validated active index without an embedding API call.

After explicit user approval, run:

```bash
$REME_PYTHON \
  $CODEX_HOME/skills/reme-memory/scripts/rebuild_index.py \
  --estimate-embedding-budget
bash $CODEX_HOME/memories/reme-memory/daemon/stop_daemon.sh
$REME_PYTHON \
  $CODEX_HOME/skills/reme-memory/scripts/rebuild_index.py \
  --confirm-full-rebuild
bash $CODEX_HOME/memories/reme-memory/daemon/start_daemon.sh
```

## Storage guidance

- Routine durable writes must use `memory_workflow.py finalize`, not `store_memory.py`.
- Summaries should be short and factual.
- Include the issue, cause, resolution, and reuse condition when possible.
- If the memory is not specific enough to help a future task, do not store it.
- Use `store_memory.py` only when explicitly debugging the ReMe local write backend, file-store writes, or cleanup behavior. It runs the low-level write path directly in the current process and is not the standard workflow entry.

```bash
$REME_PYTHON $CODEX_HOME/skills/reme-memory/scripts/store_memory.py --note "<durable lesson>"
```
