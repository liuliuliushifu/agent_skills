#!/usr/bin/env python3
import argparse
import collections
import datetime
import json
import os
from pathlib import Path
from typing import Dict, Iterable, List


SCRIPT_DIR = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
METRICS_LOG = Path(os.environ.get("REME_MEMORY_METRICS_LOG", str(CODEX_HOME / "memories/reme-memory/memory_workflow_events.jsonl")))


def _load_events() -> List[Dict]:
    if not METRICS_LOG.exists():
        return []
    events = []
    for line in METRICS_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(json.loads(line))
    return events


def _filter_events(events: Iterable[Dict], days: int) -> List[Dict]:
    if days <= 0:
        return list(events)
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    filtered = []
    for event in events:
        ts = event.get("timestamp", "")
        try:
            dt = datetime.datetime.fromisoformat(ts)
        except ValueError:
            continue
        if dt >= cutoff:
            filtered.append(event)
    return filtered


def _summarize(events: List[Dict]) -> Dict:
    prepares = [event for event in events if event.get("event") == "prepare"]
    finalizes = [event for event in events if event.get("event") == "finalize"]

    total_reads = len(prepares)
    hit_reads = sum(1 for event in prepares if event.get("hit"))
    total_hit_items = sum(int(event.get("result_count", 0)) for event in prepares)

    total_writes = sum(1 for event in finalizes if event.get("write_success"))
    skipped_writes = sum(1 for event in finalizes if not event.get("write_attempted"))

    path_counter = collections.Counter()
    for event in prepares:
        for path in event.get("paths", []):
            path_counter[path] += 1

    return {
        "log_path": str(METRICS_LOG),
        "events_total": len(events),
        "reads_total": total_reads,
        "reads_with_hits": hit_reads,
        "read_hit_rate": round(hit_reads / total_reads, 4) if total_reads else 0.0,
        "hit_items_total": total_hit_items,
        "writes_total": total_writes,
        "writes_skipped": skipped_writes,
        "top_memory_paths": path_counter.most_common(10),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Report local ReMe memory workflow metrics.")
    parser.add_argument("--days", type=int, default=7, help="Only count events in the last N days; 0 means all")
    args = parser.parse_args()

    events = _filter_events(_load_events(), args.days)
    print(json.dumps(_summarize(events), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
