#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python 3.8 can use the optional backport.
    try:
        from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:
        ZoneInfo = None
        ZoneInfoNotFoundError = Exception


TIMEZONE_NAME = os.environ.get("AI_DAILY_TIMEZONE", "UTC")
if ZoneInfo is None:
    REPORT_TZ = timezone.utc
else:
    try:
        REPORT_TZ = ZoneInfo(TIMEZONE_NAME)
    except ZoneInfoNotFoundError:
        REPORT_TZ = ZoneInfo("UTC")
SCRIPT_DIR = Path(__file__).resolve().parent
USER_ROOT = Path(os.environ.get("CODEX_USER_HOME", str(Path.home()))).expanduser()
DEFAULT_CODEX_DIR = Path(
    os.environ.get("CODEX_HOME", str(USER_ROOT / ".codex"))
).expanduser()
DEFAULT_CURSOR_DIR = Path(os.environ.get("CURSOR_HOME", str(USER_ROOT / ".cursor")))
DEFAULT_DAILY_REPORT_DIR = Path(os.environ.get("AI_DAILY_REPORT_DIR", str(DEFAULT_CODEX_DIR / "daily_report")))
DEFAULT_OUTPUT_DIR = DEFAULT_DAILY_REPORT_DIR / "report_files"
DEFAULT_CONFIG = DEFAULT_DAILY_REPORT_DIR / "config.json"
DEFAULT_USAGE_QUERY = Path(os.environ.get("CODEX_USAGE_QUERY", str(DEFAULT_CODEX_DIR / "skills/usage-mgr/scripts/query_usage.py")))
DEFAULT_USAGE_ROOT = os.environ.get("CODEX_USAGE_ROOT", "")
DEFAULT_COAGENT_SH = Path(os.environ.get("COAGENT_SH", str(DEFAULT_CODEX_DIR / "skills/co-agent/scripts/coagent.sh")))


@dataclass
class SessionSummary:
    source: str
    session_id: str
    path: Path
    cwd: str = ""
    user_messages: List[str] = field(default_factory=list)
    assistant_messages: List[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate daily markdown report from Codex/Cursor logs.")
    parser.add_argument("--date", default=datetime.now(REPORT_TZ).strftime("%Y-%m-%d"))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    return parser.parse_args()


def load_config(path: Path) -> dict:
    if not path.exists():
        return {"cursor_projects": [], "extra_repos": []}
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_text(text: str) -> str:
    text = text.replace("\r", "\n")
    query_match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, flags=re.S)
    if query_match:
        text = query_match.group(1)
    text = re.sub(r"<attached_files>.*?</attached_files>", "", text, flags=re.S)
    text = re.sub(r"# AGENTS\.md instructions.*?</INSTRUCTIONS>", "", text, flags=re.S)
    text = re.sub(r"<environment_context>.*?</environment_context>", "", text, flags=re.S)
    text = re.sub(r"<user_query>\s*", "", text)
    text = re.sub(r"\s*</user_query>", "", text)
    text = re.sub(r"\[REDACTED\]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_text_items(content: Iterable[dict]) -> str:
    texts = []
    for item in content or []:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if text:
            texts.append(text)
    return normalize_text("\n".join(texts))


def shorten(text: str, limit: int = 100) -> str:
    single = " ".join(text.split())
    if len(single) <= limit:
        return single
    return single[: limit - 1] + "…"


def unique_keep_order(items: Iterable[str]) -> List[str]:
    ordered: OrderedDict[str, None] = OrderedDict()
    for item in items:
        item = item.strip()
        if item:
            ordered.setdefault(item, None)
    return list(ordered.keys())


def format_int(value: int) -> str:
    return f"{int(value):,}"


def format_token_approx(value: int) -> str:
    amount = int(value or 0)
    if amount == 0:
        return "0"
    sign = "-" if amount < 0 else ""
    absolute = abs(amount)
    if absolute >= 100_000_000:
        scaled = absolute / 100_000_000
        unit = "亿"
    else:
        scaled = absolute / 10_000
        unit = "万"
    if scaled < 0.005:
        return f"{sign}<0.01{unit}"
    text = f"{scaled:.2f}".rstrip("0").rstrip(".")
    return f"{sign}{text}{unit}"


def format_session_subagent_approval(owner_count: int, subagent_count: int, approval_count: int) -> str:
    return f"{int(owner_count or 0)}/{int(subagent_count or 0)}/{int(approval_count or 0)}"


def display_width(value) -> int:
    width = 0
    for char in str(value):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def pad_display(value, width: int, align: str) -> str:
    text = str(value)
    padding = max(0, width - display_width(text))
    if align == "right":
        return " " * padding + text
    return text + " " * padding


def aligned_markdown_table(headers: List[str], rows: List[List[str]], aligns: List[str]) -> List[str]:
    table_rows = [headers] + rows
    widths = [
        max(3, *(display_width(row[index]) for row in table_rows))
        for index in range(len(headers))
    ]

    def render(row: List[str]) -> str:
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


def report_month(target_day: str) -> str:
    return target_day[:4] + target_day[5:7]


def iter_codex_sessions_for_day(codex_dir: Path, target_day: str) -> Iterable[Path]:
    day = datetime.strptime(target_day, "%Y-%m-%d")
    day_dir = codex_dir / "sessions" / day.strftime("%Y") / day.strftime("%m") / day.strftime("%d")
    if not day_dir.exists():
        return []
    return sorted(day_dir.glob("*.jsonl"))


def parse_codex_session(path: Path) -> SessionSummary:
    summary = SessionSummary(source="Codex", session_id=path.stem, path=path)
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "session_meta":
            payload = obj.get("payload", {})
            summary.session_id = payload.get("id", summary.session_id)
            summary.cwd = payload.get("cwd", "")
            continue
        if obj.get("type") != "response_item":
            continue
        payload = obj.get("payload", {})
        if payload.get("type") != "message":
            continue
        role = payload.get("role", "")
        text = extract_text_items(payload.get("content", []))
        if not text:
            continue
        if role == "user":
            summary.user_messages.append(text)
        elif role == "assistant":
            summary.assistant_messages.append(text)
    return summary


def iter_cursor_transcripts(cursor_dir: Path, project_names: List[str], target_day: str) -> Iterable[Path]:
    start = datetime.strptime(target_day, "%Y-%m-%d").replace(tzinfo=REPORT_TZ)
    end = start + timedelta(days=1)
    results = []
    for project in project_names:
        base = cursor_dir / "projects" / project / "agent-transcripts"
        if not base.exists():
            continue
        for path in base.rglob("*.jsonl"):
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=REPORT_TZ)
            if start <= mtime < end:
                results.append(path)
    return sorted(results)


def parse_cursor_session(path: Path) -> SessionSummary:
    session_id = path.stem
    summary = SessionSummary(source="Cursor", session_id=session_id, path=path)
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = obj.get("role")
        message = obj.get("message", {})
        text = extract_text_items(message.get("content", []))
        if not text:
            continue
        if role == "user":
            summary.user_messages.append(text)
        elif role == "assistant":
            summary.assistant_messages.append(text)
    return summary


def git_cmd(repo: str, args: List[str]) -> str:
    result = subprocess.run(
        ["git", "-C", repo] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def git_repo_root(path: str) -> str:
    if not path:
        return ""
    resolved = os.path.realpath(path)
    root = git_cmd(resolved, ["rev-parse", "--show-toplevel"])
    return root


def collect_repo_roots(summaries: List[SessionSummary], extra_repos: List[str]) -> List[str]:
    roots = []
    for summary in summaries:
        root = git_repo_root(summary.cwd)
        if root:
            roots.append(root)
    for repo in extra_repos:
        root = git_repo_root(repo)
        if root:
            roots.append(root)
    return unique_keep_order(roots)


def collect_repo_report(repo: str, target_day: str) -> dict:
    branch = git_cmd(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    status = git_cmd(repo, ["status", "--short"])
    since = f"{target_day} 00:00:00 +0800"
    until = f"{target_day} 23:59:59 +0800"
    log = git_cmd(repo, ["log", "--since", since, "--until", until, "--pretty=format:%h %s"])
    return {
        "repo": repo,
        "branch": branch or "(unknown)",
        "status": [line for line in status.splitlines() if line.strip()],
        "commits": [line for line in log.splitlines() if line.strip()],
    }


def build_topics(summaries: List[SessionSummary]) -> List[str]:
    topics = []
    for summary in summaries:
        for msg in summary.user_messages:
            one_line = " ".join(msg.split())
            if not one_line:
                continue
            if "AGENTS.md instructions" in one_line:
                continue
            if "<INSTRUCTIONS>" in one_line:
                continue
            if "<environment_context>" in one_line:
                continue
            if "Always respond in 中文" in one_line:
                continue
            if "C code rules" in one_line:
                continue
            topics.append(shorten(one_line, 120))
            break
    return unique_keep_order(topics)


def build_assistant_points(summaries: List[SessionSummary]) -> List[str]:
    points = []
    for summary in summaries:
        for msg in reversed(summary.assistant_messages[-3:]):
            line = shorten(msg, 140)
            if line:
                points.append(line)
                break
    return unique_keep_order(points)


def resolve_usage_agent_from_cwd(cwd: str, cache: dict[str, str]) -> str:
    cwd = str(cwd or "").strip()
    if not cwd:
        return "unknown"
    if cwd.startswith("external:"):
        return cwd.split(":", 1)[1] or cwd
    if not cwd.startswith(("/", "~")):
        return cwd
    if cwd in cache:
        return cache[cwd]
    if DEFAULT_COAGENT_SH.is_file():
        try:
            result = subprocess.run(
                [str(DEFAULT_COAGENT_SH), "resolve-cwd", cwd, "--json"],
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


def collect_token_usage(target_day: str) -> dict:
    target_day = target_day.isoformat() if hasattr(target_day, "isoformat") else str(target_day)
    if not DEFAULT_USAGE_QUERY.is_file():
        return {
            "rows": [],
            "total": {},
            "session_count": 0,
            "owner_count": 0,
            "subagent_count": 0,
            "approval_count": 0,
            "errors": [f"missing {DEFAULT_USAGE_QUERY}"],
        }
    result = subprocess.run(
        [
            "python3",
            str(DEFAULT_USAGE_QUERY),
            "--date",
            target_day,
            "--source",
            "ledger",
            "--group-by",
            "agent",
            "--json",
        ]
        + (["--usage-root", DEFAULT_USAGE_ROOT] if DEFAULT_USAGE_ROOT else []),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {
            "rows": [],
            "total": {},
            "session_count": 0,
            "owner_count": 0,
            "subagent_count": 0,
            "approval_count": 0,
            "errors": [f"query_usage failed: exit {result.returncode}"],
        }
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "rows": [],
            "total": {},
            "session_count": 0,
            "owner_count": 0,
            "subagent_count": 0,
            "approval_count": 0,
            "errors": ["query_usage returned invalid JSON"],
        }
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    groups = summary.get("groups") if isinstance(summary.get("groups"), list) else []
    rows_by_agent = {}
    agent_cache = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        usage = group.get("usage") if isinstance(group.get("usage"), dict) else {}
        if int(usage.get("total_tokens") or 0) <= 0:
            continue
        agent = resolve_usage_agent_from_cwd(group.get("key") or "unknown", agent_cache)
        row = rows_by_agent.setdefault(
            agent,
            {
                "agent": agent,
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
        row["session_count"] += int(group.get("session_count") or 0)
        row["owner_count"] += int(group.get("owner_count") or group.get("session_count") or 0)
        row["subagent_count"] += int(group.get("subagent_count") or 0)
        approval_count = int(group.get("approval_count") or group.get("permission_approval_count") or 0)
        row["approval_count"] += approval_count
        row["permission_approval_count"] += approval_count
        for field in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"):
            row["usage"][field] += int(usage.get(field) or 0)
    rows = sorted(rows_by_agent.values(), key=lambda item: item["usage"]["total_tokens"], reverse=True)
    total = summary.get("usage") if isinstance(summary.get("usage"), dict) else {}
    session_count = int(summary.get("session_count") or 0)
    owner_count = int(summary.get("owner_count") or session_count)
    subagent_count = int(summary.get("subagent_count") or 0)
    approval_count = int(summary.get("approval_count") or summary.get("permission_approval_count") or 0)
    return {
        "rows": rows,
        "session_count": session_count,
        "owner_count": owner_count,
        "subagent_count": subagent_count,
        "approval_count": approval_count,
        "permission_approval_count": approval_count,
        "total": {
            "input_tokens": int(total.get("input_tokens") or 0),
            "cached_input_tokens": int(total.get("cached_input_tokens") or 0),
            "output_tokens": int(total.get("output_tokens") or 0),
            "reasoning_output_tokens": int(total.get("reasoning_output_tokens") or 0),
            "total_tokens": int(total.get("total_tokens") or 0),
        },
        "errors": [],
    }


def write_report(target_day: str, output_dir: Path, summaries: List[SessionSummary], repos: List[dict]) -> Path:
    topics = build_topics(summaries)
    assistant_points = build_assistant_points(summaries)
    token_usage = collect_token_usage(target_day)
    token_rows = token_usage.get("rows", [])
    codex_count = sum(1 for s in summaries if s.source == "Codex")
    cursor_count = sum(1 for s in summaries if s.source == "Cursor")
    user_turns = sum(len(s.user_messages) for s in summaries)
    assistant_turns = sum(len(s.assistant_messages) for s in summaries)

    lines = [
        f"# 每日报告 {target_day}",
        "",
        "## 概览",
        f"- Codex 会话数：{codex_count}",
        f"- Cursor 对话数：{cursor_count}",
        f"- 用户消息数：{user_turns}",
        f"- 助手消息数：{assistant_turns}",
        "",
        "## 今日对话主题",
    ]

    if topics:
        lines.extend(f"- {topic}" for topic in topics)
    else:
        lines.append("- 今日未发现有效对话主题")

    lines.extend(["", "## 处理结果摘录"])
    if assistant_points:
        lines.extend(f"- {item}" for item in assistant_points)
    else:
        lines.append("- 今日未提取到可用的处理结果摘要")

    lines.extend(["", "## 涉及仓库"])
    if repos:
        for repo in repos:
            lines.append(f"- 仓库：`{repo['repo']}`")
            lines.append(f"  分支：`{repo['branch']}`")
            if repo["commits"]:
                lines.append("  当日提交：")
                lines.extend(f"  - {item}" for item in repo["commits"])
            else:
                lines.append("  当日提交：无")
            if repo["status"]:
                lines.append("  当前工作区变更：")
                lines.extend(f"  - `{item}`" for item in repo["status"][:20])
            else:
                lines.append("  当前工作区变更：无")
    else:
        lines.append("- 今日未识别到相关 git 仓库")

    lines.extend(["", "## token使用"])
    if token_rows:
        token_table_rows = []
        for item in token_rows:
            usage = item["usage"]
            token_table_rows.append(
                [
                    f"`{item['agent']}`",
                    format_token_approx(usage["total_tokens"]),
                    format_token_approx(usage["input_tokens"]),
                    format_token_approx(usage["cached_input_tokens"]),
                    format_token_approx(usage["output_tokens"]),
                    format_token_approx(usage["reasoning_output_tokens"]),
                    format_session_subagent_approval(
                        item.get("owner_count", item["session_count"]),
                        item.get("subagent_count", 0),
                        item.get("approval_count", item.get("permission_approval_count", 0)),
                    ),
                ]
            )
        total = token_usage.get("total") or {}
        token_table_rows.append(
            [
                "**总计**",
                format_token_approx(total.get("total_tokens", 0)),
                format_token_approx(total.get("input_tokens", 0)),
                format_token_approx(total.get("cached_input_tokens", 0)),
                format_token_approx(total.get("output_tokens", 0)),
                format_token_approx(total.get("reasoning_output_tokens", 0)),
                format_session_subagent_approval(
                    token_usage.get("owner_count", token_usage.get("session_count", 0)),
                    token_usage.get("subagent_count", 0),
                    token_usage.get("approval_count", token_usage.get("permission_approval_count", 0)),
                ),
            ]
        )
        lines.extend(
            aligned_markdown_table(
                ["Agent", "总 token", "输入 token", "缓存输入 token", "输出 token", "推理输出 token", "session/subagent/approvals"],
                token_table_rows,
                ["left", "right", "right", "right", "right", "right", "right"],
            )
        )
    else:
        errors = token_usage.get("errors") or []
        if errors:
            lines.append(f"token 使用查询失败：{'; '.join(str(item) for item in errors[:3])}")
        else:
            lines.append("暂无 token 使用记录。")

    lines.extend(["", "## 原始日志来源"])
    for summary in summaries:
        lines.append(f"- {summary.source}: `{summary.path}`")

    lines.append("")
    report_dir = output_dir / report_month(target_day)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{target_day}.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def main() -> int:
    args = parse_args()
    target_day = args.date
    output_dir = Path(args.output_dir)
    config = load_config(Path(args.config))

    codex_sessions = [parse_codex_session(path) for path in iter_codex_sessions_for_day(DEFAULT_CODEX_DIR, target_day)]
    cursor_sessions = [
        parse_cursor_session(path)
        for path in iter_cursor_transcripts(DEFAULT_CURSOR_DIR, config.get("cursor_projects", []), target_day)
    ]
    summaries = codex_sessions + cursor_sessions
    repos = [collect_repo_report(repo, target_day) for repo in collect_repo_roots(summaries, config.get("extra_repos", []))]
    report_path = write_report(target_day, output_dir, summaries, repos)
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
