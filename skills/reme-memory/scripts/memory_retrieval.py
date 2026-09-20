#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from memory_block_renderer import parse_memory_blocks


CREATED_AT_RE = re.compile(r"^Created At:\s*(?P<value>\S+)\s*$", re.MULTILINE)
EVIDENCE_AT_RE = re.compile(r"^Evidence At:\s*(?P<value>\S+)\s*$", re.MULTILINE)
UPDATED_AT_RE = re.compile(r"^Updated At:\s*(?P<value>\S+)\s*$", re.MULTILINE)
COMPACT_ID_RE = re.compile(r"^# Compact Memory - (?P<value>\S+)\s*$", re.MULTILINE)
PARENT_CAPTURE_RE = re.compile(r"parent_capture_id:\s*(?P<value>\S+)")
EVIDENCE_HASH_RE = re.compile(r"sha256:[0-9a-fA-F]{16,}")


def rerank_search_results(
    results: Iterable[Any],
    *,
    max_results: int,
    min_score: float,
    memory_dir: Path,
    compact_memory_dir: Path,
    recency_weight: float,
    recency_half_life_days: float,
    now: Optional[dt.datetime] = None,
) -> List[Dict[str, Any]]:
    if recency_weight < 0.0 or recency_weight > 1.0:
        raise ValueError("recency_weight must be in [0, 1]")
    if recency_half_life_days <= 0.0:
        raise ValueError("recency_half_life_days must be > 0")

    current_time = now or dt.datetime.now(dt.timezone.utc).astimezone()
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=dt.timezone.utc)
    cache: Dict[str, Dict[str, Any]] = {}
    items: List[Dict[str, Any]] = []
    for result in results:
        match_score = float(result.score)
        if match_score < min_score:
            continue
        path = Path(result.path).expanduser().resolve()
        facts = _result_facts(
            path,
            start_line=int(result.start_line),
            end_line=int(result.end_line),
            memory_dir=memory_dir,
            compact_memory_dir=compact_memory_dir,
            cache=cache,
        )
        evidence_time = _parse_iso(str(facts.get("evidence_at") or ""))
        if evidence_time is None:
            evidence_time = _date_from_filename(path)
        freshness = _freshness(
            evidence_time,
            current_time,
            half_life_days=recency_half_life_days,
        )
        rank_score = match_score * (1.0 + recency_weight * freshness)
        metadata = dict(result.metadata or {})
        metadata.update(
            {
                "match_score": match_score,
                "rank_score": rank_score,
                "freshness": freshness,
                "evidence_at": facts.get("evidence_at", ""),
                "source_kind": facts.get("source_kind", ""),
                "lineage_keys": sorted(facts.get("lineage_keys", set())),
            }
        )
        items.append(
            {
                "path": str(path),
                "start_line": int(result.start_line),
                "end_line": int(result.end_line),
                "score": rank_score,
                "match_score": match_score,
                "snippet": result.snippet,
                "source": result.source.value,
                "source_kind": facts.get("source_kind", ""),
                "evidence_at": facts.get("evidence_at", ""),
                "freshness": freshness,
                "metadata": metadata,
                "raw_metric": result.raw_metric,
                "_lineage_keys": set(facts.get("lineage_keys", set())),
                "_block_identity": str(facts.get("block_identity") or ""),
            }
        )

    items.sort(key=lambda item: (item["score"], item["match_score"]), reverse=True)
    collapsed = _collapse_lineage(items)
    for item in collapsed:
        item.pop("_lineage_keys", None)
        item.pop("_block_identity", None)
    return collapsed[:max_results]


def build_durable_reconcile_candidates(
    search_items: Iterable[Dict[str, Any]],
    *,
    memory_dir: Path,
    max_candidates: int = 5,
) -> List[Dict[str, Any]]:
    root = memory_dir.expanduser().resolve()
    candidates: List[Dict[str, Any]] = []
    seen = set()
    for item in search_items:
        path = Path(str(item.get("path") or "")).expanduser().resolve()
        if path.parent != root or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        try:
            blocks = parse_memory_blocks(text)
        except (ValueError, KeyError):
            continue
        block = _find_overlapping_block(blocks, int(item.get("start_line") or 0), int(item.get("end_line") or 0))
        if block is None:
            continue
        durable_key = str(block.get("durable_idempotency_key") or "")
        if not durable_key or durable_key in seen:
            continue
        seen.add(durable_key)
        memory_json = dict(block.get("meta", {}).get("memory_json") or {})
        candidates.append(
            {
                "candidate_id": f"candidate_{len(candidates) + 1}",
                "path": str(path),
                "durable_idempotency_key": durable_key,
                "topic": str(memory_json.get("topic") or "")[:500],
                "applicability": str(memory_json.get("applicability") or "")[:1000],
                "conclusions": _trim_list(memory_json.get("conclusions"), 5, 1600),
                "solutions": _trim_list(memory_json.get("solutions"), 5, 1600),
                "aliases": _trim_list(memory_json.get("aliases"), 16, 128),
                "confidence": str(memory_json.get("confidence") or ""),
                "evidence_at": str(memory_json.get("evidence_at") or ""),
                "evidence_hashes": _trim_list(memory_json.get("evidence_hashes"), 24, 128),
                "match_score": float(item.get("match_score", item.get("score", 0.0))),
            }
        )
        if len(candidates) >= max_candidates:
            break
    return candidates


def parse_compact_created_at(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    match = CREATED_AT_RE.search(text)
    return match.group("value") if match else ""


def _result_facts(
    path: Path,
    *,
    start_line: int,
    end_line: int,
    memory_dir: Path,
    compact_memory_dir: Path,
    cache: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    cache_key = str(path)
    cached = cache.get(cache_key)
    if cached is None:
        cached = _load_path_facts(
            path,
            memory_dir=memory_dir,
            compact_memory_dir=compact_memory_dir,
        )
        cache[cache_key] = cached

    if cached["source_kind"] != "durable":
        return cached
    block = _find_overlapping_block(cached.get("blocks", []), start_line, end_line)
    if block is None:
        return cached
    block_text = _block_text(cached["text"], block)
    memory_json = dict(block.get("meta", {}).get("memory_json") or {})
    evidence_at = str(memory_json.get("evidence_at") or "")
    if not evidence_at:
        evidence_at = _first_match(EVIDENCE_AT_RE, block_text) or _first_match(UPDATED_AT_RE, block_text)
    lineage_keys = set(str(item) for item in block.get("meta", {}).get("source_capture_history", []) if str(item))
    source_capture_id = str(memory_json.get("source_capture_id") or block.get("source_capture_id") or "")
    if source_capture_id:
        lineage_keys.add(source_capture_id)
    lineage_keys.update(str(item) for item in memory_json.get("evidence_hashes", []) if str(item))
    lineage_keys.update(EVIDENCE_HASH_RE.findall(block_text))
    parent_match = PARENT_CAPTURE_RE.search(block_text)
    if parent_match:
        lineage_keys.add(parent_match.group("value"))
    return {
        "source_kind": "durable",
        "evidence_at": evidence_at,
        "lineage_keys": lineage_keys,
        "block_identity": str(block.get("durable_idempotency_key") or ""),
    }


def _load_path_facts(path: Path, *, memory_dir: Path, compact_memory_dir: Path) -> Dict[str, Any]:
    durable_root = memory_dir.expanduser().resolve()
    compact_root = compact_memory_dir.expanduser().resolve()
    if path.parent == compact_root:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            text = ""
        lineage_keys = set()
        compact_match = COMPACT_ID_RE.search(text)
        if compact_match:
            lineage_keys.add(compact_match.group("value"))
        lineage_keys.update(EVIDENCE_HASH_RE.findall(text))
        return {
            "source_kind": "compact",
            "evidence_at": _first_match(CREATED_AT_RE, text),
            "lineage_keys": lineage_keys,
            "block_identity": str(path),
            "text": text,
        }
    if path.parent == durable_root:
        try:
            text = path.read_text(encoding="utf-8")
            blocks = parse_memory_blocks(text)
        except (OSError, UnicodeError, ValueError, KeyError):
            text = ""
            blocks = []
        return {
            "source_kind": "durable",
            "evidence_at": _first_match(UPDATED_AT_RE, text),
            "lineage_keys": set(),
            "block_identity": "",
            "text": text,
            "blocks": blocks,
        }
    return {
        "source_kind": "unknown",
        "evidence_at": "",
        "lineage_keys": set(),
        "block_identity": "",
        "text": "",
    }


def _collapse_lineage(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    collapsed: List[Dict[str, Any]] = []
    for item in items:
        keys = item.get("_lineage_keys", set())
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(collapsed)
                if _items_share_lineage(item, existing)
            ),
            None,
        )
        if duplicate_index is None:
            collapsed.append(item)
            continue
        existing = collapsed[duplicate_index]
        if existing.get("source_kind") == "compact" and item.get("source_kind") == "durable":
            item["metadata"]["collapsed_evidence_path"] = existing["path"]
            collapsed[duplicate_index] = item
        elif existing.get("source_kind") == "durable" and item.get("source_kind") == "compact":
            existing["metadata"]["collapsed_evidence_path"] = item["path"]
        elif item["score"] > existing["score"]:
            collapsed[duplicate_index] = item
    collapsed.sort(key=lambda item: (item["score"], item["match_score"]), reverse=True)
    return collapsed


def _items_share_lineage(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    left_keys = left.get("_lineage_keys", set())
    right_keys = right.get("_lineage_keys", set())
    if not left_keys or not left_keys.intersection(right_keys):
        return False
    if left.get("path") == right.get("path"):
        return bool(left.get("_block_identity")) and left.get("_block_identity") == right.get("_block_identity")
    return {left.get("source_kind"), right.get("source_kind")} == {"durable", "compact"}


def _find_overlapping_block(blocks: Iterable[Dict[str, Any]], start_line: int, end_line: int) -> Optional[Dict[str, Any]]:
    for block in blocks:
        if int(block["start_line"]) <= max(start_line, 1) <= int(block["end_line"]):
            return block
        if start_line <= int(block["start_line"]) <= max(end_line, start_line):
            return block
    return None


def _block_text(text: str, block: Dict[str, Any]) -> str:
    return text[int(block["start_offset"]) : int(block["end_offset"])]


def _first_match(pattern: re.Pattern, text: str) -> str:
    match = pattern.search(text)
    return match.group("value") if match else ""


def _parse_iso(value: str) -> Optional[dt.datetime]:
    text = value.strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _date_from_filename(path: Path) -> Optional[dt.datetime]:
    try:
        parsed = dt.datetime.strptime(path.name[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.timezone.utc)


def _freshness(evidence_at: Optional[dt.datetime], now: dt.datetime, *, half_life_days: float) -> float:
    if evidence_at is None:
        return 0.0
    age_seconds = max(0.0, now.timestamp() - evidence_at.timestamp())
    age_days = age_seconds / 86400.0
    return math.pow(2.0, -age_days / half_life_days)


def _trim_list(value: Any, max_items: int, max_chars: int) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:max_chars] for item in value[:max_items]]
