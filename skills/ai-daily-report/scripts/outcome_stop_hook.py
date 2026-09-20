#!/usr/bin/env python3
"""Codex Stop hook that records a normalized turn bundle for daily reports."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from extract_turn_bundle import ExtractError, extract_bundle_fast  # noqa: E402


TZ = dt.timezone(dt.timedelta(hours=8))
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
DAILY_DIR = Path(os.environ.get("AI_DAILY_REPORT_DIR", str(CODEX_HOME / "daily_report")))
OUTCOME_ROOT = Path(os.environ.get("AI_DAILY_OUTCOME_DIR", str(DAILY_DIR / "outcomes")))
RETRY_DELAYS = (0.0, 0.25, 0.5, 1.0)
VERBOSE_OUTPUT = os.environ.get("AI_DAILY_OUTCOME_HOOK_VERBOSE", "").lower() in {"1", "true", "yes"}


def now_iso() -> str:
    return dt.datetime.now(TZ).replace(microsecond=0).isoformat()


def read_stdin_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        raise ExtractError("empty hook payload")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExtractError(f"invalid hook payload json: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExtractError("hook payload is not an object")
    return payload


def bundle_path(bundle: dict[str, Any]) -> Path:
    day = str(bundle.get("bundle_date") or dt.datetime.now(TZ).date().isoformat())
    year, month = day[:4], day[5:7]
    session_id = str(bundle.get("session_id") or "unknown")
    turn_id = str(bundle.get("turn_id") or "unknown")
    name = f"{session_id}__{turn_id}.json".replace("/", "_")
    return OUTCOME_ROOT / "turn_bundles" / year / month / day / name


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_diagnostic(message: str, payload: dict[str, Any] | None = None) -> None:
    path = OUTCOME_ROOT / "logs" / "outcome_stop_hook_diagnostics.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "recorded_at": now_iso(),
        "message": message,
        "payload": payload or {},
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def emit_debug(data: dict[str, Any]) -> None:
    if VERBOSE_OUTPUT:
        print(json.dumps(data, ensure_ascii=False, sort_keys=True))


def main() -> int:
    try:
        payload = read_stdin_payload()
        event_name = payload.get("hook_event_name")
        if event_name != "Stop":
            emit_debug({"status": "skipped", "reason": "not_stop", "event": event_name})
            return 0

        transcript_path = payload.get("transcript_path")
        turn_id = payload.get("turn_id")
        if not isinstance(transcript_path, str) or not transcript_path:
            raise ExtractError("missing transcript_path")
        if not isinstance(turn_id, str) or not turn_id:
            raise ExtractError("missing turn_id")

        last_error: Exception | None = None
        bundles: list[dict[str, Any]] = []
        for delay in RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                bundle = extract_bundle_fast(Path(transcript_path), turn_id=turn_id)
                if bundle:
                    bundles = [bundle]
                    break
            except Exception as exc:  # hook should log and return cleanly
                last_error = exc

        if not bundles:
            message = f"turn bundle not available for turn_id={turn_id}"
            if last_error:
                message += f": {last_error}"
            append_diagnostic(message, {"transcript_path": transcript_path, "turn_id": turn_id})
            emit_debug({"status": "skipped", "reason": "bundle_not_available", "turn_id": turn_id})
            return 0

        bundle = bundles[0]
        path = bundle_path(bundle)
        write_json_atomic(path, bundle)
        emit_debug(
            {
                "status": "recorded",
                "turn_id": turn_id,
                "bundle_hash": bundle.get("bundle_hash"),
                "path": str(path),
            }
        )
        return 0
    except Exception as exc:  # keep Codex Stop resilient
        append_diagnostic(str(exc))
        emit_debug({"status": "error", "error": str(exc)})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
