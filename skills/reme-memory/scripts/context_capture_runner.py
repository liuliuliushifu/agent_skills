#!/usr/bin/env python3
import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from rule_registry import discover_rules
from transcript_context_extractor import (
    DEFAULT_ARTIFACT_ROOT,
    DEFAULT_COMPACT_MEMORY_ROOT,
    DEFAULT_HANDOFF_ROOT,
    MAX_CHUNK_CHARS,
    extract_context_from_lines,
    extract_context_from_transcript,
    load_transcript_text,
    write_extraction_outputs,
)


RUNNER_SCHEMA_VERSION = 1
RUNNER_VERSION = "0.1"


def run_capture(payload: Dict[str, Any]) -> Dict[str, Any]:
    warnings: List[str] = []
    errors: List[str] = []
    project = str(payload.get("project") or os.environ.get("REME_BUS_PROJECT") or "unknown")
    task = str(payload.get("task") or "Standard ReMe context capture")
    scenario = str(payload.get("scenario") or "PreCompact context capture")
    transcript_path_value = str(payload.get("transcript_path") or "")
    transcript_text = payload.get("transcript_text")
    artifact_root = Path(str(payload.get("artifact_root") or DEFAULT_ARTIFACT_ROOT)).expanduser().resolve()
    handoff_root = Path(str(payload.get("handoff_root") or DEFAULT_HANDOFF_ROOT)).expanduser().resolve()
    compact_memory_root = Path(str(payload.get("compact_memory_root") or DEFAULT_COMPACT_MEMORY_ROOT)).expanduser().resolve()
    capture_json_out = _optional_path(payload.get("capture_json_out"))
    write_modes = _string_list(payload.get("write_modes") or ["capture_json", "artifacts", "handoff", "compact_memory"])
    max_chunk_chars = int(payload.get("max_chunk_chars") or MAX_CHUNK_CHARS)

    context = {
        "project": project,
        "cwd": str(Path(str(payload.get("cwd") or os.getcwd())).expanduser().resolve()),
        "task": task,
        "scenario": scenario,
        "transcript_path": transcript_path_value,
        "keyword_text": str(payload.get("keyword_text") or "")[:16000],
        "rule_roots": payload.get("rule_roots") or {},
    }
    if transcript_text:
        context["transcript_excerpt"] = str(transcript_text)[:16000]

    registry = discover_rules(context=context, rule_roots=payload.get("rule_roots"))
    warnings.extend(registry.get("warnings", []))
    errors.extend(registry.get("errors", []))

    if transcript_text is not None:
        transcript_path = Path(str(payload.get("transcript_label") or "<inline>"))
        extraction = extract_context_from_lines(
            transcript_path=transcript_path,
            source_lines=load_transcript_text(str(transcript_text), source_format="inline"),
            project=project,
            task=task,
            scenario=scenario,
            thread_id=str(payload.get("thread_id") or ""),
            session_scope_key=str(payload.get("session_scope_key") or ""),
            artifact_root=artifact_root,
            max_chunk_chars=max_chunk_chars,
        )
    else:
        if not transcript_path_value:
            raise ValueError("entry payload must include transcript_path or transcript_text")
        if transcript_path_value == "-":
            transcript_path = Path("<stdin>")
            extraction = extract_context_from_lines(
                transcript_path=transcript_path,
                source_lines=load_transcript_text(sys.stdin.read(), source_format="stdin"),
                project=project,
                task=task,
                scenario=scenario,
                thread_id=str(payload.get("thread_id") or ""),
                session_scope_key=str(payload.get("session_scope_key") or ""),
                artifact_root=artifact_root,
                max_chunk_chars=max_chunk_chars,
            )
        else:
            transcript_path = Path(transcript_path_value).expanduser().resolve()
            extraction = extract_context_from_transcript(
                transcript_path=transcript_path,
                project=project,
                task=task,
                scenario=scenario,
                thread_id=str(payload.get("thread_id") or ""),
                session_scope_key=str(payload.get("session_scope_key") or ""),
                artifact_root=artifact_root,
                max_chunk_chars=max_chunk_chars,
            )

    write_result = write_extraction_outputs(
        extraction=extraction,
        artifact_root=artifact_root,
        handoff_root=handoff_root,
        capture_json_out=capture_json_out,
        write_handoff_files="handoff" in write_modes,
        compact_memory_root=compact_memory_root,
        write_compact_memory_file="compact_memory" in write_modes,
    )

    handoff = write_result.get("handoff") or {}
    compact_memory = write_result.get("compact_memory") or {}
    state = "partial" if errors else "ok"
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "run_id": str(payload.get("run_id") or write_result["capture_id"]),
        "state": state,
        "capture_id": write_result["capture_id"],
        "capture_json_path": write_result["capture_json_path"],
        "handoff_json_path": handoff.get("json_path"),
        "handoff_md_path": handoff.get("md_path"),
        "compact_memory_md_path": compact_memory.get("md_path"),
        "artifact_paths": write_result["artifact_paths"],
        "artifact_count": write_result["artifact_count"],
        "rules_loaded": registry.get("rules_loaded", []),
        "rule_roots": registry.get("rule_roots", {}),
        "rule_proposals": [],
        "warnings": warnings,
        "errors": errors,
    }


def failure_payload(payload: Dict[str, Any], exc: BaseException) -> Dict[str, Any]:
    return {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "run_id": str(payload.get("run_id") or ""),
        "state": "failed",
        "capture_id": None,
        "capture_json_path": None,
        "handoff_json_path": None,
        "handoff_md_path": None,
        "compact_memory_md_path": None,
        "artifact_paths": [],
        "artifact_count": 0,
        "rules_loaded": [],
        "rule_roots": {},
        "rule_proposals": [],
        "warnings": [],
        "errors": [
            {
                "type": exc.__class__.__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(limit=8),
            }
        ],
    }


def load_payload(path_value: str) -> Dict[str, Any]:
    raw = sys.stdin.read() if path_value == "-" else Path(path_value).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("entry payload must be a JSON object")
    return payload


def _optional_path(value: Any) -> Optional[Path]:
    if not value:
        return None
    return Path(str(value)).expanduser().resolve()


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Standard ReMe context capture runner.")
    parser.add_argument("--input-json", required=True, help="Standard entry JSON payload, or '-' for stdin")
    args = parser.parse_args(argv)

    payload: Dict[str, Any] = {}
    fail_open = False
    try:
        payload = load_payload(args.input_json)
        fail_open = bool(payload.get("fail_open", False))
        if args.input_json == "-" and payload.get("transcript_path") == "-":
            raise ValueError("input-json '-' and transcript_path '-' cannot share stdin")
        result = run_capture(payload)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["state"] in ("ok", "partial") else 1
    except Exception as exc:
        result = failure_payload(payload, exc)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if fail_open else 1


if __name__ == "__main__":
    raise SystemExit(main())
