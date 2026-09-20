#!/usr/bin/env python3
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from rule_registry import (
    CODEX_HOME,
    find_rule_manifests,
    load_rule_manifest,
    normalize_rule_roots,
    sort_rules,
)


LIFECYCLE_SCHEMA_VERSION = 1
DEFAULT_AUDIT_LOG = CODEX_HOME / "memories/reme-memory/rule-audit.jsonl"


def list_rules(
    context: Optional[Dict[str, Any]] = None,
    rule_roots: Optional[Dict[str, Iterable[str]]] = None,
) -> Dict[str, Any]:
    context = context or {}
    roots = normalize_rule_roots(context=context, rule_roots=rule_roots)
    warnings: List[str] = []
    errors: List[str] = []
    rules: List[Dict[str, Any]] = []

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
                rule["rule_root"] = str(rule_root)
                rules.append(rule)

    return {
        "schema_version": LIFECYCLE_SCHEMA_VERSION,
        "rules": sort_rules(rules),
        "rule_roots": {scope: [str(path) for path in paths] for scope, paths in roots.items()},
        "warnings": warnings,
        "errors": errors,
    }


def disable_rule(
    rule_id: str,
    reason: str,
    context: Optional[Dict[str, Any]] = None,
    rule_roots: Optional[Dict[str, Iterable[str]]] = None,
    audit_log: Optional[Path] = None,
    replacement_rule_id: str = "",
    allow_generic: bool = False,
) -> Dict[str, Any]:
    return _apply_lifecycle_action(
        action="disable",
        rule_id=rule_id,
        reason=reason,
        context=context,
        rule_roots=rule_roots,
        audit_log=audit_log,
        replacement_rule_id=replacement_rule_id,
        allow_generic=allow_generic,
    )


def retire_rule(
    rule_id: str,
    reason: str,
    context: Optional[Dict[str, Any]] = None,
    rule_roots: Optional[Dict[str, Iterable[str]]] = None,
    audit_log: Optional[Path] = None,
    replacement_rule_id: str = "",
    allow_generic: bool = False,
) -> Dict[str, Any]:
    return _apply_lifecycle_action(
        action="retire",
        rule_id=rule_id,
        reason=reason,
        context=context,
        rule_roots=rule_roots,
        audit_log=audit_log,
        replacement_rule_id=replacement_rule_id,
        allow_generic=allow_generic,
    )


def _apply_lifecycle_action(
    action: str,
    rule_id: str,
    reason: str,
    context: Optional[Dict[str, Any]],
    rule_roots: Optional[Dict[str, Iterable[str]]],
    audit_log: Optional[Path],
    replacement_rule_id: str,
    allow_generic: bool,
) -> Dict[str, Any]:
    if not reason.strip():
        raise ValueError("reason is required")
    listing = list_rules(context=context, rule_roots=rule_roots)
    rule = _find_unique_rule(listing["rules"], rule_id)
    if rule["scope"] == "generic" and not allow_generic:
        raise ValueError("refusing to modify generic rule without --allow-generic")

    manifest_path = Path(rule["path"])
    before_text = manifest_path.read_text(encoding="utf-8")
    before_sha = sha256_text(before_text)
    after_text = _update_manifest_for_action(
        before_text=before_text,
        action=action,
        reason=reason,
        replacement_rule_id=replacement_rule_id,
    )
    after_sha = sha256_text(after_text)

    if before_sha != after_sha:
        _atomic_write_text(manifest_path, after_text)

    updated_rule = load_rule_manifest(manifest_path=manifest_path, scope=rule["scope"])
    updated_rule["rule_root"] = rule.get("rule_root", str(manifest_path.parent.parent))
    tombstone_path = _write_tombstone(
        action=action,
        rule=rule,
        manifest_path=manifest_path,
        before_text=before_text,
        before_sha=before_sha,
        after_sha=after_sha,
        reason=reason,
        replacement_rule_id=replacement_rule_id,
    )
    audit_event = {
        "schema_version": LIFECYCLE_SCHEMA_VERSION,
        "created_at": now_iso(),
        "action": action,
        "rule_id": rule["rule_id"],
        "rule_path": str(manifest_path),
        "scope": rule["scope"],
        "reason": reason,
        "replacement_rule_id": replacement_rule_id or None,
        "before_manifest_sha256": before_sha,
        "after_manifest_sha256": after_sha,
        "tombstone_path": str(tombstone_path),
    }
    _append_audit_event(audit_log or DEFAULT_AUDIT_LOG, audit_event)

    return {
        "schema_version": LIFECYCLE_SCHEMA_VERSION,
        "state": "ok",
        "action": action,
        "rule_id": rule["rule_id"],
        "rule_path": str(manifest_path),
        "before_enabled": rule.get("enabled", True),
        "after_enabled": updated_rule.get("enabled", True),
        "tombstone_path": str(tombstone_path),
        "audit_log": str(audit_log or DEFAULT_AUDIT_LOG),
        "warnings": listing.get("warnings", []),
        "errors": listing.get("errors", []),
    }


def _find_unique_rule(rules: List[Dict[str, Any]], rule_id: str) -> Dict[str, Any]:
    matches = [rule for rule in rules if rule["rule_id"] == rule_id]
    if not matches:
        raise ValueError("rule not found: {}".format(rule_id))
    if len(matches) > 1:
        raise ValueError("ambiguous duplicate rule id: {}".format(rule_id))
    return matches[0]


def _update_manifest_for_action(
    before_text: str,
    action: str,
    reason: str,
    replacement_rule_id: str,
) -> str:
    updates = {
        "enabled": "false",
        "lifecycle_action": _toml_string(action),
        "lifecycle_reason": _toml_string(reason),
        "lifecycle_updated_at": _toml_string(now_iso()),
    }
    if action == "retire":
        updates["deprecated"] = "true"
    if replacement_rule_id:
        updates["replacement_rule_id"] = _toml_string(replacement_rule_id)

    text = before_text.rstrip("\n") + "\n"
    for key, value in updates.items():
        text = _set_root_scalar(text, key, value)
    return text


def _set_root_scalar(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    key_re = re.compile(r"^(\s*{}\s*=\s*).*$".format(re.escape(key)))
    first_section_idx = len(lines)
    for idx, line in enumerate(lines):
        if line.strip().startswith("["):
            first_section_idx = idx
            break
        if key_re.match(line):
            lines[idx] = "{} = {}".format(key, value)
            return "\n".join(lines) + "\n"

    insert_idx = first_section_idx
    while insert_idx > 0 and not lines[insert_idx - 1].strip():
        insert_idx -= 1
    lines.insert(insert_idx, "{} = {}".format(key, value))
    return "\n".join(lines) + "\n"


def _write_tombstone(
    action: str,
    rule: Dict[str, Any],
    manifest_path: Path,
    before_text: str,
    before_sha: str,
    after_sha: str,
    reason: str,
    replacement_rule_id: str,
) -> Path:
    rule_root = Path(str(rule.get("rule_root") or manifest_path.parent.parent))
    tombstone_dir = rule_root / ".tombstones"
    safe_rule = re.sub(r"[^A-Za-z0-9_.@-]+", "_", rule["rule_id"])
    filename = "{}-{}.json".format(datetime.datetime.now().strftime("%Y%m%dT%H%M%S"), safe_rule)
    tombstone_path = tombstone_dir / filename
    payload = {
        "schema_version": LIFECYCLE_SCHEMA_VERSION,
        "created_at": now_iso(),
        "action": action,
        "rule_id": rule["rule_id"],
        "name": rule["name"],
        "version": rule["version"],
        "scope": rule["scope"],
        "path": str(manifest_path),
        "reason": reason,
        "replacement_rule_id": replacement_rule_id or None,
        "before_manifest_sha256": before_sha,
        "after_manifest_sha256": after_sha,
        "before_manifest_text": before_text,
    }
    _atomic_write_text(tombstone_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return tombstone_path


def _append_audit_event(path: Path, event: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def load_context(path_value: str) -> Dict[str, Any]:
    if not path_value:
        return {}
    raw = sys.stdin.read() if path_value == "-" else Path(path_value).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("context JSON must be an object")
    return payload


def _merge_cli_context(args: argparse.Namespace) -> Dict[str, Any]:
    context = load_context(getattr(args, "context_json", ""))
    for key in ("project", "cwd", "task", "scenario", "transcript_path", "keyword_text"):
        value = getattr(args, key, "")
        if value:
            context[key] = value
    rule_roots: Dict[str, List[str]] = {}
    for scope in ("project", "global", "generic"):
        values = getattr(args, "{}_rule_root".format(scope), None) or []
        if values:
            rule_roots[scope] = values
    if rule_roots:
        context["rule_roots"] = rule_roots
    return context


def _add_context_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--context-json", default="", help="JSON file with context/rule_roots, or '-'")
    parser.add_argument("--project", default="")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--task", default="")
    parser.add_argument("--scenario", default="")
    parser.add_argument("--transcript-path", default="")
    parser.add_argument("--keyword-text", default="")
    parser.add_argument("--project-rule-root", action="append", default=[])
    parser.add_argument("--global-rule-root", action="append", default=[])
    parser.add_argument("--generic-rule-root", action="append", default=[])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Manage ReMe rule lifecycle proposals and retirement.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List all rule manifests, including disabled rules")
    _add_context_args(list_parser)

    for name in ("disable", "retire"):
        action_parser = subparsers.add_parser(name, help="{} a rule with audit metadata".format(name))
        _add_context_args(action_parser)
        action_parser.add_argument("--rule-id", required=True)
        action_parser.add_argument("--reason", required=True)
        action_parser.add_argument("--replacement-rule-id", default="")
        action_parser.add_argument("--audit-log", default=str(DEFAULT_AUDIT_LOG))
        action_parser.add_argument("--allow-generic", action="store_true")

    args = parser.parse_args(argv)
    try:
        context = _merge_cli_context(args)
        rule_roots = context.get("rule_roots")
        if args.command == "list":
            result = list_rules(context=context, rule_roots=rule_roots)
        elif args.command == "disable":
            result = disable_rule(
                rule_id=args.rule_id,
                reason=args.reason,
                context=context,
                rule_roots=rule_roots,
                audit_log=Path(args.audit_log).expanduser().resolve(),
                replacement_rule_id=args.replacement_rule_id,
                allow_generic=args.allow_generic,
            )
        elif args.command == "retire":
            result = retire_rule(
                rule_id=args.rule_id,
                reason=args.reason,
                context=context,
                rule_roots=rule_roots,
                audit_log=Path(args.audit_log).expanduser().resolve(),
                replacement_rule_id=args.replacement_rule_id,
                allow_generic=args.allow_generic,
            )
        else:
            raise ValueError("unknown command: {}".format(args.command))
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if not result.get("errors") else 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": LIFECYCLE_SCHEMA_VERSION,
                    "state": "failed",
                    "error": {"type": exc.__class__.__name__, "message": str(exc)},
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
