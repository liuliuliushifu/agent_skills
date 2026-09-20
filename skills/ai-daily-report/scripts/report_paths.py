#!/usr/bin/env python3
"""Resolve local AI daily report archive paths."""

import argparse
import os
import re
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
DEFAULT_DAILY_REPORT_DIR = Path(os.environ.get("AI_DAILY_REPORT_DIR", str(DEFAULT_CODEX_HOME / "daily_report")))
REPORT_ROOT = DEFAULT_DAILY_REPORT_DIR / "report_files"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_date(target_date: str) -> str:
    if not DATE_RE.match(target_date or ""):
        raise ValueError(f"date must be YYYY-MM-DD: {target_date!r}")
    return target_date


def month_key(target_date: str) -> str:
    target_date = validate_date(target_date)
    return target_date[:4] + target_date[5:7]


def report_dir(target_date: str) -> Path:
    return REPORT_ROOT / month_key(target_date)


def report_path(target_date: str) -> Path:
    return report_dir(target_date) / f"{validate_date(target_date)}.md"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("root", "month", "dir", "path"))
    parser.add_argument("date", nargs="?", help="target date for month/dir/path")
    args = parser.parse_args(argv)

    if args.kind == "root":
        print(REPORT_ROOT)
        return 0
    if not args.date:
        parser.error("date is required for month/dir/path")
    if args.kind == "month":
        print(month_key(args.date))
    elif args.kind == "dir":
        print(report_dir(args.date))
    else:
        print(report_path(args.date))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
