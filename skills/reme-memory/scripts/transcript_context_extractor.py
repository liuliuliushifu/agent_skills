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

from capture_schema import normalize_capture_input
from handoff_writer import write_handoff


SCRIPT_DIR = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
DEFAULT_ARTIFACT_ROOT = CODEX_HOME / "memories/reme-memory/artifacts"
DEFAULT_HANDOFF_ROOT = CODEX_HOME / "memories/reme-memory/handoffs"
USER_ROOT = Path(os.environ.get("CODEX_USER_HOME", str(CODEX_HOME.parent)))
REME_HOME = Path(os.environ.get("REME_HOME", str(USER_ROOT / ".local/share/reme")))
REME_WORKDIR = Path(os.environ.get("REME_WORKDIR", str(REME_HOME / "light_data")))
DEFAULT_COMPACT_MEMORY_ROOT = Path(os.environ.get("REME_COMPACT_MEMORY_DIR", str(REME_WORKDIR / "compact_memory")))

ARTIFACT_SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "0.1"
MAX_CHUNK_CHARS = 16000
MAX_CAPTURE_ITEMS = 32

COMMIT_RE = re.compile(r"\b(?=[0-9a-f]{7,40}\b)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b", re.IGNORECASE)
PATH_RE = re.compile(r"(?<![\w.-])(?:/localdata|/home|/tmp)/[^\s`'\"<>]+")
TRK_RE = re.compile(r"\bTRK-\d{8}-\d+\b")
JIRA_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
METRIC_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9_./-]{1,40})\s*[=:]\s*"
    r"(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>Mpps|Kpps|pps|Gbps|Mbps|ms|us|%|percent)?",
    re.IGNORECASE,
)
ENTRY_PERF_RE = re.compile(
    r"(?P<count>\d+)\s*(?:[- ]?\s*entry(?:\s*连续)?|(?:个|条)?\s*(?:条目|表项|入口)(?:\s*连续)?)"
    r"[^0-9\n]{0,40}"
    r"(?P<value>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*"
    r"(?P<unit>Mpps|mpps|Kpps|kpps|pps|entries/s)",
    re.IGNORECASE,
)
PERF_KEYWORD_RE = re.compile(
    r"\b(packet|rx|tx|pps|mpps|gbps|throughput|drop|latency|packet|pkt|queue|qps|cpu|numa)\b"
    r"|性能|吞吐|丢包|时延|延迟|包长|队列|条目|表项|入口",
    re.IGNORECASE,
)
PERFORMANCE_METRIC_KEYS = {
    "rx_mpps",
    "tx_mpps",
    "rx_pps",
    "tx_pps",
    "pps",
    "mpps",
    "gbps",
    "throughput",
    "drop",
    "drop_rate",
    "latency",
    "latency_us",
    "latency_ms",
    "cpu",
    "cpu_pct",
    "cpu_percent",
    "entry_count",
}
DECISION_RE = re.compile(
    r"(以后|保持|不要|必须|标准|默认|\bclose\b|关闭|基线|baseline|确认|决定|建议)",
    re.IGNORECASE,
)
NEXT_STEP_RE = re.compile(r"(下一步|后续|待办|todo|open issue|需要|计划)", re.IGNORECASE)
SOURCE_FIXTURE_RE = re.compile(
    r"(^|\n)\s*(def\s+test_|class\s+Test|self\.assert|pytest|unittest|json\.dumps|write_text\(|"
    r"transcript\s*=|records\s*=|artifact_payloads\s*=)",
    re.IGNORECASE,
)
SOURCE_LINE_RE = re.compile(
    r"^\s*(def\s+|class\s+|from\s+|import\s+|self\.|assert\s+|with\s+|for\s+|if\s+|elif\s+|else:|return\b|"
    r"[\]\}\)],?\s*$)",
    re.IGNORECASE,
)

FIELD_ALIASES = {
    "version": {"version", "ver", "版本", "image", "build", "build_id"},
    "commit": {"commit", "sha", "revision", "rev"},
    "packet_size": {"pkt_size", "packet_size", "packet_bytes", "包长"},
    "queues": {"queue", "queues", "rxq", "txq", "队列"},
    "cpu": {"cpu", "cpu_pct", "cpu_percent", "cpu_util", "cpu_utilization"},
    "throughput": {"throughput", "rx_mpps", "tx_mpps", "rx_pps", "tx_pps", "pps", "mpps", "gbps", "rate", "吞吐"},
    "drop": {"drop", "drops", "drop_rate", "loss", "lost", "丢包"},
    "latency": {"latency", "latency_us", "latency_ms", "delay", "时延"},
}


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def today_text() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: str) -> str:
    payload = "\n".join(parts)
    return "{}_{}".format(prefix, sha256_text(payload)[:16])


def load_transcript_lines(path: Path) -> List[Dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        return _load_jsonl_transcript(path)
    return load_transcript_text(path.read_text(encoding="utf-8", errors="replace"), source_format="text")


def load_transcript_text(text: str, source_format: str = "text") -> List[Dict[str, Any]]:
    return [
        {"line_no": idx, "text": line.rstrip("\n"), "source_format": source_format}
        for idx, line in enumerate(text.splitlines(), start=1)
    ]


def split_transcript(lines: List[Dict[str, Any]], max_chunk_chars: int = MAX_CHUNK_CHARS) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    idx = 0
    while idx < len(lines):
        raw = lines[idx]["text"]
        stripped = raw.strip()
        if not stripped:
            idx += 1
            continue

        if stripped.startswith("```"):
            idx = _collect_code_block(lines, idx, chunks, max_chunk_chars)
            continue

        if _looks_like_table_line(raw):
            idx = _collect_table_block(lines, idx, chunks, max_chunk_chars)
            continue

        if _looks_like_command_start(raw):
            idx = _collect_command_block(lines, idx, chunks, max_chunk_chars)
            continue

        idx = _collect_text_block(lines, idx, chunks, max_chunk_chars)
    return chunks


def extract_context_from_transcript(
    transcript_path: Path,
    project: str,
    task: str,
    scenario: str,
    thread_id: str = "",
    session_scope_key: str = "",
    artifact_root: Optional[Path] = None,
    max_chunk_chars: int = MAX_CHUNK_CHARS,
) -> Dict[str, Any]:
    artifact_root = artifact_root or DEFAULT_ARTIFACT_ROOT
    source_lines = load_transcript_lines(transcript_path)
    return extract_context_from_lines(
        transcript_path=transcript_path,
        source_lines=source_lines,
        project=project,
        task=task,
        scenario=scenario,
        thread_id=thread_id,
        session_scope_key=session_scope_key,
        artifact_root=artifact_root,
        max_chunk_chars=max_chunk_chars,
    )


def extract_context_from_lines(
    transcript_path: Path,
    source_lines: List[Dict[str, Any]],
    project: str,
    task: str,
    scenario: str,
    thread_id: str = "",
    session_scope_key: str = "",
    artifact_root: Optional[Path] = None,
    max_chunk_chars: int = MAX_CHUNK_CHARS,
) -> Dict[str, Any]:
    artifact_root = artifact_root or DEFAULT_ARTIFACT_ROOT
    chunks = split_transcript(source_lines, max_chunk_chars=max_chunk_chars)
    artifacts = extract_artifacts(
        chunks=chunks,
        transcript_path=transcript_path,
        project=project,
        scenario=scenario,
        artifact_root=artifact_root,
    )
    capture_raw = build_capture_payload(
        transcript_path=transcript_path,
        chunks=chunks,
        artifacts=artifacts,
        project=project,
        task=task,
        scenario=scenario,
        thread_id=thread_id,
        session_scope_key=session_scope_key,
    )
    capture = normalize_capture_input(
        capture_raw,
        thread_id_override=thread_id,
        session_scope_key_override=session_scope_key,
    )
    return {
        "source_line_count": len(source_lines),
        "chunks": chunks,
        "artifacts": artifacts,
        "capture_raw": capture_raw,
        "capture": capture,
    }


def extract_artifacts(
    chunks: List[Dict[str, Any]],
    transcript_path: Path,
    project: str,
    scenario: str,
    artifact_root: Path,
) -> List[Dict[str, Any]]:
    artifacts: List[Dict[str, Any]] = []
    for chunk in chunks:
        if should_skip_artifact_chunk(chunk):
            continue

        table_records = parse_markdown_table(chunk["text"])
        if table_records and is_performance_table(table_records, chunk["text"]):
            artifact = build_benchmark_artifact(
                chunk=chunk,
                records=table_records,
                transcript_path=transcript_path,
                project=project,
                scenario=scenario,
                artifact_root=artifact_root,
                extractor_name="generic-performance-table",
                confidence="medium",
            )
            artifacts.append(artifact)
            continue

        entry_perf_records = parse_entry_performance_lines(chunk["text"])
        if entry_perf_records:
            artifact = build_benchmark_artifact(
                chunk=chunk,
                records=entry_perf_records,
                transcript_path=transcript_path,
                project=project,
                scenario=scenario,
                artifact_root=artifact_root,
                extractor_name="generic-entry-performance-lines",
                confidence="medium",
            )
            artifacts.append(artifact)
            continue

        metric_records = parse_metric_lines(chunk["text"])
        if metric_records and any(record_has_performance_metric(record) for record in metric_records):
            artifact = build_benchmark_artifact(
                chunk=chunk,
                records=metric_records,
                transcript_path=transcript_path,
                project=project,
                scenario=scenario,
                artifact_root=artifact_root,
                extractor_name="generic-performance-lines",
                confidence="low",
            )
            artifacts.append(artifact)
    return dedupe_artifacts(artifacts)


def dedupe_artifacts(artifacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for artifact in artifacts:
        artifact_id = artifact.get("artifact_id")
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        deduped.append(artifact)
    return deduped


def build_benchmark_artifact(
    chunk: Dict[str, Any],
    records: List[Dict[str, Any]],
    transcript_path: Path,
    project: str,
    scenario: str,
    artifact_root: Path,
    extractor_name: str,
    confidence: str,
) -> Dict[str, Any]:
    source_hash = sha256_text(chunk["text"])
    artifact_id = stable_id("art", project, scenario, extractor_name, source_hash)
    benchmark_family = infer_benchmark_family(scenario + "\n" + chunk["text"])
    relative_dir = Path("benchmarks") / benchmark_family
    base_name = "{}-{}".format(today_text(), artifact_id)
    json_path = artifact_root / relative_dir / "{}.json".format(base_name)
    md_path = artifact_root / relative_dir / "{}.md".format(base_name)
    metrics = infer_metric_names(records)
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "artifact_id": artifact_id,
        "artifact_type": "benchmark_matrix",
        "project": project,
        "scenario": scenario,
        "benchmark_family": benchmark_family,
        "created_at": now_iso(),
        "extractor": {
            "name": extractor_name,
            "version": EXTRACTOR_VERSION,
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "confidence": confidence,
        },
        "source": {
            "path": str(transcript_path),
            "chunk_id": chunk["chunk_id"],
            "line_range": chunk["line_range"],
            "hash": "sha256:" + source_hash,
            "chunk_type": chunk["chunk_type"],
        },
        "metrics": metrics,
        "records": normalize_benchmark_records(records),
        "raw_excerpt": chunk["text"],
        "json_path": str(json_path),
        "md_path": str(md_path),
    }


def build_capture_payload(
    transcript_path: Path,
    chunks: List[Dict[str, Any]],
    artifacts: List[Dict[str, Any]],
    project: str,
    task: str,
    scenario: str,
    thread_id: str,
    session_scope_key: str,
) -> Dict[str, Any]:
    decisions = extract_decisions(chunks)
    next_steps = extract_next_steps(chunks)
    key_files = unique_items([str(transcript_path)] + [artifact["json_path"] for artifact in artifacts])
    commits = extract_commits(chunks)
    paths = extract_paths(chunks)
    work_ids = extract_work_ids(chunks)
    aliases = ["precompact", "transcript extraction", "context capture"]
    if any(artifact.get("benchmark_family") == "packet-rx" for artifact in artifacts):
        aliases.extend(["packet", "packet rx", "rx performance", "performance matrix"])

    facts = []
    if artifacts:
        facts.append(
            {
                "text": "Offline transcript extraction preserved {} benchmark artifact(s).".format(len(artifacts)),
                "evidence": ", ".join(_artifact_line_refs(artifacts)[:4]),
                "confidence": "high",
            }
        )
    if work_ids:
        facts.append(
            {
                "text": "Transcript references work item(s): {}.".format(", ".join(work_ids[:12])),
                "evidence": "deterministic id scan",
                "confidence": "medium",
            }
        )
    if commits:
        facts.append(
            {
                "text": "Transcript references commit-like id(s): {}.".format(", ".join(commits[:12])),
                "evidence": "deterministic commit scan",
                "confidence": "medium",
            }
        )

    benchmarks = [
        {
            "name": "{} {}".format(artifact["benchmark_family"], artifact["artifact_id"]),
            "value": "{} record(s)".format(len(artifact["records"])),
            "notes": "artifact: {}; source lines: {}".format(
                artifact["json_path"],
                _format_line_range(artifact["source"]["line_range"]),
            ),
        }
        for artifact in artifacts[:MAX_CAPTURE_ITEMS]
    ]

    summary_bits = [
        "Offline transcript extraction scanned {} chunk(s).".format(len(chunks)),
        "It found {} benchmark artifact(s).".format(len(artifacts)),
    ]
    if decisions:
        summary_bits.append("It found {} candidate decision line(s).".format(len(decisions)))
    if next_steps:
        summary_bits.append("It found {} candidate next-step line(s).".format(len(next_steps)))
    if artifacts:
        summary_bits.append("Exact benchmark values are preserved in artifact JSON files, not only in this handoff.")

    retrieval_terms = unique_items(
        [project, scenario, task]
        + aliases
        + [artifact["benchmark_family"] for artifact in artifacts]
        + [metric for artifact in artifacts for metric in artifact.get("metrics", [])]
        + work_ids
        + commits[:12]
        + paths[:12]
    )

    return {
        "capture_reason": "offline_transcript_extraction",
        "project": project,
        "thread_id": thread_id or None,
        "session_scope_key": session_scope_key or None,
        "durable_identity": "codex precompact offline extraction {}".format(
            thread_id or session_scope_key or sha256_text(str(transcript_path))[:12]
        ),
        "task": task,
        "scenario": scenario,
        "session_summary": " ".join(summary_bits),
        "subsystem": unique_items(["reme-memory", "precompact", "artifact-capture"]),
        "facts": facts,
        "decisions": decisions[:MAX_CAPTURE_ITEMS],
        "constraints": [
            "This is an offline Phase 1 capture; no Codex hook or runtime config is enabled.",
            "Exact benchmark numbers should be read from artifact JSON when available.",
        ],
        "errors": [],
        "benchmarks": benchmarks,
        "key_files": key_files[:MAX_CAPTURE_ITEMS],
        "symbols": unique_items(commits + work_ids)[:MAX_CAPTURE_ITEMS],
        "aliases": unique_items(aliases)[:MAX_CAPTURE_ITEMS],
        "open_issues": [],
        "next_steps": next_steps[:MAX_CAPTURE_ITEMS],
        "retrieval_surface": " ".join(retrieval_terms)[:16000],
    }


def write_extraction_outputs(
    extraction: Dict[str, Any],
    artifact_root: Path,
    handoff_root: Optional[Path],
    capture_json_out: Optional[Path],
    write_handoff_files: bool,
    compact_memory_root: Optional[Path] = None,
    write_compact_memory_file: bool = False,
) -> Dict[str, Any]:
    artifact_paths = []
    for artifact in extraction["artifacts"]:
        json_path = Path(artifact["json_path"])
        md_path = Path(artifact["md_path"])
        _atomic_write_text(json_path, render_artifact_json(artifact))
        _atomic_write_text(md_path, render_artifact_markdown(artifact))
        artifact_paths.extend([str(json_path), str(md_path)])

    capture_json_path = capture_json_out
    if capture_json_path is None:
        capture_dir = artifact_root / "captures"
        capture_json_path = capture_dir / "{}-{}.json".format(today_text(), extraction["capture"]["capture_id"])
    _atomic_write_text(capture_json_path, json.dumps(extraction["capture"], ensure_ascii=False, indent=2) + "\n")

    handoff_result = None
    if write_handoff_files:
        handoff_result = write_handoff(extraction["capture"], handoff_root=handoff_root)

    compact_memory_result = None
    if write_compact_memory_file:
        compact_memory_result = write_compact_memory(
            capture=extraction["capture"],
            root=compact_memory_root or DEFAULT_COMPACT_MEMORY_ROOT,
            capture_json_path=capture_json_path,
            handoff_md_path=Path(handoff_result["md_path"]) if handoff_result and handoff_result.get("md_path") else None,
            artifact_paths=artifact_paths,
        )

    return {
        "capture_id": extraction["capture"]["capture_id"],
        "capture_json_path": str(capture_json_path),
        "artifact_paths": artifact_paths,
        "handoff": handoff_result,
        "compact_memory": compact_memory_result,
        "artifact_count": len(extraction["artifacts"]),
    }


def write_compact_memory(
    capture: Dict[str, Any],
    root: Path,
    capture_json_path: Path,
    handoff_md_path: Optional[Path],
    artifact_paths: List[str],
) -> Dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    base_name = "{}-{}.md".format(today_text(), capture["capture_id"])
    path = root / base_name
    _atomic_write_text(
        path,
        render_compact_memory_markdown(
            capture=capture,
            capture_json_path=capture_json_path,
            handoff_md_path=handoff_md_path,
            artifact_paths=artifact_paths,
        ),
    )
    return {"md_path": str(path)}


def render_compact_memory_markdown(
    capture: Dict[str, Any],
    capture_json_path: Path,
    handoff_md_path: Optional[Path],
    artifact_paths: List[str],
) -> str:
    lines = [
        "# Compact Memory - {}".format(capture["capture_id"]),
        "",
        "Source: Codex PreCompact context capture",
        "Created At: {}".format(capture.get("created_at", "")),
        "Thread ID: {}".format(capture.get("thread_id") or ""),
        "Session Scope Key: {}".format(capture.get("session_scope_key") or ""),
        "Project: {}".format(capture.get("project") or "unknown"),
        "Scenario: {}".format(capture.get("scenario") or ""),
        "",
        "## Task",
        "",
        str(capture.get("task") or ""),
        "",
        "## Summary",
        "",
        str(capture.get("session_summary") or ""),
        "",
    ]

    _extend_bullets(lines, "Facts", [item.get("text", "") for item in capture.get("facts", []) if isinstance(item, dict)])
    _extend_bullets(
        lines,
        "Decisions",
        [item.get("decision", "") for item in capture.get("decisions", []) if isinstance(item, dict)],
    )
    _extend_bullets(lines, "Next Steps", [str(item) for item in capture.get("next_steps", [])])
    _extend_bullets(lines, "Open Issues", [str(item) for item in capture.get("open_issues", [])])

    key_files = [str(item) for item in capture.get("key_files", [])]
    key_files.extend(str(path) for path in artifact_paths if path.endswith(".json"))
    key_files.append(str(capture_json_path))
    if handoff_md_path is not None:
        key_files.append(str(handoff_md_path))
    _extend_bullets(lines, "Key Files", unique_items(key_files)[:MAX_CAPTURE_ITEMS])

    retrieval_surface = str(capture.get("retrieval_surface") or "").strip()
    if retrieval_surface:
        lines.extend(["", "## Retrieval Surface", "", retrieval_surface[:16000], ""])

    return "\n".join(lines).rstrip() + "\n"


def _extend_bullets(lines: List[str], title: str, items: List[str]) -> None:
    lines.extend(["", "## {}".format(title), ""])
    cleaned = [item.strip() for item in items if item and item.strip()]
    if not cleaned:
        lines.append("- None")
        return
    lines.extend("- {}".format(item) for item in unique_items(cleaned)[:MAX_CAPTURE_ITEMS])


def render_artifact_json(artifact: Dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in artifact.items()
        if key not in {"json_path", "md_path"}
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def render_artifact_markdown(artifact: Dict[str, Any]) -> str:
    lines = [
        "# ReMe Benchmark Artifact - {}".format(artifact["artifact_id"]),
        "",
        "## Metadata",
        "",
        "- Project: {}".format(artifact["project"]),
        "- Scenario: {}".format(artifact["scenario"]),
        "- Benchmark Family: {}".format(artifact["benchmark_family"]),
        "- Artifact Type: {}".format(artifact["artifact_type"]),
        "- Extractor: {} {}".format(artifact["extractor"]["name"], artifact["extractor"]["version"]),
        "- Confidence: {}".format(artifact["extractor"]["confidence"]),
        "- Source: {}".format(artifact["source"]["path"]),
        "- Source Lines: {}".format(_format_line_range(artifact["source"]["line_range"])),
        "- Source Hash: {}".format(artifact["source"]["hash"]),
        "",
        "## Metrics",
        "",
    ]
    if artifact["metrics"]:
        lines.extend("- `{}`".format(metric) for metric in artifact["metrics"])
    else:
        lines.append("- None")

    lines.extend(["", "## Records", ""])
    if artifact["records"]:
        headers = sorted({key for record in artifact["records"] for key in record.keys() if key != "raw"})
        lines.append("| {} |".format(" | ".join(headers)))
        lines.append("| {} |".format(" | ".join("---" for _ in headers)))
        for record in artifact["records"]:
            lines.append("| {} |".format(" | ".join(_markdown_cell(str(record.get(header, ""))) for header in headers)))
    else:
        lines.append("No structured records.")

    lines.extend(["", "## Raw Excerpt", "", "```text", artifact["raw_excerpt"], "```", ""])
    return "\n".join(lines)


def parse_markdown_table(text: str) -> List[Dict[str, Any]]:
    rows = [line.strip() for line in text.splitlines() if _looks_like_table_line(line)]
    if len(rows) < 3:
        return []

    header_idx = -1
    for idx in range(len(rows) - 1):
        if _looks_like_separator_row(rows[idx + 1]):
            header_idx = idx
            break
    if header_idx < 0:
        return []

    headers = [_clean_header(cell) for cell in _split_table_row(rows[header_idx])]
    if not headers:
        return []

    records: List[Dict[str, Any]] = []
    for row in rows[header_idx + 2 :]:
        if _looks_like_separator_row(row):
            continue
        cells = _split_table_row(row)
        if len(cells) != len(headers):
            continue
        raw = {headers[idx]: cells[idx].strip() for idx in range(len(headers))}
        normalized = normalize_record_fields(raw)
        normalized["raw"] = raw
        records.append(normalized)
    return records


def parse_metric_lines(text: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for line in text.splitlines():
        if is_source_fixture_line(line):
            continue
        matches = list(METRIC_RE.finditer(line))
        if len(matches) < 2:
            continue
        record: Dict[str, Any] = {"raw": {"line": line.strip()}}
        for match in matches:
            key = _normalize_key(match.group("name"))
            unit = match.group("unit") or ""
            value = match.group("value")
            record[key] = _format_metric_value(value, unit)
        records.append(record)
    return records


def parse_entry_performance_lines(text: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    seen = set()
    for line in text.splitlines():
        if is_source_fixture_line(line):
            continue
        for match in ENTRY_PERF_RE.finditer(line):
            raw_line = line.strip()
            record = {
                "entry_count": match.group("count"),
                "throughput": "{} {}".format(match.group("value"), match.group("unit")),
                "status": classify_entry_performance_line(raw_line),
                "raw": {"line": raw_line},
            }
            key = json.dumps(record, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                records.append(record)
    return records


def classify_entry_performance_line(line: str) -> str:
    lowered = line.lower()
    if any(term in lowered for term in ["测试过", "实测", "结果", "测得", "没有下降"]):
        return "measured"
    if any(term in lowered for term in ["建议", "确认", "附近", "目标", "baseline", "基线"]):
        return "reference_or_target"
    return "candidate"


def should_skip_artifact_chunk(chunk: Dict[str, Any]) -> bool:
    text = str(chunk.get("text") or "")
    if chunk.get("chunk_type") == "code_block":
        return True
    if "*** Begin Patch" in text or "*** End Patch" in text:
        return True
    return looks_like_source_fixture(text)


def looks_like_source_fixture(text: str) -> bool:
    if not text.strip():
        return False
    normalized = re.sub(r"(?m)^[+-]", "", text)
    lines = [line for line in normalized.splitlines() if line.strip()]
    if not lines:
        return False
    if SOURCE_FIXTURE_RE.search(normalized):
        return True
    quoted_metric_lines = sum(1 for line in lines if is_source_fixture_line(line) and PERF_KEYWORD_RE.search(line))
    source_lines = sum(1 for line in lines if SOURCE_LINE_RE.search(line.strip()))
    return quoted_metric_lines > 0 and source_lines >= 2


def is_source_fixture_line(line: str) -> bool:
    stripped = re.sub(r"^[+-]", "", line.strip()).strip()
    if not stripped:
        return False
    if stripped.startswith(("\"", "'")) and stripped.endswith(("\",", "',", "\"", "'")):
        return True
    if stripped.startswith(("f\"", "f'", "r\"", "r'", "b\"", "b'")):
        return True
    return False


def record_has_performance_metric(record: Dict[str, Any]) -> bool:
    return any(_normalize_key(key) in PERFORMANCE_METRIC_KEYS for key in record.keys())


def normalize_benchmark_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = []
    for record in records:
        clean = {}
        for key, value in record.items():
            if value is None:
                continue
            clean[str(key)] = value
        normalized.append(clean)
    return normalized


def normalize_record_fields(raw: Dict[str, str]) -> Dict[str, str]:
    normalized: Dict[str, str] = {}
    for header, value in raw.items():
        if not value:
            continue
        canonical = canonical_field_name(header)
        if canonical:
            normalized[canonical] = value
        else:
            normalized[_normalize_key(header)] = value
    return normalized


def canonical_field_name(header: str) -> str:
    key = _normalize_key(header)
    for canonical, aliases in FIELD_ALIASES.items():
        if key in aliases:
            return canonical
    return ""


def infer_metric_names(records: List[Dict[str, Any]]) -> List[str]:
    metric_keys = {"throughput", "drop", "latency", "cpu", "packet_size", "queues"}
    names: List[str] = []
    for record in records:
        for key in record.keys():
            normalized = _normalize_key(key)
            if normalized in metric_keys or any(term in normalized for term in metric_keys):
                if normalized not in names:
                    names.append(normalized)
    return names


def is_performance_table(records: List[Dict[str, Any]], text: str) -> bool:
    if not records:
        return False
    keys = {_normalize_key(key) for record in records for key in record.keys()}
    metric_hits = {"throughput", "drop", "latency", "packet_size", "queues", "cpu"} & keys
    return bool(metric_hits) or bool(PERF_KEYWORD_RE.search(text))


def infer_benchmark_family(text: str) -> str:
    lowered = text.lower()
    if "packet" in lowered and "rx" in lowered:
        return "packet-rx"
    if "rx" in lowered and ("mpps" in lowered or "pps" in lowered or "throughput" in lowered):
        return "rx-performance"
    return "generic-performance"


def extract_decisions(chunks: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for chunk in chunks:
        for line in chunk["text"].splitlines():
            text = line.strip("-* \t")
            if len(text) < 6 or len(text) > 512:
                continue
            if is_low_value_capture_line(text):
                continue
            if DECISION_RE.search(text):
                item = {
                    "decision": text,
                    "rationale": "source lines {}".format(_format_line_range(chunk["line_range"])),
                    "status": "candidate",
                }
                if item not in items:
                    items.append(item)
    return items


def extract_next_steps(chunks: List[Dict[str, Any]]) -> List[str]:
    items: List[str] = []
    for chunk in chunks:
        for line in chunk["text"].splitlines():
            text = line.strip("-* \t")
            if len(text) < 6 or len(text) > 512:
                continue
            if is_low_value_capture_line(text):
                continue
            if NEXT_STEP_RE.search(text):
                value = "{} (source lines {})".format(text, _format_line_range(chunk["line_range"]))
                if value not in items:
                    items.append(value)
    return items


def is_low_value_capture_line(text: str) -> bool:
    stripped = text.strip()
    normalized = re.sub(r"^[+-]", "", stripped).strip()
    if not stripped:
        return True
    if stripped.startswith("+"):
        return True
    if stripped.startswith("[line="):
        return True
    if normalized.startswith("CALL "):
        return True
    if normalized.startswith("{") and normalized.endswith("}"):
        return True
    if normalized.startswith(("[", "]", "}", ")", "except ", "finally:")):
        return True
    if normalized.startswith("#"):
        return True
    if "(source lines " in normalized:
        return True
    if re.match(r'^"[A-Za-z0-9_]+":', normalized):
        return True
    if re.match(r"^[A-Z_]+_RE\s*=", normalized) or " = re.compile" in normalized:
        return True
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*,?", normalized):
        return True
    if normalized.startswith(("_assert(", "assert ", "await ", "return ", "result = ", "process = ", "stream.", "loop.")):
        return True
    if is_source_fixture_line(normalized):
        return True
    if SOURCE_LINE_RE.search(normalized):
        return True
    return False


def extract_commits(chunks: List[Dict[str, Any]]) -> List[str]:
    return unique_items(match.group(0) for chunk in chunks for match in COMMIT_RE.finditer(chunk["text"]))


def extract_paths(chunks: List[Dict[str, Any]]) -> List[str]:
    return unique_items(match.group(0).rstrip(".,;)") for chunk in chunks for match in PATH_RE.finditer(chunk["text"]))


def extract_work_ids(chunks: List[Dict[str, Any]]) -> List[str]:
    ids = []
    for chunk in chunks:
        ids.extend(match.group(0) for match in TRK_RE.finditer(chunk["text"]))
        ids.extend(
            match.group(0)
            for match in JIRA_RE.finditer(chunk["text"])
            if not match.group(0).startswith("TRK-")
        )
    return unique_items(ids)


def unique_items(values: Iterable[str]) -> List[str]:
    items: List[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in items:
            items.append(text)
    return items


def _load_jsonl_transcript(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for idx, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            rows.append({"line_no": idx, "text": line.rstrip("\n"), "source_format": "jsonl/raw"})
            continue
        extracted = _extract_json_text(payload)
        if not extracted:
            continue
        for piece_idx, piece in enumerate(extracted, start=1):
            rows.append({"line_no": idx, "text": piece, "source_format": "jsonl", "piece": piece_idx})
    return rows


def _extract_json_text(value: Any) -> List[str]:
    texts: List[str] = []
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            texts.append(stripped)
        return texts
    if isinstance(value, list):
        for item in value:
            texts.extend(_extract_json_text(item))
        return texts
    if not isinstance(value, dict):
        return texts

    preferred_keys = ["text", "content", "message", "cmd", "command", "output", "result", "summary"]
    for key in preferred_keys:
        if key in value:
            texts.extend(_extract_json_text(value[key]))
    if texts:
        return unique_items(texts)
    for key, nested in value.items():
        if key in {"encrypted_content"}:
            continue
        texts.extend(_extract_json_text(nested))
    return unique_items(texts)


def _collect_code_block(
    lines: List[Dict[str, Any]],
    start_idx: int,
    chunks: List[Dict[str, Any]],
    max_chunk_chars: int,
) -> int:
    start_line = lines[start_idx]["line_no"]
    collected = [lines[start_idx]["text"]]
    idx = start_idx + 1
    while idx < len(lines):
        collected.append(lines[idx]["text"])
        if lines[idx]["text"].strip().startswith("```"):
            idx += 1
            break
        idx += 1
    _append_chunk(chunks, "code_block", collected, start_line, lines[idx - 1]["line_no"], max_chunk_chars)
    return idx


def _collect_table_block(
    lines: List[Dict[str, Any]],
    start_idx: int,
    chunks: List[Dict[str, Any]],
    max_chunk_chars: int,
) -> int:
    start_line = lines[start_idx]["line_no"]
    collected = []
    idx = start_idx
    while idx < len(lines) and _looks_like_table_line(lines[idx]["text"]):
        collected.append(lines[idx]["text"])
        idx += 1
    _append_chunk(chunks, "markdown_table", collected, start_line, lines[idx - 1]["line_no"], max_chunk_chars)
    return idx


def _collect_command_block(
    lines: List[Dict[str, Any]],
    start_idx: int,
    chunks: List[Dict[str, Any]],
    max_chunk_chars: int,
) -> int:
    start_line = lines[start_idx]["line_no"]
    collected = [lines[start_idx]["text"]]
    idx = start_idx + 1
    while idx < len(lines):
        current = lines[idx]["text"]
        if not current.strip():
            break
        if _looks_like_table_line(current) or current.strip().startswith("```"):
            break
        if len("\n".join(collected + [current])) > max_chunk_chars:
            break
        collected.append(current)
        idx += 1
    _append_chunk(chunks, "command_block", collected, start_line, lines[idx - 1]["line_no"], max_chunk_chars)
    return idx


def _collect_text_block(
    lines: List[Dict[str, Any]],
    start_idx: int,
    chunks: List[Dict[str, Any]],
    max_chunk_chars: int,
) -> int:
    start_line = lines[start_idx]["line_no"]
    collected = [lines[start_idx]["text"]]
    idx = start_idx + 1
    while idx < len(lines):
        current = lines[idx]["text"]
        if not current.strip():
            break
        if current.strip().startswith("```") or _looks_like_table_line(current) or _looks_like_command_start(current):
            break
        if len("\n".join(collected + [current])) > max_chunk_chars:
            break
        collected.append(current)
        idx += 1
    _append_chunk(chunks, "text", collected, start_line, lines[idx - 1]["line_no"], max_chunk_chars)
    return idx


def _append_chunk(
    chunks: List[Dict[str, Any]],
    chunk_type: str,
    collected: List[str],
    start_line: int,
    end_line: int,
    max_chunk_chars: int,
) -> None:
    text = "\n".join(collected)
    offset = 0
    while text:
        piece = text[:max_chunk_chars]
        text = text[max_chunk_chars:]
        chunk_id = stable_id("chunk", chunk_type, str(start_line), str(end_line), piece)
        chunks.append(
            {
                "chunk_id": chunk_id,
                "chunk_type": chunk_type,
                "line_range": [start_line, end_line],
                "text": piece,
                "char_count": len(piece),
                "chunk_index": len(chunks),
                "split_offset": offset,
            }
        )
        offset += len(piece)


def _looks_like_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _looks_like_separator_row(line: str) -> bool:
    cells = _split_table_row(line)
    if not cells:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def _split_table_row(line: str) -> List[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _looks_like_command_start(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith("$ "):
        return True
    return bool(re.match(r"^[\w.-]+@[\w.-]+:[^$#]+[$#]\s+", stripped))


def _clean_header(value: str) -> str:
    return value.strip().strip("`").strip()


def _normalize_key(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"[\s./-]+", "_", text)
    text = re.sub(r"[^a-z0-9_\u4e00-\u9fff]", "", text)
    return text.strip("_")


def _artifact_line_refs(artifacts: List[Dict[str, Any]]) -> List[str]:
    return [
        "{} lines {}".format(artifact["artifact_id"], _format_line_range(artifact["source"]["line_range"]))
        for artifact in artifacts
    ]


def _format_line_range(line_range: List[int]) -> str:
    if not line_range:
        return "unknown"
    if len(line_range) == 1 or line_range[0] == line_range[-1]:
        return str(line_range[0])
    return "{}-{}".format(line_range[0], line_range[-1])


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _format_metric_value(value: str, unit: str) -> str:
    if not unit:
        return value
    if unit == "%":
        return value + unit
    return "{} {}".format(value, unit)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Offline ReMe transcript context extractor.")
    parser.add_argument("--transcript", required=True, help="Transcript text or JSONL file to scan")
    parser.add_argument("--project", default=os.environ.get("REME_BUS_PROJECT", "cling_packet"))
    parser.add_argument("--task", default="Offline transcript context extraction")
    parser.add_argument("--scenario", default="PreCompact offline extraction")
    parser.add_argument("--thread-id", default="")
    parser.add_argument("--session-scope-key", default="")
    parser.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT))
    parser.add_argument("--handoff-root", default=str(DEFAULT_HANDOFF_ROOT))
    parser.add_argument("--compact-memory-root", default=str(DEFAULT_COMPACT_MEMORY_ROOT))
    parser.add_argument("--capture-json-out", default="")
    parser.add_argument("--write-handoff", action="store_true", help="Also write handoff markdown/json files")
    parser.add_argument("--write-compact-memory", action="store_true", help="Also write compact memory markdown outside normal ReMe memory")
    parser.add_argument("--max-chunk-chars", type=int, default=MAX_CHUNK_CHARS)
    args = parser.parse_args(argv)

    use_stdin = args.transcript == "-"
    transcript_path = Path("<stdin>") if use_stdin else Path(args.transcript).expanduser().resolve()
    artifact_root = Path(args.artifact_root).expanduser().resolve()
    compact_memory_root = Path(args.compact_memory_root).expanduser().resolve() if args.compact_memory_root else None
    capture_json_out = Path(args.capture_json_out).expanduser().resolve() if args.capture_json_out else None
    handoff_root = Path(args.handoff_root).expanduser().resolve() if args.handoff_root else None

    if use_stdin:
        extraction = extract_context_from_lines(
            transcript_path=transcript_path,
            source_lines=load_transcript_text(sys.stdin.read(), source_format="stdin"),
            project=args.project,
            task=args.task,
            scenario=args.scenario,
            thread_id=args.thread_id,
            session_scope_key=args.session_scope_key,
            artifact_root=artifact_root,
            max_chunk_chars=args.max_chunk_chars,
        )
    else:
        extraction = extract_context_from_transcript(
            transcript_path=transcript_path,
            project=args.project,
            task=args.task,
            scenario=args.scenario,
            thread_id=args.thread_id,
            session_scope_key=args.session_scope_key,
            artifact_root=artifact_root,
            max_chunk_chars=args.max_chunk_chars,
        )
    result = write_extraction_outputs(
        extraction=extraction,
        artifact_root=artifact_root,
        handoff_root=handoff_root,
        capture_json_out=capture_json_out,
        write_handoff_files=args.write_handoff,
        compact_memory_root=compact_memory_root,
        write_compact_memory_file=args.write_compact_memory,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
