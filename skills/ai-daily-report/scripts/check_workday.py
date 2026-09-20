#!/usr/bin/env python3
"""Decide whether the AI daily report should run for a date."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
DEFAULT_DAILY_REPORT_DIR = Path(os.environ.get("AI_DAILY_REPORT_DIR", str(DEFAULT_CODEX_HOME / "daily_report")))
CACHE_DIR = Path(
    os.environ.get("AI_DAILY_CALENDAR_CACHE_DIR", str(DEFAULT_DAILY_REPORT_DIR / "calendar"))
).expanduser()
HTTP_TIMEOUT_SEC = float(os.environ.get("AI_DAILY_CALENDAR_HTTP_TIMEOUT_SEC", "8"))
UPGRADE_TIMEOUT_SEC = int(os.environ.get("AI_DAILY_CALENDAR_UPGRADE_TIMEOUT_SEC", "900"))
PIP_PACKAGE = os.environ.get("AI_DAILY_CALENDAR_PIP_PACKAGE", "chinesecalendar")
PRIMARY_URL_TEMPLATE = os.environ.get(
    "AI_DAILY_CALENDAR_PRIMARY_URL_TEMPLATE",
    "https://api.jiejiariapi.com/v1/holidays/{year}",
)
SECONDARY_URL_TEMPLATE = os.environ.get(
    "AI_DAILY_CALENDAR_SECONDARY_URL_TEMPLATE",
    "https://timor.tech/api/holiday/info/{date}",
)
HTTP_USER_AGENT = os.environ.get("AI_DAILY_CALENDAR_USER_AGENT", "codex-ai-daily-report/1.0")


class Decision:
    def __init__(self, should_run: bool, source: str, reason: str) -> None:
        self.should_run = should_run
        self.source = source
        self.reason = reason


def log(message: str) -> None:
    print(f"calendar_check {message}", flush=True)


def parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date {value!r}; expected YYYY-MM-DD") from exc


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": HTTP_USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return json.loads(resp.read().decode(charset))


def weekday_decision(target_date: dt.date, source: str) -> Decision:
    if target_date.weekday() < 5:
        return Decision(True, source, f"weekday_{target_date.isoweekday()}")
    return Decision(False, source, f"weekend_{target_date.isoweekday()}")


def decide_from_jiejiari_payload(payload: dict, target_date: dt.date, source: str) -> Decision:
    key = target_date.isoformat()
    item = payload.get(key)
    if isinstance(item, dict):
        is_off_day = bool(item.get("isOffDay"))
        name = str(item.get("name") or ("offday" if is_off_day else "adjusted_workday"))
        return Decision(not is_off_day, source, name)
    return weekday_decision(target_date, f"{source}+weekday")


def try_jiejiari(target_date: dt.date) -> Decision | None:
    try:
        url = PRIMARY_URL_TEMPLATE.format(year=target_date.year, date=target_date.isoformat())
        payload = fetch_json(url)
    except Exception as exc:  # noqa: BLE001 - log and try the next source.
        log(f"source=jiejiariapi status=failed error={type(exc).__name__}")
        return None
    if not isinstance(payload, dict):
        log("source=jiejiariapi status=failed error=invalid_payload")
        return None

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"jiejiariapi-{target_date.year}.json"
    cache_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"source=jiejiariapi status=ok cache={cache_file}")
    return decide_from_jiejiari_payload(payload, target_date, "jiejiariapi")


def try_timor(target_date: dt.date) -> Decision | None:
    try:
        url = SECONDARY_URL_TEMPLATE.format(year=target_date.year, date=target_date.isoformat())
        payload = fetch_json(url)
    except Exception as exc:  # noqa: BLE001 - log and try the next source.
        log(f"source=timor status=failed error={type(exc).__name__}")
        return None
    if not isinstance(payload, dict):
        log("source=timor status=failed error=invalid_payload")
        return None

    type_info = payload.get("type")
    if not isinstance(type_info, dict):
        log("source=timor status=failed error=missing_type")
        return None

    type_code = type_info.get("type")
    name = str(type_info.get("name") or f"type_{type_code}")
    if type_code in (0, 3):
        log("source=timor status=ok")
        return Decision(True, "timor", name)
    if type_code in (1, 2):
        log("source=timor status=ok")
        return Decision(False, "timor", name)

    log(f"source=timor status=failed error=unknown_type_{type_code}")
    return None


def try_jiejiari_cache(target_date: dt.date) -> Decision | None:
    cache_file = CACHE_DIR / f"jiejiariapi-{target_date.year}.json"
    if not cache_file.is_file():
        return None
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - log and try the next source.
        log(f"source=jiejiariapi-cache status=failed error={type(exc).__name__}")
        return None
    if not isinstance(payload, dict):
        log("source=jiejiariapi-cache status=failed error=invalid_payload")
        return None

    log(f"source=jiejiariapi-cache status=ok cache={cache_file}")
    return decide_from_jiejiari_payload(payload, target_date, "jiejiariapi-cache")


def try_chinese_calendar(target_date: dt.date) -> Decision | None:
    try:
        from chinese_calendar import get_holiday_detail, is_workday
    except Exception as exc:  # noqa: BLE001 - package may not be installed yet.
        log(f"source=chinese_calendar status=failed error={type(exc).__name__}")
        return None

    try:
        should_run = bool(is_workday(target_date))
        is_holiday, holiday_name = get_holiday_detail(target_date)
    except Exception as exc:  # noqa: BLE001 - keep the checker non-fatal.
        log(f"source=chinese_calendar status=failed error={type(exc).__name__}")
        return None

    if is_holiday:
        reason = str(getattr(holiday_name, "value", holiday_name) or "holiday")
    elif should_run:
        reason = "workday"
    else:
        reason = "non_workday"
    log("source=chinese_calendar status=ok")
    return Decision(should_run, "chinese_calendar", reason)


def upgrade_chinese_calendar() -> None:
    if os.environ.get("AI_DAILY_CALENDAR_SKIP_UPGRADE") == "1":
        log("upgrade=skipped reason=env")
        return

    cmd = [sys.executable, "-m", "pip", "install", "--user", "-U", PIP_PACKAGE]
    env = os.environ.copy()
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    env.setdefault("PIP_NO_INPUT", "1")
    log(f"upgrade=start package={PIP_PACKAGE} timeout={UPGRADE_TIMEOUT_SEC}s")
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=UPGRADE_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log("upgrade=failed error=timeout")
        return
    except Exception as exc:  # noqa: BLE001 - upgrade must not affect the decision.
        log(f"upgrade=failed error={type(exc).__name__}")
        return

    if result.returncode == 0:
        log("upgrade=ok")
    else:
        log(f"upgrade=failed rc={result.returncode}")


def decide(target_date: dt.date) -> Decision:
    if os.environ.get("AI_DAILY_FORCE") == "1":
        return Decision(True, "force", "AI_DAILY_FORCE")

    for source in (
        try_jiejiari,
        try_timor,
        try_jiejiari_cache,
        try_chinese_calendar,
    ):
        decision = source(target_date)
        if decision is not None:
            return decision

    return weekday_decision(target_date, "weekday-rule")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check whether a date is a Chinese workday.")
    parser.add_argument("date", nargs="?", type=parse_date, default=dt.date.today())
    args = parser.parse_args()

    decision = decide(args.date)
    action = "workday" if decision.should_run else "skip"
    log(
        f"decision={action} date={args.date.isoformat()} "
        f"source={decision.source} reason={decision.reason}"
    )
    upgrade_chinese_calendar()
    return 0 if decision.should_run else 10


if __name__ == "__main__":
    sys.exit(main())
