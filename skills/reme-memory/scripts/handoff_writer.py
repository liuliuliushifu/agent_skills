#!/usr/bin/env python3
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from capture_schema import has_stable_session_scope


SCRIPT_DIR = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
DEFAULT_HANDOFF_ROOT = str(CODEX_HOME / "memories/reme-memory/handoffs")


def get_handoff_root(root: Optional[Path] = None) -> Path:
    if root is not None:
        return root
    return Path(os.environ.get("REME_HANDOFF_DIR", DEFAULT_HANDOFF_ROOT)).expanduser().resolve()


def write_handoff(capture: Dict[str, Any], handoff_root: Optional[Path] = None) -> Dict[str, Any]:
    root = get_handoff_root(handoff_root)
    root.mkdir(parents=True, exist_ok=True)

    stable_scope = has_stable_session_scope(capture)
    existing_json_path = _find_existing_json_path(root, capture["handoff_idempotency_key"]) if stable_scope else None
    if existing_json_path is not None:
        base_name = existing_json_path.stem
    else:
        created_date = str(capture["created_at"]).split("T", 1)[0]
        base_name = f"{created_date}-{capture['capture_id']}"

    json_path = root / f"{base_name}.json"
    md_path = root / f"{base_name}.md"
    raw_json_name = json_path.name
    overwrote = bool(existing_json_path or json_path.exists() or md_path.exists())

    _atomic_write_text(json_path, _render_capture_json(capture))
    _atomic_write_text(md_path, render_handoff_markdown(capture, raw_json_name))

    return {
        "capture_id": capture["capture_id"],
        "json_path": str(json_path),
        "md_path": str(md_path),
        "overwrote": overwrote,
        "stable_session_scope": stable_scope,
    }


def render_handoff_markdown(capture: Dict[str, Any], raw_json_name: str) -> str:
    sections = [
        ("Metadata", _render_metadata(capture)),
        ("Task", _render_task(capture)),
        ("Session Summary", _render_session_summary(capture)),
        ("Facts", _render_fact_lines(capture.get("facts", []))),
        ("Decisions", _render_decision_lines(capture.get("decisions", []))),
        ("Constraints", _render_simple_list(capture.get("constraints", []))),
        ("Open Issues", _render_simple_list(capture.get("open_issues", []))),
        ("Next Steps", _render_simple_list(capture.get("next_steps", []))),
        ("Key Files", _render_simple_list(capture.get("key_files", []), code=True)),
        ("Aliases", _render_simple_list(capture.get("aliases", []))),
        ("Audit", [f"- raw capture: {raw_json_name}"]),
    ]
    lines = [f"# ReMe Handoff - {capture['capture_id']}"]
    for title, body in sections:
        lines.append("")
        lines.append(f"## {title}")
        lines.append("")
        lines.extend(body)
    return "\n".join(lines) + "\n"


def _render_capture_json(capture: Dict[str, Any]) -> str:
    return json.dumps(capture, ensure_ascii=False, indent=2) + "\n"


def _render_metadata(capture: Dict[str, Any]) -> List[str]:
    return [
        f"- Schema Version: {capture['schema_version']}",
        f"- Capture ID: {capture['capture_id']}",
        f"- Handoff Idempotency Key: {capture['handoff_idempotency_key']}",
        f"- Durable Idempotency Key: {capture['durable_idempotency_key']}",
        f"- Capture Reason: {capture['capture_reason']}",
        f"- Project: {capture['project']}",
        f"- Thread ID: {capture.get('thread_id') or 'null'}",
        f"- Session Scope Key: {capture.get('session_scope_key') or 'null'}",
        f"- Durable Identity: {capture.get('durable_identity') or 'null'}",
        f"- Created At: {capture['created_at']}",
    ]


def _render_task(capture: Dict[str, Any]) -> List[str]:
    lines = [capture["task"]]
    if capture.get("scenario"):
        lines.extend(["", f"Scenario: {capture['scenario']}"])
    if capture.get("subsystem"):
        lines.append(f"Subsystem: {', '.join(capture['subsystem'])}")
    return lines


def _render_session_summary(capture: Dict[str, Any]) -> List[str]:
    lines = [capture["session_summary"]]
    if capture.get("retrieval_surface"):
        lines.extend(["", f"Retrieval Surface: {capture['retrieval_surface']}"])
    if capture.get("errors"):
        lines.extend(["", "Known Errors:"])
        lines.extend(_render_error_lines(capture["errors"]))
    if capture.get("benchmarks"):
        lines.extend(["", "Benchmarks:"])
        lines.extend(_render_benchmark_lines(capture["benchmarks"]))
    return lines


def _render_fact_lines(items: List[Dict[str, str]]) -> List[str]:
    if not items:
        return ["- None"]
    lines: List[str] = []
    for item in items:
        details: List[str] = []
        if item.get("evidence"):
            details.append(f"evidence: {item['evidence']}")
        if item.get("confidence"):
            details.append(f"confidence: {item['confidence']}")
        suffix = f" ({'; '.join(details)})" if details else ""
        lines.append(f"- {item['text']}{suffix}")
    return lines


def _render_decision_lines(items: List[Dict[str, str]]) -> List[str]:
    if not items:
        return ["- None"]
    lines: List[str] = []
    for item in items:
        details: List[str] = []
        if item.get("status"):
            details.append(f"status: {item['status']}")
        if item.get("rationale"):
            details.append(f"rationale: {item['rationale']}")
        suffix = f" ({'; '.join(details)})" if details else ""
        lines.append(f"- {item['decision']}{suffix}")
    return lines


def _render_error_lines(items: List[Dict[str, str]]) -> List[str]:
    lines: List[str] = []
    for item in items:
        details: List[str] = []
        if item.get("root_cause"):
            details.append(f"root cause: {item['root_cause']}")
        if item.get("workaround"):
            details.append(f"workaround: {item['workaround']}")
        suffix = f" ({'; '.join(details)})" if details else ""
        lines.append(f"- {item['fingerprint']}{suffix}")
    return lines or ["- None"]


def _render_benchmark_lines(items: List[Dict[str, str]]) -> List[str]:
    lines: List[str] = []
    for item in items:
        details: List[str] = []
        if item.get("value"):
            details.append(f"value: {item['value']}")
        if item.get("notes"):
            details.append(f"notes: {item['notes']}")
        suffix = f" ({'; '.join(details)})" if details else ""
        lines.append(f"- {item['name']}{suffix}")
    return lines or ["- None"]


def _render_simple_list(items: List[str], code: bool = False) -> List[str]:
    if not items:
        return ["- None"]
    if code:
        return [f"- `{item}`" for item in items]
    return [f"- {item}" for item in items]


def _find_existing_json_path(root: Path, handoff_idempotency_key: str) -> Optional[Path]:
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("handoff_idempotency_key") == handoff_idempotency_key:
            return path
    return None


def _atomic_write_text(path: Path, text: str) -> None:
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)
