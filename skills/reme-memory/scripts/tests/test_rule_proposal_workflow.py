#!/usr/bin/env python3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from rule_registry import discover_rules


SCRIPT_DIR = Path(__file__).resolve().parents[1]
WORKFLOW = SCRIPT_DIR / "rule_proposal_workflow.py"
PYTHON = sys.executable


class RuleProposalWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="reme_rule_lifecycle_", dir="/tmp"))
        self.project_rules = self.root / "project_rules"
        self.audit_log = self.root / "audit.jsonl"
        self.rule_path = self.project_rules / "old-rule" / "rule.toml"
        self.rule_path.parent.mkdir(parents=True, exist_ok=True)
        self.rule_path.write_text(
            "\n".join(
                [
                    "schema_version = 1",
                    'name = "old-rule"',
                    'version = "0.1"',
                    'scope = "project"',
                    "priority = 10",
                    "enabled = true",
                    "",
                    "[match]",
                    'projects = ["cling_glb"]',
                    "",
                    "[outputs]",
                    'artifact_type = "benchmark_matrix"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_list_includes_enabled_rule(self) -> None:
        result = subprocess.run(
            [
                PYTHON,
                str(WORKFLOW),
                "list",
                "--project",
                "cling_glb",
                "--project-rule-root",
                str(self.project_rules),
                "--global-rule-root",
                str(self.root / "empty_global"),
                "--generic-rule-root",
                str(self.root / "empty_generic"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([rule["rule_id"] for rule in payload["rules"]], ["project:old-rule@0.1"])

    def test_disable_marks_rule_disabled_and_audits(self) -> None:
        result = self._run_action("disable", "project:old-rule@0.1", "covered by new rule")

        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["before_enabled"], True)
        self.assertEqual(result["after_enabled"], False)
        text = self.rule_path.read_text(encoding="utf-8")
        self.assertIn("enabled = false", text)
        self.assertIn('lifecycle_action = "disable"', text)
        self.assertTrue(Path(result["tombstone_path"]).exists())
        self.assertTrue(self.audit_log.exists())

        registry = discover_rules(
            context={"project": "cling_glb", "cwd": str(self.root)},
            rule_roots={
                "project": [str(self.project_rules)],
                "global": [str(self.root / "empty_global")],
                "generic": [str(self.root / "empty_generic")],
            },
        )
        self.assertEqual(registry["rules_loaded"], [])

    def test_retire_marks_deprecated_and_replacement(self) -> None:
        result = self._run_action(
            "retire",
            "project:old-rule@0.1",
            "replaced after sample parity",
            replacement="project:new-rule@0.2",
        )

        self.assertEqual(result["state"], "ok")
        text = self.rule_path.read_text(encoding="utf-8")
        self.assertIn("enabled = false", text)
        self.assertIn("deprecated = true", text)
        self.assertIn('replacement_rule_id = "project:new-rule@0.2"', text)
        tombstone = json.loads(Path(result["tombstone_path"]).read_text(encoding="utf-8"))
        self.assertEqual(tombstone["replacement_rule_id"], "project:new-rule@0.2")
        self.assertIn("before_manifest_text", tombstone)

    def _run_action(self, action: str, rule_id: str, reason: str, replacement: str = ""):
        command = [
            PYTHON,
            str(WORKFLOW),
            action,
            "--rule-id",
            rule_id,
            "--reason",
            reason,
            "--project",
            "cling_glb",
            "--project-rule-root",
            str(self.project_rules),
            "--global-rule-root",
            str(self.root / "empty_global"),
            "--generic-rule-root",
            str(self.root / "empty_generic"),
            "--audit-log",
            str(self.audit_log),
        ]
        if replacement:
            command.extend(["--replacement-rule-id", replacement])
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        return json.loads(result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
