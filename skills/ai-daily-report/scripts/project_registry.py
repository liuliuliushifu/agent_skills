#!/usr/bin/env python3
"""Project registry helpers for AI daily reports."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


DEFAULT_PROJECTS = []


def slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9_]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "unassigned"


def default_projects_file(daily_dir: Path, script_dir: Path) -> Path:
    env_value = os.environ.get("AI_DAILY_PROJECTS_FILE")
    if env_value:
        return Path(env_value).expanduser()
    daily_file = daily_dir / "projects.json"
    if daily_file.exists():
        return daily_file
    script_file = script_dir / "projects.json"
    if script_file.exists():
        return script_file
    return daily_file


def normalize_string_list(values) -> list[str]:
    result = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def normalize_report_profile(value) -> dict:
    if not isinstance(value, dict):
        return {}
    profile = {}
    focus = normalize_string_list(value.get("focus"))
    signals = normalize_string_list(value.get("signals"))
    if focus:
        profile["focus"] = focus
    if signals:
        profile["signals"] = signals
    return profile


def normalize_project(item: dict) -> dict:
    key = slugify(str(item.get("key") or item.get("name") or "unassigned"))
    aliases = normalize_string_list(item.get("aliases"))
    project = {
        "key": key,
        "name": str(item.get("name") or key).strip(),
        "active": bool(item.get("active", True)),
        "aliases": aliases,
        "goal": str(item.get("goal") or "").strip(),
    }
    report_profile = normalize_report_profile(item.get("report_profile"))
    if report_profile:
        project["report_profile"] = report_profile
    return project


def load_projects(daily_dir: Path, script_dir: Path) -> list[dict]:
    path = default_projects_file(daily_dir, script_dir)
    data = None
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = None
    if isinstance(data, dict):
        projects = data.get("projects")
    else:
        projects = data
    if not isinstance(projects, list):
        projects = DEFAULT_PROJECTS
    result = []
    seen = set()
    for item in projects:
        if not isinstance(item, dict):
            continue
        project = normalize_project(item)
        if project["key"] in seen:
            continue
        seen.add(project["key"])
        result.append(project)
    return result or [normalize_project(item) for item in DEFAULT_PROJECTS]


def project_lookup(projects: list[dict]) -> dict[str, dict]:
    return {project["key"]: project for project in projects}


def resolve_project_key(value: str, projects: list[dict]) -> str:
    """Resolve a project identifier from key, display name, or alias."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    slug = slugify(raw)
    for project in projects:
        if slug == project["key"]:
            return project["key"]
        if lowered == str(project.get("name") or "").strip().lower():
            return project["key"]
        for alias in project.get("aliases") or []:
            if lowered == str(alias or "").strip().lower():
                return project["key"]
    return ""


def infer_project_key(task: dict, projects: list[dict]) -> str:
    explicit = resolve_project_key(str(task.get("project") or task.get("project_key") or ""), projects)
    if explicit:
        return explicit

    tracking = task.get("tracking") or {}
    text_parts = [
        str(task.get("text") or ""),
        str(task.get("area") or ""),
        str(task.get("note") or ""),
        " ".join(str(item) for item in tracking.get("keywords") or []),
        " ".join(str(item) for item in tracking.get("jira_keys") or []),
        " ".join(str(item) for item in tracking.get("repo_paths") or []),
    ]
    haystack = "\n".join(text_parts).lower()
    best_key = ""
    best_score = 0
    for project in projects:
        if not project.get("active", True):
            continue
        terms = [project.get("name", "")] + list(project.get("aliases") or [])
        score = 0
        for term in terms:
            term = str(term or "").strip().lower()
            if term and term in haystack:
                score += 1
        if score > best_score:
            best_score = score
            best_key = project["key"]
    return best_key or "unassigned"


def project_name(project_key: str, projects: list[dict]) -> str:
    project = project_lookup(projects).get(project_key)
    return project["name"] if project else project_key
