#!/usr/bin/env python3
"""Normalize generated AI daily report markdown before validation/sync."""

from __future__ import annotations

import argparse
import json
import unicodedata
from pathlib import Path


def split_table_row(line: str) -> list[str]:
    text = line.strip()
    if not (text.startswith("|") and text.endswith("|")):
        return []
    return [cell.strip() for cell in text.strip("|").split("|")]


def is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(cell and set(cell) <= {"-", ":", " "} for cell in cells)


def join_unique(values: list[str]) -> str:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return "；".join(ordered)


def display_width(value: str) -> int:
    width = 0
    for char in str(value):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def pad_display(value: str, width: int, align: str) -> str:
    text = str(value)
    padding = max(0, width - display_width(text))
    if align == "right":
        return " " * padding + text
    return text + " " * padding


def aligned_markdown_table(headers: list[str], rows: list[list[str]], aligns: list[str]) -> list[str]:
    table_rows = [headers] + rows
    widths = [
        max(3, *(display_width(row[index]) for row in table_rows))
        for index in range(len(headers))
    ]

    def render(row: list[str]) -> str:
        return "| " + " | ".join(
            pad_display(row[index], widths[index], aligns[index])
            for index in range(len(headers))
        ) + " |"

    separators = []
    for width, align in zip(widths, aligns):
        if align == "right":
            separators.append("-" * (width - 1) + ":")
        else:
            separators.append("-" * width)
    return [render(headers), render(separators)] + [render(row) for row in rows]


def section_bounds(lines: list[str], title: str) -> tuple[int, int] | None:
    start = None
    expected = f"## {title}"
    for idx, line in enumerate(lines):
        if line.strip() == expected:
            start = idx
            break
    if start is None:
        return None

    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if lines[idx].strip().startswith("## "):
            end = idx
            break
    return start, end


def strip_callout_prefix(line: str) -> str:
    if line.startswith("> "):
        return line[2:]
    if line.startswith(">"):
        return line[1:]
    return line


def sanitize_state_projects(value) -> bool:
    changed = False
    if isinstance(value, dict):
        project = value.get("project")
        if isinstance(project, str) and project.strip().lower() == "unassigned":
            value.pop("project", None)
            changed = True
        for child in value.values():
            changed = sanitize_state_projects(child) or changed
    elif isinstance(value, list):
        for child in value:
            changed = sanitize_state_projects(child) or changed
    return changed


def normalize_state_json(lines: list[str]) -> tuple[list[str], bool]:
    callout_start = None
    for idx, line in enumerate(lines):
        normalized = strip_callout_prefix(line).strip()
        if normalized.startswith("[!info]") and "ai-daily-state" in normalized:
            callout_start = idx
            break
    if callout_start is None:
        return lines, False

    code_start = None
    code_end = None
    for idx in range(callout_start + 1, len(lines)):
        text = strip_callout_prefix(lines[idx]).strip()
        if text.startswith("```"):
            if code_start is None:
                code_start = idx
            else:
                code_end = idx
                break
    if code_start is None or code_end is None:
        return lines, False

    raw_json = "\n".join(strip_callout_prefix(line) for line in lines[code_start + 1 : code_end])
    try:
        state = json.loads(raw_json)
    except Exception:
        return lines, False
    if not sanitize_state_projects(state):
        return lines, False

    state_lines = json.dumps(state, ensure_ascii=False, indent=2).splitlines()
    quoted_state = ["> " + line for line in state_lines]
    normalized = lines[: code_start + 1] + quoted_state + lines[code_end:]
    return normalized, True


def normalize_project_progress(lines: list[str]) -> tuple[list[str], bool]:
    bounds = section_bounds(lines, "项目进展")
    if bounds is None:
        return lines, False
    start, end = bounds

    section = lines[start:end]
    if len(section) < 3:
        return lines, False

    header = section[1]
    separator = section[2]
    body = section[3:]

    merged: dict[str, list[list[str]]] = {}
    order: list[str] = []
    preserved: list[str] = []

    for raw_line in body:
        cells = split_table_row(raw_line)
        if not cells:
            preserved.append(raw_line)
            continue
        if is_separator_row(cells):
            preserved.append(raw_line)
            continue
        if len(cells) < 7:
            preserved.append(raw_line)
            continue
        task_id = cells[1].strip().strip("`")
        if not task_id:
            preserved.append(raw_line)
            continue
        if task_id not in merged:
            merged[task_id] = [cells]
            order.append(task_id)
        else:
            merged[task_id].append(cells)

    if all(len(rows) == 1 for rows in merged.values()):
        return lines, False

    normalized_rows: list[str] = []
    for task_id in order:
        rows = merged[task_id]
        if len(rows) == 1:
            row = rows[0]
        else:
            row = rows[0][:]
            row[0] = rows[0][0]
            row[2] = join_unique([r[2] for r in rows])
            row[3] = join_unique([r[3] for r in rows])
            row[4] = join_unique([r[4] for r in rows])
            row[5] = join_unique([r[5] for r in rows])
            row[6] = join_unique([r[6] for r in rows])
        formatted = [row[0], f"`{task_id}`"] + row[2:7]
        normalized_rows.append("| " + " | ".join(formatted) + " |")

    new_section = section[:3] + normalized_rows + preserved
    normalized_lines = lines[:start] + new_section + lines[end:]
    return normalized_lines, True


def normalize_token_usage(lines: list[str]) -> tuple[list[str], bool]:
    bounds = section_bounds(lines, "token使用")
    if bounds is None:
        return lines, False
    start, end = bounds
    section = lines[start:end]

    table_start = None
    for idx, line in enumerate(section[1:], start=1):
        if split_table_row(line):
            table_start = idx
            break
    if table_start is None:
        return lines, False

    table_end = table_start
    while table_end < len(section) and split_table_row(section[table_end]):
        table_end += 1

    table_cells = [split_table_row(line) for line in section[table_start:table_end]]
    table_cells = [cells for cells in table_cells if cells and not is_separator_row(cells)]
    if len(table_cells) < 2:
        return lines, False

    headers = table_cells[0]
    rows = table_cells[1:]
    if len(headers) != 7 or any(len(row) != len(headers) for row in rows):
        return lines, False

    aligned = aligned_markdown_table(
        headers,
        rows,
        ["left", "right", "right", "right", "right", "right", "right"],
    )
    new_section = section[:table_start] + aligned + section[table_end:]
    normalized_lines = lines[:start] + new_section + lines[end:]
    return normalized_lines, normalized_lines != lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="report path to normalize")
    args = parser.parse_args(argv)

    path = Path(args.report)
    lines = path.read_text(encoding="utf-8").splitlines()
    normalized, changed_project = normalize_project_progress(lines)
    normalized, changed_token = normalize_token_usage(normalized)
    normalized, changed_state = normalize_state_json(normalized)
    changed = changed_project or changed_token or changed_state
    if changed:
        path.write_text("\n".join(normalized) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
