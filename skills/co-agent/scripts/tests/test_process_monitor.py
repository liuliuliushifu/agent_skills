#!/usr/bin/env python3
import json
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import coagent


TOP_AGENT = {
    "name": "master agent",
    "cwd": "/tmp/master-agent",
    "role": "Own the collaboration round.",
}


class ProcessMonitorTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.goal_dir = Path(self.tempdir.name) / "GOAL-TEST-001"
        self.run_dir = self.goal_dir / "runs" / "RUN-TEST-001"
        self.run_dir.mkdir(parents=True)
        coagent.write_json_atomic(
            self.goal_dir / "goal.json",
            {
                "version": 1,
                "goal_id": self.goal_dir.name,
                "owner_agent": TOP_AGENT["name"],
                "status": "active",
            },
        )
        coagent.write_json_atomic(
            self.run_dir / "run.json",
            {
                "version": 1,
                "goal_id": self.goal_dir.name,
                "run_id": self.run_dir.name,
                "created_by_agent": TOP_AGENT["name"],
                "created_by_agent_snapshot": TOP_AGENT,
                "created_at": "2026-08-03T10:00:00+08:00",
                "status": "active",
            },
        )
        self.processes = []

    def tearDown(self):
        for proc in self.processes:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, 9)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=3)
        self.tempdir.cleanup()

    def start_process(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            start_new_session=True,
        )
        self.processes.append(proc)
        return proc

    def write_events(self, events):
        path = self.run_dir / "segments.jsonl"
        path.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
            encoding="utf-8",
        )

    def wake_event(self, proc, segment=1, attempt=1):
        command = [sys.executable, "-c", "import time; time.sleep(120)"]
        identity = coagent.capture_wake_identity(proc.pid, command)
        return {
            "event": "wake_finished",
            "segment_id": segment,
            "attempt": attempt,
            "status": "wake_detached",
            "pid": proc.pid,
            "command": command,
            "updated_at": coagent.now_iso(),
            **identity,
        }

    def base_events(self, proc, terminal=False):
        events = [
            {
                "event": "segment_started",
                "segment_id": 1,
                "status": "active",
                "from_agent": TOP_AGENT["name"],
                "to_agent": "worker agent",
                "created_at": coagent.now_iso(),
            },
            {
                "event": "attempt_started",
                "segment_id": 1,
                "attempt": 1,
                "status": "active",
                "created_at": coagent.now_iso(),
            },
            self.wake_event(proc),
        ]
        if terminal:
            old = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
            events.extend(
                [
                    {
                        "event": "attempt_finished",
                        "segment_id": 1,
                        "attempt": 1,
                        "status": "finished",
                        "updated_at": old,
                    },
                    {
                        "event": "segment_finished",
                        "segment_id": 1,
                        "status": "finished",
                        "updated_at": old,
                    },
                ]
            )
        return events

    def test_active_process_is_recorded_at_goal_level_and_waits(self):
        proc = self.start_process()
        self.write_events(self.base_events(proc))
        run_before = (self.run_dir / "run.json").read_bytes()

        data = coagent.refresh_process_monitor(self.goal_dir)

        self.assertEqual(data["state"], "WAITING")
        self.assertEqual(data["phase"], "BUSINESS_ACTIVE")
        self.assertEqual(data["counts"]["business_pending"], 1)
        self.assertEqual(data["top_from_agent"], TOP_AGENT["name"])
        self.assertEqual(data["attempts"][0]["pid"], proc.pid)
        self.assertEqual(data["attempts"][0]["process_state"], "RUNNING")
        self.assertTrue((self.goal_dir / "process-monitor.json").exists())
        self.assertFalse((self.run_dir / "process-monitor.json").exists())
        self.assertEqual((self.run_dir / "run.json").read_bytes(), run_before)
        self.assertIsNone(proc.poll())

    def test_terminal_process_is_terminated_after_grace(self):
        proc = self.start_process()
        self.write_events(self.base_events(proc, terminal=True))

        data = coagent.refresh_process_monitor(
            self.goal_dir,
            perform_cleanup=True,
            grace_seconds=60,
            term_wait_seconds=1,
        )

        proc.wait(timeout=3)
        self.assertEqual(data["state"], "ALL CLEARED")
        self.assertEqual(data["phase"], "ALL_CLEARED")
        self.assertTrue(data["attempts"][0]["cleared"])
        self.assertIn(data["attempts"][0]["process_state"], ("TERMINATED", "KILLED"))

    def test_all_business_terminal_enters_final_monitoring_during_grace(self):
        proc = self.start_process()
        events = self.base_events(proc, terminal=True)
        terminal_now = coagent.now_iso()
        events[-2]["updated_at"] = terminal_now
        events[-1]["updated_at"] = terminal_now
        self.write_events(events)

        data = coagent.refresh_process_monitor(
            self.goal_dir,
            perform_cleanup=True,
            grace_seconds=60,
            term_wait_seconds=1,
        )

        self.assertEqual(data["state"], "WAITING")
        self.assertEqual(data["phase"], "FINAL_MONITORING")
        self.assertEqual(data["counts"]["business_pending"], 0)
        self.assertEqual(data["attempts"][0]["process_state"], "EXIT_GRACE")
        self.assertIsNone(proc.poll())

    def test_older_attempt_is_not_hidden_by_latest_attempt(self):
        proc = self.start_process()
        events = self.base_events(proc)
        later = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
        events.extend(
            [
                {
                    "event": "attempt_started",
                    "segment_id": 1,
                    "attempt": 2,
                    "status": "active",
                    "created_at": later,
                },
                {
                    "event": "wake_finished",
                    "segment_id": 1,
                    "attempt": 2,
                    "status": "wake_skipped",
                    "pid": None,
                    "command": [],
                    "updated_at": later,
                },
            ]
        )
        self.write_events(events)

        data = coagent.refresh_process_monitor(
            self.goal_dir,
            perform_cleanup=True,
            grace_seconds=0,
            term_wait_seconds=1,
        )

        proc.wait(timeout=3)
        self.assertEqual(len(data["attempts"]), 2)
        self.assertEqual(data["state"], "ALL CLEARED")
        self.assertTrue(all(item["cleared"] for item in data["attempts"]))

    def test_retry_attempt_reopens_segment_and_ignores_prior_terminal_event(self):
        for prior_state in ("blocked", "failed"):
            with self.subTest(prior_state=prior_state):
                events = [
                    {
                        "event": "segment_started",
                        "segment_id": 1,
                        "status": "active",
                        "from_agent": TOP_AGENT["name"],
                        "to_agent": "worker agent",
                        "created_at": "2026-08-03T10:00:00+08:00",
                    },
                    {
                        "event": "attempt_started",
                        "segment_id": 1,
                        "attempt": 1,
                        "status": "active",
                        "created_at": "2026-08-03T10:00:01+08:00",
                    },
                    {
                        "event": "attempt_finished",
                        "segment_id": 1,
                        "attempt": 1,
                        "status": prior_state,
                        "summary": "prior attempt terminal",
                        "updated_at": "2026-08-03T10:01:00+08:00",
                    },
                    {
                        "event": "segment_finished",
                        "segment_id": 1,
                        "status": prior_state,
                        "summary": "prior segment terminal",
                        "updated_at": "2026-08-03T10:01:01+08:00",
                    },
                    {
                        "event": "attempt_started",
                        "segment_id": 1,
                        "attempt": 2,
                        "status": "active",
                        "created_at": "2026-08-03T10:02:00+08:00",
                    },
                ]

                segment = coagent.build_segments(events)[1]
                terminal = coagent.attempt_terminal_info(events, 1, 2)

                self.assertEqual(segment["status"], "active")
                self.assertEqual(segment["latest_attempt"], 2)
                self.assertNotIn("summary", segment)
                self.assertEqual(terminal["business_state"], "active")
                self.assertEqual(terminal["terminal_at"], "")

    def test_missing_wake_registration_never_reports_all_cleared(self):
        old = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
        self.write_events(
            [
                {
                    "event": "segment_started",
                    "segment_id": 1,
                    "status": "active",
                    "from_agent": TOP_AGENT["name"],
                    "to_agent": "worker agent",
                    "created_at": old,
                },
                {
                    "event": "attempt_started",
                    "segment_id": 1,
                    "attempt": 1,
                    "status": "active",
                    "created_at": old,
                },
                {
                    "event": "attempt_finished",
                    "segment_id": 1,
                    "attempt": 1,
                    "status": "finished",
                    "updated_at": old,
                },
                {
                    "event": "segment_finished",
                    "segment_id": 1,
                    "status": "finished",
                    "updated_at": old,
                },
            ]
        )

        data = coagent.refresh_process_monitor(
            self.goal_dir,
            perform_cleanup=True,
            grace_seconds=0,
            term_wait_seconds=0,
        )

        self.assertEqual(data["state"], "WAITING")
        self.assertEqual(data["phase"], "FINAL_MONITORING")
        self.assertEqual(data["counts"]["business_pending"], 0)
        self.assertEqual(data["attempts"][0]["process_state"], "WAKE_REGISTRATION_PENDING")
        self.assertFalse(data["attempts"][0]["cleared"])

    def test_wake_receipt_recovers_process_before_ledger_registration(self):
        proc = self.start_process()
        old = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
        command = [sys.executable, "-c", "import time; time.sleep(120)"]
        receipt_file = self.run_dir / "wake-SEG001-ATT001.json"
        coagent.write_json_atomic(
            receipt_file,
            {
                "version": 1,
                "status": "wake_detached",
                "exit_code": None,
                "pid": proc.pid,
                "command": command,
                "updated_at": old,
                **coagent.capture_wake_identity(proc.pid, command),
            },
        )
        self.write_events(
            [
                {
                    "event": "segment_started",
                    "segment_id": 1,
                    "status": "active",
                    "from_agent": TOP_AGENT["name"],
                    "to_agent": "worker agent",
                    "created_at": old,
                },
                {
                    "event": "attempt_started",
                    "segment_id": 1,
                    "attempt": 1,
                    "status": "active",
                    "wake_receipt_file": str(receipt_file),
                    "created_at": old,
                },
                {
                    "event": "attempt_finished",
                    "segment_id": 1,
                    "attempt": 1,
                    "status": "finished",
                    "updated_at": old,
                },
                {
                    "event": "segment_finished",
                    "segment_id": 1,
                    "status": "finished",
                    "updated_at": old,
                },
            ]
        )

        data = coagent.refresh_process_monitor(
            self.goal_dir,
            perform_cleanup=True,
            grace_seconds=0,
            term_wait_seconds=1,
        )

        proc.wait(timeout=3)
        self.assertEqual(data["state"], "ALL CLEARED")
        self.assertEqual(data["attempts"][0]["pid"], proc.pid)
        self.assertIn(data["attempts"][0]["process_state"], ("TERMINATED", "KILLED"))

    def test_create_segment_persists_attempt_wake_receipt(self):
        worker = {
            "name": "worker agent",
            "cwd": "/tmp/worker-agent",
            "role": "Execute delegated work.",
        }
        sid = coagent.create_segment(
            self.run_dir,
            self.goal_dir.name,
            self.run_dir.name,
            "forward",
            TOP_AGENT,
            worker,
            None,
            "Test context",
            "Verify receipt persistence.",
            [],
            [],
            "receipt integration test",
            {
                "wake_method": "last",
                "requested_chat_name": "",
                "resolved_thread_id": "",
                "chat_resolution": None,
            },
            no_wake=True,
            wake_args=type("WakeArgs", (), {"foreground": False})(),
        )

        events = coagent.read_events(self.run_dir)
        attempt_started = next(event for event in events if event["event"] == "attempt_started")
        wake_finished = next(event for event in events if event["event"] == "wake_finished")
        receipt = coagent.read_json(Path(attempt_started["wake_receipt_file"]))

        self.assertEqual(sid, 1)
        self.assertEqual(wake_finished["status"], "wake_skipped")
        self.assertEqual(receipt["status"], "wake_skipped")
        self.assertEqual(receipt["pid"], None)
        self.assertTrue((self.goal_dir / "process-monitor.json").exists())

    def test_cleared_process_is_not_reinspected_after_pid_reuse(self):
        proc = self.start_process()
        events = self.base_events(proc, terminal=True)
        proc.terminate()
        proc.wait(timeout=3)
        self.write_events(events)

        first = coagent.refresh_process_monitor(self.goal_dir)
        self.assertEqual(first["state"], "ALL CLEARED")
        self.assertEqual(first["attempts"][0]["process_state"], "EXITED")

        with patch.object(
            coagent,
            "inspect_process_record",
            side_effect=AssertionError("cleared records must not be reinspected"),
        ):
            second = coagent.refresh_process_monitor(self.goal_dir)

        self.assertEqual(second["state"], "ALL CLEARED")
        self.assertTrue(second["attempts"][0]["cleared"])
        self.assertEqual(second["attempts"][0]["process_state"], "EXITED")

    def test_parallel_and_nested_targets_are_cleaned_independently(self):
        first = self.start_process()
        second = self.start_process()
        old = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
        events = self.base_events(first, terminal=True)
        events.extend(
            [
                {
                    "event": "segment_started",
                    "segment_id": 2,
                    "status": "active",
                    "from_agent": "worker agent",
                    "to_agent": "nested agent",
                    "parent_segment_id": 1,
                    "created_at": old,
                },
                {
                    "event": "attempt_started",
                    "segment_id": 2,
                    "attempt": 1,
                    "status": "active",
                    "created_at": old,
                },
                self.wake_event(second, segment=2),
                {
                    "event": "attempt_finished",
                    "segment_id": 2,
                    "attempt": 1,
                    "status": "finished",
                    "updated_at": old,
                },
                {
                    "event": "segment_finished",
                    "segment_id": 2,
                    "status": "finished",
                    "updated_at": old,
                },
            ]
        )
        self.write_events(events)

        data = coagent.refresh_process_monitor(
            self.goal_dir,
            perform_cleanup=True,
            grace_seconds=60,
            term_wait_seconds=1,
        )

        first.wait(timeout=3)
        second.wait(timeout=3)
        self.assertEqual(data["state"], "ALL CLEARED")
        self.assertEqual(
            data["counts"],
            {"attempts": 2, "cleared": 2, "waiting": 0, "business_pending": 0},
        )

    def test_cleanup_waits_until_every_business_attempt_is_terminal(self):
        finished = self.start_process()
        active = self.start_process()
        old = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
        events = self.base_events(finished, terminal=True)
        events.extend(
            [
                {
                    "event": "segment_started",
                    "segment_id": 2,
                    "status": "active",
                    "from_agent": TOP_AGENT["name"],
                    "to_agent": "active agent",
                    "created_at": old,
                },
                {
                    "event": "attempt_started",
                    "segment_id": 2,
                    "attempt": 1,
                    "status": "active",
                    "created_at": old,
                },
                self.wake_event(active, segment=2),
            ]
        )
        self.write_events(events)

        data = coagent.refresh_process_monitor(
            self.goal_dir,
            perform_cleanup=True,
            grace_seconds=60,
            term_wait_seconds=1,
        )

        self.assertEqual(data["state"], "WAITING")
        self.assertEqual(data["phase"], "BUSINESS_ACTIVE")
        self.assertEqual(data["counts"]["business_pending"], 1)
        self.assertEqual(data["attempts"][0]["process_state"], "BUSINESS_PHASE_HOLD")
        self.assertIsNone(finished.poll())
        self.assertIsNone(active.poll())

    def test_identity_mismatch_is_never_killed(self):
        proc = self.start_process()
        events = self.base_events(proc, terminal=True)
        events[2]["proc_start_ticks"] += 1
        self.write_events(events)

        data = coagent.refresh_process_monitor(
            self.goal_dir,
            perform_cleanup=True,
            grace_seconds=0,
            term_wait_seconds=0,
        )

        self.assertEqual(data["state"], "WAITING")
        self.assertEqual(data["attempts"][0]["process_state"], "IDENTITY_UNVERIFIED")
        self.assertIsNone(proc.poll())

    def test_only_top_from_can_run_cleanup_command(self):
        args = type("Args", (), {"goal": self.goal_dir.name, "from_agent": "worker agent"})()
        with patch.object(coagent, "find_goal", return_value=self.goal_dir), patch.object(
            coagent,
            "resolve_agent",
            return_value={"name": "worker agent", "cwd": "/tmp/worker"},
        ):
            with self.assertRaises(SystemExit) as raised:
                coagent.cmd_process_monitor(args)

        self.assertIn("owned by top-from agent", str(raised.exception))

    def test_top_from_command_prints_all_cleared(self):
        self.write_events([])
        args = type("Args", (), {"goal": self.goal_dir.name, "from_agent": TOP_AGENT["name"]})()
        output = io.StringIO()
        with patch.object(coagent, "find_goal", return_value=self.goal_dir), patch.object(
            coagent,
            "resolve_agent",
            return_value=TOP_AGENT,
        ), contextlib.redirect_stdout(output):
            coagent.cmd_process_monitor(args)

        self.assertEqual(output.getvalue().splitlines()[0], "ALL CLEARED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
