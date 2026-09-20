#!/usr/bin/env python3
"""Maintain the AI daily report plan ledger.

The ledger keeps durable task records, including completed and dropped work.
Prompt context stays compact by selecting the previous report's task IDs plus
currently active tasks instead of loading the full ledger.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from project_registry import infer_project_key, load_projects, project_name, resolve_project_key  # noqa: E402

DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(SCRIPT_DIR.parents[2])))
DEFAULT_DAILY_REPORT_DIR = Path(os.environ.get("AI_DAILY_REPORT_DIR", str(DEFAULT_CODEX_HOME / "daily_report")))
DEFAULT_STATE = DEFAULT_DAILY_REPORT_DIR / "plan_state.json"
DEFAULT_REPORTS_DIR = DEFAULT_DAILY_REPORT_DIR / "report_files"
PROJECTS = load_projects(DEFAULT_DAILY_REPORT_DIR, SCRIPT_DIR)
REPORT_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")

ACTIVE_STATUSES = {"open", "in_progress", "blocked", "deferred"}
TERMINAL_STATUSES = {"done", "dropped"}
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
SOURCE_VALUES = {"next_plan", "risk", "carryover", "user_request", "jira", "ledger", "report"}
TASK_ID_RE = re.compile(r"^\d{8}-\d+$")
TRACK_ID_RE = re.compile(r"^TRK-\d{8}-\d+$")
TRACKING_MODES = {"none", "long_term"}
PROGRESS_STYLES = {"outcome", "process"}
ITEM_TYPES = {"task", "track"}


def now_iso():
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).replace(microsecond=0).isoformat()


def today():
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).date().isoformat()


def read_json(path):
    if not path.exists():
        return {"version": 1, "updated_at": None, "tasks": []}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        data = {"version": 1, "updated_at": None, "tasks": data}
    if not isinstance(data, dict):
        raise ValueError(f"state must be a JSON object: {path}")
    data.setdefault("version", 1)
    data.setdefault("updated_at", None)
    data.setdefault("tasks", [])
    if not isinstance(data["tasks"], list):
        raise ValueError("state.tasks must be a list")
    data["tasks"] = [normalize_task(item) for item in data["tasks"]]
    return data


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    data["version"] = 1
    data["updated_at"] = now_iso()
    data["tasks"] = sort_tasks([normalize_task(item) for item in data.get("tasks", [])])
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def normalize_status(status):
    status = (status or "open").strip()
    if status not in ALL_STATUSES:
        raise ValueError(f"invalid status: {status}")
    return status


def normalize_priority(priority):
    priority = (priority or "P2").strip().upper()
    if priority not in PRIORITY_ORDER:
        raise ValueError(f"invalid priority: {priority}")
    return priority


def normalize_source(source):
    source = (source or "ledger").strip()
    if source not in SOURCE_VALUES:
        source = "ledger"
    return source


def normalize_string_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    result = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def normalize_tracking(value):
    if not isinstance(value, dict):
        value = {}
    keywords = normalize_string_list(value.get("keywords"))
    jira_keys = normalize_string_list(value.get("jira_keys"))
    repo_paths = normalize_string_list(value.get("repo_paths"))
    evidence_patterns = normalize_string_list(value.get("evidence_patterns"))
    mode = str(value.get("mode") or "").strip()
    if not mode:
        mode = "long_term" if any((keywords, jira_keys, repo_paths, evidence_patterns)) else "none"
    if mode not in TRACKING_MODES:
        mode = "none"
    outcome_hint = str(value.get("outcome_hint") or "").strip()
    if mode == "none":
        progress_style = str(value.get("progress_style") or "").strip()
        if progress_style not in PROGRESS_STYLES:
            progress_style = "outcome"
        return {
            "mode": "none",
            "progress_style": progress_style,
            "keywords": [],
            "jira_keys": [],
            "repo_paths": [],
            "evidence_patterns": [],
            "outcome_hint": outcome_hint,
        }
    progress_style = "outcome"
    return {
        "mode": mode,
        "progress_style": progress_style,
        "keywords": keywords,
        "jira_keys": jira_keys,
        "repo_paths": repo_paths,
        "evidence_patterns": evidence_patterns,
        "outcome_hint": outcome_hint,
    }


def is_long_task_item(item, tracking=None):
    item_type = str(item.get("item_type") or "").strip()
    if item_type == "task":
        return True
    if item_type == "track":
        return False
    tracking = normalize_tracking(tracking if tracking is not None else item.get("tracking"))
    return tracking.get("mode") == "long_term"


def normalize_record_identity(item, tracking):
    explicit_track_id = str(item.get("track_id") or "").strip()
    raw_task_id = str(item.get("task_id") or "").strip()
    if explicit_track_id:
        if not TRACK_ID_RE.match(explicit_track_id):
            raise ValueError(f"invalid track id: {explicit_track_id!r}")
        return {
            "task_id": explicit_track_id,
            "track_id": explicit_track_id,
            "legacy_task_id": raw_task_id if TASK_ID_RE.match(raw_task_id) else str(item.get("legacy_task_id") or "").strip(),
            "item_type": "track",
        }
    if raw_task_id and TRACK_ID_RE.match(raw_task_id):
        return {
            "task_id": raw_task_id,
            "track_id": raw_task_id,
            "legacy_task_id": str(item.get("legacy_task_id") or "").strip(),
            "item_type": "track",
        }
    if not raw_task_id or not TASK_ID_RE.match(raw_task_id):
        raise ValueError(f"invalid task id: {raw_task_id!r}")
    if is_long_task_item(item, tracking):
        return {
            "task_id": raw_task_id,
            "track_id": "",
            "legacy_task_id": str(item.get("legacy_task_id") or "").strip(),
            "item_type": "task",
        }
    return {
        "task_id": f"TRK-{raw_task_id}",
        "track_id": f"TRK-{raw_task_id}",
        "legacy_task_id": raw_task_id,
        "item_type": "track",
    }


def build_tracking_from_args(args, previous=None):
    tracking = dict(previous or {})
    if getattr(args, "tracking_mode", None) is not None:
        tracking["mode"] = args.tracking_mode
    if getattr(args, "progress_style", None) is not None:
        tracking["progress_style"] = args.progress_style
    if getattr(args, "outcome_hint", None) is not None:
        tracking["outcome_hint"] = args.outcome_hint
    for attr, field in (
        ("tracking_keyword", "keywords"),
        ("tracking_jira", "jira_keys"),
        ("tracking_repo", "repo_paths"),
        ("tracking_pattern", "evidence_patterns"),
    ):
        values = getattr(args, attr, None)
        if values:
            existing = normalize_string_list(tracking.get(field))
            for value in values:
                text = str(value or "").strip()
                if text and text not in existing:
                    existing.append(text)
            tracking[field] = existing
    return normalize_tracking(tracking)


def tracking_summary(task):
    tracking = normalize_tracking(task.get("tracking"))
    if tracking.get("mode") == "none":
        return ""
    parts = [tracking.get("mode", "long_term")]
    if tracking.get("progress_style") == "outcome":
        parts.append("outcome")
    for label, field in (("kw", "keywords"), ("jira", "jira_keys"), ("repo", "repo_paths")):
        values = tracking.get(field) or []
        if values:
            parts.append(f"{label}:{','.join(values[:3])}")
    return "; ".join(parts)


def normalize_project_for_task(item):
    return infer_project_key(item, PROJECTS)


def normalize_task(item):
    if not isinstance(item, dict):
        raise ValueError("task item must be a JSON object")
    tracking = normalize_tracking(item.get("tracking"))
    identity = normalize_record_identity(item, tracking)
    record_id = identity["task_id"]
    text = str(item.get("text") or "").strip()
    if not text:
        raise ValueError(f"task {record_id} is missing text")
    status = normalize_status(item.get("status"))
    completed_date = item.get("completed_date")
    if status in ACTIVE_STATUSES:
        completed_date = None
    result = {
        "task_id": record_id,
        "text": text,
        "item_type": identity["item_type"],
        "project": normalize_project_for_task(item),
        "area": str(item.get("area") or "general").strip(),
        "status": status,
        "priority": normalize_priority(item.get("priority")),
        "origin_date": item.get("origin_date") or item.get("date") or today(),
        "target_date": item.get("target_date"),
        "last_reviewed_date": item.get("last_reviewed_date"),
        "completed_date": completed_date,
        "source": normalize_source(item.get("source")),
        "note": str(item.get("note") or "").strip(),
        "related_task_id": str(item.get("related_task_id") or "").strip(),
        "completion_criteria": str(item.get("completion_criteria") or "").strip(),
        "metric": str(item.get("metric") or "").strip(),
        "target_metric": str(item.get("target_metric") or "").strip(),
        "evidence": list(item.get("evidence") or []),
        "tracking": tracking,
    }
    if identity["track_id"]:
        result["track_id"] = identity["track_id"]
    if identity["legacy_task_id"]:
        result["legacy_task_id"] = identity["legacy_task_id"]
    return result


def sort_tasks(tasks):
    return sorted(
        tasks,
        key=lambda item: (
            PRIORITY_ORDER.get(item.get("priority", "P2"), 9),
            1 if item.get("status") in TERMINAL_STATUSES else 0,
            item.get("target_date") or "9999-12-31",
            item.get("origin_date") or "9999-12-31",
            item.get("task_id", ""),
        ),
    )


def task_index(data):
    index = {}
    for idx, task in enumerate(data.get("tasks", [])):
        for key in (task.get("task_id"), task.get("track_id"), task.get("legacy_task_id")):
            key = str(key or "").strip()
            if key:
                index[key] = idx
    return index


def resolve_task_records(data, item_ids):
    if not item_ids:
        return []
    index = task_index(data)
    records = []
    seen = set()
    missing = []
    for item_id in item_ids:
        key = str(item_id or "").strip()
        if not key:
            continue
        idx = index.get(key)
        if idx is None:
            missing.append(key)
            continue
        task = data["tasks"][idx]
        canonical_id = task["task_id"]
        if canonical_id not in seen:
            seen.add(canonical_id)
            records.append(task)
    if missing:
        raise ValueError("unknown plan item id(s): " + ", ".join(missing))
    return records


def upsert_task(data, args):
    if args.project is not None:
        project = resolve_project_key(args.project, PROJECTS)
        if not project:
            raise ValueError(f"unknown project: {args.project}")
        args.project = project
    item_type = args.item_type
    if not args.task_id and not args.track_id:
        raise ValueError("upsert requires --task-id or --track-id")
    if item_type == "task" and not args.task_id:
        raise ValueError("--item-type task requires --task-id")
    if item_type == "track" and not args.track_id:
        args.track_id = args.task_id if TRACK_ID_RE.match(args.task_id or "") else f"TRK-{args.task_id}"
    idx = task_index(data).get(args.track_id or args.task_id)
    if idx is None:
        task = {
            "task_id": args.task_id,
            "track_id": args.track_id,
            "item_type": item_type,
            "text": args.text,
            "project": args.project,
            "area": args.area or "general",
            "status": args.status or "open",
            "priority": args.priority or "P2",
            "origin_date": args.origin_date or args.date or today(),
            "target_date": args.target_date,
            "last_reviewed_date": args.date or today(),
            "completed_date": args.completed_date,
            "source": args.source or "ledger",
            "note": args.note or "",
            "related_task_id": args.related_task_id or "",
            "completion_criteria": args.completion_criteria or "",
            "metric": args.metric or "",
            "target_metric": args.target_metric or "",
            "evidence": args.evidence or [],
            "tracking": build_tracking_from_args(args),
        }
        if (args.status or "open") in TERMINAL_STATUSES and not task["completed_date"]:
            task["completed_date"] = args.date or today()
        data["tasks"].append(normalize_task(task))
        return "created"

    task = data["tasks"][idx]
    if args.track_id is not None:
        task["track_id"] = args.track_id
    if args.item_type is not None:
        task["item_type"] = args.item_type
    for field in (
        "text",
        "project",
        "area",
        "status",
        "priority",
        "target_date",
        "source",
        "note",
        "completed_date",
        "related_task_id",
        "completion_criteria",
        "metric",
        "target_metric",
    ):
        value = getattr(args, field, None)
        if value is not None:
            task[field] = value
    task["tracking"] = build_tracking_from_args(args, previous=task.get("tracking"))
    if args.origin_date is not None:
        task["origin_date"] = args.origin_date
    task["last_reviewed_date"] = args.date or today()
    if args.evidence:
        existing = list(task.get("evidence") or [])
        for item in args.evidence:
            if item not in existing:
                existing.append(item)
        task["evidence"] = existing
    if task.get("status") in TERMINAL_STATUSES and not task.get("completed_date"):
        task["completed_date"] = args.date or today()
    if task.get("status") in ACTIVE_STATUSES:
        task["completed_date"] = None
    data["tasks"][idx] = normalize_task(task)
    return "updated"


def set_task_status(data, task_id, status, date=None, note=None, evidence=None):
    idx = task_index(data).get(task_id)
    if idx is None:
        return False
    task = dict(data["tasks"][idx])
    task["status"] = normalize_status(status)
    task["last_reviewed_date"] = date or today()
    if task["status"] in TERMINAL_STATUSES:
        task["completed_date"] = date or today()
    else:
        task["completed_date"] = None
    if note is not None:
        task["note"] = note
    if evidence:
        existing = list(task.get("evidence") or [])
        for item in evidence:
            if item not in existing:
                existing.append(item)
        task["evidence"] = existing
    data["tasks"][idx] = normalize_task(task)
    return True


def active_tasks(data):
    return sort_tasks([task for task in data.get("tasks", []) if task.get("status") in ACTIVE_STATUSES])


def find_previous_report(target_date, reports_dir):
    if not target_date:
        return None
    candidates = []
    for path in Path(reports_dir).rglob("*.md"):
        if not REPORT_NAME_RE.match(path.name):
            continue
        report_date = path.stem
        if report_date < target_date:
            candidates.append((report_date, path))
    if not candidates:
        return None
    return sorted(candidates)[-1][1]


def extract_state_task_ids(markdown):
    state = extract_state_json(markdown)
    if not state:
        return []
    ids = []
    for section_name in ("next_long_tasks", "next_plan", "next_tracking", "tracking_items"):
        section = state.get(section_name) or []
        if not isinstance(section, list):
            continue
        for item in section:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("track_id") or item.get("task_id") or "").strip()
            if item_id and item_id not in ids:
                ids.append(item_id)
    return ids


def compact_date(value):
    if not value:
        return None
    return str(value).replace("-", "")


def task_id_date(task_id):
    match = re.match(r"^(?:TRK-)?(\d{8})-\d+$", task_id or "")
    return match.group(1) if match else None


def task_is_before_target(task, target_date):
    target_key = compact_date(target_date)
    if not target_key:
        return True
    task_key = task_id_date(task.get("task_id"))
    return bool(task_key and task_key < target_key)


def select_context_tasks(data, target_date=None, reports_dir=None, item_ids=None):
    tasks_by_id = {}
    for task in data.get("tasks", []):
        for key in (task.get("task_id"), task.get("track_id"), task.get("legacy_task_id")):
            key = str(key or "").strip()
            if key:
                tasks_by_id[key] = task
    review_selected = []
    current_selected = []
    selected_ids = set()
    previous_ids = []
    missing_previous_ids = []
    previous_report = None

    def add_selected(scope, task):
        if task_is_before_target(task, target_date):
            review_selected.append((scope, task))
        else:
            current_selected.append((scope, task))
        selected_ids.add(task["task_id"])

    if item_ids:
        for task in resolve_task_records(data, item_ids):
            add_selected("explicit-id", task)
        return previous_report, previous_ids, missing_previous_ids, review_selected, current_selected

    if target_date and reports_dir:
        previous_report = find_previous_report(target_date, reports_dir)
        if previous_report:
            previous_ids = extract_state_task_ids(previous_report.read_text(encoding="utf-8"))
            for task_id in previous_ids:
                task = tasks_by_id.get(task_id)
                if task:
                    add_selected("previous-report", task)
                else:
                    missing_previous_ids.append(task_id)

    for task in active_tasks(data):
        if task["task_id"] in selected_ids:
            continue
        add_selected("active-ledger", task)

    return previous_report, previous_ids, missing_previous_ids, review_selected, current_selected


def render_task_table(rows):
    lines = []
    lines.append("| Scope | Type | Project | Priority | Status | ID | Related Task | Target Date | Metric | Target Metric | Completed | Text | Completion Criteria | Tracking | Note |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for scope, task in rows:
        note = (task.get("note") or "").replace("|", "/")
        text = task["text"].replace("|", "/")
        completion = (task.get("completion_criteria") or "").replace("|", "/")
        metric = (task.get("metric") or "").replace("|", "/")
        target_metric = (task.get("target_metric") or "").replace("|", "/")
        tracking = tracking_summary(task).replace("|", "/")
        project = project_name(task.get("project") or "unassigned", PROJECTS).replace("|", "/")
        item_type = "long_task" if task.get("item_type") == "task" else "tracking_item"
        lines.append(
            "| {scope} | {item_type} | {project} | {priority} | {status} | `{task_id}` | {related} | {target} | {metric} | {target_metric} | {completed} | {text} | {completion} | {tracking} | {note} |".format(
                scope=scope,
                item_type=item_type,
                project=project,
                priority=task.get("priority", "P2"),
                status=task.get("status", "open"),
                task_id=task["task_id"],
                related=task.get("related_task_id") or "",
                target=task.get("target_date") or "",
                metric=metric,
                target_metric=target_metric,
                completed=task.get("completed_date") or "",
                text=text,
                completion=completion,
                tracking=tracking,
                note=note,
            )
        )
    return lines


def render_project_profile(project):
    profile = project.get("report_profile") or {}
    parts = []
    focus = profile.get("focus") or []
    signals = profile.get("signals") or []
    if focus:
        parts.append("focus:" + ",".join(str(item).replace("|", "/") for item in focus))
    if signals:
        parts.append("signals:" + ",".join(str(item).replace("|", "/") for item in signals))
    return "; ".join(parts)


def render_context(data, target_date=None, max_items=None, reports_dir=None, item_ids=None):
    previous_report, previous_ids, missing_previous_ids, review_selected, current_selected = select_context_tasks(
        data, target_date=target_date, reports_dir=reports_dir, item_ids=item_ids
    )
    tasks = review_selected + current_selected
    if max_items is not None and not item_ids:
        tasks = tasks[:max_items]
        review_ids = {task["task_id"] for _, task in tasks}
        review_selected = [(scope, task) for scope, task in review_selected if task["task_id"] in review_ids]
        current_selected = [(scope, task) for scope, task in current_selected if task["task_id"] in review_ids]
    lines = []
    lines.append("# Plan Ledger Context")
    if target_date:
        lines.append(f"Target date: {target_date}")
    if previous_report:
        lines.append(f"Previous report: {previous_report}")
    lines.append(
        "Context is split into long-term tasks and daily tracking items. "
        "Use long-term tasks for 项目进展 and outcome progress. "
        "Use tracking items for 跟踪事项, including both carryover and newly added unclosed work. "
        "The full ledger keeps completed and dropped records. "
        "Tasks with long_term/outcome tracking should be summarized as outcome progress, not step logs. "
        "项目进展 must be organized only by registered active projects."
    )
    lines.append("")
    lines.append("## Registered Projects")
    lines.append("")
    lines.append("| Project | Key | Goal | Report Profile | Selected IDs |")
    lines.append("|---|---|---|---|---|")
    for project in PROJECTS:
        if not project.get("active", True):
            continue
        task_ids = [
            task["task_id"]
            for _, task in tasks
            if task.get("project") == project["key"]
        ]
        lines.append(
            "| {name} | `{key}` | {goal} | {profile} | {task_ids} |".format(
                name=project["name"].replace("|", "/"),
                key=project["key"],
                goal=(project.get("goal") or "").replace("|", "/"),
                profile=render_project_profile(project),
                task_ids=", ".join(f"`{task_id}`" for task_id in task_ids) if task_ids else "",
            )
        )
    if missing_previous_ids:
        lines.append("")
        lines.append("Missing previous report Task/Track IDs in ledger: " + ", ".join(f"`{item}`" for item in missing_previous_ids))
    if not tasks:
        lines.append("")
        lines.append("No task items selected for this report.")
        return "\n".join(lines) + "\n"
    lines.append("")
    long_rows = [(scope, task) for scope, task in tasks if task.get("item_type") == "task"]
    tracking_rows = [(scope, task) for scope, task in tasks if task.get("item_type") != "task"]
    lines.append("## Long-Term Tasks")
    lines.append("")
    if long_rows:
        lines.extend(render_task_table(long_rows))
    else:
        lines.append("No long-term tasks selected.")
    lines.append("")
    lines.append("## Tracking Items")
    lines.append("")
    lines.append("Use these items in 跟踪事项. New and old items are distinguished by ID date, not by separate sections.")
    lines.append("")
    if tracking_rows:
        lines.extend(render_task_table(tracking_rows))
    else:
        lines.append("No tracking items selected.")
    return "\n".join(lines) + "\n"


def strip_callout_prefix(line):
    if line.startswith("> "):
        return line[2:]
    if line.startswith(">"):
        return line[1:]
    return line


def extract_state_json(markdown):
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


def import_report(data, args):
    report_path = Path(args.report)
    state = extract_state_json(report_path.read_text(encoding="utf-8"))
    if not state:
        raise ValueError(f"no ai-daily-state block found in {report_path}")
    report_date = args.date or state.get("date") or today()
    task_updates = state.get("task_updates") or []
    next_long_tasks = state.get("next_long_tasks") or []
    next_plan = state.get("next_plan") or []
    tracking_updates = state.get("tracking_updates") or []
    next_tracking = state.get("next_tracking") or state.get("tracking_items") or []
    if not isinstance(task_updates, list):
        raise ValueError("ai-daily-state.task_updates must be a list")
    if not isinstance(next_long_tasks, list):
        raise ValueError("ai-daily-state.next_long_tasks must be a list")
    if not isinstance(next_plan, list):
        raise ValueError("ai-daily-state.next_plan must be a list")
    if not isinstance(tracking_updates, list):
        raise ValueError("ai-daily-state.tracking_updates must be a list")
    if not isinstance(next_tracking, list):
        raise ValueError("ai-daily-state.next_tracking must be a list")

    existing = {}
    for task in data.get("tasks", []):
        for key in (task.get("task_id"), task.get("track_id"), task.get("legacy_task_id")):
            key = str(key or "").strip()
            if key:
                existing[key] = task
    allowed_ids = None
    if args.item_ids:
        allowed_ids = {task["task_id"] for task in resolve_task_records(data, args.item_ids)}
        rejected_ids = []
        for section in (task_updates, next_long_tasks, next_plan, tracking_updates, next_tracking):
            for item in section:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("track_id") or item.get("task_id") or "").strip()
                if not item_id:
                    continue
                previous = existing.get(item_id)
                canonical_id = previous.get("task_id") if previous else item_id
                if canonical_id not in allowed_ids and item_id not in rejected_ids:
                    rejected_ids.append(item_id)
        if rejected_ids:
            raise ValueError(
                "report contains plan item id(s) outside allowed scope: " + ", ".join(rejected_ids)
            )
    incoming_ids = set()

    def apply_item(item, default_source, force_type=None):
        if not isinstance(item, dict):
            return
        status = normalize_status(item.get("status") or "open")
        task_id = str(item.get("task_id") or "").strip()
        track_id = str(item.get("track_id") or "").strip()
        item_id = track_id or task_id
        text = str(item.get("text") or "").strip()
        if not item_id or not text:
            return
        incoming_ids.add(item_id)
        previous = existing.get(item_id, {})
        task = {
            "task_id": task_id,
            "track_id": track_id,
            "item_type": force_type or item.get("item_type") or previous.get("item_type"),
            "text": text,
            "project": item.get("project") or previous.get("project"),
            "area": previous.get("area") or item.get("area") or "general",
            "status": status,
            "priority": item.get("priority") or previous.get("priority") or "P2",
            "origin_date": previous.get("origin_date") or item.get("origin_date") or report_date,
            "target_date": item.get("target_date") or previous.get("target_date"),
            "last_reviewed_date": report_date,
            "completed_date": item.get("completed_date") or previous.get("completed_date"),
            "source": item.get("source") or previous.get("source") or default_source,
            "note": item.get("note") or previous.get("note") or "",
            "related_task_id": item.get("related_task_id") or previous.get("related_task_id") or "",
            "completion_criteria": item.get("completion_criteria") or previous.get("completion_criteria") or "",
            "metric": item.get("metric") or previous.get("metric") or "",
            "target_metric": item.get("target_metric") or previous.get("target_metric") or "",
            "evidence": previous.get("evidence") or [],
            "tracking": item.get("tracking") or previous.get("tracking") or {},
        }
        if item.get("evidence"):
            for evidence in item.get("evidence"):
                if evidence not in task["evidence"]:
                    task["evidence"].append(evidence)
        if status in TERMINAL_STATUSES and not task.get("completed_date"):
            task["completed_date"] = report_date
        if status in ACTIVE_STATUSES:
            task["completed_date"] = None
        idx = task_index(data).get(item_id)
        if idx is None:
            normalized = normalize_task(task)
            data["tasks"].append(normalized)
        else:
            normalized = normalize_task(task)
            data["tasks"][idx] = normalized
        for key in (normalized.get("task_id"), normalized.get("track_id"), normalized.get("legacy_task_id")):
            key = str(key or "").strip()
            if key:
                existing[key] = normalized

    for item in task_updates:
        apply_item(item, "report", force_type="task")

    for item in next_long_tasks:
        apply_item(item, "report", force_type="task")

    for item in next_plan:
        apply_item(item, "report")

    for item in tracking_updates:
        apply_item(item, "report", force_type="track")

    for item in next_tracking:
        apply_item(item, "report", force_type="track")

    if args.replace_active:
        # Compatibility flag retained for older wrapper invocations. The ledger
        # is durable now; missing active tasks are kept so they can reappear in
        # the next compact context instead of being silently lost.
        pass
    return len(incoming_ids)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="plan_state.json path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_context = sub.add_parser("context", help="print compact active plan context")
    p_context.add_argument("--date", help="target report date")
    p_context.add_argument("--max-items", type=int, default=30)
    p_context.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR))
    p_context.add_argument(
        "--item-id",
        dest="item_ids",
        action="append",
        help="include only this existing Task/Track ID; repeat for multiple items",
    )

    p_list = sub.add_parser("list", help="list active ledger tasks as JSON")
    p_list.add_argument("--all", action="store_true", help="include terminal tasks if any exist")

    p_upsert = sub.add_parser("upsert", help="create or update a ledger task")
    p_upsert.add_argument("--task-id")
    p_upsert.add_argument("--track-id")
    p_upsert.add_argument("--item-type", choices=sorted(ITEM_TYPES))
    p_upsert.add_argument("--text", required=True)
    p_upsert.add_argument("--project")
    p_upsert.add_argument("--area")
    p_upsert.add_argument("--status", choices=sorted(ALL_STATUSES))
    p_upsert.add_argument("--priority", choices=sorted(PRIORITY_ORDER))
    p_upsert.add_argument("--origin-date")
    p_upsert.add_argument("--target-date")
    p_upsert.add_argument("--completed-date")
    p_upsert.add_argument("--date")
    p_upsert.add_argument("--source", choices=sorted(SOURCE_VALUES))
    p_upsert.add_argument("--note")
    p_upsert.add_argument("--related-task-id")
    p_upsert.add_argument("--completion-criteria")
    p_upsert.add_argument("--metric")
    p_upsert.add_argument("--target-metric")
    p_upsert.add_argument("--evidence", action="append")
    p_upsert.add_argument("--tracking-mode", choices=sorted(TRACKING_MODES))
    p_upsert.add_argument("--progress-style", choices=sorted(PROGRESS_STYLES))
    p_upsert.add_argument("--tracking-keyword", action="append")
    p_upsert.add_argument("--tracking-jira", action="append")
    p_upsert.add_argument("--tracking-repo", action="append")
    p_upsert.add_argument("--tracking-pattern", action="append")
    p_upsert.add_argument("--outcome-hint")

    p_done = sub.add_parser("done", help="mark a task done and keep it in the ledger")
    p_done.add_argument("task_id")
    p_done.add_argument("--date")
    p_done.add_argument("--note")
    p_done.add_argument("--evidence", action="append")

    p_drop = sub.add_parser("drop", help="mark a task dropped and keep it in the ledger")
    p_drop.add_argument("task_id")
    p_drop.add_argument("--date")
    p_drop.add_argument("--note")
    p_drop.add_argument("--evidence", action="append")

    p_set_status = sub.add_parser("set-status", help="set an existing Task/Track ID status")
    p_set_status.add_argument("task_id")
    p_set_status.add_argument("status", choices=sorted(ALL_STATUSES))
    p_set_status.add_argument("--date")
    p_set_status.add_argument("--note")
    p_set_status.add_argument("--evidence", action="append")

    sub.add_parser("prune", help="rewrite and sort the ledger without deleting terminal tasks")

    p_import = sub.add_parser("import-report", help="import ai-daily-state records from a generated daily report")
    p_import.add_argument("report")
    p_import.add_argument("--date")
    p_import.add_argument("--replace-active", action="store_true")
    p_import.add_argument(
        "--item-id",
        dest="item_ids",
        action="append",
        help="allow updates only for this existing Task/Track ID; repeat for multiple items",
    )

    args = parser.parse_args(argv)
    path = Path(args.state)
    data = read_json(path)

    if args.cmd == "context":
        sys.stdout.write(
            render_context(
                data,
                target_date=args.date,
                max_items=args.max_items,
                reports_dir=args.reports_dir,
                item_ids=args.item_ids,
            )
        )
        return 0
    if args.cmd == "list":
        tasks = data.get("tasks", []) if args.all else active_tasks(data)
        json.dump(tasks, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.cmd == "upsert":
        action = upsert_task(data, args)
        write_json(path, data)
        print(f"{action}: {args.track_id or args.task_id}")
        return 0
    if args.cmd == "done":
        updated = set_task_status(data, args.task_id, "done", date=args.date, note=args.note, evidence=args.evidence)
        write_json(path, data)
        print(f"done: {args.task_id} updated={int(updated)}")
        return 0
    if args.cmd == "drop":
        updated = set_task_status(data, args.task_id, "dropped", date=args.date, note=args.note, evidence=args.evidence)
        write_json(path, data)
        print(f"dropped: {args.task_id} updated={int(updated)}")
        return 0
    if args.cmd == "set-status":
        updated = set_task_status(
            data,
            args.task_id,
            args.status,
            date=args.date,
            note=args.note,
            evidence=args.evidence,
        )
        if not updated:
            raise ValueError(f"unknown plan item id: {args.task_id}")
        write_json(path, data)
        print(f"status: {args.task_id} status={args.status}")
        return 0
    if args.cmd == "prune":
        write_json(path, data)
        print("rewritten=1 pruned=0")
        return 0
    if args.cmd == "import-report":
        count = import_report(data, args)
        write_json(path, data)
        print(f"imported={count} report={args.report}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
