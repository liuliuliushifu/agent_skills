#!/usr/bin/env python3
"""Collect structured evidence for AI daily reports.

This script intentionally collects broadly and judges lightly. The goal is to
give the report generator a stable fact package with coverage metrics, not to
decide what is important.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, date, time, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python 3.8 can use the optional backport.
    try:
        from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:
        ZoneInfo = None
        ZoneInfoNotFoundError = Exception


TIMEZONE_NAME = os.environ.get("AI_DAILY_TIMEZONE", os.environ.get("TZ", "UTC"))
if ZoneInfo is None:
    TZ = timezone.utc
else:
    try:
        TZ = ZoneInfo(TIMEZONE_NAME)
    except ZoneInfoNotFoundError:
        TZ = ZoneInfo("UTC")
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("AI_DAILY_CONFIG_FILE", str(SCRIPT_DIR / ".config.json")))
sys.path.insert(0, str(SCRIPT_DIR))

from project_registry import infer_project_key, load_projects, project_name  # noqa: E402


def load_config():
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def config_get(config, dotted, default=None):
    current = config
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


CONFIG = load_config()
USER_ROOT = Path(os.environ.get("CODEX_USER_HOME", str(Path.home()))).expanduser()
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(USER_ROOT / ".codex"))).expanduser()
DAILY_DIR = Path(os.environ.get("AI_DAILY_REPORT_DIR", str(CODEX_HOME / "daily_report")))
SESSIONS_ROOT = Path(
    os.environ.get("AI_DAILY_SESSIONS_ROOT", str(CODEX_HOME / "sessions"))
).expanduser()
COAGENT_SH = Path(os.environ.get("COAGENT_SH", str(CODEX_HOME / "skills/co-agent/scripts/coagent.sh")))
USAGE_QUERY = Path(os.environ.get("CODEX_USAGE_QUERY", str(CODEX_HOME / "skills/usage-mgr/scripts/query_usage.py")))
USAGE_ROOT = os.environ.get("CODEX_USAGE_ROOT", "")
MEMORIES_ROOT = Path(
    os.environ.get("AI_DAILY_MEMORIES_ROOT", str(CODEX_HOME / "memories"))
).expanduser()
REME_MEMORY_ROOT = Path(os.environ.get("REME_MEMORY_ROOT", str(USER_ROOT / ".local/share/reme/light_data/memory")))
MEMORY_ROOTS = [MEMORIES_ROOT, REME_MEMORY_ROOT]
JIRA_LEDGER = Path(
    os.environ.get("AI_DAILY_JIRA_LEDGER", str(MEMORIES_ROOT / "jira-workflow" / "ledger.jsonl"))
).expanduser()
PLAN_STATE = Path(
    os.environ.get("AI_DAILY_PLAN_STATE", str(DAILY_DIR / "plan_state.json"))
).expanduser()
LOG_DIR = DAILY_DIR / "logs"
ACLI_BIN = Path(os.environ.get("ACLI_BIN", str(USER_ROOT / ".local/bin/acli")))
DEFAULT_PROXY = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY", "")
DEFAULT_NO_PROXY = os.environ.get("no_proxy") or os.environ.get("NO_PROXY", "")
PROJECTS = load_projects(DAILY_DIR, SCRIPT_DIR)
PROJECT_BY_KEY = {project["key"]: project for project in PROJECTS}

JIRA_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
COMMIT_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:[^\s'\"`<>|\\]|\\ )+")
SAFE_STAT_LIMIT = 2_000_000
MAX_TEXT_SCAN_CHARS = 20_000_000
KNOWN_JIRA_PROJECTS = set(config_get(CONFIG, "jira.projects", []))
DONE_STATUS_NAMES = set(config_get(CONFIG, "jira.done_status_names", ["已完成", "done", "closed", "resolved"]))
ACTIVE_TASK_STATUSES = {"open", "in_progress", "blocked", "deferred"}
SENSITIVE_RE = re.compile(
    r"(password|passwd|token|secret|authorization|cookie|api[_-]?key)"
    r"([\"'\s:=]+)([^\s\"']{6,})|"
    r"Bearer\s+[A-Za-z0-9._~+/-]{10,}|"
    r"BEGIN .*PRIVATE KEY|"
    r"ssh-(rsa|ed25519)\s+[A-Za-z0-9+/=]{20,}",
    re.IGNORECASE,
)


def project_report_profile(project_key):
    project = PROJECT_BY_KEY.get(project_key or "")
    if not project:
        return {}
    profile = project.get("report_profile")
    return profile if isinstance(profile, dict) else {}
METRIC_TABLE_RE = re.compile(
    r"(pps|entry/s|entries/s|条目/秒|rx count|tx count|parse errors|discard count|"
    r"throughput|pass|性能矩阵|矩阵|拐点)",
    re.IGNORECASE,
)
METRIC_NUMBER_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})+(?:\s*(?:pps|entry/s|entries/s))?|"
    r"\d+(?:\.\d+)?\s*M(?:\s*(?:entry/s|entries/s|条目/秒))?|"
    r"\d+\s*/\s*\d+)",
    re.IGNORECASE,
)
METRIC_SOURCE_NAME_RE = re.compile(
    r"(performance|perf|matrix|benchmark|throughput|latency|性能|矩阵|吞吐|时延)",
    re.IGNORECASE,
)
REL_METRIC_SOURCE_RE = re.compile(
    r"(?<![A-Za-z0-9_./\\-])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.(?:md|txt)):\d+(?::|\b)",
    re.IGNORECASE,
)
STRONG_METRIC_TABLE_RE = re.compile(
    r"(Entry\s*数|Entry/s|PPS|throughput|latency|baseline|拐点|parse/discard|吞吐|时延)",
    re.IGNORECASE,
)
MAX_METRIC_TABLES_PER_TASK = 8
MAX_METRIC_TABLE_ROWS = 32
MAX_REFERENCED_METRIC_SOURCES = 40
DEFAULT_ALLOWED_REPO_PREFIXES = []
def expand_config_path(value):
    return Path(
        str(value)
        .replace("$CODEX_HOME", str(CODEX_HOME))
        .replace("$HOME", str(USER_ROOT))
        .replace("$CODEX_USER_HOME", str(USER_ROOT))
    ).expanduser()


configured_prefixes = config_get(CONFIG, "git.allowed_repo_prefixes", None)
default_prefix_value = os.pathsep.join(str(item) for item in DEFAULT_ALLOWED_REPO_PREFIXES)
if configured_prefixes:
    configured_prefix_text = os.pathsep.join(str(item) for item in configured_prefixes)
else:
    configured_prefix_text = default_prefix_value

ALLOWED_REPO_PREFIXES = tuple(
    expand_config_path(item)
    for item in os.environ.get(
        "AI_DAILY_ALLOWED_REPO_PREFIXES",
        configured_prefix_text,
    ).split(os.pathsep)
    if item
)


def safe_path_exists(path):
    try:
        return Path(path).exists()
    except (OSError, PermissionError, ValueError):
        return False


def safe_path_is_dir(path):
    try:
        return Path(path).is_dir()
    except (OSError, PermissionError, ValueError):
        return False


def safe_path_is_file(path):
    try:
        return Path(path).is_file()
    except (OSError, PermissionError, ValueError):
        return False


def has_git_metadata(path):
    marker = Path(path) / ".git"
    if safe_path_is_file(marker):
        return True
    if not safe_path_is_dir(marker):
        return False
    return safe_path_exists(marker / "HEAD") or safe_path_exists(marker / "commondir")


def path_under_allowed_repo_prefix(path):
    if "\x00" in str(path):
        return False
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except (OSError, PermissionError, ValueError):
        resolved = Path(path).expanduser()
    for prefix in ALLOWED_REPO_PREFIXES:
        try:
            resolved.relative_to(prefix.expanduser())
            return True
        except ValueError:
            continue
    return False


def parse_date(value):
    return date.fromisoformat(value)


def day_bounds(target):
    start = datetime.combine(target, time.min, tzinfo=TZ)
    end = start + timedelta(days=1)
    return start, end


def iso_from_timestamp(ts):
    return datetime.fromtimestamp(ts, TZ).replace(microsecond=0).isoformat()


def in_day(ts, start, end):
    dt = datetime.fromtimestamp(ts, TZ)
    return start <= dt < end


def parse_record_time(record):
    value = record.get("timestamp")
    if not isinstance(value, str) or not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except Exception:
        return None


def parse_jira_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def jira_time_in_day(value, start, end):
    dt = parse_jira_time(value)
    return bool(dt and start <= dt < end)


def session_path_date(path):
    try:
        parts = path.relative_to(SESSIONS_ROOT).parts
        if len(parts) < 4:
            return None
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except Exception:
        return None


def session_candidates(target, start):
    target_dir = SESSIONS_ROOT / f"{target:%Y/%m/%d}"
    candidates = {}

    if target_dir.exists():
        for path in target_dir.glob("*.jsonl"):
            if not path_has_blocked_component(path):
                candidates[str(path)] = path

    if SESSIONS_ROOT.exists():
        start_ts = start.timestamp()
        for path in SESSIONS_ROOT.rglob("*.jsonl"):
            if path_has_blocked_component(path):
                continue
            created_date = session_path_date(path)
            if created_date is not None and created_date > target:
                continue
            try:
                if path.stat().st_mtime >= start_ts:
                    candidates[str(path)] = path
            except Exception:
                continue

    return [candidates[key] for key in sorted(candidates)], target_dir


def path_has_blocked_component(path):
    if "\x00" in str(path):
        return True
    return any(part in {".agent", ".agents"} for part in Path(path).parts)


def file_summary(path):
    st = path.stat()
    return {
        "path": str(path),
        "size": st.st_size,
        "mtime": iso_from_timestamp(st.st_mtime),
    }


def scan_jira_keys(text, buckets, conversation=False):
    for key in JIRA_RE.findall(text):
        buckets["jira_candidates"].add(key)
        prefix = key.rsplit("-", 1)[0]
        if prefix in KNOWN_JIRA_PROJECTS:
            buckets["jira_keys"].add(key)
            if conversation:
                buckets["conversation_jira_keys"].add(key)


def scan_text(text, buckets, conversation=False):
    scan_command_hints(text, buckets)
    scan_jira_keys(text, buckets, conversation=conversation)
    buckets["commit_hashes"].update(COMMIT_RE.findall(text))
    for match in REL_METRIC_SOURCE_RE.findall(text):
        if ".." not in Path(match).parts and not path_has_blocked_component(match):
            buckets.setdefault("metric_source_refs", set()).add(match)
    for match in ABS_PATH_RE.findall(text):
        cleaned = match.rstrip(".,);:]}")
        cleaned = cleaned.replace("\\ ", " ")
        if path_has_blocked_component(cleaned):
            continue
        buckets["paths"].add(cleaned)


def scan_command_hints(text, buckets):
    for line in text.splitlines():
        if "git" not in line or "-C" not in line:
            continue
        try:
            parts = shlex.split(line)
        except Exception:
            continue
        for idx, part in enumerate(parts[:-1]):
            if part == "-C" and idx > 0 and parts[idx - 1].endswith("git"):
                candidate = parts[idx + 1]
                if candidate.startswith("/") and not path_has_blocked_component(candidate):
                    buckets["repo_hint_paths"].add(candidate)


def content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict):
            text = item.get("text")
            if text is None:
                text = item.get("input_text") or item.get("output_text")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


def is_jira_command(cmd):
    if not isinstance(cmd, str):
        return False
    try:
        parts = shlex.split(cmd)
    except Exception:
        parts = cmd.split()
    for idx, part in enumerate(parts[:-1]):
        if part in {"acli", str(ACLI_BIN)} and parts[idx + 1] == "jira":
            return True
    return False


def scan_session_record(record, buckets, call_context):
    """Return text to scan plus whether it is user/assistant dialogue.

    Dialogue Jira keys are used for next-day tracking. Jira CLI output also
    counts, because an explicit status query is intentional evidence. Other
    tool output is still scanned for paths, commits, and general evidence, but
    does not create Jira follow-up tasks by itself.
    """
    rec_type = record.get("type")
    payload = record.get("payload") or {}
    if rec_type in {"session_meta", "turn_context"}:
        return "", False
    if rec_type == "response_item":
        item_type = payload.get("type")
        if item_type == "message":
            role = payload.get("role")
            if role in {"user", "assistant"}:
                return content_text(payload.get("content")), True
            return "", False
        if item_type == "function_call":
            arguments = payload.get("arguments")
            call_id = payload.get("call_id")
            jira_command = False
            if isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments)
                    cmd = parsed.get("cmd")
                    if isinstance(cmd, str):
                        scan_command_hints(cmd, buckets)
                        jira_command = is_jira_command(cmd)
                except Exception:
                    pass
            if call_id:
                call_context[call_id] = {"jira_command": jira_command}
            return " ".join(
                str(part)
                for part in (payload.get("name"), arguments)
                if part is not None
            ), jira_command
        if item_type == "function_call_output":
            call_id = payload.get("call_id")
            meta = call_context.get(call_id, {}) if call_id else {}
            return str(payload.get("output") or ""), bool(meta.get("jira_command"))
        return "", False
    if rec_type == "event_msg":
        event_type = payload.get("type")
        if event_type in {"agent_message", "user_message"}:
            return str(payload.get("message") or payload.get("text") or ""), True
        return "", False
    return "", False


def read_limited_text(path):
    try:
        size = path.stat().st_size
        if size > SAFE_STAT_LIMIT:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                return fh.read(SAFE_STAT_LIMIT), None
        return path.read_text(encoding="utf-8", errors="replace"), None
    except Exception as exc:
        return None, str(exc)


def redact_sensitive(text):
    return SENSITIVE_RE.sub("[REDACTED]", text)


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


def record_plan_ids(item):
    return {
        str(value or "").strip()
        for value in (item.get("task_id"), item.get("track_id"), item.get("legacy_task_id"))
        if str(value or "").strip()
    }


def load_long_tracking_tasks(item_ids=None):
    if not PLAN_STATE.exists():
        return []
    try:
        data = json.loads(PLAN_STATE.read_text(encoding="utf-8"))
    except Exception:
        return []
    tasks = []
    requested_ids = {str(item_id or "").strip() for item_id in (item_ids or []) if str(item_id or "").strip()}
    for item in data.get("tasks", []):
        if not isinstance(item, dict):
            continue
        if requested_ids and not (record_plan_ids(item) & requested_ids):
            continue
        if not requested_ids and item.get("status") not in ACTIVE_TASK_STATUSES:
            continue
        tracking = item.get("tracking") or {}
        if not isinstance(tracking, dict) or tracking.get("mode") != "long_term":
            continue
        terms = []
        for field in ("keywords", "jira_keys", "evidence_patterns"):
            terms.extend(normalize_string_list(tracking.get(field)))
        repo_paths = normalize_string_list(tracking.get("repo_paths"))
        jira_keys = normalize_string_list(tracking.get("jira_keys"))
        if not terms and not repo_paths and not jira_keys:
            continue
        tasks.append(
            {
                "task_id": str(item.get("task_id") or "").strip(),
                "text": str(item.get("text") or "").strip(),
                "project": infer_project_key(item, PROJECTS),
                "status": item.get("status") or "open",
                "priority": item.get("priority") or "P2",
                "area": item.get("area") or "general",
                "tracking": {
                    "mode": "long_term",
                    "progress_style": "outcome",
                    "keywords": normalize_string_list(tracking.get("keywords")),
                    "jira_keys": jira_keys,
                    "repo_paths": repo_paths,
                    "evidence_patterns": normalize_string_list(tracking.get("evidence_patterns")),
                    "outcome_hint": str(tracking.get("outcome_hint") or "").strip(),
                },
            }
        )
    return [task for task in tasks if task["task_id"] and task["text"]]


def task_terms(task):
    tracking = task.get("tracking") or {}
    terms = []
    for field in ("keywords", "jira_keys", "evidence_patterns"):
        terms.extend(normalize_string_list(tracking.get(field)))
    result = []
    for term in terms:
        if term not in result:
            result.append(term)
    return result


def matched_terms(text, terms):
    if not text:
        return []
    lowered = text.lower()
    matches = []
    for term in terms:
        if term and term.lower() in lowered:
            matches.append(term)
    return matches


def snippet_for_match(text, terms, limit=220):
    if not text:
        return ""
    lowered = text.lower()
    pos = -1
    for term in terms:
        pos = lowered.find(term.lower())
        if pos >= 0:
            break
    if pos < 0:
        pos = 0
    start = max(0, pos - limit // 3)
    end = min(len(text), start + limit)
    snippet = text[start:end].replace("\n", " ").strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return redact_sensitive(snippet)


def strip_markdown_table_line(line):
    text = line.strip()
    while text.startswith(">"):
        text = text[1:].strip()
    pipe_pos = text.find("|")
    if pipe_pos < 0:
        return None
    text = text[pipe_pos:].strip()
    if text.count("|") < 2:
        return None
    return text


def split_markdown_table_cells(row):
    text = row.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def is_markdown_separator_row(row):
    cells = [cell for cell in split_markdown_table_cells(row) if cell]
    if not cells:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def nearby_heading(lines, start_index):
    for index in range(start_index - 1, max(-1, start_index - 10), -1):
        text = lines[index].strip()
        if not text:
            continue
        if text.startswith("#"):
            return text.lstrip("#").strip()
        if strip_markdown_table_line(text) is None and len(text) <= 120:
            return text
    return ""


def metric_table_is_relevant(rows, context, terms):
    text = "\n".join(rows + ([context] if context else []))
    if not METRIC_TABLE_RE.search(text):
        return False
    if not METRIC_NUMBER_RE.search(text):
        return False
    if terms and not matched_terms(text, terms):
        return False
    return True


def metric_table_record(source_path, source_kind, lines, row_block, start_index, end_index, terms):
    title = nearby_heading(lines, start_index)
    markdown_rows = [item["row"] for item in row_block]
    cells = [split_markdown_table_cells(row) for row in markdown_rows]
    header = []
    data_rows = cells
    if len(cells) >= 2 and is_markdown_separator_row(markdown_rows[1]):
        header = cells[0]
        data_rows = cells[2:]
    elif cells:
        header = cells[0]
        data_rows = cells[1:]

    if len(data_rows) > MAX_METRIC_TABLE_ROWS:
        data_rows = data_rows[:MAX_METRIC_TABLE_ROWS]
        markdown_rows = markdown_rows[: MAX_METRIC_TABLE_ROWS + (2 if header else 0)]

    text_for_terms = "\n".join(markdown_rows + ([title] if title else []))
    return {
        "source_kind": source_kind,
        "path": str(source_path),
        "line_start": row_block[0]["line"],
        "line_end": row_block[-1]["line"],
        "title": redact_sensitive(title),
        "terms": matched_terms(text_for_terms, terms)[:5],
        "header": header,
        "rows": data_rows,
        "markdown": [redact_sensitive(row) for row in markdown_rows],
        "row_count": len(data_rows),
    }


def extract_metric_tables(text, source_path, source_kind, terms=None, max_tables=MAX_METRIC_TABLES_PER_TASK):
    if not text or not METRIC_TABLE_RE.search(text):
        return []
    lines = text.splitlines()
    tables = []
    index = 0
    terms = terms or []
    while index < len(lines):
        row = strip_markdown_table_line(lines[index])
        if row is None:
            index += 1
            continue
        start_index = index
        row_block = []
        while index < len(lines):
            current = strip_markdown_table_line(lines[index])
            if current is None:
                break
            row_block.append({"line": index + 1, "row": current})
            index += 1
        context_start = max(0, start_index - 4)
        context_end = min(len(lines), index + 4)
        context = "\n".join(lines[context_start:context_end])
        markdown_rows = [item["row"] for item in row_block]
        if len(markdown_rows) >= 2 and metric_table_is_relevant(markdown_rows, context, terms):
            tables.append(
                metric_table_record(
                    source_path,
                    source_kind,
                    lines,
                    row_block,
                    start_index,
                    index,
                    terms,
                )
            )
            if len(tables) >= max_tables:
                break
        index += 1
    return tables


def metric_table_signature(table):
    return (
        table.get("path"),
        table.get("line_start"),
        tuple(table.get("header") or []),
        tuple(tuple(row) for row in (table.get("rows") or [])[:3]),
    )


def metric_table_priority(table):
    text = "\n".join(
        [
            str(table.get("path") or ""),
            str(table.get("title") or ""),
            " | ".join(table.get("header") or []),
            "\n".join(table.get("markdown") or []),
        ]
    )
    score = 0
    source_kind = table.get("source_kind")
    if source_kind == "referenced_path":
        score += 20
    elif source_kind == "memory":
        score += 10
    if STRONG_METRIC_TABLE_RE.search(text):
        score += 20
    if re.search(r"Entry\s*数", text, re.IGNORECASE):
        score += 15
    if re.search(r"\bPPS\b", text, re.IGNORECASE):
        score += 12
    if re.search(r"Entry/s", text, re.IGNORECASE):
        score += 12
    row_count = int(table.get("row_count") or 0)
    score += min(row_count, MAX_METRIC_TABLE_ROWS)
    if row_count >= 5:
        score += 10
    return score


def add_limited_metric_tables(task_item, tables):
    seen = task_item.setdefault("_metric_table_signatures", set())
    for table in tables:
        signature = metric_table_signature(table)
        if signature in seen:
            continue
        seen.add(signature)
        table["priority_score"] = metric_table_priority(table)
        if len(task_item["metric_tables"]) >= MAX_METRIC_TABLES_PER_TASK:
            task_item["metric_table_overflow"] += 1
            lowest_index = min(
                range(len(task_item["metric_tables"])),
                key=lambda idx: task_item["metric_tables"][idx].get("priority_score", 0),
            )
            if table["priority_score"] > task_item["metric_tables"][lowest_index].get("priority_score", 0):
                task_item["metric_tables"][lowest_index] = table
            continue
        task_item["metric_tables"].append(table)
    task_item["metric_tables"].sort(
        key=lambda item: (
            -int(item.get("priority_score") or 0),
            str(item.get("path") or ""),
            int(item.get("line_start") or 0),
        )
    )


def normalize_metric_source_path(candidate):
    text = str(candidate or "").strip().rstrip(".,);]}")
    if not text or "\x00" in text:
        return None
    variants = [text]
    md_pos = text.find(".md:")
    if md_pos >= 0:
        variants.append(text[: md_pos + 3])
    for value in variants:
        path = Path(value)
        if path_has_blocked_component(path):
            continue
        if not path_under_allowed_repo_prefix(path):
            continue
        if safe_path_is_file(path):
            return path
    return None


def metric_source_candidate(path):
    if path.suffix.lower() not in {".md", ".txt"}:
        return False
    return bool(METRIC_SOURCE_NAME_RE.search(str(path)))


def metric_source_path_priority(path):
    text = str(path)
    score = 0
    if METRIC_SOURCE_NAME_RE.search(text):
        score += 15
    if "/memories/" in text:
        score -= 5
    return score


def metric_source_base_dirs(candidate_paths):
    bases = set()
    for raw in candidate_paths or []:
        text = str(raw or "").strip()
        if not text:
            continue
        md_pos = text.find(".md:")
        if md_pos >= 0:
            text = text[: md_pos + 3]
        existing = normalize_existing_path(text)
        if existing is None:
            continue
        base = existing if safe_path_is_dir(existing) else existing.parent
        for candidate in [base] + list(base.parents):
            if path_has_blocked_component(candidate) or not path_under_allowed_repo_prefix(candidate):
                continue
            if safe_path_is_dir(candidate):
                bases.add(candidate)
    return bases


def resolve_relative_metric_source_path(relative_path, base_dirs):
    text = str(relative_path or "").strip().rstrip(".,);:]}")
    if not text or "\x00" in text:
        return None
    rel = Path(text)
    if rel.is_absolute() or ".." in rel.parts or path_has_blocked_component(rel):
        return None
    if rel.suffix.lower() not in {".md", ".txt"}:
        return None
    for base in sorted(base_dirs, key=lambda item: -len(str(item))):
        candidate = base / rel
        if not path_under_allowed_repo_prefix(candidate):
            continue
        if safe_path_is_file(candidate):
            return candidate
    return None


def repo_matches_tracking(repo_path, repo_paths):
    if not repo_paths:
        return False
    repo_text = str(repo_path)
    for candidate in repo_paths:
        if not candidate:
            continue
        try:
            repo_resolved = Path(repo_text).resolve(strict=False)
            candidate_resolved = Path(candidate).expanduser().resolve(strict=False)
            if repo_resolved == candidate_resolved:
                return True
            repo_resolved.relative_to(candidate_resolved)
            return True
        except Exception:
            if candidate in repo_text:
                return True
    return False


def add_limited_match(task_item, source, match, limit=6):
    matches = task_item["matches"][source]
    if len(matches) >= limit:
        task_item["overflow"][source] += 1
        return
    matches.append(match)
    task_item["match_count"] += 1


def collect_referenced_metric_tables(task_items, term_map, candidate_paths, relative_refs=None):
    seen_paths = {}
    for candidate in sorted(candidate_paths or []):
        path = normalize_metric_source_path(candidate)
        if path is None or not metric_source_candidate(path):
            continue
        seen_paths[str(path)] = path

    base_dirs = metric_source_base_dirs(candidate_paths)
    for relative_ref in sorted(relative_refs or []):
        path = resolve_relative_metric_source_path(relative_ref, base_dirs)
        if path is None or not metric_source_candidate(path):
            continue
        seen_paths[str(path)] = path

    read_count = 0
    ordered_paths = sorted(
        seen_paths.values(),
        key=lambda item: (-metric_source_path_priority(item), str(item)),
    )
    for path in ordered_paths:
        read_count += 1
        if read_count > MAX_REFERENCED_METRIC_SOURCES:
            break
        text_or_none, error = read_limited_text(path)
        if not text_or_none or error:
            continue
        for task_id, item in task_items.items():
            terms = term_map[task_id]
            if not matched_terms(text_or_none, terms):
                continue
            tables = extract_metric_tables(text_or_none, path, "referenced_path", terms)
            add_limited_metric_tables(item, tables)


def collect_long_task_evidence(target, start, end, sessions, memory, git, jira, buckets=None, task_ids=None):
    tasks = load_long_tracking_tasks(task_ids)
    result = {
        "enabled": True,
        "count": len(tasks),
        "match_count": 0,
        "rules": (
            "Long-term tasks are tracked with explicit keywords, Jira keys, and repo paths. "
            "Report progress as one or two outcome-focused sentences; do not expand step-by-step process logs. "
            "Use task.metric_tables when present; they preserve full quantitative matrix evidence for PPS, Entry/s, pass rates, and turning-point conclusions."
        ),
        "projects": [],
        "tasks": [],
    }
    if not tasks:
        return result

    task_items = {}
    term_map = {}
    for task in tasks:
        item = {
            "task_id": task["task_id"],
            "text": task["text"],
            "project": task["project"],
            "project_name": project_name(task["project"], PROJECTS),
            "priority": task["priority"],
            "status": task["status"],
            "area": task["area"],
            "tracking": task["tracking"],
            "progress_style": task["tracking"].get("progress_style") or "outcome",
            "outcome_hint": task["tracking"].get("outcome_hint") or "",
            "match_count": 0,
            "overflow": {"sessions": 0, "memory": 0, "git": 0, "jira": 0},
            "matches": {"sessions": [], "memory": [], "git": [], "jira": []},
            "metric_tables": [],
            "metric_table_overflow": 0,
            "_metric_table_signatures": set(),
        }
        task_items[task["task_id"]] = item
        term_map[task["task_id"]] = task_terms(task)

    session_paths = [Path(item["path"]) for item in sessions.get("files", [])]
    for path in session_paths:
        call_context = {}
        scratch = {
            "jira_keys": set(),
            "conversation_jira_keys": set(),
            "jira_candidates": set(),
            "commit_hashes": set(),
            "paths": set(),
            "repo_hint_paths": set(),
        }
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line_no, line in enumerate(fh, 1):
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    record_time = parse_record_time(record)
                    if record_time is None or not (start <= record_time < end):
                        continue
                    text, _ = scan_session_record(record, scratch, call_context)
                    if not text:
                        continue
                    for task_id, item in task_items.items():
                        terms = term_map[task_id]
                        matches = matched_terms(text, terms)
                        if matches:
                            add_limited_match(
                                item,
                                "sessions",
                                {
                                    "path": str(path),
                                    "line": line_no,
                                    "terms": matches[:5],
                                    "snippet": snippet_for_match(text, matches),
                                },
                            )
        except Exception as exc:
            for item in task_items.values():
                item.setdefault("errors", []).append({"source": "sessions", "path": str(path), "error": str(exc)})

    for mem_item in memory.get("files", []):
        path = Path(mem_item.get("path") or "")
        if not path.exists() or mem_item.get("low_signal"):
            continue
        text_or_none, error = read_limited_text(path)
        if not text_or_none:
            continue
        for task_id, item in task_items.items():
            matches = matched_terms(text_or_none, term_map[task_id])
            if matches:
                if path.suffix.lower() in {".md", ".txt"}:
                    tables = extract_metric_tables(text_or_none, path, "memory", term_map[task_id])
                    add_limited_metric_tables(item, tables)
                add_limited_match(
                    item,
                    "memory",
                    {
                        "path": str(path),
                        "terms": matches[:5],
                        "snippet": snippet_for_match(text_or_none, matches),
                    },
                )

    for repo in git.get("repos", []):
        repo_path = repo.get("path") or ""
        for task_id, item in task_items.items():
            tracking = item.get("tracking") or {}
            terms = term_map[task_id]
            repo_tracked = repo_matches_tracking(repo_path, tracking.get("repo_paths") or [])
            for commit in repo.get("commits", []):
                subject = commit.get("subject") or ""
                matches = matched_terms(subject, terms)
                if repo_tracked or matches:
                    add_limited_match(
                        item,
                        "git",
                        {
                            "repo": repo_path,
                            "short": commit.get("short"),
                            "author_date": commit.get("author_date"),
                            "subject": subject,
                            "terms": matches[:5],
                            "repo_tracked": repo_tracked,
                        },
                    )

    jira_items = []
    jira_items.extend(jira.get("resolved") or [])
    jira_items.extend(jira.get("created_on_target_date") or [])
    jira_items.extend(jira.get("closed_on_target_date") or [])
    seen_jira = set()
    unique_jira_items = []
    for item in jira_items:
        key = item.get("key")
        marker = (key, item.get("summary"), str(item.get("status")))
        if marker in seen_jira:
            continue
        seen_jira.add(marker)
        unique_jira_items.append(item)
    for jira_item in unique_jira_items:
        key = jira_item.get("key") or ""
        summary = jira_item.get("summary") or jira_item.get("text") or ""
        for task_id, item in task_items.items():
            tracking = item.get("tracking") or {}
            tracked_keys = tracking.get("jira_keys") or []
            matches = matched_terms(key, term_map[task_id])
            if key in tracked_keys or matches:
                add_limited_match(
                    item,
                    "jira",
                    {
                        "key": key,
                        "summary": summary,
                        "status": jira_item.get("status"),
                        "done": jira_item.get("done"),
                        "terms": matches[:5],
                    },
                )

    if buckets is not None:
        tracking_paths = []
        for task in tasks:
            tracking_paths.extend((task.get("tracking") or {}).get("repo_paths") or [])
        collect_referenced_metric_tables(
            task_items,
            term_map,
            list(buckets.get("paths") or []) + tracking_paths,
            buckets.get("metric_source_refs") or [],
        )

    for item in task_items.values():
        item.pop("_metric_table_signatures", None)
        result["match_count"] += item["match_count"]
        result["tasks"].append(item)
    for project in PROJECTS:
        if not project.get("active", True):
            continue
        project_tasks = [item for item in result["tasks"] if item.get("project") == project["key"]]
        result["projects"].append(
            {
                "key": project["key"],
                "name": project["name"],
                "goal": project.get("goal") or "",
                "report_profile": project.get("report_profile") or {},
                "active": True,
                "task_ids": [item["task_id"] for item in project_tasks],
                "long_task_count": len(project_tasks),
                "match_count": sum(item.get("match_count", 0) for item in project_tasks),
                "progress_rule": "项目进展只围绕本项目 report_profile 和长期任务写成果性进展，不按当天零散事项发散。",
            }
        )
    return result


def collect_sessions(target, start, end, buckets):
    paths, session_dir = session_candidates(target, start)
    result = {
        "root": str(session_dir),
        "files": [],
        "errors": [],
        "count": 0,
    }
    if not paths and not session_dir.exists():
        result["errors"].append({"path": str(session_dir), "error": "session directory missing"})
        return result

    for path in paths:
        item = file_summary(path)
        item.update(
            {
                "line_count": 0,
                "records_in_target_day": 0,
                "json_errors": 0,
                "event_types": {},
                "user_messages": 0,
                "assistant_messages": 0,
                "tool_calls": 0,
                "mtime_in_target_day": in_day(path.stat().st_mtime, start, end),
            }
        )
        scanned = 0
        call_context = {}
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    item["line_count"] += 1
                    try:
                        record = json.loads(line)
                    except Exception:
                        item["json_errors"] += 1
                        continue
                    record_time = parse_record_time(record)
                    if record_time is None or not (start <= record_time < end):
                        continue
                    item["records_in_target_day"] += 1
                    rec_type = record.get("type") or "unknown"
                    item["event_types"][rec_type] = item["event_types"].get(rec_type, 0) + 1
                    payload_text = json.dumps(record.get("payload", {}), ensure_ascii=False)
                    if '"role": "user"' in payload_text or '"role":"user"' in payload_text:
                        item["user_messages"] += 1
                    if '"role": "assistant"' in payload_text or '"role":"assistant"' in payload_text:
                        item["assistant_messages"] += 1
                    if "exec_command" in payload_text or "apply_patch" in payload_text:
                        item["tool_calls"] += 1
                    if scanned < MAX_TEXT_SCAN_CHARS:
                        scan_target, is_dialogue = scan_session_record(record, buckets, call_context)
                        if scan_target:
                            chunk = scan_target[: min(len(scan_target), MAX_TEXT_SCAN_CHARS - scanned)]
                            scan_text(chunk, buckets, conversation=is_dialogue)
                            scanned += len(chunk)
        except Exception as exc:
            result["errors"].append({"path": str(path), "error": str(exc)})
        if item["records_in_target_day"] > 0:
            result["files"].append(item)

    result["count"] = len(result["files"])
    if result["count"] == 0 and not result["errors"]:
        result["errors"].append({"path": str(session_dir), "error": "no session records for target date"})
    return result


def collect_memory_files(start, end, buckets):
    result = {
        "roots": [str(root) for root in MEMORY_ROOTS],
        "files": [],
        "errors": [],
        "count": 0,
        "low_signal_count": 0,
    }
    for root in MEMORY_ROOTS:
        if not root.exists():
            result["errors"].append({"path": str(root), "error": "memory directory missing"})
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path_has_blocked_component(path):
                continue
            try:
                st = path.stat()
            except Exception as exc:
                result["errors"].append({"path": str(path), "error": str(exc)})
                continue
            if not in_day(st.st_mtime, start, end):
                continue
            rel = path.relative_to(root)
            low_signal = root == MEMORIES_ROOT and any(
                part in {"bus", "daemon", "logs", "lock", "result"} for part in rel.parts
            )
            item = file_summary(path)
            item["root"] = str(root)
            item["relative_path"] = str(rel)
            item["low_signal"] = low_signal
            if low_signal:
                result["low_signal_count"] += 1
            text_or_none = None
            error = None
            try:
                if st.st_size <= SAFE_STAT_LIMIT:
                    text_or_none = path.read_text(encoding="utf-8", errors="replace")
                else:
                    with path.open("r", encoding="utf-8", errors="replace") as fh:
                        text_or_none = fh.read(SAFE_STAT_LIMIT)
            except Exception as exc:
                error = str(exc)
            if text_or_none is not None:
                scan_text(text_or_none, buckets)
            if error:
                item["scan_error"] = error
                result["errors"].append({"path": str(path), "error": error})
            result["files"].append(item)

    result["count"] = len(result["files"])
    return result


def normalize_existing_path(candidate):
    candidate = candidate.strip()
    if not candidate or "\x00" in candidate or path_has_blocked_component(candidate):
        return None
    path = Path(candidate)
    suffixes = ["", ".", ":", ",", ";"]
    for _ in suffixes:
        if not path_under_allowed_repo_prefix(path):
            return None
        if safe_path_exists(path):
            return path
        parent = path.parent
        if parent != path and path_under_allowed_repo_prefix(parent) and safe_path_exists(parent):
            return parent
        candidate = candidate.rstrip(".,);:]}")
        path = Path(candidate)
    return None


def find_git_root(path):
    current = path if safe_path_is_dir(path) else path.parent
    for candidate in [current] + list(current.parents):
        if path_has_blocked_component(candidate):
            return None
        if not path_under_allowed_repo_prefix(candidate):
            continue
        if has_git_metadata(candidate):
            return candidate
    return None


def repo_allowed(repo):
    try:
        resolved = repo.resolve()
    except Exception:
        resolved = repo
    for prefix in ALLOWED_REPO_PREFIXES:
        try:
            resolved.relative_to(prefix)
            return True
        except Exception:
            continue
    return False


def run_git(repo, args):
    cmd = ["git", "-C", str(repo)] + args
    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def run_acli(args, timeout=30):
    if not ACLI_BIN.exists():
        return 127, "", f"acli not found: {ACLI_BIN}"
    cmd = [str(ACLI_BIN), "jira"] + list(args)
    env = os.environ.copy()
    env.setdefault("HOME", str(USER_ROOT))
    env.setdefault("PATH", os.environ.get("AI_DAILY_COMMAND_PATH", os.defpath))
    env.setdefault("http_proxy", DEFAULT_PROXY)
    env.setdefault("https_proxy", DEFAULT_PROXY)
    env.setdefault("HTTP_PROXY", env["http_proxy"])
    env.setdefault("HTTPS_PROXY", env["https_proxy"])
    env.setdefault("no_proxy", DEFAULT_NO_PROXY)
    env.setdefault("NO_PROXY", env["no_proxy"])
    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        cwd=str(CODEX_HOME),
        env=env,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def jira_status_is_done(status):
    if not isinstance(status, dict):
        return False
    name = str(status.get("name") or "").strip()
    if name and name.lower() in DONE_STATUS_NAMES:
        return True
    category = status.get("statusCategory") or {}
    return str(category.get("key") or "").lower() == "done"


def make_track_id_allocator(target):
    prefix = "TRK-" + target.strftime("%Y%m%d")
    max_seen = 0
    if PLAN_STATE.exists():
        try:
            data = json.loads(PLAN_STATE.read_text(encoding="utf-8"))
            for task in data.get("tasks", []):
                item_id = str(task.get("track_id") or task.get("task_id") or "").strip()
                if re.fullmatch(r"\d{8}-\d+", item_id) and (task.get("tracking") or {}).get("mode") != "long_term":
                    item_id = f"TRK-{item_id}"
                match = re.fullmatch(rf"{prefix}-(\d+)", item_id)
                if match:
                    max_seen = max(max_seen, int(match.group(1)))
        except Exception:
            pass

    next_value = max_seen + 1

    def allocate():
        nonlocal next_value
        task_id = f"{prefix}-{next_value}"
        next_value += 1
        return task_id

    return allocate


def resolve_jira_key(key):
    rc, out, err = run_acli(
        ["workitem", "view", key, "--fields", "key,summary,status,created,updated,resolutiondate,resolution", "--json"]
    )
    if rc != 0:
        return None, {"key": key, "command": "workitem view", "error": err or out}
    try:
        data = json.loads(out)
    except Exception as exc:
        return None, {"key": key, "command": "workitem view", "error": f"invalid json: {exc}"}
    fields = data.get("fields") or {}
    status = fields.get("status") or {}
    summary = fields.get("summary") or ""
    resolution = fields.get("resolution") or {}
    resolved = {
        "key": data.get("key") or key,
        "summary": summary,
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "resolutiondate": fields.get("resolutiondate"),
        "resolution": {
            "name": resolution.get("name"),
            "id": resolution.get("id"),
        } if isinstance(resolution, dict) else None,
        "status": {
            "name": status.get("name"),
            "category": (status.get("statusCategory") or {}).get("key"),
            "category_name": (status.get("statusCategory") or {}).get("name"),
        },
        "done": jira_status_is_done(status),
        "self": data.get("self"),
    }
    return resolved, None


def jira_item_from_search_entry(entry, fallback_key=None):
    fields = entry.get("fields") or {}
    status = fields.get("status") or {}
    key = entry.get("key") or fallback_key
    return {
        "key": key,
        "summary": fields.get("summary") or "",
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "resolutiondate": fields.get("resolutiondate"),
        "resolution": fields.get("resolution"),
        "status": {
            "name": status.get("name"),
            "category": (status.get("statusCategory") or {}).get("key"),
            "category_name": (status.get("statusCategory") or {}).get("name"),
        },
        "done": jira_status_is_done(status),
        "self": entry.get("self"),
    }


def resolve_jira_keys(keys):
    keys = sorted(set(keys))
    resolved = {}
    errors = []
    if not keys:
        return resolved, errors

    for key in keys:
        if key in resolved:
            continue
        item, error = resolve_jira_key(key)
        if error:
            errors.append(error)
        else:
            resolved[item["key"]] = item
    return resolved, errors


def jira_project_clause():
    projects = sorted(str(item).strip() for item in KNOWN_JIRA_PROJECTS if str(item).strip())
    if not projects:
        return ""
    escaped = []
    for project in projects:
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", project):
            escaped.append(project)
        else:
            escaped.append('"' + project.replace('"', '\\"') + '"')
    return "project in ({}) AND ".format(",".join(escaped))


def search_jira_keys_by_jql(jql, label):
    rc, out, err = run_acli(
        ["workitem", "search", "--jql", jql, "--fields", "key,summary,status", "--json"],
        timeout=45,
    )
    if rc != 0:
        return [], {"command": "workitem search", "label": label, "jql": jql, "error": err or out}
    try:
        items = json.loads(out)
    except Exception as exc:
        return [], {"command": "workitem search", "label": label, "jql": jql, "error": f"invalid json: {exc}"}
    if not isinstance(items, list):
        return [], {"command": "workitem search", "label": label, "jql": jql, "error": "search result is not a list"}
    keys = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        if key and key not in keys:
            keys.append(key)
    return keys, None


def discover_repos(paths):
    repos = {}
    for raw in sorted(paths):
        existing = normalize_existing_path(raw)
        if existing is None:
            continue
        if not path_under_allowed_repo_prefix(existing):
            continue
        repo = find_git_root(existing)
        if repo is not None and repo_allowed(repo):
            repos[str(repo)] = repo
    return repos


def collect_git_commits(start, end, buckets):
    repo_source_paths = buckets["repo_hint_paths"] or buckets["paths"]
    repos = discover_repos(repo_source_paths)
    result = {
        "repos": [],
        "repo_count": len(repos),
        "repo_hint_paths": sorted(buckets["repo_hint_paths"]),
        "commit_hashes_mentioned": sorted(buckets["commit_hashes"]),
        "errors": [],
    }
    since = start.isoformat()
    until = end.isoformat()
    for repo in sorted(repos.values(), key=lambda item: str(item)):
        item = {"path": str(repo), "commits": [], "errors": []}
        rc, branch, err = run_git(repo, ["branch", "--show-current"])
        if rc == 0:
            item["branch"] = branch
        else:
            item["errors"].append({"command": "branch --show-current", "error": err})
        rc, head, err = run_git(repo, ["rev-parse", "--short=12", "HEAD"])
        if rc == 0:
            item["head"] = head
        else:
            item["errors"].append({"command": "rev-parse HEAD", "error": err})
        rc, status_out, err = run_git(repo, ["status", "--short"])
        if rc == 0:
            status_lines = [line for line in status_out.splitlines() if line.strip()]
            item["worktree"] = {
                "dirty": bool(status_lines),
                "changed_count": len(status_lines),
                "status_sample": status_lines[:40],
            }
        else:
            item["errors"].append({"command": "status --short", "error": err})
        fmt = "%H%x09%h%x09%aI%x09%an%x09%s"
        rc, out, err = run_git(
            repo,
            ["log", "--all", f"--since={since}", f"--until={until}", "--date=iso-strict", f"--pretty=format:{fmt}", "-n", "80"],
        )
        if rc != 0:
            item["errors"].append({"command": "log --all", "error": err})
        elif out:
            seen = set()
            for line in out.splitlines():
                parts = line.split("\t", 4)
                if len(parts) != 5:
                    continue
                commit = {
                    "hash": parts[0],
                    "short": parts[1],
                    "author_date": parts[2],
                    "author": parts[3],
                    "subject": parts[4],
                }
                if commit["hash"] in seen:
                    continue
                seen.add(commit["hash"])
                item["commits"].append(commit)
                scan_text(commit["subject"], buckets)
        result["repos"].append(item)
        result["errors"].extend({"repo": str(repo), **entry} for entry in item["errors"])
    return result


def collect_jira(target, start, end, buckets):
    created_jql = f'{jira_project_clause()}created >= "{target.isoformat()}" AND created < "{(target + timedelta(days=1)).isoformat()}"'
    closed_jql = f'{jira_project_clause()}resolutiondate >= "{target.isoformat()}" AND resolutiondate < "{(target + timedelta(days=1)).isoformat()}"'
    result = {
        "resolved": [],
        "created_on_target_date": [],
        "closed_on_target_date": [],
        "errors": [],
        "jql": {
            "created_on_target_date": created_jql,
            "closed_on_target_date": closed_jql,
        },
        "classification_rule": "Only Jira issues created on the target date or resolved/closed on the target date are report evidence. Jira keys merely mentioned in dialogue are intentionally suppressed; historical tracking comes from plan_state.",
    }

    created_keys, error = search_jira_keys_by_jql(created_jql, "created_on_target_date")
    if error:
        result["errors"].append(error)
    closed_keys, error = search_jira_keys_by_jql(closed_jql, "closed_on_target_date")
    if error:
        result["errors"].append(error)

    resolved_map, resolve_errors = resolve_jira_keys(created_keys + closed_keys)
    result["errors"].extend(resolve_errors)
    for resolved in sorted(resolved_map.values(), key=lambda item: item["key"]):
        result["resolved"].append(resolved)
        if jira_time_in_day(resolved.get("created"), start, end):
            result["created_on_target_date"].append(
                {
                    "key": resolved["key"],
                    "summary": resolved["summary"],
                    "status": resolved["status"],
                    "created": resolved.get("created"),
                    "reason": "created_on_target_date",
                }
            )
        if jira_time_in_day(resolved.get("resolutiondate"), start, end):
            result["closed_on_target_date"].append(
                {
                    "key": resolved["key"],
                    "summary": resolved["summary"],
                    "status": resolved["status"],
                    "resolution": resolved.get("resolution"),
                    "resolutiondate": resolved.get("resolutiondate"),
                    "reason": "closed_on_target_date",
                }
            )

    return result


def empty_long_task_evidence(reason):
    return {
        "enabled": False,
        "disabled_reason": reason,
        "count": 0,
        "match_count": 0,
        "rules": (
            "Long-term task evidence was intentionally skipped for this run. "
            "Do not infer project progress from current plan_state.json."
        ),
        "projects": [],
        "tasks": [],
    }


def plan_record_summary(item):
    tracking = item.get("tracking") if isinstance(item.get("tracking"), dict) else {}
    project_key = item.get("project") or infer_project_key(item, PROJECTS)
    result = {
        "id": item.get("track_id") or item.get("task_id"),
        "task_id": item.get("task_id"),
        "track_id": item.get("track_id", ""),
        "item_type": item.get("item_type") or ("track" if item.get("track_id") else "task"),
        "project": project_key,
        "project_name": project_name(project_key, PROJECTS),
        "project_report_profile": project_report_profile(project_key),
        "text": item.get("text") or "",
        "status": item.get("status") or "",
        "priority": item.get("priority") or "",
        "origin_date": item.get("origin_date") or "",
        "last_reviewed_date": item.get("last_reviewed_date") or "",
        "completed_date": item.get("completed_date") or "",
        "related_task_id": item.get("related_task_id") or "",
        "completion_criteria": item.get("completion_criteria") or "",
        "metric": item.get("metric") or "",
        "target_metric": item.get("target_metric") or "",
        "note": item.get("note") or "",
    }
    if tracking:
        result["tracking"] = {
            "mode": tracking.get("mode") or "none",
            "progress_style": tracking.get("progress_style") or "outcome",
            "keywords": normalize_string_list(tracking.get("keywords"))[:10],
            "jira_keys": normalize_string_list(tracking.get("jira_keys"))[:10],
            "repo_paths": normalize_string_list(tracking.get("repo_paths"))[:10],
            "outcome_hint": str(tracking.get("outcome_hint") or "").strip(),
        }
    return result


def collect_plan_state_history(target, item_ids=None):
    result = {
        "enabled": PLAN_STATE.exists(),
        "path": str(PLAN_STATE),
        "updated_at": None,
        "active_long_tasks": [],
        "active_tracking_items": [],
        "target_date_terminal_items": [],
        "errors": [],
        "rules": (
            "This is historical ledger context only. The report generator decides whether to update, close, "
            "or create tracking items based on outcome evidence; collect_evidence.py does not infer tracking changes."
        ),
    }
    if not PLAN_STATE.exists():
        result["errors"].append({"path": str(PLAN_STATE), "error": "missing plan_state.json"})
        return result
    try:
        data = json.loads(PLAN_STATE.read_text(encoding="utf-8"))
    except Exception as exc:
        result["errors"].append({"path": str(PLAN_STATE), "error": str(exc)})
        return result
    result["updated_at"] = data.get("updated_at")
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
    requested_ids = {str(item_id or "").strip() for item_id in (item_ids or []) if str(item_id or "").strip()}
    for item in tasks:
        if not isinstance(item, dict):
            continue
        if requested_ids and not (record_plan_ids(item) & requested_ids):
            continue
        status = str(item.get("status") or "").strip()
        item_type = str(item.get("item_type") or "").strip()
        is_tracking = item_type == "track" or bool(item.get("track_id"))
        summary = plan_record_summary(item)
        if status in ACTIVE_TASK_STATUSES:
            if is_tracking:
                result["active_tracking_items"].append(summary)
            else:
                result["active_long_tasks"].append(summary)
        elif requested_ids or item.get("completed_date") == target.isoformat() or item.get("last_reviewed_date") == target.isoformat():
            result["target_date_terminal_items"].append(summary)
    result["counts"] = {
        "active_long_tasks": len(result["active_long_tasks"]),
        "active_tracking_items": len(result["active_tracking_items"]),
        "target_date_terminal_items": len(result["target_date_terminal_items"]),
    }
    return result


def read_json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def collect_reme_evidence(target, start, end):
    day = target.isoformat()
    audit_day_dir = CODEX_HOME / "memories" / "reme-memory" / "audit" / "precompact" / day
    result = {
        "enabled": audit_day_dir.exists(),
        "audit_day_dir": str(audit_day_dir),
        "precompact_event_count": 0,
        "refine_enabled_count": 0,
        "refine_accepted_count": 0,
        "refine_missing_result_count": 0,
        "capture_count": 0,
        "compact_memory_count": 0,
        "artifact_count": 0,
        "source_transcript_count": 0,
        "source_transcripts": [],
        "samples": [],
        "errors": [],
        "rules": (
            "Operational evidence is context. Treat it as project progress only when target-day outcomes, git, "
            "long tasks, or project report_profile explicitly make this tool work relevant."
        ),
    }
    if not audit_day_dir.exists():
        return result

    events_path = audit_day_dir / "events.jsonl"
    event_roots = []
    if events_path.exists():
        try:
            with events_path.open("r", encoding="utf-8", errors="replace") as fh:
                for line_no, line in enumerate(fh, 1):
                    try:
                        event = json.loads(line)
                    except Exception as exc:
                        result["errors"].append({"path": str(events_path), "line": line_no, "error": str(exc)})
                        continue
                    if event.get("event") != "PreCompact":
                        continue
                    result["precompact_event_count"] += 1
                    if event.get("refine_enabled"):
                        result["refine_enabled_count"] += 1
                    transcript = str(event.get("transcript_path") or "").strip()
                    if transcript and transcript not in result["source_transcripts"]:
                        result["source_transcripts"].append(transcript)
                    root = str(event.get("audit_run_root") or "").strip()
                    if root:
                        event_roots.append(Path(root))
        except Exception as exc:
            result["errors"].append({"path": str(events_path), "error": str(exc)})
    else:
        event_roots = sorted(audit_day_dir.glob("precompact_*"))

    seen_roots = set()
    for root in event_roots:
        root_key = str(root)
        if root_key in seen_roots or not root.is_dir():
            continue
        seen_roots.add(root_key)
        event = read_json_file(root / "event.json") or {}
        refine_out = read_json_file(root / "commands" / "refine.stdout.json") or {}
        runner_out = read_json_file(root / "commands" / "runner.stdout.json") or {}
        if refine_out.get("status") == "accepted":
            result["refine_accepted_count"] += 1
        elif event.get("refine_enabled"):
            result["refine_missing_result_count"] += 1

        capture_id = str(runner_out.get("capture_id") or "").strip()
        if capture_id:
            result["capture_count"] += 1
        if runner_out.get("compact_memory_md_path"):
            result["compact_memory_count"] += 1
        try:
            result["artifact_count"] += int(runner_out.get("artifact_count") or 0)
        except Exception:
            pass
        for err in runner_out.get("errors") or []:
            result["errors"].append({"path": str(root), "error": err})
        if len(result["samples"]) < 12:
            result["samples"].append(
                {
                    "audit_run": root.name,
                    "transcript_path": event.get("transcript_path"),
                    "refine_enabled": bool(event.get("refine_enabled")),
                    "refine_status": refine_out.get("status") or "",
                    "request_id": refine_out.get("request_id") or "",
                    "capture_id": capture_id,
                    "compact_memory_md_path": runner_out.get("compact_memory_md_path") or "",
                    "handoff_md_path": runner_out.get("handoff_md_path") or "",
                    "artifact_count": runner_out.get("artifact_count") or 0,
                }
            )

    result["source_transcripts"] = result["source_transcripts"][:20]
    result["source_transcript_count"] = len(result["source_transcripts"])
    return result


def empty_token_usage(reason, error=None):
    errors = []
    if error:
        errors.append({"message": str(error)})
    return {
        "enabled": False,
        "disabled_reason": reason,
        "source": "",
        "records": 0,
        "session_count": 0,
        "owner_count": 0,
        "subagent_count": 0,
        "approval_count": 0,
        "permission_approval_count": 0,
        "agent_count": 0,
        "total": {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 0,
        },
        "by_agent": [],
        "errors": errors,
    }


def resolve_usage_agent_from_cwd(cwd, cache):
    cwd = str(cwd or "").strip()
    if not cwd:
        return "unknown"
    if cwd.startswith("external:"):
        return cwd.split(":", 1)[1] or cwd
    if not cwd.startswith(("/", "~")):
        return cwd
    if cwd in cache:
        return cache[cwd]
    if COAGENT_SH.is_file():
        try:
            result = subprocess.run(
                [str(COAGENT_SH), "resolve-cwd", cwd, "--json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                timeout=10,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                name = str(data.get("name") or "").strip()
                if name:
                    cache[cwd] = name
                    return name
        except Exception:
            pass
    cache[cwd] = cwd
    return cwd


def merge_token_usage_row(bucket, item, usage):
    bucket["records"] += int(item.get("records") or 0)
    bucket["session_count"] += int(item.get("session_count") or 0)
    bucket["owner_count"] += int(item.get("owner_count") or item.get("session_count") or 0)
    bucket["subagent_count"] += int(item.get("subagent_count") or 0)
    approval_count = int(item.get("approval_count") or item.get("permission_approval_count") or 0)
    bucket["approval_count"] += approval_count
    bucket["permission_approval_count"] += approval_count
    for field in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"):
        bucket["usage"][field] += int(usage.get(field) or 0)


def collect_token_usage(target):
    if not USAGE_QUERY.is_file():
        return empty_token_usage("query_script_missing", f"missing {USAGE_QUERY}")
    cmd = [
        sys.executable,
        str(USAGE_QUERY),
        "--date",
        target.isoformat(),
        "--source",
        "ledger",
        "--group-by",
        "agent",
        "--json",
    ]
    if USAGE_ROOT:
        cmd.extend(["--usage-root", USAGE_ROOT])
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=60,
        )
    except Exception as exc:
        return empty_token_usage("query_failed", exc)
    if result.returncode != 0:
        return empty_token_usage(
            "query_failed",
            {
                "returncode": result.returncode,
                "stderr": result.stderr[-2000:],
            },
        )
    try:
        data = json.loads(result.stdout)
    except Exception as exc:
        return empty_token_usage("invalid_query_json", exc)

    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    groups = summary.get("groups") if isinstance(summary.get("groups"), list) else []
    by_agent_map = {}
    agent_cache = {}
    for item in groups:
        if not isinstance(item, dict):
            continue
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
        if int(usage.get("total_tokens") or 0) <= 0:
            continue
        agent = resolve_usage_agent_from_cwd(item.get("key") or "unknown", agent_cache)
        bucket = by_agent_map.setdefault(
            agent,
            {
                "agent": agent,
                "records": 0,
                "session_count": 0,
                "owner_count": 0,
                "subagent_count": 0,
                "approval_count": 0,
                "permission_approval_count": 0,
                "usage": {
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "total_tokens": 0,
                },
            },
        )
        merge_token_usage_row(bucket, item, usage)
    by_agent = sorted(by_agent_map.values(), key=lambda row: row["usage"]["total_tokens"], reverse=True)

    total = summary.get("usage") if isinstance(summary.get("usage"), dict) else {}
    session_count = int(summary.get("session_count") or 0)
    owner_count = int(summary.get("owner_count") or session_count)
    subagent_count = int(summary.get("subagent_count") or 0)
    approval_count = int(summary.get("approval_count") or summary.get("permission_approval_count") or 0)
    return {
        "enabled": True,
        "source": data.get("source") or "",
        "records": int(summary.get("records") or 0),
        "session_count": session_count,
        "owner_count": owner_count,
        "subagent_count": subagent_count,
        "approval_count": approval_count,
        "permission_approval_count": approval_count,
        "agent_count": len(by_agent),
        "total": {
            "input_tokens": int(total.get("input_tokens") or 0),
            "cached_input_tokens": int(total.get("cached_input_tokens") or 0),
            "output_tokens": int(total.get("output_tokens") or 0),
            "reasoning_output_tokens": int(total.get("reasoning_output_tokens") or 0),
            "total_tokens": int(total.get("total_tokens") or 0),
        },
        "by_agent": by_agent,
        "errors": [],
    }


def build_coverage(sessions, memory, git, jira, long_tasks, token_usage, plan_state, reme):
    errors = []
    errors.extend({"source": "sessions", **err} for err in sessions.get("errors", []))
    errors.extend({"source": "memory", **err} for err in memory.get("errors", []))
    errors.extend({"source": "git", **err} for err in git.get("errors", []))
    errors.extend({"source": "jira", **err} for err in jira.get("errors", []))
    errors.extend({"source": "token_usage", **err} for err in token_usage.get("errors", []))
    errors.extend({"source": "plan_state", **err} for err in plan_state.get("errors", []))
    errors.extend({"source": "reme", **err} for err in reme.get("errors", []))
    for task in long_tasks.get("tasks", []):
        errors.extend({"source": "long_tasks", "task_id": task.get("task_id"), **err} for err in task.get("errors", []))
    return {
        "session_files": sessions.get("count", 0),
        "memory_files": memory.get("count", 0),
        "memory_low_signal_files": memory.get("low_signal_count", 0),
        "git_repos": git.get("repo_count", 0),
        "git_commits_today": sum(len(repo.get("commits", [])) for repo in git.get("repos", [])),
        "jira_created_on_target_date": len(jira.get("created_on_target_date", [])),
        "jira_closed_on_target_date": len(jira.get("closed_on_target_date", [])),
        "long_tasks": long_tasks.get("count", 0),
        "long_task_matches": long_tasks.get("match_count", 0),
        "long_task_metric_tables": sum(
            len(task.get("metric_tables", [])) for task in long_tasks.get("tasks", [])
        ),
        "active_projects": len([item for item in long_tasks.get("projects", []) if item.get("active")]),
        "plan_active_long_tasks": len(plan_state.get("active_long_tasks", [])),
        "plan_active_tracking_items": len(plan_state.get("active_tracking_items", [])),
        "plan_target_date_terminal_items": len(plan_state.get("target_date_terminal_items", [])),
        "reme_precompact_events": reme.get("precompact_event_count", 0),
        "reme_refine_accepted": reme.get("refine_accepted_count", 0),
        "reme_refine_missing": reme.get("refine_missing_result_count", 0),
        "reme_captures": reme.get("capture_count", 0),
        "token_usage_agents": token_usage.get("agent_count", 0),
        "token_usage_total_tokens": (token_usage.get("total") or {}).get("total_tokens", 0),
        "error_count": len(errors),
        "errors": errors,
    }


def collect(target_date, include_long_tasks=True, plan_item_ids=None):
    start, end = day_bounds(target_date)
    buckets = {
        "jira_keys": set(),
        "conversation_jira_keys": set(),
        "jira_candidates": set(),
        "commit_hashes": set(),
        "paths": set(),
        "repo_hint_paths": set(),
        "metric_source_refs": set(),
    }
    sessions = collect_sessions(target_date, start, end, buckets)
    memory = collect_memory_files(start, end, buckets)
    git = collect_git_commits(start, end, buckets)
    jira = collect_jira(target_date, start, end, buckets)
    plan_state = collect_plan_state_history(target_date, item_ids=plan_item_ids)
    reme = collect_reme_evidence(target_date, start, end)
    if include_long_tasks:
        long_tasks = collect_long_task_evidence(
            target_date,
            start,
            end,
            sessions,
            memory,
            git,
            jira,
            buckets,
            task_ids=plan_item_ids,
        )
    else:
        long_tasks = empty_long_task_evidence("plan_state_disabled")
    token_usage = collect_token_usage(target_date)
    result = {
        "version": 1,
        "date": target_date.isoformat(),
        "timezone": getattr(TZ, "key", str(TZ)),
        "generated_at": datetime.now(TZ).replace(microsecond=0).isoformat(),
        "coverage": build_coverage(sessions, memory, git, jira, long_tasks, token_usage, plan_state, reme),
        "sessions": sessions,
        "memory": memory,
        "git": git,
        "jira": jira,
        "plan_state": plan_state,
        "reme": reme,
        "long_tasks": long_tasks,
        "token_usage": token_usage,
        "candidates": {
            "paths": sorted(buckets["paths"]),
            "commit_hashes": sorted(buckets["commit_hashes"]),
            "repo_hint_paths": sorted(buckets["repo_hint_paths"]),
            "metric_source_refs": sorted(buckets["metric_source_refs"]),
        },
    }
    return result


def print_summary(data):
    cov = data["coverage"]
    print(f"evidence_date={data['date']} timezone={data['timezone']}")
    print(
        "coverage "
        f"sessions={cov['session_files']} "
        f"memory={cov['memory_files']} "
        f"memory_low_signal={cov['memory_low_signal_files']} "
        f"git_repos={cov['git_repos']} "
        f"git_commits_today={cov['git_commits_today']} "
        f"jira_created={cov.get('jira_created_on_target_date', 0)} "
        f"jira_closed={cov.get('jira_closed_on_target_date', 0)} "
        f"long_tasks={cov['long_tasks']} "
        f"long_task_matches={cov['long_task_matches']} "
        f"long_task_metric_tables={cov.get('long_task_metric_tables', 0)} "
        f"active_projects={cov['active_projects']} "
        f"plan_long_tasks={cov.get('plan_active_long_tasks', 0)} "
        f"plan_tracking={cov.get('plan_active_tracking_items', 0)} "
        f"reme_precompact={cov.get('reme_precompact_events', 0)} "
        f"reme_refine_accepted={cov.get('reme_refine_accepted', 0)} "
        f"token_agents={cov.get('token_usage_agents', 0)} "
        f"token_total={cov.get('token_usage_total_tokens', 0)} "
        f"errors={cov['error_count']}"
    )
    if data["sessions"]["files"]:
        print("sessions:")
        for item in data["sessions"]["files"]:
            print(f"- {item['path']} size={item['size']} mtime={item['mtime']} lines={item['line_count']}")
    if data["memory"]["files"]:
        high_signal = [item for item in data["memory"]["files"] if not item.get("low_signal")]
        low_signal = [item for item in data["memory"]["files"] if item.get("low_signal")]
        print("memory_files_high_signal:")
        for item in high_signal[:40]:
            print(f"- {item['path']} size={item['size']} mtime={item['mtime']}")
        if len(high_signal) > 40:
            print(f"- ... {len(high_signal) - 40} more")
        print(f"memory_files_low_signal_count={len(low_signal)}")
    if data["git"]["repos"]:
        print("git_repos:")
        for repo in data["git"]["repos"]:
            worktree = repo.get("worktree") or {}
            changed = worktree.get("changed_count", 0)
            dirty = "dirty" if worktree.get("dirty") else "clean"
            print(f"- {repo['path']} branch={repo.get('branch', '')} head={repo.get('head', '')} commits={len(repo.get('commits', []))} worktree={dirty} changed={changed}")
            for line in worktree.get("status_sample", [])[:8]:
                print(f"  status: {line}")
            for commit in repo.get("commits", [])[:8]:
                print(f"  - {commit['short']} {commit['author_date']} {commit['subject']}")
    if data["jira"].get("created_on_target_date"):
        print("jira_created_on_target_date:")
        for item in data["jira"]["created_on_target_date"]:
            status_name = (item.get("status") or {}).get("name") or ""
            print(f"- {item['key']} created={item.get('created', '')} status={status_name} {item.get('summary', '')}")
    if data["jira"].get("closed_on_target_date"):
        print("jira_closed_on_target_date:")
        for item in data["jira"]["closed_on_target_date"]:
            status_name = (item.get("status") or {}).get("name") or ""
            print(f"- {item['key']} resolutiondate={item.get('resolutiondate', '')} status={status_name} {item.get('summary', '')}")
    plan_state = data.get("plan_state") or {}
    if plan_state.get("active_tracking_items"):
        print("plan_tracking_items:")
        for item in plan_state["active_tracking_items"][:20]:
            print(f"- {item.get('track_id') or item.get('id')} status={item.get('status')} priority={item.get('priority')} {item.get('text', '')}")
    if data.get("long_tasks", {}).get("tasks"):
        if data["long_tasks"].get("projects"):
            print("projects:")
            for project in data["long_tasks"]["projects"]:
                print(
                    f"- {project['name']} key={project['key']} tasks={len(project.get('task_ids') or [])} "
                    f"matches={project.get('match_count', 0)}"
                )
        print("long_tasks:")
        for item in data["long_tasks"]["tasks"]:
            tracking = item.get("tracking") or {}
            print(
                f"- {item['task_id']} project={item.get('project_name', item.get('project', ''))} matches={item.get('match_count', 0)} "
                f"style={item.get('progress_style', '')} keywords={','.join((tracking.get('keywords') or [])[:5])}"
            )
            for source, matches in (item.get("matches") or {}).items():
                if not matches:
                    continue
                print(f"  {source}: {len(matches)}")
            if item.get("metric_tables"):
                print(f"  metric_tables: {len(item['metric_tables'])}")
                for table in item["metric_tables"][:3]:
                    title = table.get("title") or table.get("path", "")
                    print(
                        f"    - {table.get('source_kind')} {title} "
                        f"rows={table.get('row_count', 0)} path={table.get('path')}"
                    )
    reme = data.get("reme") or {}
    if reme.get("precompact_event_count"):
        print(
            "reme: "
            f"precompact={reme.get('precompact_event_count', 0)} "
            f"refine_enabled={reme.get('refine_enabled_count', 0)} "
            f"refine_accepted={reme.get('refine_accepted_count', 0)} "
            f"captures={reme.get('capture_count', 0)} "
            f"compact_memories={reme.get('compact_memory_count', 0)} "
            f"artifacts={reme.get('artifact_count', 0)}"
        )
        for item in (reme.get("samples") or [])[:5]:
            print(
                f"- {item.get('audit_run')} status={item.get('refine_status')} "
                f"capture={item.get('capture_id')} artifacts={item.get('artifact_count')}"
            )
    token_usage = data.get("token_usage") or {}
    if token_usage.get("by_agent"):
        print("token_usage:")
        for item in token_usage["by_agent"]:
            usage = item.get("usage") or {}
            print(
                f"- {item.get('agent')} total={usage.get('total_tokens', 0)} "
                f"input={usage.get('input_tokens', 0)} output={usage.get('output_tokens', 0)} "
                f"sessions={item.get('owner_count', 0)} subagents={item.get('subagent_count', 0)} "
                f"approvals={item.get('approval_count', item.get('permission_approval_count', 0))}"
            )
        total = token_usage.get("total") or {}
        print(
            f"- 总计 total={total.get('total_tokens', 0)} "
            f"input={total.get('input_tokens', 0)} output={total.get('output_tokens', 0)} "
            f"sessions={token_usage.get('owner_count', 0)} subagents={token_usage.get('subagent_count', 0)} "
            f"approvals={token_usage.get('approval_count', token_usage.get('permission_approval_count', 0))}"
        )
    if cov["errors"]:
        print("errors:")
        for err in cov["errors"][:20]:
            print("- " + json.dumps(err, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", nargs="?", default=datetime.now(TZ).date().isoformat())
    parser.add_argument("--output", help="output JSON path")
    parser.add_argument("--summary", action="store_true", help="print a human-readable summary")
    parser.add_argument(
        "--skip-long-tasks",
        action="store_true",
        help="do not read current plan_state.json for long-term task evidence",
    )
    parser.add_argument(
        "--plan-id",
        dest="plan_item_ids",
        action="append",
        help="limit plan-state and long-task evidence to this Task/Track ID; repeat for multiple items",
    )
    args = parser.parse_args(argv)

    target = parse_date(args.date)
    data = collect(
        target,
        include_long_tasks=not args.skip_long_tasks,
        plan_item_ids=args.plan_item_ids,
    )
    output = Path(args.output) if args.output else LOG_DIR / f"evidence-{target.isoformat()}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {output}")
    if args.summary:
        print_summary(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
