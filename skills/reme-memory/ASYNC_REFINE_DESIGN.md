# ReMe Async Refine Design

## Goal

Add a slow-path refine stage that turns script-selected evidence into durable ReMe memory without blocking the main memory daemon.

## Flow

1. Main bus receives `memory_refine`.
2. Main daemon validates the request, writes an async refine job, marks the request `accepted_async`, and continues normal work.
3. Async supervisor scans the refine queue and runs up to `REME_REFINE_MAX_CONCURRENCY` refine attempts in parallel.
4. Each attempt runs a Codex backend against evidence-only JSON. The prompt does not include `source_transcript_path`.
5. Attempt output must be strict JSON. Invalid output is retried twice.
6. After all attempts fail validation, deterministic fallback builds a conservative memory candidate from the evidence.
7. Valid candidates are enqueued back to the main bus as `memory_write` requests.
8. The main daemon performs normal durable write and indexing.

## Queues

The regular bus gains `inbox/refine` and `processing/refine` for quick main-thread acceptance.

Long-running state is stored under:

```text
bus/async/refine/
  queued/
  running/
  tmp/
  logs/YYYY-MM-DD.jsonl
```

Queued/running/tmp files are processing cache only. Terminal jobs append a small audit row to the daily log and remove processing cache files by default.

## Ownership

- Main daemon owns durable memory files and index updates.
- Async supervisor owns Codex refine processes and validation.
- Async supervisor never writes memory/index directly; it only enqueues main-bus writes.

## Usage

Codex refine attempts should run with `codex exec --json`. After each attempt, `record_codex_exec_usage.py` records official usage as agent `ReMe`. Usage recording errors are logged but do not block memory validation.

## Safety

- Evidence is the only content sent to Codex refine.
- Refine output must reference known evidence hashes.
- `REME_REFINE_WORKER=1` is set for refine subprocesses to avoid recursive refine scheduling.
- Concurrency is bounded by `REME_REFINE_MAX_CONCURRENCY`.
- Terminal processing cache is cleaned to prevent unbounded growth.
