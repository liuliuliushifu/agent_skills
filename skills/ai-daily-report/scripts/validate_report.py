#!/usr/bin/env python3
"""Validate generated AI daily report structure before sync."""

from __future__ import annotations

import argparse
import json
import os
import sys
import re
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from project_registry import load_projects, resolve_project_key  # noqa: E402


CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
DAILY_DIR = Path(os.environ.get("AI_DAILY_REPORT_DIR", str(CODEX_HOME / "daily_report")))
PROJECTS = load_projects(DAILY_DIR, SCRIPT_DIR)
TASK_ID_RE = re.compile(r"^\d{8}-\d+$")
TRACK_ID_RE = re.compile(r"^TRK-\d{8}-\d+$")


def strip_callout_prefix(line: str) -> str:
    if line.startswith("> "):
        return line[2:]
    if line.startswith(">"):
        return line[1:]
    return line


def split_markdown_row(line: str) -> list[str]:
    text = line.strip()
    if not (text.startswith("|") and text.endswith("|")):
        return []
    return [cell.strip().strip("`") for cell in text.strip("|").split("|")]


def is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(cell and set(cell) <= {"-", ":", " "} for cell in cells)


def extract_project_progress_rows(markdown: str) -> tuple[list[tuple[int, list[str]]], bool, list[str]]:
    lines = markdown.splitlines()
    in_section = False
    found = False
    rows: list[tuple[int, list[str]]] = []
    errors: list[str] = []
    for line_no, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("## "):
            title = stripped.lstrip("#").strip()
            in_section = title == "项目进展"
            found = found or in_section
            continue
        if not in_section:
            continue
        if not stripped:
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        cells = split_markdown_row(line)
        if not cells:
            errors.append(f"line {line_no}: 项目进展 must use table rows only")
            continue
        if is_separator_row(cells):
            continue
        first = cells[0].strip()
        if not first or first in {"项目", "项目名", "项目/专题", "注册项目", "注册项目名", "Project"}:
            continue
        rows.append((line_no, cells))
    return rows, found, errors


def extract_state_json(markdown: str):
    lines = markdown.splitlines()
    for idx, line in enumerate(lines):
        normalized = strip_callout_prefix(line).strip()
        if normalized.startswith("[!info]") and "ai-daily-state" in normalized:
            in_code = False
            buf = []
            for raw in lines[idx + 1 :]:
                text = strip_callout_prefix(raw)
                if text.strip().startswith("```"):
                    if not in_code:
                        in_code = True
                        continue
                    break
                if in_code:
                    buf.append(text)
            if buf:
                return json.loads("\n".join(buf))
    return None


def find_section_line(markdown: str, title: str) -> int | None:
    expected = f"## {title}"
    for line_no, line in enumerate(markdown.splitlines(), 1):
        if line.strip() == expected:
            return line_no
    return None


def validate_project_value(value: str, active_keys: set[str]) -> str:
    key = resolve_project_key(value, PROJECTS)
    if key and key in active_keys:
        return ""
    return f"unknown or inactive project: {value}"


def validate_report(path: Path) -> list[str]:
    markdown = path.read_text(encoding="utf-8")
    active_keys = {project["key"] for project in PROJECTS if project.get("active", True)}
    errors: list[str] = []

    rows, found_project_section, project_section_errors = extract_project_progress_rows(markdown)
    errors.extend(project_section_errors)
    if not found_project_section:
        errors.append("missing required section: 项目进展")
    for line_no, cells in rows:
        message = validate_project_value(cells[0], active_keys)
        if message:
            errors.append(f"line {line_no}: 项目进展 contains {message}")

    try:
        state = extract_state_json(markdown)
    except Exception as exc:
        errors.append(f"invalid ai-daily-state JSON: {exc}")
        state = None
    if not state:
        errors.append("missing ai-daily-state JSON callout")
        return errors

    project_line = find_section_line(markdown, "项目进展")
    tracking_line = find_section_line(markdown, "跟踪事项")
    learning_line = find_section_line(markdown, "学习到了哪些事")
    token_line = find_section_line(markdown, "token使用")
    risk_line = find_section_line(markdown, "风险与阻塞")
    if tracking_line is None:
        errors.append("missing required section: 跟踪事项")
    if project_line is not None and tracking_line is not None and tracking_line < project_line:
        errors.append("section order error: 跟踪事项 must appear after 项目进展")
    if token_line is None:
        errors.append("missing required section: token使用")
    if learning_line is not None and token_line is not None and token_line < learning_line:
        errors.append("section order error: token使用 must appear after 学习到了哪些事")
    if token_line is not None and risk_line is not None and risk_line < token_line:
        errors.append("section order error: token使用 must appear before 风险与阻塞")
    for forbidden in ("## 前次计划回顾", "## 下一步计划"):
        if forbidden in markdown:
            errors.append(f"forbidden section after tracking merge: {forbidden}")

    for section_name in ("task_updates", "next_long_tasks"):
        items = state.get(section_name) or []
        if not isinstance(items, list):
            errors.append(f"ai-daily-state.{section_name} must be a list")
            continue
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"ai-daily-state.{section_name}[{idx}] must be an object")
                continue
            project = str(item.get("project") or "").strip()
            if project:
                message = validate_project_value(project, active_keys)
                if message:
                    errors.append(f"ai-daily-state.{section_name}[{idx}] contains {message}")
            task_id = str(item.get("task_id") or "").strip()
            if not task_id:
                errors.append(f"ai-daily-state.{section_name}[{idx}] is missing task_id")
            elif not TASK_ID_RE.match(task_id):
                errors.append(f"ai-daily-state.{section_name}[{idx}] has invalid task_id: {task_id}")
    for section_name in ("tracking_updates", "next_tracking", "tracking_items"):
        items = state.get(section_name) or []
        if not isinstance(items, list):
            errors.append(f"ai-daily-state.{section_name} must be a list")
            continue
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"ai-daily-state.{section_name}[{idx}] must be an object")
                continue
            project = str(item.get("project") or "").strip()
            if project:
                message = validate_project_value(project, active_keys)
                if message:
                    errors.append(f"ai-daily-state.{section_name}[{idx}] contains {message}")
            track_id = str(item.get("track_id") or "").strip()
            if not track_id:
                errors.append(f"ai-daily-state.{section_name}[{idx}] is missing track_id")
            elif not TRACK_ID_RE.match(track_id):
                errors.append(f"ai-daily-state.{section_name}[{idx}] has invalid track_id: {track_id}")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="generated Markdown report path")
    args = parser.parse_args(argv)

    errors = validate_report(Path(args.report))
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"report validation passed: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
