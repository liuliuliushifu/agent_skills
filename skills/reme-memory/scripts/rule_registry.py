#!/usr/bin/env python3
import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))

DEFAULT_GLOBAL_RULE_ROOT = CODEX_HOME / "memories/reme-memory/rules"
DEFAULT_GENERIC_RULE_ROOT = SCRIPT_DIR.parent / "rules/generic"
DEFAULT_PROJECT_RULE_RELATIVE = Path(".codex/reme/rules")

REGISTRY_SCHEMA_VERSION = 1
RULE_SCHEMA_VERSION = 1
SCOPE_ORDER = {"project": 0, "global": 1, "generic": 2}


def discover_rules(
    context: Optional[Dict[str, Any]] = None,
    rule_roots: Optional[Dict[str, Iterable[str]]] = None,
) -> Dict[str, Any]:
    context = context or {}
    roots = normalize_rule_roots(context=context, rule_roots=rule_roots)
    warnings: List[str] = []
    errors: List[str] = []
    loaded: List[Dict[str, Any]] = []

    for scope in ("project", "global", "generic"):
        for root in roots.get(scope, []):
            rule_root = Path(str(root)).expanduser()
            if not rule_root.exists():
                if scope == "project":
                    continue
                warnings.append("rule root does not exist: {}".format(rule_root))
                continue
            for manifest_path in find_rule_manifests(rule_root):
                try:
                    rule = load_rule_manifest(manifest_path=manifest_path, scope=scope)
                except Exception as exc:
                    errors.append("failed to load rule manifest {}: {}".format(manifest_path, exc))
                    continue
                if not rule.get("enabled", True):
                    continue
                matched, reason = rule_matches_context(rule, context)
                if not matched:
                    warnings.append("rule skipped: {} ({})".format(rule["rule_id"], reason))
                    continue
                loaded.append(rule)

    loaded = dedupe_rules(sort_rules(loaded), warnings)
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "rules_loaded": loaded,
        "warnings": warnings,
        "errors": errors,
        "rule_roots": {scope: [str(path) for path in paths] for scope, paths in roots.items()},
    }


def normalize_rule_roots(
    context: Dict[str, Any],
    rule_roots: Optional[Dict[str, Iterable[str]]] = None,
) -> Dict[str, List[Path]]:
    explicit = rule_roots or context.get("rule_roots") or {}
    cwd = Path(str(context.get("cwd") or os.getcwd())).expanduser().resolve()
    roots: Dict[str, List[Path]] = {"project": [], "global": [], "generic": []}

    roots["project"].extend(_roots_from_explicit_or_env(explicit, "project", "REME_RULE_PROJECT_DIR"))
    roots["global"].extend(_roots_from_explicit_or_env(explicit, "global", "REME_RULE_GLOBAL_DIR"))
    roots["generic"].extend(_roots_from_explicit_or_env(explicit, "generic", "REME_RULE_GENERIC_DIR"))

    if not roots["project"]:
        project_root = find_nearest_project_rule_root(cwd)
        if project_root is not None:
            roots["project"].append(project_root)
    if not roots["global"]:
        roots["global"].append(DEFAULT_GLOBAL_RULE_ROOT)
    if not roots["generic"]:
        roots["generic"].append(DEFAULT_GENERIC_RULE_ROOT)

    return {scope: unique_paths(paths) for scope, paths in roots.items()}


def _roots_from_explicit_or_env(
    explicit: Dict[str, Any],
    scope: str,
    env_name: str,
) -> List[Path]:
    roots = explicit.get(scope)
    if roots is None:
        roots = explicit.get("{}_roots".format(scope))
    if roots is None:
        roots = explicit.get("{}_rule_roots".format(scope))
    if roots is None:
        env_value = os.environ.get(env_name, "")
        if env_value:
            roots = [item for item in env_value.split(os.pathsep) if item]
    if roots is None:
        return []
    if isinstance(roots, (str, Path)):
        roots = [str(roots)]
    return [Path(str(root)).expanduser().resolve() for root in roots]


def unique_paths(paths: Iterable[Path]) -> List[Path]:
    result: List[Path] = []
    seen = set()
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def find_nearest_project_rule_root(cwd: Path) -> Optional[Path]:
    current = cwd
    for candidate in [current] + list(current.parents):
        rule_root = candidate / DEFAULT_PROJECT_RULE_RELATIVE
        if rule_root.exists():
            return rule_root
    return None


def find_rule_manifests(root: Path) -> List[Path]:
    direct = root / "rule.toml"
    if direct.exists():
        return [direct]
    return sorted(root.glob("*/rule.toml"))


def load_rule_manifest(manifest_path: Path, scope: str) -> Dict[str, Any]:
    payload = parse_simple_toml(manifest_path.read_text(encoding="utf-8"))
    name = str(payload.get("name") or manifest_path.parent.name)
    version = str(payload.get("version") or "0.1")
    priority = int(payload.get("priority") or 0)
    enabled = bool(payload.get("enabled", True))
    schema_version = int(payload.get("schema_version") or RULE_SCHEMA_VERSION)
    match = _dict_value(payload.get("match"))
    outputs = _dict_value(payload.get("outputs"))
    rule = {
        "rule_id": "{}:{}@{}".format(scope, name, version),
        "name": name,
        "version": version,
        "schema_version": schema_version,
        "scope": scope,
        "priority": priority,
        "enabled": enabled,
        "path": str(manifest_path),
        "match": normalize_match_block(match),
        "outputs": outputs,
        "description": str(payload.get("description") or ""),
    }
    return rule


def parse_simple_toml(text: str) -> Dict[str, Any]:
    root: Dict[str, Any] = {}
    current = root
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = strip_toml_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if not section or "." in section:
                raise ValueError("unsupported section at line {}".format(line_no))
            current = root.setdefault(section, {})
            if not isinstance(current, dict):
                raise ValueError("section collides with scalar at line {}".format(line_no))
            continue
        if "=" not in line:
            raise ValueError("expected key=value at line {}".format(line_no))
        key, value = line.split("=", 1)
        current[key.strip()] = parse_toml_value(value.strip())
    return root


def strip_toml_comment(line: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    result = []
    for char in line:
        if char == "\\" and in_double and not escaped:
            escaped = True
            result.append(char)
            continue
        if char == "'" and not in_double and not escaped:
            in_single = not in_single
        elif char == '"' and not in_single and not escaped:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            break
        escaped = False
        result.append(char)
    return "".join(result)


def parse_toml_value(raw: str) -> Any:
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw)
    if raw.startswith("[") and raw.endswith("]"):
        try:
            return ast.literal_eval(raw)
        except Exception:
            inner = raw[1:-1].strip()
            if not inner:
                return []
            return [parse_toml_value(item.strip()) for item in inner.split(",")]
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        try:
            return ast.literal_eval(raw)
        except Exception:
            return raw[1:-1]
    return raw


def normalize_match_block(match: Dict[str, Any]) -> Dict[str, List[str]]:
    return {
        "projects": _string_list(match.get("projects")),
        "path_contains": _string_list(match.get("path_contains")),
        "keywords": _string_list(match.get("keywords")),
    }


def rule_matches_context(rule: Dict[str, Any], context: Dict[str, Any]) -> Tuple[bool, str]:
    match = rule.get("match") or {}
    projects = match.get("projects") or []
    project = str(context.get("project") or "")
    if projects and "*" not in projects and project not in projects:
        return False, "project '{}' not in {}".format(project, ",".join(projects))

    path_terms = match.get("path_contains") or []
    if path_terms:
        path_text = "\n".join(
            str(context.get(key) or "")
            for key in ("cwd", "transcript_path", "task", "scenario")
        ).lower()
        if not any(term.lower() in path_text for term in path_terms):
            return False, "path terms did not match"

    keywords = match.get("keywords") or []
    if keywords:
        keyword_text = "\n".join(
            str(context.get(key) or "")
            for key in ("task", "scenario", "keyword_text", "transcript_excerpt")
        ).lower()
        if not any(keyword.lower() in keyword_text for keyword in keywords):
            return False, "keywords did not match"
    return True, "matched"


def sort_rules(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        rules,
        key=lambda rule: (
            SCOPE_ORDER.get(str(rule.get("scope")), 99),
            -int(rule.get("priority") or 0),
            str(rule.get("name") or ""),
            str(rule.get("path") or ""),
        ),
    )


def dedupe_rules(rules: List[Dict[str, Any]], warnings: List[str]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen = set()
    for rule in rules:
        key = rule["rule_id"]
        if key in seen:
            warnings.append("duplicate rule ignored: {}".format(key))
            continue
        seen.add(key)
        result.append(rule)
    return result


def _dict_value(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        return [str(value)]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Discover ReMe context capture rules.")
    parser.add_argument("--context-json", default="", help="JSON file with project/task/cwd/rule_roots context")
    parser.add_argument("--project", default="")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--task", default="")
    parser.add_argument("--scenario", default="")
    parser.add_argument("--transcript-path", default="")
    parser.add_argument("--keyword-text", default="")
    args = parser.parse_args(argv)

    context: Dict[str, Any] = {}
    if args.context_json:
        source = sys.stdin.read() if args.context_json == "-" else Path(args.context_json).read_text(encoding="utf-8")
        context.update(json.loads(source))
    for key in ("project", "cwd", "task", "scenario", "transcript_path", "keyword_text"):
        value = getattr(args, key)
        if value:
            context[key] = value
    result = discover_rules(context=context)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
