#!/usr/bin/env python3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import shutil
import tempfile
import unittest
from pathlib import Path

from rule_registry import discover_rules, parse_simple_toml


class RuleRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="reme_rule_registry_", dir="/tmp"))
        self.project_rules = self.root / "project_rules"
        self.global_rules = self.root / "global_rules"
        self.generic_rules = self.root / "generic_rules"
        self._write_rule(
            self.project_rules / "packet-project" / "rule.toml",
            """
schema_version = 1
name = "packet-project"
version = "0.1"
scope = "project"
priority = 5
enabled = true

[match]
projects = ["cling_packet"]
keywords = ["packet"]

[outputs]
artifact_type = "benchmark_matrix"
""",
        )
        self._write_rule(
            self.global_rules / "perf-global" / "rule.toml",
            """
schema_version = 1
name = "perf-global"
version = "0.1"
scope = "global"
priority = 50
enabled = true

[match]
projects = ["*"]

[outputs]
artifact_type = "summary"
""",
        )
        self._write_rule(
            self.generic_rules / "perf-generic" / "rule.toml",
            """
schema_version = 1
name = "perf-generic"
version = "0.1"
scope = "generic"
priority = 100
enabled = true

[match]
projects = ["*"]
""",
        )
        self._write_rule(
            self.generic_rules / "disabled" / "rule.toml",
            """
schema_version = 1
name = "disabled"
version = "0.1"
scope = "generic"
enabled = false
""",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_parse_simple_toml_supports_manifest_shape(self) -> None:
        payload = parse_simple_toml(
            """
name = "demo"
enabled = true
priority = 7

[match]
projects = ["cling_packet", "sdk"]
"""
        )

        self.assertEqual(payload["name"], "demo")
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["priority"], 7)
        self.assertEqual(payload["match"]["projects"], ["cling_packet", "sdk"])

    def test_discover_rules_loads_project_global_generic_in_scope_order(self) -> None:
        result = discover_rules(
            context={
                "project": "cling_packet",
                "task": "Packet RX performance capture",
                "scenario": "Packet RX performance",
                "cwd": str(self.root),
            },
            rule_roots={
                "project": [str(self.project_rules)],
                "global": [str(self.global_rules)],
                "generic": [str(self.generic_rules)],
            },
        )

        rule_ids = [rule["rule_id"] for rule in result["rules_loaded"]]
        self.assertEqual(
            rule_ids,
            [
                "project:packet-project@0.1",
                "global:perf-global@0.1",
                "generic:perf-generic@0.1",
            ],
        )
        self.assertEqual(result["errors"], [])
        self.assertNotIn("generic:disabled@0.1", rule_ids)

    def test_discover_rules_filters_nonmatching_project_rule(self) -> None:
        result = discover_rules(
            context={
                "project": "other_project",
                "task": "Packet RX performance capture",
                "scenario": "Packet RX performance",
                "cwd": str(self.root),
            },
            rule_roots={
                "project": [str(self.project_rules)],
                "global": [str(self.global_rules)],
                "generic": [str(self.generic_rules)],
            },
        )

        rule_ids = [rule["rule_id"] for rule in result["rules_loaded"]]
        self.assertNotIn("project:packet-project@0.1", rule_ids)
        self.assertIn("global:perf-global@0.1", rule_ids)
        self.assertIn("generic:perf-generic@0.1", rule_ids)

    def _write_rule(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.strip() + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
