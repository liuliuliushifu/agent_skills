#!/usr/bin/env python3
import json
import os
import re
import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Optional


START_RE = re.compile(r"^<!-- reme-memory:start source_capture_id=(?P<capture_id>\S+) durable_idempotency_key=(?P<durable_key>\S+) -->$")
META_PREFIX = "<!-- reme-memory:meta "
META_SUFFIX = " -->"
END_MARKER = "<!-- reme-memory:end -->"
SCRIPT_DIR = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
USER_ROOT = Path(os.environ.get("CODEX_USER_HOME", str(CODEX_HOME.parent)))
REME_HOME = Path(os.environ.get("REME_HOME", str(USER_ROOT / ".local/share/reme")))
DEFAULT_REME_WORKDIR = str(REME_HOME / "light_data")
DEFAULT_MEMORY_ROOT = str(Path(DEFAULT_REME_WORKDIR) / "memory")


def get_memory_root(memory_root: Optional[Path] = None) -> Path:
    if memory_root is not None:
        return memory_root
    workdir = os.environ.get("REME_WORKDIR", DEFAULT_REME_WORKDIR)
    return Path(workdir).expanduser().resolve() / "memory"


def write_memory_block(
    memory_json: Dict[str, Any],
    capture: Dict[str, Any],
    memory_root: Optional[Path] = None,
) -> Dict[str, Any]:
    root = get_memory_root(memory_root)
    root.mkdir(parents=True, exist_ok=True)
    target_path = root / f"{str(capture['created_at']).split('T', 1)[0]}.md"
    existing_text = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
    target_file_bytes_before = len(existing_text.encode("utf-8"))
    blocks = parse_memory_blocks(existing_text)
    replacement_block = None
    replacement_index = None

    for index, block in enumerate(blocks):
        if block["durable_idempotency_key"] == capture["durable_idempotency_key"]:
            replacement_block = block
            replacement_index = index
            break

    merged_payload = _merge_memory_json(
        replacement_block["meta"]["memory_json"] if replacement_block else None,
        memory_json,
    )
    source_capture_history = list(replacement_block["meta"].get("source_capture_history", [])) if replacement_block else []
    if capture["capture_id"] not in source_capture_history:
        source_capture_history.append(capture["capture_id"])

    rendered = render_memory_block(
        merged_payload,
        capture,
        source_capture_history=source_capture_history,
    )
    rendered_block_bytes = len(rendered.encode("utf-8"))

    if replacement_block is None:
        if existing_text and not existing_text.endswith("\n"):
            existing_text += "\n"
        new_text = existing_text + ("" if not existing_text else "\n") + rendered
        replaced = False
    else:
        prefix = existing_text[: replacement_block["start_offset"]]
        suffix = existing_text[replacement_block["end_offset"] :]
        new_text = prefix + rendered + suffix
        replaced = True

    _atomic_write_text(target_path, new_text)
    target_file_bytes_after = len(new_text.encode("utf-8"))
    final_blocks = parse_memory_blocks(target_path.read_text(encoding="utf-8"))
    current_block = next(
        block for block in final_blocks if block["durable_idempotency_key"] == capture["durable_idempotency_key"]
    )
    return {
        "path": str(target_path),
        "replaced": replaced,
        "reconcile_action": "replace" if replaced else "create",
        "start_line": current_block["start_line"],
        "end_line": current_block["end_line"],
        "durable_idempotency_key": capture["durable_idempotency_key"],
        "rendered_block_bytes": rendered_block_bytes,
        "target_file_bytes_before": target_file_bytes_before,
        "target_file_bytes_after": target_file_bytes_after,
        "existing_block_count": len(blocks),
    }


def reconcile_memory_block(
    memory_json: Dict[str, Any],
    capture: Dict[str, Any],
    *,
    action: str,
    target_path: str = "",
    target_durable_idempotency_key: str = "",
    memory_root: Optional[Path] = None,
) -> Dict[str, Any]:
    normalized_action = str(action or "create").strip().lower()
    if normalized_action in {"create", "keep_both"}:
        result = write_memory_block(memory_json, capture, memory_root=memory_root)
        result["reconcile_action"] = normalized_action
        return result
    if normalized_action not in {"overwrite", "merge"}:
        raise ValueError(f"unsupported reconcile action: {normalized_action}")
    if not target_path or not target_durable_idempotency_key:
        raise ValueError(f"{normalized_action} requires a target path and durable idempotency key")

    root = get_memory_root(memory_root).expanduser().resolve()
    resolved_target = Path(target_path).expanduser().resolve()
    if resolved_target.suffix.lower() != ".md" or resolved_target.parent != root:
        raise ValueError(f"reconcile target is outside durable memory root: {resolved_target}")
    if not resolved_target.is_file():
        raise FileNotFoundError(resolved_target)

    existing_text = resolved_target.read_text(encoding="utf-8")
    blocks = parse_memory_blocks(existing_text)
    target_block = next(
        (
            block
            for block in blocks
            if block["durable_idempotency_key"] == target_durable_idempotency_key
        ),
        None,
    )
    if target_block is None:
        raise ValueError(f"reconcile target block not found: {target_durable_idempotency_key}")

    existing_memory_json = dict(target_block["meta"].get("memory_json") or {})
    if normalized_action == "overwrite":
        reconciled_payload = _overwrite_memory_json(existing_memory_json, memory_json)
    else:
        reconciled_payload = _merge_memory_json(existing_memory_json, memory_json)

    source_capture_history = list(target_block["meta"].get("source_capture_history", []))
    if capture["capture_id"] not in source_capture_history:
        source_capture_history.append(capture["capture_id"])

    target_capture = dict(capture)
    target_capture["durable_idempotency_key"] = target_durable_idempotency_key
    target_capture["created_at"] = (
        reconciled_payload.get("evidence_at")
        or capture.get("created_at")
        or existing_memory_json.get("evidence_at")
    )
    rendered = render_memory_block(
        reconciled_payload,
        target_capture,
        source_capture_history=source_capture_history,
    )
    prefix = existing_text[: target_block["start_offset"]]
    suffix = existing_text[target_block["end_offset"] :]
    new_text = prefix + rendered + suffix
    _atomic_write_text(resolved_target, new_text)

    final_blocks = parse_memory_blocks(new_text)
    current_block = next(
        block
        for block in final_blocks
        if block["durable_idempotency_key"] == target_durable_idempotency_key
    )
    return {
        "path": str(resolved_target),
        "replaced": True,
        "reconcile_action": normalized_action,
        "start_line": current_block["start_line"],
        "end_line": current_block["end_line"],
        "durable_idempotency_key": target_durable_idempotency_key,
        "rendered_block_bytes": len(rendered.encode("utf-8")),
        "target_file_bytes_before": len(existing_text.encode("utf-8")),
        "target_file_bytes_after": len(new_text.encode("utf-8")),
        "existing_block_count": len(blocks),
    }


def render_memory_block(
    memory_json: Dict[str, Any],
    capture: Dict[str, Any],
    *,
    source_capture_history: List[str],
) -> str:
    meta = {
        "durable_idempotency_key": capture["durable_idempotency_key"],
        "source_capture_history": source_capture_history,
        "memory_json": memory_json,
    }
    lines = [
        f"<!-- reme-memory:start source_capture_id={capture['capture_id']} durable_idempotency_key={capture['durable_idempotency_key']} -->",
        f"{META_PREFIX}{json.dumps(meta, ensure_ascii=False, sort_keys=True)}{META_SUFFIX}",
        f"Source Capture ID: {capture['capture_id']}",
        f"Durable Idempotency Key: {capture['durable_idempotency_key']}",
        f"Retrieval Surface: {memory_json['retrieval_surface']}",
        f"### Topic: {memory_json['topic']}",
        f"Applicability: {memory_json['applicability']}",
        "Conclusions:",
        *_bullet_lines(memory_json["conclusions"]),
        "Root Cause Patterns:",
        *_bullet_lines(memory_json["root_cause_patterns"]),
        "Solutions:",
        *_bullet_lines(memory_json["solutions"]),
        "Aliases:",
        *_bullet_lines(memory_json["aliases"]),
        "Benchmark Names:",
        *_bullet_lines(memory_json["benchmark_names"]),
        "Key Files:",
        *_bullet_lines(memory_json["key_locations"]["files"]),
        "Symbols:",
        *_bullet_lines(memory_json["key_locations"]["symbols"]),
        "Errors:",
        *_bullet_lines(memory_json["key_locations"]["errors"]),
        f"Confidence: {memory_json['confidence']}",
        f"Evidence At: {memory_json.get('evidence_at') or capture['created_at']}",
        f"Updated At: {memory_json.get('evidence_at') or capture['created_at']}",
        END_MARKER,
    ]
    return "\n".join(lines) + "\n"


def parse_memory_blocks(text: str) -> List[Dict[str, Any]]:
    if not text:
        return []
    lines = text.splitlines(keepends=True)
    blocks: List[Dict[str, Any]] = []
    offset = 0
    index = 0
    while index < len(lines):
        start_match = START_RE.match(lines[index].rstrip("\n"))
        if not start_match:
            offset += len(lines[index])
            index += 1
            continue

        start_line = index + 1
        start_offset = offset
        index += 1
        offset += len(lines[index - 1])
        if index >= len(lines) or not lines[index].startswith(META_PREFIX):
            raise ValueError("missing block meta line")
        meta_line = lines[index].rstrip("\n")
        meta_json = meta_line[len(META_PREFIX) : -len(META_SUFFIX)]
        meta = json.loads(meta_json)
        index += 1
        offset += len(lines[index - 1])

        while index < len(lines):
            current = lines[index]
            offset += len(current)
            index += 1
            if current.rstrip("\n") == END_MARKER:
                end_line = index
                blocks.append(
                    {
                        "start_line": start_line,
                        "end_line": end_line,
                        "start_offset": start_offset,
                        "end_offset": offset,
                        "durable_idempotency_key": meta["durable_idempotency_key"],
                        "source_capture_id": start_match.group("capture_id"),
                        "meta": meta,
                    }
                )
                break
        else:
            raise ValueError("unterminated memory block")

    return blocks


def _merge_memory_json(existing: Optional[Dict[str, Any]], new: Dict[str, Any]) -> Dict[str, Any]:
    if existing is None:
        return new

    merged = dict(new)
    merged["conclusions"] = _ordered_union(existing.get("conclusions", []), new.get("conclusions", []))
    merged["root_cause_patterns"] = _ordered_union(
        existing.get("root_cause_patterns", []),
        new.get("root_cause_patterns", []),
    )
    merged["solutions"] = _ordered_union(existing.get("solutions", []), new.get("solutions", []))
    merged["aliases"] = _sorted_union(existing.get("aliases", []), new.get("aliases", []))
    merged["benchmark_names"] = _sorted_union(existing.get("benchmark_names", []), new.get("benchmark_names", []))
    merged["evidence_hashes"] = _sorted_union(
        existing.get("evidence_hashes", []),
        new.get("evidence_hashes", []),
    )
    merged["supersedes"] = _sorted_union(existing.get("supersedes", []), new.get("supersedes", []))
    merged["evidence_at"] = _latest_iso(existing.get("evidence_at", ""), new.get("evidence_at", ""))
    merged["key_locations"] = {
        "files": _sorted_union(existing.get("key_locations", {}).get("files", []), new.get("key_locations", {}).get("files", [])),
        "symbols": _sorted_union(existing.get("key_locations", {}).get("symbols", []), new.get("key_locations", {}).get("symbols", [])),
        "errors": _sorted_union(existing.get("key_locations", {}).get("errors", []), new.get("key_locations", {}).get("errors", [])),
    }
    merged["retrieval_surface"] = _build_retrieval_surface(merged, existing.get("retrieval_surface", ""))
    return merged


def _overwrite_memory_json(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(new)
    merged["aliases"] = _sorted_union(existing.get("aliases", []), new.get("aliases", []))
    merged["benchmark_names"] = _sorted_union(existing.get("benchmark_names", []), new.get("benchmark_names", []))
    merged["evidence_hashes"] = _sorted_union(
        existing.get("evidence_hashes", []),
        new.get("evidence_hashes", []),
    )
    merged["supersedes"] = _sorted_union(existing.get("supersedes", []), new.get("supersedes", []))
    merged["evidence_at"] = _latest_iso(existing.get("evidence_at", ""), new.get("evidence_at", ""))
    merged["key_locations"] = {
        "files": _sorted_union(existing.get("key_locations", {}).get("files", []), new.get("key_locations", {}).get("files", [])),
        "symbols": _sorted_union(existing.get("key_locations", {}).get("symbols", []), new.get("key_locations", {}).get("symbols", [])),
        "errors": _sorted_union(existing.get("key_locations", {}).get("errors", []), new.get("key_locations", {}).get("errors", [])),
    }
    merged["retrieval_surface"] = _build_retrieval_surface(merged, existing.get("retrieval_surface", ""))
    return merged


def _build_retrieval_surface(memory_json: Dict[str, Any], existing_surface: str) -> str:
    tokens: List[str] = []
    for raw in [existing_surface, memory_json.get("retrieval_surface", "")]:
        tokens.extend(item for item in raw.split(" ") if item)
    tokens.append(memory_json["topic"])
    tokens.extend(memory_json["aliases"])
    tokens.extend(memory_json["benchmark_names"])
    tokens.extend(memory_json["key_locations"]["files"])
    tokens.extend(memory_json["key_locations"]["symbols"])
    tokens.extend(memory_json["key_locations"]["errors"])
    return " ".join(_sorted_union(tokens, []))


def _sorted_union(left: List[str], right: List[str]) -> List[str]:
    merged = sorted({item.strip() for item in [*left, *right] if item and item.strip()})
    return merged


def _ordered_union(left: List[str], right: List[str]) -> List[str]:
    merged: List[str] = []
    for raw in [*left, *right]:
        item = str(raw).strip()
        if item and item not in merged:
            merged.append(item)
    return merged


def _latest_iso(left: str, right: str) -> str:
    candidates = []
    for value in (left, right):
        text = str(value or "").strip()
        if not text:
            continue
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = dt.datetime.fromisoformat(normalized)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        candidates.append((parsed.timestamp(), text))
    if not candidates:
        return str(right or left or "")
    return max(candidates, key=lambda item: item[0])[1]


def _bullet_lines(items: List[str]) -> List[str]:
    if not items:
        return ["- None"]
    return [f"- {item}" for item in items]


def _atomic_write_text(path: Path, text: str) -> None:
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)
