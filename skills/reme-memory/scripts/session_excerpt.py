#!/usr/bin/env python3
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
DEFAULT_EXCERPT_ROOT = CODEX_HOME / "memories/reme-memory/excerpts"

EXCERPT_SCHEMA_VERSION = 1
EXCERPT_VERSION = "0.1"
DEFAULT_MAX_CHARS = 64000
DEFAULT_PER_RECORD_MAX_CHARS = 12000
DEFAULT_CONTEXT_BEFORE = 2
DEFAULT_CONTEXT_AFTER = 2
DEFAULT_FALLBACK_LAST_RECORDS = 80
MAX_REFINE_EVIDENCE_ITEMS = 24
MAX_REFINE_EVIDENCE_TEXT_CHARS = 2048
MAX_REFINE_PAYLOAD_BYTES = 52 * 1024

SKIP_MESSAGE_ROLES = {"developer", "system"}
SENSITIVE_KEYS = {"base_instructions", "encrypted_content"}
VALUE_PATTERN_RE = re.compile(
    r"("
    r"\bA\d+\.\d+(?:[-_][A-Za-z0-9_.-]+)?\b|"
    r"\bTRK-\d{8}-\d+\b|"
    r"\b[A-Z][A-Z0-9]+-\d+\b|"
    r"\d[\d,]*(?:\.\d+)?\s*(?:Mpps|mpps|Kpps|kpps|pps|Gbps|Mbps|%)\b|"
    r"baseline|基线|下一步|待办|决定|确认|结论|close|关闭"
    r")",
    re.IGNORECASE,
)


@dataclass
class TranscriptRecord:
    index: int
    line_no: int
    timestamp: str
    source_type: str
    payload_type: str
    role: str
    phase: str
    name: str
    call_id: str
    turn_id: str
    text: str


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def today_text() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: str) -> str:
    return "{}_{}".format(prefix, sha256_text("\n".join(parts))[:16])


def load_payload(path_value: str) -> Dict[str, Any]:
    if not path_value:
        return {}
    raw = sys.stdin.read() if path_value == "-" else Path(path_value).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("input payload must be a JSON object")
    return payload


def load_transcript_records(path: Path, include_system: bool = False) -> Tuple[List[TranscriptRecord], Dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        return load_codex_jsonl_records(path, include_system=include_system)
    return load_text_records(path)


def load_text_records(path: Path) -> Tuple[List[TranscriptRecord], Dict[str, Any]]:
    records: List[TranscriptRecord] = []
    for idx, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        if not line.strip():
            continue
        records.append(
            TranscriptRecord(
                index=len(records),
                line_no=idx,
                timestamp="",
                source_type="text",
                payload_type="line",
                role="",
                phase="",
                name="",
                call_id="",
                turn_id="",
                text=line.rstrip("\n"),
            )
        )
    return records, {"transcript_format": "text"}


def load_codex_jsonl_records(path: Path, include_system: bool = False) -> Tuple[List[TranscriptRecord], Dict[str, Any]]:
    records: List[TranscriptRecord] = []
    metadata: Dict[str, Any] = {"transcript_format": "codex-jsonl"}
    for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            records.append(
                TranscriptRecord(
                    index=len(records),
                    line_no=line_no,
                    timestamp="",
                    source_type="jsonl/raw",
                    payload_type="raw",
                    role="",
                    phase="",
                    name="",
                    call_id="",
                    turn_id="",
                    text=line.rstrip("\n"),
                )
            )
            continue

        if not isinstance(row, dict):
            continue
        source_type = str(row.get("type") or "")
        timestamp = str(row.get("timestamp") or "")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}

        if source_type == "session_meta":
            if isinstance(payload, dict):
                metadata["session_id"] = payload.get("id") or metadata.get("session_id")
                metadata["cwd"] = payload.get("cwd") or metadata.get("cwd")
                metadata["cli_version"] = payload.get("cli_version") or metadata.get("cli_version")
                metadata["model_provider"] = payload.get("model_provider") or metadata.get("model_provider")
            continue

        record = record_from_codex_row(
            line_no=line_no,
            timestamp=timestamp,
            source_type=source_type,
            payload=payload,
            include_system=include_system,
        )
        if record is None:
            continue
        record.index = len(records)
        records.append(record)
    return records, metadata


def record_from_codex_row(
    line_no: int,
    timestamp: str,
    source_type: str,
    payload: Dict[str, Any],
    include_system: bool,
) -> Optional[TranscriptRecord]:
    payload_type = str(payload.get("type") or "")
    role = str(payload.get("role") or "")
    phase = str(payload.get("phase") or "")
    name = str(payload.get("name") or "")
    call_id = str(payload.get("call_id") or "")
    turn_id = str(payload.get("turn_id") or "")
    text = ""

    if source_type == "response_item":
        if payload_type == "message":
            if role in SKIP_MESSAGE_ROLES and not include_system:
                return None
            text = extract_text_from_content(payload.get("content"))
        elif payload_type == "function_call":
            args = payload.get("arguments")
            text = "CALL {}".format(name or "<tool>")
            if args:
                text += "\n" + _stringify(args)
        elif payload_type == "function_call_output":
            text = extract_text_from_content(payload.get("output"))
        elif payload_type == "custom_tool_call":
            text = "CALL {}".format(name or "<custom_tool>")
            if payload.get("input") is not None:
                text += "\n" + _stringify(payload.get("input"))
        elif payload_type == "custom_tool_call_output":
            text = extract_text_from_content(payload.get("output"))
        elif payload_type == "reasoning":
            text = extract_text_from_content(payload.get("summary") or payload.get("content"))
        else:
            text = extract_text_from_content(payload)
    elif source_type == "event_msg":
        event_type = str(payload.get("type") or payload_type)
        payload_type = event_type
        if event_type in {"agent_message", "user_message"}:
            role = "assistant" if event_type == "agent_message" else "user"
            text = str(payload.get("message") or "")
        else:
            return None
    else:
        text = extract_text_from_content(payload)

    text = text.strip()
    if not text:
        return None
    return TranscriptRecord(
        index=-1,
        line_no=line_no,
        timestamp=timestamp,
        source_type=source_type,
        payload_type=payload_type,
        role=role,
        phase=phase,
        name=name,
        call_id=call_id,
        turn_id=turn_id,
        text=text,
    )


def extract_text_from_content(value: Any) -> str:
    pieces: List[str] = []
    _collect_text(value, pieces)
    return "\n".join(piece for piece in pieces if piece.strip())


def _collect_text(value: Any, pieces: List[str]) -> None:
    if value is None:
        return
    if isinstance(value, str):
        if value.strip():
            pieces.append(value)
        return
    if isinstance(value, list):
        for item in value:
            _collect_text(item, pieces)
        return
    if not isinstance(value, dict):
        return

    preferred_keys = ["text", "input_text", "output_text", "message", "output", "result", "summary"]
    for key in preferred_keys:
        if key in value:
            _collect_text(value.get(key), pieces)
    if pieces:
        return
    for key, nested in value.items():
        if key in SENSITIVE_KEYS:
            continue
        _collect_text(nested, pieces)


def select_records(
    records: List[TranscriptRecord],
    keywords: Sequence[str],
    context_before: int,
    context_after: int,
    fallback_last_records: int,
    max_chars: int,
    per_record_max_chars: int,
    include_value_patterns: bool,
) -> Tuple[List[TranscriptRecord], Dict[int, str], List[str]]:
    warnings: List[str] = []
    normalized_keywords = normalize_keywords(keywords)
    priority: Dict[int, int] = {}
    reason: Dict[int, str] = {}

    for idx, record in enumerate(records):
        if record_matches_keywords(record, normalized_keywords):
            priority[idx] = max(priority.get(idx, 0), 100)
            reason[idx] = "keyword"
        elif include_value_patterns and record_can_trigger_value_pattern(record) and VALUE_PATTERN_RE.search(record.text):
            priority[idx] = max(priority.get(idx, 0), 60)
            reason[idx] = "value-pattern"

    if priority:
        target_indexes = sorted(priority.keys())
        for idx in target_indexes:
            for neighbor in range(max(0, idx - context_before), min(len(records), idx + context_after + 1)):
                if neighbor not in priority:
                    if not is_safe_context_record(records[neighbor]):
                        continue
                    priority[neighbor] = 20
                    reason[neighbor] = "context"
    else:
        start = max(0, len(records) - fallback_last_records)
        for idx in range(start, len(records)):
            priority[idx] = 10
            reason[idx] = "fallback-tail"
        warnings.append("no keyword or value-pattern match; selected fallback tail records")

    selected_indexes = dedupe_indexes(records, fit_indexes_to_budget(records, priority, max_chars, per_record_max_chars))
    selected_records = [records[idx] for idx in sorted(selected_indexes)]
    selected_reason = {idx: reason.get(idx, "selected") for idx in selected_indexes}
    return selected_records, selected_reason, warnings


def fit_indexes_to_budget(
    records: List[TranscriptRecord],
    priority: Dict[int, int],
    max_chars: int,
    per_record_max_chars: int,
) -> Set[int]:
    ranked = sorted(priority.keys(), key=lambda idx: (priority[idx], records[idx].line_no), reverse=True)
    kept: Set[int] = set()
    total = 0
    for idx in ranked:
        rendered_len = len(render_record(records[idx], {}, [], per_record_max_chars))
        if total + rendered_len > max_chars and kept:
            continue
        kept.add(idx)
        total += rendered_len
        if total >= max_chars:
            break
    return kept


def dedupe_indexes(records: List[TranscriptRecord], indexes: Set[int]) -> Set[int]:
    kept: Set[int] = set()
    seen: Set[str] = set()
    for idx in sorted(indexes):
        digest = sha256_text(_normalize_record_text(records[idx].text))
        if digest in seen:
            continue
        seen.add(digest)
        kept.add(idx)
    return kept


def _normalize_record_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def record_matches_keywords(record: TranscriptRecord, keywords: Sequence[str]) -> bool:
    if not keywords:
        return False
    haystack = record.text.lower()
    return any(keyword.lower() in haystack for keyword in keywords if keyword)


def is_safe_context_record(record: TranscriptRecord) -> bool:
    if record.payload_type in {"function_call_output", "custom_tool_call_output"} and len(record.text) > 2000:
        return False
    if record.payload_type in {"function_call_output", "custom_tool_call_output"} and looks_like_source_or_test_output(record.text):
        return False
    return True


def record_can_trigger_value_pattern(record: TranscriptRecord) -> bool:
    if record.payload_type not in {"function_call_output", "custom_tool_call_output"}:
        return True
    return not looks_like_source_or_test_output(record.text)


def looks_like_source_or_test_output(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    normalized = re.sub(r"(?m)^[+-]", "", stripped)
    markers = [
        "def test_",
        "class Test",
        "self.assert",
        "unittest",
        "pytest",
        "json.dumps",
        "write_text(",
        "transcript =",
        "artifact_payloads =",
    ]
    marker_hits = sum(1 for marker in markers if marker in normalized)
    lines = [re.sub(r"^[+-]", "", line.strip()).strip() for line in stripped.splitlines() if line.strip()]
    quoted_metric_lines = sum(
        1
        for line in lines
        if line.startswith(("\"", "'")) and VALUE_PATTERN_RE.search(line)
    )
    code_like_lines = sum(
        1
        for line in lines
        if line.startswith(("def ", "class ", "self.", "with ", "for ", "if ", "return ", "[", "]", "{", "}"))
    )
    return marker_hits >= 2 or (quoted_metric_lines > 0 and code_like_lines >= 2)


def normalize_keywords(values: Sequence[str]) -> List[str]:
    keywords: List[str] = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        if len(text) > 160:
            continue
        if text not in keywords:
            keywords.append(text)
    return keywords


def render_excerpt(
    records: List[TranscriptRecord],
    reasons: Dict[int, str],
    keywords: Sequence[str],
    metadata: Dict[str, Any],
    source_path: Path,
    per_record_max_chars: int,
) -> str:
    lines = [
        "# ReMe Session Excerpt",
        "",
        "schema_version: {}".format(EXCERPT_SCHEMA_VERSION),
        "excerpt_version: {}".format(EXCERPT_VERSION),
        "created_at: {}".format(now_iso()),
        "source_transcript_path: {}".format(source_path),
        "transcript_format: {}".format(metadata.get("transcript_format", "unknown")),
        "session_id: {}".format(metadata.get("session_id") or ""),
        "cwd: {}".format(metadata.get("cwd") or ""),
        "keywords: {}".format(", ".join(keywords)),
        "selected_records: {}".format(len(records)),
        "",
        "## Selected Transcript",
        "",
    ]
    for record in records:
        lines.append(render_record(record, reasons, keywords, per_record_max_chars))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_record(
    record: TranscriptRecord,
    reasons: Dict[int, str],
    keywords: Sequence[str],
    per_record_max_chars: int,
) -> str:
    header_bits = [
        "line={}".format(record.line_no),
        "source={}".format(record.source_type),
    ]
    if record.payload_type:
        header_bits.append("type={}".format(record.payload_type))
    if record.role:
        header_bits.append("role={}".format(record.role))
    if record.phase:
        header_bits.append("phase={}".format(record.phase))
    if record.name:
        header_bits.append("name={}".format(record.name))
    if record.turn_id:
        header_bits.append("turn_id={}".format(record.turn_id))
    if reasons.get(record.index):
        header_bits.append("reason={}".format(reasons[record.index]))
    if record.timestamp:
        header_bits.append("timestamp={}".format(record.timestamp))
    text = trim_record_text(record.text, keywords, per_record_max_chars)
    return "[{}]\n{}".format(" ".join(header_bits), text)


def trim_record_text(text: str, keywords: Sequence[str], max_chars: int) -> str:
    if len(text) <= max_chars:
        return text

    lowered = text.lower()
    positions: List[int] = []
    for keyword in keywords:
        needle = keyword.lower()
        start = 0
        while needle:
            found = lowered.find(needle, start)
            if found < 0:
                break
            positions.append(found)
            start = found + max(1, len(needle))
    if not positions:
        return text[:max_chars] + "\n[...record truncated...]"

    positions = sorted(set(positions))[:8]
    window = max(800, max_chars // max(1, len(positions)))
    ranges: List[Tuple[int, int]] = []
    for pos in positions:
        half = window // 2
        ranges.append((max(0, pos - half), min(len(text), pos + half)))
    merged = merge_ranges(ranges)
    pieces = []
    used = 0
    for start, end in merged:
        piece = text[start:end]
        if used + len(piece) > max_chars:
            piece = piece[: max(0, max_chars - used)]
        if start > 0:
            piece = "[...record trimmed before match...]\n" + piece
        if end < len(text):
            piece += "\n[...record trimmed after match...]"
        pieces.append(piece)
        used += len(piece)
        if used >= max_chars:
            break
    return "\n--- excerpt window ---\n".join(piece for piece in pieces if piece)


def merge_ranges(ranges: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    merged: List[Tuple[int, int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def build_runner_payload(
    payload: Dict[str, Any],
    args: argparse.Namespace,
    excerpt_path: Path,
    source_transcript_path: Path,
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    runner = dict(payload)
    runner["schema_version"] = int(runner.get("schema_version") or 1)
    runner["run_reason"] = args.run_reason or runner.get("run_reason") or _default_run_reason(payload)
    runner["project"] = args.project or runner.get("project") or os.environ.get("REME_BUS_PROJECT") or "unknown"
    runner["cwd"] = args.cwd or runner.get("cwd") or metadata.get("cwd") or os.getcwd()
    runner["task"] = args.task or runner.get("task") or "ReMe session excerpt capture"
    runner["scenario"] = args.scenario or runner.get("scenario") or "PreCompact session excerpt"
    runner["thread_id"] = args.thread_id or runner.get("thread_id") or runner.get("session_id") or metadata.get("session_id") or ""
    runner["session_scope_key"] = args.session_scope_key or runner.get("session_scope_key") or ""
    runner["source_transcript_path"] = str(source_transcript_path)
    runner["transcript_excerpt_path"] = str(excerpt_path)
    runner["transcript_path"] = str(excerpt_path)
    runner.setdefault("write_modes", ["capture_json", "artifacts", "handoff", "compact_memory"])
    runner.setdefault("fail_open", bool(payload.get("hook_event_name")))
    return runner


def build_refine_payload(
    payload: Dict[str, Any],
    args: argparse.Namespace,
    records: List[TranscriptRecord],
    reasons: Dict[int, str],
    excerpt_text: str,
    excerpt_path: Path,
    source_transcript_path: Path,
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    refine_payload = {
        "parent_capture_id": str(payload.get("run_id") or payload.get("capture_id") or ""),
        "capture_id": str(payload.get("capture_id") or ""),
        "thread_id": args.thread_id or payload.get("session_id") or metadata.get("session_id") or "",
        "owner_session_id": args.session_scope_key or payload.get("session_id") or metadata.get("session_id") or "",
        "source_excerpt_hash": "sha256:" + sha256_text(excerpt_text),
        "source_excerpt_path": str(excerpt_path),
        "source_transcript_path": str(source_transcript_path),
        "task": args.task or payload.get("task") or "ReMe session evidence refine",
        "scenario": args.scenario or payload.get("scenario") or "PreCompact evidence refine",
        "evidence": [],
        "tags": ["reme-refine", "precompact"],
        "max_attempts": 3,
    }
    evidence = []
    for record in records:
        if len(evidence) >= MAX_REFINE_EVIDENCE_ITEMS:
            break
        reason = reasons.get(record.index, "selected")
        if reason == "fallback-tail":
            continue
        text = trim_record_text(
            record.text,
            [],
            min(args.per_record_max_chars, MAX_REFINE_EVIDENCE_TEXT_CHARS),
        ).strip()
        if not text:
            continue
        candidate = {
            "evidence_id": "line-{}".format(record.line_no),
            "reason": reason,
            "line_range": [record.line_no, record.line_no],
            "evidence_hash": "sha256:" + sha256_text(text),
            "text": text,
        }
        candidate_payload = {**refine_payload, "evidence": [*evidence, candidate]}
        encoded = json.dumps(candidate_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_REFINE_PAYLOAD_BYTES:
            break
        evidence.append(candidate)
    refine_payload["evidence"] = evidence
    return refine_payload


def write_outputs(
    excerpt_text: str,
    metadata_payload: Dict[str, Any],
    output_path: Optional[Path],
    metadata_path: Optional[Path],
    runner_payload: Optional[Dict[str, Any]],
    runner_input_path: Optional[Path],
) -> Dict[str, Any]:
    excerpt_id = stable_id(
        "excerpt",
        metadata_payload.get("source_transcript_path", ""),
        json.dumps(metadata_payload.get("keywords", []), ensure_ascii=False),
        json.dumps(metadata_payload.get("selected_line_ranges", []), ensure_ascii=False),
        sha256_text(excerpt_text),
    )
    if output_path is None:
        output_path = DEFAULT_EXCERPT_ROOT / "{}-{}.txt".format(today_text(), excerpt_id)
    if metadata_path is None:
        metadata_path = output_path.with_suffix(".json")

    metadata_payload["excerpt_id"] = excerpt_id
    metadata_payload["excerpt_path"] = str(output_path)
    metadata_payload["metadata_path"] = str(metadata_path)

    _atomic_write_text(output_path, excerpt_text)
    _atomic_write_text(metadata_path, json.dumps(metadata_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    runner_input_json_path = None
    if runner_payload is not None:
        if runner_input_path is None:
            runner_input_path = output_path.with_name(output_path.stem + ".runner-input.json")
        _atomic_write_text(runner_input_path, json.dumps(runner_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        runner_input_json_path = str(runner_input_path)

    return {
        "excerpt_id": excerpt_id,
        "excerpt_path": str(output_path),
        "metadata_path": str(metadata_path),
        "runner_input_json_path": runner_input_json_path,
    }


def run_excerpt(payload: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    transcript_path = resolve_transcript_path(payload, args)
    records, source_metadata = load_transcript_records(transcript_path, include_system=args.include_system)
    keywords = extract_keywords(payload, args)
    include_value_patterns = args.include_value_patterns
    if args.no_value_patterns:
        include_value_patterns = False
    if not keywords and not args.no_value_patterns:
        include_value_patterns = True

    selected, reasons, warnings = select_records(
        records=records,
        keywords=keywords,
        context_before=args.context_before,
        context_after=args.context_after,
        fallback_last_records=args.fallback_last_records,
        max_chars=args.max_chars,
        per_record_max_chars=args.per_record_max_chars,
        include_value_patterns=include_value_patterns,
    )
    selected_line_ranges = [[record.line_no, record.line_no] for record in selected]
    metadata_payload: Dict[str, Any] = {
        "schema_version": EXCERPT_SCHEMA_VERSION,
        "excerpt_version": EXCERPT_VERSION,
        "created_at": now_iso(),
        "state": "ok",
        "source_transcript_path": str(transcript_path),
        "transcript_format": source_metadata.get("transcript_format", "unknown"),
        "session_id": payload.get("session_id") or source_metadata.get("session_id"),
        "hook_event_name": payload.get("hook_event_name"),
        "cwd": payload.get("cwd") or source_metadata.get("cwd"),
        "keywords": keywords,
        "include_value_patterns": include_value_patterns,
        "source_record_count": len(records),
        "selected_record_count": len(selected),
        "selected_line_ranges": selected_line_ranges,
        "selection_reasons": {str(record.line_no): reasons.get(record.index, "selected") for record in selected},
        "warnings": warnings,
        "errors": [],
    }
    excerpt_text = render_excerpt(
        records=selected,
        reasons=reasons,
        keywords=keywords,
        metadata={**source_metadata, **payload},
        source_path=transcript_path,
        per_record_max_chars=args.per_record_max_chars,
    )
    metadata_payload["selected_char_count"] = len(excerpt_text)

    output_path = _optional_path(args.output)
    metadata_path = _optional_path(args.metadata_out)
    runner_payload = None
    if args.runner_input_json_out:
        runner_payload = build_runner_payload(
            payload=payload,
            args=args,
            excerpt_path=output_path or DEFAULT_EXCERPT_ROOT / "pending.txt",
            source_transcript_path=transcript_path,
            metadata=source_metadata,
        )

    write_result = write_outputs(
        excerpt_text=excerpt_text,
        metadata_payload=metadata_payload,
        output_path=output_path,
        metadata_path=metadata_path,
        runner_payload=None,
        runner_input_path=None,
    )

    if args.runner_input_json_out:
        actual_excerpt_path = Path(write_result["excerpt_path"])
        runner_payload = build_runner_payload(
            payload=payload,
            args=args,
            excerpt_path=actual_excerpt_path,
            source_transcript_path=transcript_path,
            metadata=source_metadata,
        )
        runner_input_path = _optional_path(args.runner_input_json_out)
        _atomic_write_text(
            runner_input_path,
            json.dumps(runner_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        write_result["runner_input_json_path"] = str(runner_input_path)

    if args.refine_json_out:
        actual_excerpt_path = Path(write_result["excerpt_path"])
        refine_payload = build_refine_payload(
            payload=payload,
            args=args,
            records=selected,
            reasons=reasons,
            excerpt_text=excerpt_text,
            excerpt_path=actual_excerpt_path,
            source_transcript_path=transcript_path,
            metadata=source_metadata,
        )
        refine_json_path = _optional_path(args.refine_json_out)
        if refine_payload.get("evidence"):
            _atomic_write_text(
                refine_json_path,
                json.dumps(refine_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
            write_result["refine_json_path"] = str(refine_json_path)
        else:
            if refine_json_path is not None:
                refine_json_path.unlink(missing_ok=True)
            warnings.append("no evidence selected; skipped refine JSON")

    return {
        "schema_version": EXCERPT_SCHEMA_VERSION,
        "excerpt_version": EXCERPT_VERSION,
        "state": "ok",
        "excerpt_id": write_result["excerpt_id"],
        "source_transcript_path": str(transcript_path),
        "excerpt_path": write_result["excerpt_path"],
        "metadata_path": write_result["metadata_path"],
        "runner_input_json_path": write_result.get("runner_input_json_path"),
        "refine_json_path": write_result.get("refine_json_path"),
        "source_record_count": len(records),
        "selected_record_count": len(selected),
        "selected_char_count": len(excerpt_text),
        "keywords": keywords,
        "warnings": warnings,
        "errors": [],
    }


def failure_payload(payload: Dict[str, Any], exc: BaseException) -> Dict[str, Any]:
    return {
        "schema_version": EXCERPT_SCHEMA_VERSION,
        "excerpt_version": EXCERPT_VERSION,
        "state": "failed",
        "excerpt_id": None,
        "source_transcript_path": str(payload.get("transcript_path") or ""),
        "excerpt_path": None,
        "metadata_path": None,
        "runner_input_json_path": None,
        "source_record_count": 0,
        "selected_record_count": 0,
        "selected_char_count": 0,
        "keywords": [],
        "warnings": [],
        "errors": [
            {
                "type": exc.__class__.__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(limit=8),
            }
        ],
    }


def resolve_transcript_path(payload: Dict[str, Any], args: argparse.Namespace) -> Path:
    value = args.transcript_path or payload.get("transcript_path")
    if not value:
        raise ValueError("transcript path is required via --transcript-path or input JSON transcript_path")
    if str(value) == "-":
        raise ValueError("session_excerpt.py requires a materialized transcript path; stdin transcript is not supported")
    return Path(str(value)).expanduser().resolve()


def extract_keywords(payload: Dict[str, Any], args: argparse.Namespace) -> List[str]:
    raw: List[str] = []
    raw.extend(args.keyword or [])
    raw.extend(_payload_string_list(payload.get("keywords")))
    raw.extend(_payload_string_list(payload.get("keyword_text")))
    if args.use_prompt_keywords:
        raw.extend(extract_prompt_keywords(payload.get("prompt")))
    raw.extend(_payload_string_list(args.keyword_text))
    return normalize_keywords(raw)


def extract_prompt_keywords(prompt: Any) -> List[str]:
    text = str(prompt or "").strip()
    if not text:
        return []
    chunks = re.split(r"[\s,，。；;:：]+", text)
    return [chunk for chunk in chunks if len(chunk) >= 4 and len(chunk) <= 80]


def _payload_string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    text = str(value)
    if "\n" in text:
        return [line.strip() for line in text.splitlines() if line.strip()]
    return [text.strip()] if text.strip() else []


def _default_run_reason(payload: Dict[str, Any]) -> str:
    hook_event = payload.get("hook_event_name")
    if hook_event:
        return "hook_{}".format(str(hook_event).lower())
    return "offline_excerpt"


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _optional_path(value: Any) -> Optional[Path]:
    if not value:
        return None
    return Path(str(value)).expanduser().resolve()


def _atomic_write_text(path: Optional[Path], text: str) -> None:
    if path is None:
        raise ValueError("output path is required")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a bounded plaintext excerpt from a Codex session transcript.")
    parser.add_argument("--input-json", default="", help="Hook/entry JSON object, or '-' for stdin")
    parser.add_argument("--transcript-path", default="", help="Codex rollout JSONL or plain text transcript")
    parser.add_argument("--keyword", action="append", default=[], help="Select records containing this keyword")
    parser.add_argument("--keyword-text", default="", help="Additional keyword text or newline-separated keywords")
    parser.add_argument("--use-prompt-keywords", action="store_true", help="Derive keywords from input JSON prompt")
    parser.add_argument("--include-value-patterns", action="store_true", help="Also select built-in high-value patterns")
    parser.add_argument("--no-value-patterns", action="store_true", help="Disable built-in high-value pattern fallback")
    parser.add_argument("--context-before", type=int, default=DEFAULT_CONTEXT_BEFORE)
    parser.add_argument("--context-after", type=int, default=DEFAULT_CONTEXT_AFTER)
    parser.add_argument("--fallback-last-records", type=int, default=DEFAULT_FALLBACK_LAST_RECORDS)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--per-record-max-chars", type=int, default=DEFAULT_PER_RECORD_MAX_CHARS)
    parser.add_argument("--output", default="", help="Excerpt text output path")
    parser.add_argument("--metadata-out", default="", help="Excerpt metadata JSON output path")
    parser.add_argument("--runner-input-json-out", default="", help="Write context_capture_runner.py input JSON")
    parser.add_argument("--refine-json-out", default="", help="Write evidence-only async refine JSON")
    parser.add_argument("--include-system", action="store_true", help="Include developer/system messages")
    parser.add_argument("--fail-open", action="store_true", help="Return success even when excerpt generation fails")
    parser.add_argument("--project", default="")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--task", default="")
    parser.add_argument("--scenario", default="")
    parser.add_argument("--thread-id", default="")
    parser.add_argument("--session-scope-key", default="")
    parser.add_argument("--run-reason", default="")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    payload: Dict[str, Any] = {}
    try:
        payload = load_payload(args.input_json)
        if args.input_json == "-" and payload.get("transcript_path") == "-":
            raise ValueError("input-json '-' and transcript_path '-' cannot share stdin")
        result = run_excerpt(payload, args)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        result = failure_payload(payload, exc)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        fail_open = bool(args.fail_open or payload.get("fail_open") or payload.get("hook_event_name"))
        return 0 if fail_open else 1


if __name__ == "__main__":
    raise SystemExit(main())
