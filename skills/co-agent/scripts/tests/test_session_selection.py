#!/usr/bin/env python3
import argparse
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import coagent


AGENT = {
    "name": "build agent",
    "cwd": "/tmp/build-agent",
}
CANDIDATE = {
    "thread_id": "thread-001",
    "chat_name": "build mgr",
    "title": "Build Manager",
    "updated_at": 123,
}
CANDIDATES = {
    "cwd": AGENT["cwd"],
    "matched": 1,
    "stale": [],
    "candidates": [CANDIDATE],
}


def wake_args(session=None, resume_last=False):
    return argparse.Namespace(session=session, resume_last=resume_last)


class SessionSelectionTest(unittest.TestCase):
    def test_explicit_last_does_not_query_candidates(self):
        with patch.object(coagent, "chat_candidates_for_cwd") as candidates:
            target = coagent.resolve_wake_target(
                AGENT,
                wake_args(resume_last=True),
            )

        candidates.assert_not_called()
        self.assertEqual(target["wake_method"], "last")
        self.assertEqual(target["resolved_thread_id"], "")

    def test_missing_selection_fails_with_candidate_list(self):
        with patch.object(
            coagent,
            "chat_candidates_for_cwd",
            return_value=CANDIDATES,
        ):
            with self.assertRaises(SystemExit) as raised:
                coagent.resolve_wake_target(AGENT, wake_args())

        message = str(raised.exception)
        self.assertIn("wake target selection required", message)
        self.assertIn("--session <thread_id> or --last", message)
        self.assertIn(CANDIDATE["thread_id"], message)
        self.assertIn(CANDIDATE["title"], message)

    def test_valid_session_uses_candidate_metadata(self):
        with patch.object(
            coagent,
            "chat_candidates_for_cwd",
            return_value=CANDIDATES,
        ):
            target = coagent.resolve_wake_target(
                AGENT,
                wake_args(session=CANDIDATE["thread_id"]),
            )

        self.assertEqual(target["wake_method"], "session_id")
        self.assertEqual(target["resolved_thread_id"], CANDIDATE["thread_id"])
        self.assertEqual(target["requested_chat_name"], CANDIDATE["chat_name"])
        self.assertEqual(target["chat_resolution"], CANDIDATE)

    def test_unknown_session_fails_with_valid_candidates(self):
        with patch.object(
            coagent,
            "chat_candidates_for_cwd",
            return_value=CANDIDATES,
        ):
            with self.assertRaises(SystemExit) as raised:
                coagent.resolve_wake_target(
                    AGENT,
                    wake_args(session="thread-missing"),
                )

        message = str(raised.exception)
        self.assertIn("session is not a valid candidate", message)
        self.assertIn("thread-missing", message)
        self.assertIn(CANDIDATE["thread_id"], message)

    def test_cli_rejects_session_and_last_together_for_all_wake_commands(self):
        parser = coagent.build_parser()
        commands = [
            [
                "handoff",
                "--goal",
                "GOAL-1",
                "--from",
                "source agent",
                "--to",
                "build agent",
                "--requested-outcome",
                "Build it",
            ],
            ["return", "--run", "RUN-1", "--segment", "1", "--summary", "Done"],
            ["retry", "--run", "RUN-1", "--segment", "1"],
            ["unblock", "--run", "RUN-1", "--segment", "1"],
        ]
        for command in commands:
            with self.subTest(command=command[0]):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(
                            command
                            + ["--session", CANDIDATE["thread_id"], "--last"]
                        )

    def test_handoff_rejects_missing_selection_before_goal_lookup(self):
        source = {"name": "source agent", "cwd": "/tmp/source-agent"}
        args = argparse.Namespace(
            from_agent="source agent",
            to_agent="build agent",
            session=None,
            resume_last=False,
        )
        with patch.object(coagent, "resolve_agent", side_effect=[source, AGENT]), patch.object(
            coagent,
            "chat_candidates_for_cwd",
            return_value=CANDIDATES,
        ), patch.object(coagent, "find_goal") as find_goal:
            with self.assertRaises(SystemExit):
                coagent.cmd_handoff(args)

        find_goal.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
