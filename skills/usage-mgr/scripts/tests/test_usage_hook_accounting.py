#!/usr/bin/env python3
"""Unit tests for production usage accounting."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "usage_hook.py"
SPEC = importlib.util.spec_from_file_location("usage_hook", SCRIPT_PATH)
assert SPEC and SPEC.loader
usage_hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(usage_hook)


CHILD_ID = "019fa377-d47b-7eb0-b23a-f01007324f71"
LIVE_TURN_ID = "019fa377-dfa6-7f50-a49c-3ff5e52fd9ec"
OLD_TURN_ID = "019fa321-27d9-7751-ae77-d82f6d7cb0b4"
PARENT_ID = "019efd6d-0da5-70c3-8d59-0ff3d2a31f70"


def usage(total: int) -> dict[str, int]:
    return {
        "input_tokens": total - 10,
        "cached_input_tokens": max(0, total - 100),
        "output_tokens": 10,
        "reasoning_output_tokens": 5,
        "total_tokens": total,
    }


def session_meta(subagent: bool = True) -> dict:
    source = "cli"
    if subagent:
        source = {
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": PARENT_ID,
                    "depth": 1,
                }
            }
        }
    return {
        "timestamp": "2026-07-27T12:00:00.000Z",
        "type": "session_meta",
        "payload": {
            "id": CHILD_ID,
            "cwd": "/workspace/example-project",
            "source": source,
        },
    }


def task_started(turn_id: str, timestamp: str) -> dict:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {"type": "task_started", "turn_id": turn_id},
    }


def token_count(total: int, timestamp: str) -> dict:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": usage(total),
                "last_token_usage": usage(100),
                "model_context_window": 258400,
            },
        },
    }


def write_transcript(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


class UsageHookAccountingTest(unittest.TestCase):
    def test_inherited_subagent_history_becomes_initial_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript_path = Path(tmp) / f"rollout-{CHILD_ID}.jsonl"
            write_transcript(
                transcript_path,
                [
                    session_meta(),
                    task_started(OLD_TURN_ID, "2026-07-27T12:00:00.001Z"),
                    token_count(1000, "2026-07-27T12:00:00.002Z"),
                    token_count(2000, "2026-07-27T12:00:00.003Z"),
                    task_started(LIVE_TURN_ID, "2026-07-27T12:00:00.100Z"),
                    token_count(2500, "2026-07-27T12:00:10.000Z"),
                ],
            )

            root = Path(tmp) / "usage"
            event, stale = usage_hook.build_fresh_hook_event(
                root,
                transcript_path,
                {
                    "hook_event_name": "SubagentStop",
                    "session_id": PARENT_ID,
                    "agent_id": CHILD_ID,
                    "agent_transcript_path": str(transcript_path),
                    "turn_id": LIVE_TURN_ID,
                },
                {},
            )
            result = usage_hook.ingest_event(root, event)

            self.assertFalse(stale)
            self.assertEqual("subagent_bootstrap_delta", result["reason"])
            self.assertEqual(500, result["record"]["delta"]["total_tokens"])
            self.assertEqual(2000, result["record"]["previous_total"]["total_tokens"])
            self.assertEqual("inherited_subagent_history", event["baseline_source"])
            self.assertEqual(2, event["inherited_token_events"])

    def test_subagent_without_inherited_tokens_keeps_zero_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript_path = Path(tmp) / f"rollout-{CHILD_ID}.jsonl"
            write_transcript(
                transcript_path,
                [
                    session_meta(),
                    task_started(LIVE_TURN_ID, "2026-07-27T12:00:00.100Z"),
                    token_count(500, "2026-07-27T12:00:10.000Z"),
                ],
            )

            baseline = usage_hook.extract_subagent_bootstrap_baseline(
                transcript_path,
                usage_hook.extract_session_meta(transcript_path),
                LIVE_TURN_ID,
            )
            self.assertEqual({}, baseline)

    def test_owner_transcript_never_uses_subagent_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript_path = Path(tmp) / f"rollout-{CHILD_ID}.jsonl"
            write_transcript(
                transcript_path,
                [
                    session_meta(subagent=False),
                    token_count(2000, "2026-07-27T12:00:00.003Z"),
                    task_started(LIVE_TURN_ID, "2026-07-27T12:00:00.100Z"),
                    token_count(2500, "2026-07-27T12:00:10.000Z"),
                ],
            )

            baseline = usage_hook.extract_subagent_bootstrap_baseline(
                transcript_path,
                usage_hook.extract_session_meta(transcript_path),
                LIVE_TURN_ID,
            )
            self.assertEqual({}, baseline)

    def test_backfill_uses_inherited_baseline_on_spawn_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript_path = Path(tmp) / f"rollout-{CHILD_ID}.jsonl"
            write_transcript(
                transcript_path,
                [
                    session_meta(),
                    task_started(OLD_TURN_ID, "2026-07-27T12:00:00.001Z"),
                    token_count(2000, "2026-07-27T12:00:00.003Z"),
                    task_started(LIVE_TURN_ID, "2026-07-27T12:00:00.100Z"),
                    token_count(2500, "2026-07-27T12:00:10.000Z"),
                ],
            )

            transcript = usage_hook.extract_transcript_for_date(
                transcript_path,
                usage_hook.dt.date(2026, 7, 27),
            )
            assert transcript is not None
            self.assertEqual(2000, transcript["baseline_total"]["total_tokens"])
            self.assertEqual("inherited_subagent_history", transcript["baseline_source"])

            event = usage_hook.build_event_from_transcript(
                transcript,
                {},
                "backfill",
                {},
            )
            result = usage_hook.ingest_event(Path(tmp) / "usage", event)
            self.assertEqual(500, result["record"]["delta"]["total_tokens"])

    def test_existing_state_wins_over_inherited_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "usage"
            first_event = {
                "event_key": "first",
                "accounting_session_id": CHILD_ID,
                "raw_session_id": CHILD_ID,
                "owner_session_id": PARENT_ID,
                "usage_date": "2026-07-27",
                "source": "hook",
                "is_subagent": True,
                "baseline_source": "inherited_subagent_history",
                "baseline_total": usage(2000),
                "total": usage(2500),
                "last": usage(100),
                "token_count": {
                    "timestamp": "2026-07-27T12:00:10.000Z",
                    "line_no": 5,
                },
            }
            usage_hook.ingest_event(root, first_event)

            second_event = dict(first_event)
            second_event.update(
                {
                    "event_key": "second",
                    "total": usage(2800),
                    "token_count": {
                        "timestamp": "2026-07-27T12:01:00.000Z",
                        "line_no": 6,
                    },
                }
            )
            result = usage_hook.ingest_event(root, second_event)
            self.assertEqual("cumulative_delta", result["reason"])
            self.assertEqual(300, result["record"]["delta"]["total_tokens"])


if __name__ == "__main__":
    unittest.main()
