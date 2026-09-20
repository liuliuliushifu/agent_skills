#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - this skill is intended for Linux hosts.
    fcntl = None


SCRIPT_DIR = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
STATE_DIR = Path(os.environ.get("COAGENT_HOME", str(CODEX_HOME / "coagents"))).expanduser()
AGENTS_FILE = STATE_DIR / "agents.json"
CHAT_CANDIDATES_FILE = Path(
    os.environ.get("CHAT_CANDIDATES_FILE", str(STATE_DIR / "chat_candidates.json"))
).expanduser()
GOALS_DIR = STATE_DIR / "goals"
GLOBAL_LOCK = STATE_DIR / ".lock"
CHAT_MANAGER = CODEX_HOME / "skills" / "chat_manager" / "scripts" / "chat_manager.sh"
SEED_AGENTS_VALUE = os.environ.get("COAGENT_SEED_REGISTRY", "").strip()
SEED_AGENTS_FILE = Path(SEED_AGENTS_VALUE).expanduser() if SEED_AGENTS_VALUE else None
PROCESS_MONITOR_FILE = "process-monitor.json"
PROCESS_EXIT_GRACE_SECONDS = 60
PROCESS_TERM_WAIT_SECONDS = 5
ROUTE_STOP_TERMS = {
    "agent",
    "codex",
    "dev",
    "项目",
    "相关",
    "内容",
    "逻辑",
    "负责",
    "需要",
    "一下",
    "这个",
    "那个",
    "代码",
    "源码",
}
ROUTE_CJK_TERMS = {
    "修改",
    "代码修改",
    "源码实现",
    "编译",
    "部署",
    "环境部署",
    "提交",
    "代码提交",
    "测试",
    "回归测试",
    "测试用例",
    "验证",
    "配置",
    "配置同步",
    "接收端",
    "显示命令",
    "数据回传",
    "交付件",
    "整包",
    "白名单",
    "运行",
    "诊断",
    "同步",
}


def now_iso():
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def iso_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def event_time(event):
    for field in ("updated_at", "at", "created_at"):
        value = event.get(field)
        if value:
            return value
    return ""


def command_sha256(command):
    payload = json.dumps(command or [], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def bytes_sha256(value):
    return hashlib.sha256(value or b"").hexdigest()


def read_process_identity(pid):
    if not pid:
        return None
    proc_dir = Path("/proc") / str(int(pid))
    try:
        stat_text = (proc_dir / "stat").read_text(encoding="utf-8")
        close_paren = stat_text.rfind(")")
        if close_paren < 0:
            return None
        fields = stat_text[close_paren + 2 :].split()
        status_text = (proc_dir / "status").read_text(encoding="utf-8")
        uid_line = next(line for line in status_text.splitlines() if line.startswith("Uid:"))
        cmdline = (proc_dir / "cmdline").read_bytes()
        return {
            "pid": int(pid),
            "state": fields[0],
            "ppid": int(fields[1]),
            "pgid": int(fields[2]),
            "session_id": int(fields[3]),
            "start_ticks": int(fields[19]),
            "uid": int(uid_line.split()[1]),
            "cmdline_sha256": bytes_sha256(cmdline),
        }
    except (FileNotFoundError, ProcessLookupError, PermissionError, StopIteration, ValueError, IndexError):
        return None


def process_group_members(pgid):
    if not pgid:
        return []
    members = []
    try:
        proc_entries = list(Path("/proc").iterdir())
    except OSError:
        return members
    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        identity = read_process_identity(int(entry.name))
        if identity and identity.get("pgid") == int(pgid) and identity.get("state") != "Z":
            members.append(identity)
    return members


def capture_wake_identity(pid, command):
    identity = read_process_identity(pid) or {}
    return {
        "pgid": identity.get("pgid"),
        "session_id": identity.get("session_id"),
        "proc_start_ticks": identity.get("start_ticks"),
        "proc_uid": identity.get("uid"),
        "proc_cmdline_sha256": identity.get("cmdline_sha256", ""),
        "command_sha256": command_sha256(command),
    }


def today_key():
    return datetime.now().strftime("%Y%m%d")


def die(message, code=2):
    raise SystemExit("{0}: {1}".format(Path(sys.argv[0]).name, message))


def normalize_name(value):
    return " ".join((value or "").strip().casefold().split())


def routing_terms(value):
    text = normalize_name(value)
    terms = set()
    for token in re.findall(r"[a-z0-9][a-z0-9_+.-]*", text):
        token = token.strip("_+.-")
        if len(token) >= 2 and token not in ROUTE_STOP_TERMS:
            terms.add(token)
    for term in ROUTE_CJK_TERMS:
        if term in text and term not in ROUTE_STOP_TERMS:
            terms.add(term)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if 2 <= len(run) <= 8 and run not in ROUTE_STOP_TERMS:
            terms.add(run)
    return terms


def ensure_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    GOALS_DIR.mkdir(parents=True, exist_ok=True)
    if not AGENTS_FILE.exists():
        if SEED_AGENTS_FILE and SEED_AGENTS_FILE.is_file():
            seed = read_json(SEED_AGENTS_FILE, {"version": 1, "agents": {}})
            write_json_atomic(AGENTS_FILE, seed)
        else:
            write_json_atomic(AGENTS_FILE, {"version": 1, "agents": {}})


def read_json(path, default=None):
    if not path.exists():
        if default is not None:
            return default
        die("missing file {0}".format(path))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die("invalid json {0}: {1}".format(path, exc))


def write_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def write_text_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def read_text_excerpt(path, limit=1200):
    if not path:
        return ""
    target = Path(path)
    if not target.exists():
        return ""
    text = target.read_text(encoding="utf-8", errors="replace")
    if limit is None or limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated {0} chars]".format(len(text) - limit)


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_agents():
    ensure_state()
    data = read_json(AGENTS_FILE, {"version": 1, "agents": {}})
    data.setdefault("version", 1)
    data.setdefault("agents", {})
    return data


def save_agents(data):
    write_json_atomic(AGENTS_FILE, data)


def agent_key(name):
    key = normalize_name(name)
    if not key:
        die("agent name is empty")
    return key


def normalize_aliases(values):
    seen = set()
    aliases = []
    for value in values or []:
        alias = " ".join((value or "").strip().split())
        norm = normalize_name(alias)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        aliases.append(alias)
    return aliases


def normalize_text_items(values):
    seen = set()
    items = []
    for value in values or []:
        item = " ".join((value or "").strip().split())
        norm = normalize_name(item)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        items.append(item)
    return items


def normalize_responsibilities(value):
    if not isinstance(value, dict):
        value = {}
    result = {
        "owns": normalize_text_items(value.get("owns", [])),
        "not_for": normalize_text_items(value.get("not_for", [])),
    }
    handoff_when = normalize_text_items(value.get("handoff_when", []))
    if handoff_when:
        result["handoff_when"] = handoff_when
    return result


def build_responsibilities(args, old):
    current = normalize_responsibilities(old.get("responsibilities", {}))
    if args.owns is None and args.handoff_when is None and args.not_for is None:
        return current
    return normalize_responsibilities({
        "owns": normalize_text_items(args.owns if args.owns is not None else current.get("owns", [])),
        "not_for": normalize_text_items(args.not_for if args.not_for is not None else current.get("not_for", [])),
        "handoff_when": normalize_text_items(args.handoff_when if args.handoff_when is not None else current.get("handoff_when", [])),
    })


def responsibilities_lines(value):
    resp = normalize_responsibilities(value)
    labels = (
        ("owns", "owns"),
        ("not_for", "not_for"),
        ("handoff_when", "handoff_when"),
    )
    lines = []
    for field, label in labels:
        items = resp.get(field, [])
        if items:
            lines.append("- {0}: {1}".format(label, "; ".join(items)))
    return lines or ["-"]


def responsibilities_inline(value):
    lines = [line[2:] for line in responsibilities_lines(value) if line != "-"]
    return " | ".join(lines)


def agent_match_names(key, agent):
    names = {
        normalize_name(key),
        normalize_name(agent.get("name", "")),
        normalize_name(agent.get("display_name", "")),
    }
    names.update(normalize_name(alias) for alias in agent.get("aliases", []) or [])
    return {name for name in names if name}


def resolve_agent(name):
    data = load_agents()
    want = normalize_name(name)
    matches = []
    for key, agent in data.get("agents", {}).items():
        if want in agent_match_names(key, agent):
            item = dict(agent)
            item.setdefault("name", key)
            item["_key"] = key
            matches.append(item)
    if len(matches) == 1:
        return matches[0]
    if matches:
        die("ambiguous agent name {0}: {1}".format(name, ", ".join(a["name"] for a in matches)))
    known = ", ".join(a.get("name", k) for k, a in data.get("agents", {}).items())
    die("unknown agent {0}. Known agents: {1}".format(name, known or "(none)"))


def normalize_cwd_path(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if not text.startswith(("/", "~")):
        return text.rstrip("/")
    try:
        return str(Path(text).expanduser().resolve(strict=False)).rstrip("/")
    except Exception:
        return text.rstrip("/")


def resolve_agent_by_cwd(cwd):
    cwd_path = normalize_cwd_path(cwd)
    data = load_agents()
    best = None
    best_len = -1
    best_match = "none"
    for key, agent in data.get("agents", {}).items():
        root = normalize_cwd_path(agent.get("cwd", ""))
        if not root:
            continue
        if cwd_path == root:
            match_type = "exact"
        elif cwd_path.startswith(root + "/"):
            match_type = "prefix"
        else:
            continue
        if len(root) > best_len:
            item = dict(agent)
            item.setdefault("name", key)
            item["_key"] = key
            best = item
            best_len = len(root)
            best_match = match_type
    return cwd_path, best, best_match


def agent_snapshot(agent):
    return {
        "name": agent.get("name", agent.get("_key", "")),
        "cwd": agent.get("cwd", ""),
        "role": agent.get("role", ""),
        "aliases": agent.get("aliases", []) or [],
        "responsibilities": normalize_responsibilities(agent.get("responsibilities", {})),
    }


def cmd_register(args):
    ensure_state()
    cwd = str(Path(args.cwd).expanduser().resolve())
    if not Path(cwd).exists():
        die("cwd does not exist: {0}".format(cwd))
    key = agent_key(args.agent)
    with file_lock(GLOBAL_LOCK):
        data = load_agents()
        old = data["agents"].get(key, {})
        if key in data["agents"] and not args.update:
            die("agent already exists: {0}. Use --update to replace it".format(args.agent))
        aliases = normalize_aliases(args.alias if args.alias is not None else old.get("aliases", []))
        next_agent = {
            "name": args.agent.strip(),
            "aliases": aliases,
        }
        next_names = agent_match_names(key, next_agent)
        for other_key, other_agent in data["agents"].items():
            if other_key == key:
                continue
            overlap = sorted(next_names & agent_match_names(other_key, other_agent))
            if overlap:
                die(
                    "agent name/alias conflict with {0}: {1}".format(
                        other_agent.get("name", other_key),
                        ", ".join(overlap),
                    )
                )
        data["agents"][key] = {
            "name": args.agent.strip(),
            "cwd": cwd,
            "role": args.role or "",
            "aliases": aliases,
            "responsibilities": build_responsibilities(args, old),
            "updated_at": now_iso(),
        }
        save_agents(data)
    print("registered\t{0}\t{1}".format(args.agent.strip(), cwd))


def cmd_rename_agent(args):
    ensure_state()
    old_key = agent_key(args.old_agent)
    new_key = agent_key(args.new_agent)
    with file_lock(GLOBAL_LOCK):
        data = load_agents()
        if old_key not in data["agents"]:
            die("unknown agent: {0}".format(args.old_agent))
        if new_key != old_key and new_key in data["agents"]:
            die("agent already exists: {0}".format(args.new_agent))

        old_agent = data["agents"][old_key]
        aliases = normalize_aliases(args.alias if args.alias is not None else old_agent.get("aliases", []))
        next_agent = {
            "name": args.new_agent.strip(),
            "aliases": aliases,
        }
        next_names = agent_match_names(new_key, next_agent)
        for other_key, other_agent in data["agents"].items():
            if other_key == old_key:
                continue
            overlap = sorted(next_names & agent_match_names(other_key, other_agent))
            if overlap:
                die(
                    "agent name/alias conflict with {0}: {1}".format(
                        other_agent.get("name", other_key),
                        ", ".join(overlap),
                    )
                )

        renamed = dict(old_agent)
        renamed["name"] = args.new_agent.strip()
        renamed["aliases"] = aliases
        renamed["updated_at"] = now_iso()
        if new_key != old_key:
            data["agents"].pop(old_key)
        data["agents"][new_key] = renamed
        save_agents(data)
    print("renamed\t{0}\t{1}".format(args.old_agent.strip(), args.new_agent.strip()))


def cmd_list_agents(args):
    data = load_agents()
    rows = []
    for key, agent in sorted(data.get("agents", {}).items()):
        rows.append(
            {
                "key": key,
                "name": agent.get("name", key),
                "cwd": agent.get("cwd", ""),
                "aliases": agent.get("aliases", []) or [],
                "role": agent.get("role", ""),
                "responsibilities": normalize_responsibilities(agent.get("responsibilities", {})),
            }
        )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("agent\taliases\tcwd\trole\towns")
    for row in rows:
        display = dict(row)
        display["aliases"] = ",".join(row.get("aliases", []))
        display["owns"] = ";".join(row.get("responsibilities", {}).get("owns", []))
        print(
            "{name}\t{aliases}\t{cwd}\t{role}\t{owns}".format(**display)
        )


def normalize_candidate_data(data):
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    cwd_map = data.get("cwd_map")
    if not isinstance(cwd_map, dict):
        cwd_map = {}
    normalized = {}
    for cwd, entries in cwd_map.items():
        cwd_key = normalize_cwd_path(cwd)
        if not cwd_key:
            continue
        if isinstance(entries, dict):
            entries = entries.get("sessions", [])
        if not isinstance(entries, list):
            continue
        cleaned = []
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            thread_id = str(entry.get("thread_id") or "").strip()
            chat_name = str(entry.get("chat_name") or "").strip()
            if not thread_id or not chat_name or thread_id in seen:
                continue
            seen.add(thread_id)
            item = dict(entry)
            item["thread_id"] = thread_id
            item["chat_name"] = chat_name
            item["cwd"] = cwd_key
            cleaned.append(item)
        if cleaned:
            normalized[cwd_key] = cleaned
    data["cwd_map"] = normalized
    return data


def load_chat_candidates():
    return normalize_candidate_data(
        read_json(CHAT_CANDIDATES_FILE, {"version": 1, "cwd_map": {}})
    )


def resolve_thread_id(thread_id):
    if not thread_id or not CHAT_MANAGER.exists():
        return None
    cmd = [str(CHAT_MANAGER), "resolve", "--id", thread_id, "--json"]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": (proc.stderr or proc.stdout).strip(),
            "command": cmd,
        }
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "invalid chat_manager json: {0}".format(proc.stdout.strip()),
            "command": cmd,
        }
    data["ok"] = True
    data["command"] = cmd
    return data


def chat_candidates_for_cwd(cwd, limit=None):
    target_cwd = normalize_cwd_path(cwd)
    data = load_chat_candidates()
    entries = data.get("cwd_map", {}).get(target_cwd, [])
    candidates = []
    stale = []
    for entry in entries:
        resolved = resolve_thread_id(entry.get("thread_id", ""))
        if not resolved or not resolved.get("ok"):
            stale.append(dict(entry, reason=(resolved or {}).get("error", "missing_thread")))
            continue
        row_cwd = normalize_cwd_path(resolved.get("cwd", ""))
        if row_cwd != target_cwd:
            stale.append(dict(entry, reason="cwd_mismatch", actual_cwd=row_cwd))
            continue
        if resolved.get("archived"):
            stale.append(dict(entry, reason="archived"))
            continue
        item = dict(entry)
        item.update(
            {
                "title": resolved.get("title", ""),
                "updated_at": int(resolved.get("updated_at") or 0),
                "created_at": int(resolved.get("created_at") or 0),
                "rollout_path": resolved.get("rollout_path", ""),
            }
        )
        candidates.append(item)
    candidates.sort(key=lambda item: (-int(item.get("updated_at") or 0), item.get("thread_id", "")))
    if limit is not None:
        candidates = candidates[:limit]
    return {
        "cwd": target_cwd,
        "candidate_file": str(CHAT_CANDIDATES_FILE),
        "matched": len(candidates),
        "stale": stale,
        "candidates": candidates,
    }


def cmd_chat_candidates(args):
    if args.agent:
        agent = resolve_agent(args.agent)
        cwd = agent.get("cwd", "")
        agent_name = agent.get("name", args.agent)
    elif args.cwd:
        cwd = args.cwd
        agent_name = ""
    else:
        die("chat-candidates requires --agent or --cwd")
    result = chat_candidates_for_cwd(cwd, limit=args.limit)
    result["agent"] = agent_name
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("thread_id\tupdated_at\ttitle\tchat_name")
    for item in result["candidates"]:
        print(
            "{thread_id}\t{updated_at}\t{title}\t{chat_name}".format(
                thread_id=item.get("thread_id", ""),
                updated_at=item.get("updated_at", ""),
                title=(item.get("title") or "").replace("\n", " "),
                chat_name=(item.get("chat_name") or "").replace("\n", " "),
            )
        )


def format_wake_candidates(result):
    candidates = result.get("candidates", []) if isinstance(result, dict) else []
    if not candidates:
        return "  (none)"
    lines = []
    for item in candidates:
        label = item.get("title") or item.get("chat_name") or "(untitled)"
        lines.append(
            "  {0}\t{1}\t{2}".format(
                item.get("thread_id", ""),
                item.get("updated_at", ""),
                str(label).replace("\n", " "),
            )
        )
    return "\n".join(lines)


def cmd_resolve_agent(args):
    agent = resolve_agent(args.agent)
    data = agent_snapshot(agent)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_resolve_cwd(args):
    cwd, agent, match_type = resolve_agent_by_cwd(args.cwd)
    if agent:
        data = agent_snapshot(agent)
        data.update(
            {
                "input_cwd": args.cwd,
                "resolved_cwd": cwd,
                "match": match_type,
                "matched": True,
            }
        )
    else:
        data = {
            "input_cwd": args.cwd,
            "resolved_cwd": cwd,
            "match": "none",
            "matched": False,
            "name": cwd or "unknown",
            "cwd": cwd,
            "role": "",
            "aliases": [],
            "responsibilities": {"owns": [], "not_for": []},
        }
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(data.get("name") or cwd or "unknown")


def resolve_current_agent(from_agent=None, require=True):
    if from_agent:
        return resolve_agent(from_agent), "explicit"
    env_agent = os.environ.get("COAGENT_NAME")
    if env_agent:
        return resolve_agent(env_agent), "env"

    cwd = str(Path.cwd().resolve())
    data = load_agents()
    matches = []
    for key, agent in data.get("agents", {}).items():
        if str(Path(agent.get("cwd", "")).expanduser()) == cwd:
            item = dict(agent)
            item.setdefault("name", key)
            item["_key"] = key
            matches.append(item)
    if len(matches) == 1:
        return matches[0], "cwd"
    if matches:
        die("cwd matches multiple agents: {0}".format(", ".join(a["name"] for a in matches)))
    if require:
        die("cannot resolve current agent; pass --from")
    return None, "unresolved"


def cmd_whoami(args):
    agent, method = resolve_current_agent(args.from_agent, require=True)
    data = agent_snapshot(agent)
    data["method"] = method
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("{name}\t{cwd}".format(**data))


def route_item_matches(task_norm, task_terms, item):
    item_norm = normalize_name(item)
    item_terms = routing_terms(item)
    matched_terms = sorted(item_terms & task_terms)
    phrase_match = bool(item_norm and item_norm in task_norm)
    if not phrase_match and not matched_terms:
        return None
    return {
        "text": item,
        "matched_terms": matched_terms,
        "phrase_match": phrase_match,
    }


def score_route_agent(key, agent, task):
    task_norm = normalize_name(task)
    task_terms = routing_terms(task)
    resp = normalize_responsibilities(agent.get("responsibilities", {}))
    score = 0
    explicit_matches = []
    for name in sorted(agent_match_names(key, agent)):
        if name and name in task_norm:
            explicit_matches.append(name)
            score += 12 if " " in name or len(name) > 3 else 9

    owns_matches = []
    for item in resp.get("owns", []):
        match = route_item_matches(task_norm, task_terms, item)
        if match:
            owns_matches.append(match)
            score += 4 + len(match["matched_terms"])
            if match["phrase_match"]:
                score += 4

    handoff_matches = []
    for item in resp.get("handoff_when", []):
        match = route_item_matches(task_norm, task_terms, item)
        if match:
            handoff_matches.append(match)
            score += 3 + len(match["matched_terms"])
            if match["phrase_match"]:
                score += 3

    role_terms = sorted(routing_terms(agent.get("role", "")) & task_terms)
    score += min(len(role_terms), 3)

    not_for_matches = []
    for item in resp.get("not_for", []):
        match = route_item_matches(task_norm, task_terms, item)
        if match:
            not_for_matches.append(match)
            score -= 5 + len(match["matched_terms"])
            if match["phrase_match"]:
                score -= 4

    return {
        "agent": agent.get("name", key),
        "key": key,
        "score": score,
        "explicit_name_matches": explicit_matches,
        "owns_matches": owns_matches,
        "handoff_when_matches": handoff_matches,
        "not_for_matches": not_for_matches,
        "role_matched_terms": role_terms,
        "cwd": agent.get("cwd", ""),
    }


def route_match_text(matches):
    return [item["text"] for item in matches]


def route_reason(best, current, action):
    parts = []
    if best.get("explicit_name_matches"):
        parts.append("explicit agent alias/name matched: {0}".format(", ".join(best["explicit_name_matches"])))
    if best.get("owns_matches"):
        parts.append("owns matched: {0}".format("; ".join(route_match_text(best["owns_matches"]))))
    if best.get("handoff_when_matches"):
        parts.append("handoff_when matched: {0}".format("; ".join(route_match_text(best["handoff_when_matches"]))))
    if current and current.get("not_for_matches") and normalize_name(current.get("agent")) != normalize_name(best.get("agent")):
        parts.append("current not_for matched: {0}".format("; ".join(route_match_text(current["not_for_matches"]))))
    if action == "handle_here":
        parts.append("best owner is the current agent")
    if action == "ask":
        parts.append("ownership is ambiguous or weak")
    return "; ".join(parts) if parts else "best score selected from registered responsibilities"


def cmd_route(args):
    from_agent, method = resolve_current_agent(args.from_agent, require=False)
    data = load_agents()
    candidates = []
    for key, agent in data.get("agents", {}).items():
        item = dict(agent)
        item.setdefault("name", key)
        item["_key"] = key
        candidates.append(score_route_agent(key, item, args.task))
    candidates.sort(key=lambda item: (-item["score"], item["agent"].casefold()))

    current_name = from_agent.get("name", "") if from_agent else ""
    current_norm = normalize_name(current_name)
    current = None
    for item in candidates:
        if normalize_name(item["agent"]) == current_norm:
            current = item
            break

    best = candidates[0] if candidates else None
    second_score = candidates[1]["score"] if len(candidates) > 1 else None
    action = "ask"
    confidence = "low"
    recommended = ""
    reason = "no registered agents"
    if best:
        recommended = best["agent"]
        best_score = best["score"]
        current_score = current["score"] if current else 0
        second_gap = best_score - second_score if second_score is not None else best_score
        current_gap = best_score - current_score
        best_is_current = current is not None and normalize_name(best["agent"]) == current_norm

        if best_score <= 0:
            action = "ask"
            confidence = "low"
        elif best_is_current:
            action = "handle_here"
            confidence = "high" if best_score >= 12 else "medium"
        elif best_score >= 8 and (not current or current_gap >= 4) and (second_gap >= 2 or best.get("explicit_name_matches")):
            action = "handoff"
            confidence = "high" if best_score >= 12 and current_gap >= 4 else "medium"
        elif best_score >= 8 and not current and (second_gap >= 2 or best.get("explicit_name_matches")):
            action = "handoff"
            confidence = "medium"
        else:
            action = "ask"
            confidence = "low"
        reason = route_reason(best, current, action)

    result = {
        "task": args.task,
        "from_agent": current_name,
        "from_resolution": method,
        "action": action,
        "recommended_agent": recommended if action in ("handoff", "handle_here") else "",
        "confidence": confidence,
        "reason": reason,
        "candidates": candidates,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(
        "route\tfrom:{0}\taction:{1}\trecommended:{2}\tconfidence:{3}\treason:{4}".format(
            current_name or "(unresolved)",
            action,
            result["recommended_agent"] or "(none)",
            confidence,
            reason,
        )
    )
    print("agent\tscore\towns_matches\tnot_for_matches")
    for item in candidates:
        print(
            "{agent}\t{score}\t{owns}\t{not_for}".format(
                agent=item["agent"],
                score=item["score"],
                owns=";".join(route_match_text(item["owns_matches"])),
                not_for=";".join(route_match_text(item["not_for_matches"])),
            )
        )


def iter_goal_dirs():
    if not GOALS_DIR.exists():
        return []
    return sorted([p for p in GOALS_DIR.iterdir() if p.is_dir() and p.name.startswith("GOAL-")])


def iter_run_dirs(goal_dir=None):
    goals = [goal_dir] if goal_dir is not None else iter_goal_dirs()
    result = []
    for gd in goals:
        runs_dir = gd / "runs"
        if runs_dir.exists():
            result.extend(sorted([p for p in runs_dir.iterdir() if p.is_dir() and p.name.startswith("RUN-")]))
    return result


def next_id(prefix, existing_names):
    date = today_key()
    base = "{0}-{1}-".format(prefix, date)
    nums = []
    for name in existing_names:
        if name.startswith(base):
            try:
                nums.append(int(name.rsplit("-", 1)[1]))
            except ValueError:
                pass
    return "{0}{1:03d}".format(base, (max(nums) if nums else 0) + 1)


def find_goal(goal_id):
    ensure_state()
    path = GOALS_DIR / goal_id
    if path.exists():
        return path
    die("unknown goal: {0}".format(goal_id))


def find_run(run_id, goal_id=None):
    goal_dir = find_goal(goal_id) if goal_id else None
    matches = [p for p in iter_run_dirs(goal_dir) if p.name == run_id]
    if len(matches) == 1:
        return matches[0]
    if matches:
        die("ambiguous run id {0}; pass --goal".format(run_id))
    die("unknown run: {0}".format(run_id))


def run_goal_dir(run_dir):
    return run_dir.parent.parent


def run_lock(run_dir):
    return run_dir / ".lock"


def goal_lock(goal_dir):
    return goal_dir / ".lock"


def cmd_start_goal(args):
    owner = resolve_agent(args.owner)
    ensure_state()
    with file_lock(GLOBAL_LOCK):
        goal_id = next_id("GOAL", [p.name for p in iter_goal_dirs()])
        goal_dir = GOALS_DIR / goal_id
        goal_dir.mkdir(parents=True, exist_ok=False)
        (goal_dir / "runs").mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "goal_id": goal_id,
            "title": args.title,
            "owner_agent": owner.get("name", args.owner),
            "owner_agent_snapshot": agent_snapshot(owner),
            "created_at": now_iso(),
            "status": "active",
            "next_hint": "handoff can be run without --new-run; the first handoff auto-creates a run.",
        }
        write_json_atomic(goal_dir / "goal.json", data)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("goal_created\t{0}\t{1}".format(goal_id, args.title))
        print("next\thandoff --goal {0} --from \"{1}\" --to <agent> ...".format(goal_id, owner.get("name", args.owner)))


def create_run(goal_id, from_agent, reason, previous_run=None):
    goal_dir = find_goal(goal_id)
    with file_lock(GLOBAL_LOCK):
        run_id = next_id("RUN", [p.name for p in iter_run_dirs()])
        run_dir = goal_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        data = {
            "version": 1,
            "goal_id": goal_id,
            "run_id": run_id,
            "previous_run_id": previous_run or "",
            "created_by_agent": from_agent.get("name", from_agent.get("_key", "")),
            "created_by_agent_snapshot": agent_snapshot(from_agent),
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "status": "active",
            "reason": reason,
        }
        write_json_atomic(run_dir / "run.json", data)
        (run_dir / "segments.jsonl").touch()
    return run_dir, data


def cmd_start_run(args):
    from_agent = resolve_agent(args.from_agent)
    run_dir, data = create_run(args.goal, from_agent, args.reason, args.previous_run)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("run_created\t{0}\t{1}".format(data["run_id"], run_dir))


def read_events(run_dir):
    path = run_dir / "segments.jsonl"
    if not path.exists():
        return []
    events = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            die("invalid jsonl {0}:{1}: {2}".format(path, lineno, exc))
    return events


def append_event(run_dir, event):
    event = dict(event)
    event.setdefault("at", now_iso())
    path = run_dir / "segments.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def build_segments(events):
    segments = {}
    for event in events:
        sid = event.get("segment_id")
        if sid is None:
            continue
        seg = segments.setdefault(
            int(sid),
            {
                "segment_id": int(sid),
                "attempts": {},
                "status": "unknown",
                "events": [],
            },
        )
        seg["events"].append(event)
        etype = event.get("event")
        if etype == "segment_started":
            seg.update(event)
            seg["status"] = event.get("status", "active")
        elif etype == "attempt_started":
            attempt = int(event.get("attempt", 1))
            att = seg["attempts"].setdefault(attempt, {"attempt": attempt})
            att.update(event)
            att["status"] = event.get("status", "active")
            seg["latest_attempt"] = attempt
            seg["status"] = "active"
            seg.pop("summary", None)
            seg.pop("result_file", None)
        elif etype == "wake_finished":
            attempt = int(event.get("attempt", 1))
            att = seg["attempts"].setdefault(attempt, {"attempt": attempt})
            att["wake_status"] = event.get("status", "")
            att["wake_exit_code"] = event.get("exit_code")
            att["wake_pid"] = event.get("pid")
            att["wake_command"] = event.get("command", [])
            att["wake_stdout_file"] = event.get("stdout_file", "")
            att["wake_stderr_file"] = event.get("stderr_file", "")
            att["wake_pgid"] = event.get("pgid")
            att["wake_session_id"] = event.get("session_id")
            att["wake_proc_start_ticks"] = event.get("proc_start_ticks")
            att["wake_proc_uid"] = event.get("proc_uid")
            att["wake_proc_cmdline_sha256"] = event.get("proc_cmdline_sha256", "")
            att["wake_command_sha256"] = event.get("command_sha256", "")
            if event.get("status") == "wake_failed":
                att["status"] = "wake_failed"
        elif etype == "attempt_finished":
            attempt = int(event.get("attempt", 1))
            att = seg["attempts"].setdefault(attempt, {"attempt": attempt})
            att.update(event)
            att["status"] = event.get("status", att.get("status", "finished"))
            seg["latest_attempt"] = attempt
            seg["summary"] = event.get("summary", seg.get("summary", ""))
            seg["result_file"] = event.get("result_file", seg.get("result_file", ""))
        elif etype == "segment_finished":
            seg["status"] = event.get("status", seg.get("status", "unknown"))
            seg["summary"] = event.get("summary", seg.get("summary", ""))
            seg["result_file"] = event.get("result_file", seg.get("result_file", ""))
        elif etype == "segment_superseded":
            seg["status"] = "superseded"
            seg["superseded_by"] = event.get("by_segment")
        elif etype == "segment_cancelled":
            seg["status"] = "cancelled"
            seg["summary"] = event.get("reason", "")
    return segments


def latest_attempt(seg):
    attempts = seg.get("attempts", {})
    if not attempts:
        return None
    return attempts[max(attempts)]


def allocate_segment_id(segments):
    return (max(segments) if segments else 0) + 1


def allocate_attempt_id(seg):
    attempts = seg.get("attempts", {})
    return (max(attempts) if attempts else 0) + 1


def seg_label(segment_id):
    return "SEG{0:03d}".format(int(segment_id))


def att_label(attempt):
    return "ATT{0:03d}".format(int(attempt))


def wake_receipt_path(run_dir, segment_id, attempt):
    return run_dir / "wake-{0}-{1}.json".format(seg_label(segment_id), att_label(attempt))


def rel(path, base):
    return str(path.relative_to(base))


def read_context(args):
    if args.context:
        return Path(args.context).expanduser().read_text(encoding="utf-8")
    if args.context_text:
        return args.context_text
    die("handoff requires --context or --context-text")


def render_handoff(goal_id, run_id, segment_id, direction, from_agent, to_agent, parent_segment_id, context, requested_outcome, artifacts, constraints):
    lines = [
        "# Co-Agent Handoff",
        "",
        "goal_id: {0}".format(goal_id),
        "run_id: {0}".format(run_id),
        "segment_id: {0}".format(segment_id),
        "direction: {0}".format(direction),
        "from_agent: {0}".format(from_agent.get("name", "")),
        "to_agent: {0}".format(to_agent.get("name", "")),
        "parent_segment_id: {0}".format(parent_segment_id or ""),
        "created_at: {0}".format(now_iso()),
        "",
        "## From Agent Role",
        from_agent.get("role", ""),
        "",
        "## From Agent Responsibilities",
        *responsibilities_lines(from_agent.get("responsibilities", {})),
        "",
        "## To Agent Role",
        to_agent.get("role", ""),
        "",
        "## To Agent Responsibilities",
        *responsibilities_lines(to_agent.get("responsibilities", {})),
        "",
        "## Context",
        context.strip(),
        "",
        "## Requested Outcome",
        requested_outcome.strip(),
        "",
        "## Artifacts",
    ]
    if artifacts:
        lines.extend(["- {0}".format(item) for item in artifacts])
    else:
        lines.append("-")
    lines.extend(["", "## Constraints"])
    if constraints:
        lines.extend(["- {0}".format(item) for item in constraints])
    else:
        lines.append("-")
    lines.extend(
        [
            "",
            "## Return Rules",
            "- If the task is complete, use `coagent.sh finish`.",
            "- If upstream work is needed, use `coagent.sh return`.",
            "- If another agent is needed before return, use `coagent.sh handoff --run {0}` with yourself as `from_agent`.".format(run_id),
            "",
        ]
    )
    return "\n".join(lines)


def render_prompt(goal_id, run_id, segment_id, attempt, direction, from_agent, to_agent, parent_segment_id, handoff_file, result_file, status_file, segments_file):
    return """You are the target Codex agent for a co-agent handoff.

Stable identity:
- agent: {to_agent}
- role: {to_role}
- cwd: {to_cwd}
- responsibilities: {to_responsibilities}

Current segment:
- goal_id: {goal_id}
- run_id: {run_id}
- segment_id: {segment_id}
- attempt: {attempt}
- direction: {direction}
- from_agent: {from_agent}
- to_agent: {to_agent}
- parent_segment_id: {parent_segment_id}

Required files:
- handoff: {handoff_file}
- result: {result_file}
- status view: {status_file}
- ledger: {segments_file}

Instructions:
1. Read the handoff file first.
2. Treat your stable identity as {to_agent}. Do not inherit the identity of {from_agent}.
3. Continue the work in the current directory only unless the handoff says otherwise.
4. Do not edit workflow ledger or status files manually. Use `{coagent_cmd}` commands for status, return, retry, or further handoff.
5. If you delegate to a third agent, you become `from_agent` for the new segment.
6. When done, write the result with `coagent.sh finish` or return with `coagent.sh return`.
""".format(
        goal_id=goal_id,
        run_id=run_id,
        segment_id=segment_id,
        attempt=attempt,
        direction=direction,
        from_agent=from_agent.get("name", ""),
        to_agent=to_agent.get("name", ""),
        to_role=to_agent.get("role", ""),
        to_cwd=to_agent.get("cwd", ""),
        to_responsibilities=responsibilities_inline(to_agent.get("responsibilities", {})) or "(none)",
        parent_segment_id=parent_segment_id or "",
        handoff_file=handoff_file,
        result_file=result_file,
        status_file=status_file,
        segments_file=segments_file,
        coagent_cmd=str(SCRIPT_DIR / "coagent.sh"),
    )


def command_message(prompt_file):
    return "Read {0} and execute the co-agent handoff.".format(prompt_file)


def resolve_wake_target(agent, args=None):
    session_id = str(getattr(args, "session", "") or "").strip() if args is not None else ""
    resume_last = bool(getattr(args, "resume_last", False)) if args is not None else False
    if session_id and resume_last:
        die("choose exactly one wake target: --session <thread_id> or --last")
    if resume_last:
        return {
            "wake_method": "last",
            "session_id": "",
            "requested_chat_name": "",
            "resolved_thread_id": "",
            "chat_resolution": None,
        }

    candidates = chat_candidates_for_cwd(agent.get("cwd", ""), limit=20)
    if session_id:
        selected = next(
            (
                item
                for item in candidates.get("candidates", [])
                if item.get("thread_id") == session_id
            ),
            None,
        )
        if not selected:
            die(
                "session is not a valid candidate for {0}: {1}\n"
                "validated candidates (thread_id, updated_at, title):\n{2}\n"
                "choose one listed id with --session, or explicitly pass --last".format(
                    agent.get("name", "target agent"),
                    session_id,
                    format_wake_candidates(candidates),
                )
            )
        return {
            "wake_method": "session_id",
            "session_id": session_id,
            "requested_chat_name": selected.get("chat_name", ""),
            "resolved_thread_id": session_id,
            "chat_resolution": selected,
        }

    die(
        "wake target selection required for {0}; pass --session <thread_id> or --last\n"
        "validated candidates (thread_id, updated_at, title):\n{1}".format(
            agent.get("name", "target agent"),
            format_wake_candidates(candidates),
        )
    )


def build_wake_command(agent, prompt_file, wake_target):
    message = command_message(str(prompt_file))
    cmd = ["codex", "exec", "--cd", agent["cwd"], "resume"]
    if wake_target.get("wake_method") == "session_id":
        cmd.append(wake_target.get("session_id", ""))
    else:
        cmd.append("--last")
    cmd.append(message)
    return cmd


def persist_wake_receipt(receipt_file, result):
    if receipt_file:
        payload = {"version": 1, **result, "updated_at": now_iso()}
        write_json_atomic(Path(receipt_file), payload)
    return result


def run_wake(agent, prompt_file, wake_target, no_wake=False, detach=True, receipt_file=None):
    stdout_file = prompt_file.with_suffix(".wake.stdout")
    stderr_file = prompt_file.with_suffix(".wake.stderr")
    if no_wake:
        write_text_atomic(stdout_file, "wake skipped by --no-wake\n")
        write_text_atomic(stderr_file, "")
        return persist_wake_receipt(
            receipt_file,
            {
                "status": "wake_skipped",
                "exit_code": 0,
                "pid": None,
                "stdout_file": str(stdout_file),
                "stderr_file": str(stderr_file),
                "command": [],
                **capture_wake_identity(None, []),
            },
        )

    cmd = build_wake_command(agent, prompt_file, wake_target)
    if detach:
        stdout_file.parent.mkdir(parents=True, exist_ok=True)
        stdout_handle = stdout_file.open("ab")
        stderr_handle = stderr_file.open("ab")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
        finally:
            stdout_handle.close()
            stderr_handle.close()
        result = {
            "status": "wake_detached",
            "exit_code": None,
            "pid": proc.pid,
            "stdout_file": str(stdout_file),
            "stderr_file": str(stderr_file),
            "command": cmd,
        }
        result.update(capture_wake_identity(proc.pid, cmd))
        return persist_wake_receipt(receipt_file, result)

    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    write_text_atomic(stdout_file, proc.stdout or "")
    write_text_atomic(stderr_file, proc.stderr or "")
    result = {
        "status": "active" if proc.returncode == 0 else "wake_failed",
        "exit_code": proc.returncode,
        "pid": None,
        "stdout_file": str(stdout_file),
        "stderr_file": str(stderr_file),
        "command": cmd,
    }
    result.update(capture_wake_identity(None, cmd))
    return persist_wake_receipt(receipt_file, result)


def infer_or_create_active_run(goal_id, from_agent, reason, previous_run=None):
    goal_dir = find_goal(goal_id)
    active = []
    for rd in iter_run_dirs(goal_dir):
        data = read_json(rd / "run.json")
        if data.get("status") == "active":
            active.append(rd)
    if len(active) == 1:
        return active[0], False
    if active:
        die("multiple active runs under {0}; pass --run or --new-run".format(goal_id))
    run_dir, _ = create_run(goal_id, from_agent, reason, previous_run)
    return run_dir, True


def create_segment(run_dir, goal_id, run_id, direction, from_agent, to_agent, parent_segment_id, context, requested_outcome, artifacts, constraints, reason, wake_target, no_wake=False, wake_args=None):
    with file_lock(run_lock(run_dir)):
        events = read_events(run_dir)
        segments = build_segments(events)
        sid = allocate_segment_id(segments)
        attempt = 1
        seg = seg_label(sid)
        att = att_label(attempt)
        handoff_file = run_dir / "handoff-{0}.md".format(seg)
        prompt_file = run_dir / "prompt-{0}-{1}.md".format(seg, att)
        result_file = run_dir / "result-{0}-{1}.md".format(seg, att)
        status_file = run_dir / "status-{0}.json".format(seg)
        receipt_file = wake_receipt_path(run_dir, sid, attempt)
        handoff = render_handoff(
            goal_id,
            run_id,
            sid,
            direction,
            from_agent,
            to_agent,
            parent_segment_id,
            context,
            requested_outcome,
            artifacts,
            constraints,
        )
        write_text_atomic(handoff_file, handoff)
        prompt = render_prompt(
            goal_id,
            run_id,
            sid,
            attempt,
            direction,
            from_agent,
            to_agent,
            parent_segment_id,
            str(handoff_file),
            str(result_file),
            str(status_file),
            str(run_dir / "segments.jsonl"),
        )
        write_text_atomic(prompt_file, prompt)
        write_json_atomic(
            receipt_file,
            {
                "version": 1,
                "status": "wake_registration_pending",
                "pid": None,
                "command": [],
                "created_at": now_iso(),
            },
        )
        append_event(
            run_dir,
            {
                "event": "segment_started",
                "segment_id": sid,
                "status": "active",
                "direction": direction,
                "from_agent": from_agent.get("name", ""),
                "to_agent": to_agent.get("name", ""),
                "from_agent_snapshot": agent_snapshot(from_agent),
                "to_agent_snapshot": agent_snapshot(to_agent),
                "parent_segment_id": parent_segment_id,
                "handoff_file": str(handoff_file),
                "status_file": str(status_file),
                "created_at": now_iso(),
                "reason": reason,
            },
        )
        append_event(
            run_dir,
            {
                "event": "attempt_started",
                "segment_id": sid,
                "attempt": attempt,
                "status": "active",
                "prompt_file": str(prompt_file),
                "result_file": str(result_file),
                "wake_receipt_file": str(receipt_file),
                "wake_method": wake_target.get("wake_method"),
                "target_cwd": to_agent.get("cwd", ""),
                "requested_chat_name": wake_target.get("requested_chat_name", ""),
                "resolved_thread_id": wake_target.get("resolved_thread_id", ""),
                "chat_resolution": wake_target.get("chat_resolution"),
                "created_at": now_iso(),
                "idempotency_key": "{0}:{1}:{2}:{3}".format(run_id, sid, attempt, int(time.time() * 1000)),
            },
        )

    wake = run_wake(
        to_agent,
        prompt_file,
        wake_target,
        no_wake=no_wake,
        detach=not getattr(wake_args, "foreground", False),
        receipt_file=receipt_file,
    )

    with file_lock(run_lock(run_dir)):
        append_event(
            run_dir,
            {
                "event": "wake_finished",
                "segment_id": sid,
                "attempt": attempt,
                "status": wake["status"],
                "exit_code": wake["exit_code"],
                "pid": wake["pid"],
                "pgid": wake.get("pgid"),
                "session_id": wake.get("session_id"),
                "proc_start_ticks": wake.get("proc_start_ticks"),
                "proc_uid": wake.get("proc_uid"),
                "proc_cmdline_sha256": wake.get("proc_cmdline_sha256", ""),
                "command_sha256": wake.get("command_sha256", ""),
                "stdout_file": wake["stdout_file"],
                "stderr_file": wake["stderr_file"],
                "command": wake["command"],
                "updated_at": now_iso(),
            },
        )
        refresh_status_files(run_dir)
        update_run_status(run_dir)
    refresh_process_monitor(run_goal_dir(run_dir), perform_cleanup=False)
    return sid


def cmd_handoff(args):
    from_agent = resolve_agent(args.from_agent)
    to_agent = resolve_agent(args.to_agent)
    wake_target = resolve_wake_target(to_agent, args)
    goal_dir = find_goal(args.goal)
    auto_created_run = False
    if args.new_run:
        run_dir, _ = create_run(args.goal, from_agent, args.reason or args.requested_outcome, args.previous_run)
    elif args.run:
        run_dir = find_run(args.run, args.goal)
    else:
        run_dir, auto_created_run = infer_or_create_active_run(
            args.goal,
            from_agent,
            args.reason or args.requested_outcome,
            args.previous_run,
        )
    run = read_json(run_dir / "run.json")
    context = read_context(args)
    sid = create_segment(
        run_dir,
        args.goal,
        run["run_id"],
        "forward",
        from_agent,
        to_agent,
        args.parent_segment,
        context,
        args.requested_outcome,
        args.artifact or [],
        args.constraint or [],
        args.reason or args.requested_outcome,
        wake_target,
        no_wake=args.no_wake,
        wake_args=args,
    )
    seg = seg_label(sid)
    print("handoff_created\t{0}\t{1}".format(run["run_id"], seg))
    if auto_created_run:
        print("run_auto_created\t{0}".format(run["run_id"]))
    print("handoff_file\t{0}".format(run_dir / "handoff-{0}.md".format(seg)))
    print("prompt_file\t{0}".format(run_dir / "prompt-{0}-ATT001.md".format(seg)))
    print("result_file\t{0}".format(run_dir / "result-{0}-ATT001.md".format(seg)))
    print("status_file\t{0}".format(run_dir / "status-{0}.json".format(seg)))


def get_segment_or_die(run_dir, segment_id):
    segments = build_segments(read_events(run_dir))
    sid = int(segment_id)
    if sid not in segments:
        die("unknown segment {0} in {1}".format(segment_id, run_dir.name))
    return segments, segments[sid]


def materialize_result(run_dir, seg, summary, result_path=None):
    att = latest_attempt(seg)
    if not att:
        die("segment has no attempts: {0}".format(seg["segment_id"]))
    dest = Path(att.get("result_file") or (run_dir / "result-{0}-{1}.md".format(seg_label(seg["segment_id"]), att_label(att["attempt"]))))
    if result_path:
        src = Path(result_path).expanduser()
        if not src.exists():
            die("result file does not exist: {0}".format(src))
        if src.resolve() != dest.resolve():
            shutil.copyfile(str(src), str(dest))
    elif not dest.exists():
        write_text_atomic(dest, "# Co-Agent Result\n\nsummary: {0}\n".format(summary))
    return dest, int(att["attempt"])


def cmd_finish(args):
    run_dir = find_run(args.run, args.goal)
    with file_lock(run_lock(run_dir)):
        segments, seg = get_segment_or_die(run_dir, args.segment)
        if seg.get("status") in ("finished", "returned", "failed", "blocked", "cancelled", "superseded"):
            die("segment already terminal: {0} status={1}".format(args.segment, seg.get("status")))
        result_file, attempt = materialize_result(run_dir, seg, args.summary, args.result)
        append_event(
            run_dir,
            {
                "event": "attempt_finished",
                "segment_id": int(args.segment),
                "attempt": attempt,
                "status": args.state,
                "summary": args.summary,
                "result_file": str(result_file),
                "updated_at": now_iso(),
            },
        )
        append_event(
            run_dir,
            {
                "event": "segment_finished",
                "segment_id": int(args.segment),
                "status": args.state,
                "summary": args.summary,
                "result_file": str(result_file),
                "updated_at": now_iso(),
            },
        )
        refresh_status_files(run_dir)
        update_run_status(run_dir)
    refresh_process_monitor(run_goal_dir(run_dir), perform_cleanup=False)
    print("segment_{0}\t{1}\tSEG{2:03d}".format(args.state, run_dir.name, int(args.segment)))


def ancestry_agents(segments, seg):
    agents = set()
    current = seg
    while current:
        agents.add(normalize_name(current.get("from_agent", "")))
        agents.add(normalize_name(current.get("to_agent", "")))
        parent = current.get("parent_segment_id")
        current = segments.get(int(parent)) if parent else None
    return agents


def cmd_return(args):
    run_dir = find_run(args.run, args.goal)
    with file_lock(run_lock(run_dir)):
        segments, seg = get_segment_or_die(run_dir, args.segment)
        target_name = args.to_agent or seg.get("from_agent")
        if normalize_name(target_name) not in ancestry_agents(segments, seg):
            die("return target is not in ancestry: {0}".format(target_name))
        from_agent = resolve_agent(seg.get("to_agent"))
        to_agent = resolve_agent(target_name)
        wake_target = resolve_wake_target(to_agent, args)
        result_file, attempt = materialize_result(run_dir, seg, args.summary, args.result)
        append_event(
            run_dir,
            {
                "event": "attempt_finished",
                "segment_id": int(args.segment),
                "attempt": attempt,
                "status": "finished",
                "summary": args.summary,
                "result_file": str(result_file),
                "updated_at": now_iso(),
            },
        )
        append_event(
            run_dir,
            {
                "event": "segment_finished",
                "segment_id": int(args.segment),
                "status": "returned",
                "summary": args.summary,
                "result_file": str(result_file),
                "updated_at": now_iso(),
            },
        )
    run = read_json(run_dir / "run.json")
    sid = create_segment(
        run_dir,
        run["goal_id"],
        run["run_id"],
        "return",
        from_agent,
        to_agent,
        int(args.segment),
        args.summary,
        "Review returned co-agent result and continue the upstream work.",
        [],
        [],
        "Return result from SEG{0:03d}".format(int(args.segment)),
        wake_target,
        no_wake=args.no_wake,
        wake_args=args,
    )
    print("return_created\t{0}\tSEG{1:03d}".format(run["run_id"], sid))


def cmd_retry(args):
    run_dir = find_run(args.run, args.goal)
    with file_lock(run_lock(run_dir)):
        segments, seg = get_segment_or_die(run_dir, args.segment)
        from_agent = resolve_agent(seg.get("from_agent"))
        to_agent = resolve_agent(seg.get("to_agent"))
        wake_target = resolve_wake_target(to_agent, args)
        attempt = allocate_attempt_id(seg)
        att = att_label(attempt)
        sid = int(args.segment)
        prompt_file = run_dir / "prompt-{0}-{1}.md".format(seg_label(sid), att)
        result_file = run_dir / "result-{0}-{1}.md".format(seg_label(sid), att)
        status_file = Path(seg.get("status_file") or (run_dir / "status-{0}.json".format(seg_label(sid))))
        receipt_file = wake_receipt_path(run_dir, sid, attempt)
        run = read_json(run_dir / "run.json")
        prompt = render_prompt(
            run["goal_id"],
            run["run_id"],
            sid,
            attempt,
            seg.get("direction", "forward"),
            from_agent,
            to_agent,
            seg.get("parent_segment_id"),
            seg.get("handoff_file", ""),
            str(result_file),
            str(status_file),
            str(run_dir / "segments.jsonl"),
        )
        write_text_atomic(prompt_file, prompt)
        write_json_atomic(
            receipt_file,
            {
                "version": 1,
                "status": "wake_registration_pending",
                "pid": None,
                "command": [],
                "created_at": now_iso(),
            },
        )
        append_event(
            run_dir,
            {
                "event": "attempt_started",
                "segment_id": sid,
                "attempt": attempt,
                "status": "active",
                "prompt_file": str(prompt_file),
                "result_file": str(result_file),
                "wake_receipt_file": str(receipt_file),
                "wake_method": wake_target.get("wake_method"),
                "target_cwd": to_agent.get("cwd", ""),
                "requested_chat_name": wake_target.get("requested_chat_name", ""),
                "resolved_thread_id": wake_target.get("resolved_thread_id", ""),
                "chat_resolution": wake_target.get("chat_resolution"),
                "created_at": now_iso(),
                "reason": args.reason or "retry",
                "idempotency_key": "{0}:{1}:{2}:{3}".format(run["run_id"], sid, attempt, int(time.time() * 1000)),
            },
        )
    wake = run_wake(
        to_agent,
        prompt_file,
        wake_target,
        no_wake=args.no_wake,
        detach=not getattr(args, "foreground", False),
        receipt_file=receipt_file,
    )
    with file_lock(run_lock(run_dir)):
        append_event(
            run_dir,
            {
                "event": "wake_finished",
                "segment_id": sid,
                "attempt": attempt,
                "status": wake["status"],
                "exit_code": wake["exit_code"],
                "pid": wake["pid"],
                "pgid": wake.get("pgid"),
                "session_id": wake.get("session_id"),
                "proc_start_ticks": wake.get("proc_start_ticks"),
                "proc_uid": wake.get("proc_uid"),
                "proc_cmdline_sha256": wake.get("proc_cmdline_sha256", ""),
                "command_sha256": wake.get("command_sha256", ""),
                "stdout_file": wake["stdout_file"],
                "stderr_file": wake["stderr_file"],
                "command": wake["command"],
                "updated_at": now_iso(),
            },
        )
        refresh_status_files(run_dir)
        update_run_status(run_dir)
    refresh_process_monitor(run_goal_dir(run_dir), perform_cleanup=False)
    print("retry_created\t{0}\tSEG{1:03d}\tATT{2:03d}".format(run_dir.name, sid, attempt))


def cmd_cancel(args):
    run_dir = find_run(args.run, args.goal)
    with file_lock(run_lock(run_dir)):
        get_segment_or_die(run_dir, args.segment)
        append_event(
            run_dir,
            {
                "event": "segment_cancelled",
                "segment_id": int(args.segment),
                "status": "cancelled",
                "reason": args.reason,
                "updated_at": now_iso(),
            },
        )
        refresh_status_files(run_dir)
        update_run_status(run_dir)
    refresh_process_monitor(run_goal_dir(run_dir), perform_cleanup=False)
    print("segment_cancelled\t{0}\tSEG{1:03d}".format(run_dir.name, int(args.segment)))


def cmd_supersede(args):
    run_dir = find_run(args.run, args.goal)
    with file_lock(run_lock(run_dir)):
        get_segment_or_die(run_dir, args.segment)
        get_segment_or_die(run_dir, args.by_segment)
        append_event(
            run_dir,
            {
                "event": "segment_superseded",
                "segment_id": int(args.segment),
                "status": "superseded",
                "by_segment": int(args.by_segment),
                "reason": args.reason,
                "updated_at": now_iso(),
            },
        )
        refresh_status_files(run_dir)
        update_run_status(run_dir)
    refresh_process_monitor(run_goal_dir(run_dir), perform_cleanup=False)
    print("segment_superseded\t{0}\tSEG{1:03d}".format(run_dir.name, int(args.segment)))


def cmd_unblock(args):
    args.reason = args.reason or "unblock"
    cmd_retry(args)


def wake_note(att):
    status = att.get("wake_status", "")
    method = att.get("wake_method", "")
    if status == "wake_detached":
        return "codex wake was started in background; wake_exit_code is intentionally not tracked. Use status/history/result for segment completion."
    if status == "wake_skipped":
        return "wake was skipped by --no-wake."
    if method == "last" and not att.get("resolved_thread_id"):
        return "resume --last was used; resolved_thread_id is not available by design."
    if status == "wake_failed":
        return "wake command failed; inspect wake stdout/stderr files."
    return ""


def segment_status_view(seg):
    att = latest_attempt(seg) or {}
    result_file = seg.get("result_file", att.get("result_file", ""))
    wake_status = att.get("wake_status", "")
    if wake_status == "wake_detached":
        wake_tracking = "detached"
    elif wake_status == "wake_skipped":
        wake_tracking = "skipped"
    elif wake_status:
        wake_tracking = "foreground"
    else:
        wake_tracking = "unknown"
    return {
        "version": 1,
        "segment_id": seg.get("segment_id"),
        "agent": seg.get("to_agent", ""),
        "state": seg.get("status", "unknown"),
        "direction": seg.get("direction", ""),
        "from_agent": seg.get("from_agent", ""),
        "to_agent": seg.get("to_agent", ""),
        "parent_segment_id": seg.get("parent_segment_id"),
        "latest_attempt": att.get("attempt"),
        "wake_method": att.get("wake_method", ""),
        "target_cwd": att.get("target_cwd", ""),
        "requested_chat_name": att.get("requested_chat_name", ""),
        "resolved_thread_id": att.get("resolved_thread_id", ""),
        "wake_status": wake_status,
        "wake_exit_code": att.get("wake_exit_code"),
        "wake_pid": att.get("wake_pid"),
        "wake_tracking": wake_tracking,
        "wake_note": wake_note(att),
        "summary": seg.get("summary", ""),
        "result_file": result_file,
        "result_excerpt": read_text_excerpt(result_file, 1200),
        "needs_followup": seg.get("status") in ("active", "blocked"),
        "updated_at": now_iso(),
    }


def refresh_status_files(run_dir):
    segments = build_segments(read_events(run_dir))
    for sid, seg in segments.items():
        path = Path(seg.get("status_file") or (run_dir / "status-{0}.json".format(seg_label(sid))))
        write_json_atomic(path, segment_status_view(seg))


def aggregate_run_status(segments):
    if not segments:
        return "active"
    statuses = [seg.get("status", "unknown") for seg in segments.values()]
    if any(status == "active" for status in statuses):
        return "active"
    if any(status == "failed" for status in statuses):
        return "failed"
    if any(status == "blocked" for status in statuses):
        return "blocked"
    if all(status in ("finished", "returned", "cancelled", "superseded") for status in statuses):
        return "finished"
    return "active"


def update_run_status(run_dir):
    data = read_json(run_dir / "run.json")
    segments = build_segments(read_events(run_dir))
    data["status"] = aggregate_run_status(segments)
    data["updated_at"] = now_iso()
    write_json_atomic(run_dir / "run.json", data)
    update_goal_status(run_goal_dir(run_dir))


def update_goal_status(goal_dir):
    goal = read_json(goal_dir / "goal.json")
    runs = [read_json(rd / "run.json") for rd in iter_run_dirs(goal_dir)]
    statuses = [run.get("status") for run in runs]
    if not statuses or any(status == "active" for status in statuses):
        goal["status"] = "active"
    elif any(status == "blocked" for status in statuses):
        goal["status"] = "blocked"
    elif any(status == "failed" for status in statuses):
        goal["status"] = "blocked"
    else:
        goal["status"] = "finished"
    goal["updated_at"] = now_iso()
    write_json_atomic(goal_dir / "goal.json", goal)


def attempt_terminal_info(events, segment_id, attempt):
    terminal = None
    attempt_started = not any(
        event.get("event") == "attempt_started"
        and event.get("segment_id") is not None
        and int(event.get("segment_id")) == int(segment_id)
        and int(event.get("attempt", 0) or 0) == int(attempt)
        for event in events
    )
    for event in events:
        event_segment = event.get("segment_id")
        if event_segment is None or int(event_segment) != int(segment_id):
            continue
        etype = event.get("event")
        event_attempt = int(event.get("attempt", 0) or 0)
        if etype == "attempt_started" and event_attempt == int(attempt):
            attempt_started = True
            continue
        if not attempt_started:
            continue
        if etype == "attempt_finished" and event_attempt == int(attempt) and terminal is None:
            terminal = {
                "terminal_at": event_time(event),
                "business_state": event.get("status", "finished"),
                "terminal_reason": "attempt_finished",
            }
        elif etype == "attempt_started" and event_attempt > int(attempt) and terminal is None:
            terminal = {
                "terminal_at": event_time(event),
                "business_state": "superseded",
                "terminal_reason": "later_attempt_started",
            }
        elif etype in ("segment_finished", "segment_cancelled", "segment_superseded") and terminal is None:
            terminal = {
                "terminal_at": event_time(event),
                "business_state": event.get("status", etype.replace("segment_", "")),
                "terminal_reason": etype,
            }
    return terminal or {
        "terminal_at": "",
        "business_state": "active",
        "terminal_reason": "",
    }


def goal_top_from_agent(goal_dir, previous=None):
    if previous and previous.get("top_from_agent"):
        return previous.get("top_from_agent"), previous.get("top_from_agent_snapshot", {})
    candidates = []
    for run_dir in iter_run_dirs(goal_dir):
        run = read_json(run_dir / "run.json", {})
        candidates.append((run.get("created_at", ""), run_dir.name, run))
    if candidates:
        run = sorted(candidates, key=lambda item: (item[0], item[1]))[0][2]
        return run.get("created_by_agent", ""), run.get("created_by_agent_snapshot", {})
    goal = read_json(goal_dir / "goal.json", {})
    return goal.get("owner_agent", ""), goal.get("owner_agent_snapshot", {})


def collect_process_attempts(goal_dir, previous=None):
    previous_records = {
        item.get("key"): item
        for item in (previous or {}).get("attempts", [])
        if item.get("key")
    }
    records = []
    for run_dir in iter_run_dirs(goal_dir):
        events = read_events(run_dir)
        segments = build_segments(events)
        for sid in sorted(segments):
            seg = segments[sid]
            for attempt_id in sorted(seg.get("attempts", {})):
                att = seg["attempts"][attempt_id]
                receipt = {}
                if not att.get("wake_status") and att.get("wake_receipt_file"):
                    receipt = read_json(Path(att["wake_receipt_file"]), {})
                wake_status = att.get("wake_status", "") or receipt.get("status", "")
                wake_command = att.get("wake_command", []) or receipt.get("command", [])
                key = "{0}:SEG{1:03d}:ATT{2:03d}".format(run_dir.name, sid, attempt_id)
                record = dict(previous_records.get(key, {}))
                record.update(
                    {
                        "key": key,
                        "run_id": run_dir.name,
                        "segment_id": sid,
                        "attempt": attempt_id,
                        "from_agent": seg.get("from_agent", ""),
                        "to_agent": seg.get("to_agent", ""),
                        "parent_segment_id": seg.get("parent_segment_id"),
                        "direction": seg.get("direction", ""),
                        "attempt_created_at": att.get("created_at", ""),
                        "wake_status": wake_status,
                        "wake_receipt_file": att.get("wake_receipt_file", ""),
                        "pid": att.get("wake_pid") if att.get("wake_status") else receipt.get("pid"),
                        "pgid": att.get("wake_pgid") if att.get("wake_status") else receipt.get("pgid"),
                        "session_id": att.get("wake_session_id") if att.get("wake_status") else receipt.get("session_id"),
                        "proc_start_ticks": att.get("wake_proc_start_ticks") if att.get("wake_status") else receipt.get("proc_start_ticks"),
                        "proc_uid": att.get("wake_proc_uid") if att.get("wake_status") else receipt.get("proc_uid"),
                        "proc_cmdline_sha256": att.get("wake_proc_cmdline_sha256", "")
                        if att.get("wake_status")
                        else receipt.get("proc_cmdline_sha256", ""),
                        "command": wake_command,
                        "command_sha256": att.get("wake_command_sha256", "")
                        if att.get("wake_status")
                        else receipt.get("command_sha256", "") or command_sha256(wake_command),
                    }
                )
                record.update(attempt_terminal_info(events, sid, attempt_id))
                records.append(record)
    return records


def process_identity_matches(record, identity):
    required = (
        ("pgid", "pgid"),
        ("proc_start_ticks", "start_ticks"),
        ("proc_uid", "uid"),
        ("proc_cmdline_sha256", "cmdline_sha256"),
    )
    for record_field, identity_field in required:
        expected = record.get(record_field)
        if expected in (None, "") or identity.get(identity_field) != expected:
            return False
    return True


def inspect_process_record(record):
    pid = record.get("pid")
    pgid = record.get("pgid")
    if record.get("wake_status") != "wake_detached" or not pid:
        return {"alive": False, "safe_to_signal": False, "members": [], "reason": "not_detached"}

    leader = read_process_identity(pid)
    members = process_group_members(pgid)
    if leader and leader.get("state") != "Z":
        if not process_identity_matches(record, leader):
            return {
                "alive": True,
                "safe_to_signal": False,
                "members": members or [leader],
                "reason": "identity_mismatch",
            }
        safe = all(item.get("uid") == record.get("proc_uid") for item in members)
        current_group = int(pgid or 0) == os.getpgrp()
        return {
            "alive": True,
            "safe_to_signal": safe and not current_group,
            "members": members or [leader],
            "reason": "current_process_group" if current_group else ("leader_verified" if safe else "group_uid_mismatch"),
        }

    if members:
        safe = (
            record.get("proc_uid") not in (None, "")
            and all(item.get("uid") == record.get("proc_uid") for item in members)
            and int(pgid or 0) != os.getpgrp()
        )
        return {
            "alive": True,
            "safe_to_signal": safe,
            "members": members,
            "reason": "orphaned_group_verified" if safe else "orphaned_group_unverified",
        }
    return {"alive": False, "safe_to_signal": False, "members": [], "reason": "process_exited"}


def classify_process_record(record, checked_at, checked_epoch, grace_seconds):
    stable_cleared = record.get("process_state") in ("EXITED", "TERMINATED", "KILLED") or (
        record.get("process_state") == "NOT_RUNNING"
        and record.get("wake_status") in ("wake_skipped", "active", "wake_failed")
    )
    if record.get("cleared") and record.get("exit_observed_at") and stable_cleared:
        record.update(
            {
                "last_checked_at": checked_at,
                "process_count": 0,
                "inspection_reason": "previously_cleared",
                "grace_remaining_seconds": 0,
            }
        )
        return {
            "alive": False,
            "safe_to_signal": False,
            "members": [],
            "reason": "previously_cleared",
        }

    if record.get("wake_status") in ("", "wake_registration_pending"):
        record.update(
            {
                "last_checked_at": checked_at,
                "process_count": 0,
                "inspection_reason": "wake_registration_pending",
                "process_state": "WAKE_REGISTRATION_PENDING",
                "cleared": False,
                "grace_remaining_seconds": None,
            }
        )
        return {
            "alive": False,
            "safe_to_signal": False,
            "members": [],
            "reason": "wake_registration_pending",
        }

    inspection = inspect_process_record(record)
    record["last_checked_at"] = checked_at
    record["process_count"] = len(inspection.get("members", []))
    record["inspection_reason"] = inspection.get("reason", "")
    if not inspection.get("alive"):
        if record.get("kill_sent_at"):
            state = "KILLED"
        elif record.get("term_sent_at"):
            state = "TERMINATED"
        elif record.get("wake_status") == "wake_detached":
            state = "EXITED"
        else:
            state = "NOT_RUNNING"
        record.update({"process_state": state, "cleared": True, "grace_remaining_seconds": 0})
        record.setdefault("exit_observed_at", checked_at)
        return inspection

    terminal_epoch = iso_timestamp(record.get("terminal_at"))
    if terminal_epoch is None:
        record.update({"process_state": "RUNNING", "cleared": False, "grace_remaining_seconds": None})
        return inspection

    remaining = max(0, int(terminal_epoch + grace_seconds - checked_epoch + 0.999))
    if remaining > 0:
        record.update({"process_state": "EXIT_GRACE", "cleared": False, "grace_remaining_seconds": remaining})
        return inspection

    if inspection.get("safe_to_signal"):
        state = "CLEANUP_DUE"
    elif inspection.get("reason") == "current_process_group":
        state = "CURRENT_CALLER_GROUP"
    else:
        state = "IDENTITY_UNVERIFIED"
    record.update({"process_state": state, "cleared": False, "grace_remaining_seconds": 0})
    return inspection


def refresh_process_monitor(goal_dir, perform_cleanup=False, grace_seconds=PROCESS_EXIT_GRACE_SECONDS, term_wait_seconds=PROCESS_TERM_WAIT_SECONDS):
    monitor_path = goal_dir / PROCESS_MONITOR_FILE
    with file_lock(goal_lock(goal_dir)):
        previous = read_json(monitor_path, {})
        checked_at = now_iso()
        checked_epoch = time.time()
        top_from, top_snapshot = goal_top_from_agent(goal_dir, previous)
        records = collect_process_attempts(goal_dir, previous)
        business_pending = [
            record
            for record in records
            if not record.get("terminal_at") and record.get("wake_status") != "wake_skipped"
        ]
        due = []
        for record in records:
            inspection = classify_process_record(record, checked_at, checked_epoch, grace_seconds)
            if business_pending and record.get("process_state") == "CLEANUP_DUE":
                record["process_state"] = "BUSINESS_PHASE_HOLD"
            if perform_cleanup and not business_pending and record.get("process_state") == "CLEANUP_DUE":
                due.append(record)

        term_sent = []
        for record in due:
            try:
                os.killpg(int(record["pgid"]), signal.SIGTERM)
                record["term_sent_at"] = checked_at
                record["cleanup_error"] = ""
                term_sent.append(record)
            except ProcessLookupError:
                pass
            except (PermissionError, OSError) as exc:
                record["process_state"] = "CLEANUP_FAILED"
                record["cleanup_error"] = str(exc)

        if term_sent and term_wait_seconds > 0:
            deadline = time.monotonic() + term_wait_seconds
            while time.monotonic() < deadline:
                if not any(inspect_process_record(record).get("alive") for record in term_sent):
                    break
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))

        for record in term_sent:
            inspection = inspect_process_record(record)
            if not inspection.get("alive"):
                continue
            if not inspection.get("safe_to_signal"):
                record["process_state"] = "IDENTITY_UNVERIFIED"
                continue
            try:
                os.killpg(int(record["pgid"]), signal.SIGKILL)
                record["kill_sent_at"] = now_iso()
            except ProcessLookupError:
                pass
            except (PermissionError, OSError) as exc:
                record["process_state"] = "CLEANUP_FAILED"
                record["cleanup_error"] = str(exc)

        if term_sent:
            time.sleep(0.1)
            final_at = now_iso()
            final_epoch = time.time()
            for record in term_sent:
                inspection = classify_process_record(record, final_at, final_epoch, grace_seconds)
                if inspection.get("alive") and record.get("cleanup_error"):
                    record["process_state"] = "CLEANUP_FAILED"

        waiting = [record for record in records if not record.get("cleared")]
        if business_pending:
            phase = "BUSINESS_ACTIVE"
        elif waiting:
            phase = "FINAL_MONITORING"
        else:
            phase = "ALL_CLEARED"
        data = {
            "version": 1,
            "goal_id": goal_dir.name,
            "top_from_agent": top_from,
            "top_from_agent_snapshot": top_snapshot,
            "grace_seconds": grace_seconds,
            "updated_at": now_iso(),
            "state": "ALL CLEARED" if phase == "ALL_CLEARED" else "WAITING",
            "phase": phase,
            "counts": {
                "attempts": len(records),
                "cleared": len(records) - len(waiting),
                "waiting": len(waiting),
                "business_pending": len(business_pending),
            },
            "attempts": records,
        }
        write_json_atomic(monitor_path, data)
        return data


def cmd_process_monitor(args):
    goal_dir = find_goal(args.goal)
    caller = resolve_agent(args.from_agent)
    existing = read_json(goal_dir / PROCESS_MONITOR_FILE, {})
    top_from, top_snapshot = goal_top_from_agent(goal_dir, existing)
    caller_name = caller.get("name", args.from_agent)
    same_name = normalize_name(caller_name) == normalize_name(top_from)
    same_cwd = (
        bool(caller.get("cwd"))
        and bool(top_snapshot.get("cwd"))
        and normalize_cwd_path(caller.get("cwd")) == normalize_cwd_path(top_snapshot.get("cwd"))
    )
    if not (same_name or same_cwd):
        die("process-monitor is owned by top-from agent {0}; caller was {1}".format(top_from, caller_name))
    data = refresh_process_monitor(goal_dir, perform_cleanup=True)
    print(data["state"])
    if data["state"] != "ALL CLEARED":
        print("phase\t{0}".format(data["phase"]))
        if data["counts"]["business_pending"]:
            print("business_pending\t{0}".format(data["counts"]["business_pending"]))
        print("waiting\t{0}".format(data["counts"]["waiting"]))
        print("next_check_seconds\t30")
    print("process_monitor_file\t{0}".format(goal_dir / PROCESS_MONITOR_FILE))


def cmd_reconcile(args):
    run_dir = find_run(args.run, args.goal)
    with file_lock(run_lock(run_dir)):
        refresh_status_files(run_dir)
        update_run_status(run_dir)
    refresh_process_monitor(run_goal_dir(run_dir), perform_cleanup=False)
    print("reconciled\t{0}".format(run_dir.name))


def run_summary(run_dir):
    run = read_json(run_dir / "run.json")
    segments = build_segments(read_events(run_dir))
    return {
        "run": run,
        "segments": [segment_status_view(segments[sid]) for sid in sorted(segments)],
    }


def cmd_status(args):
    ref = args.ref
    if ref.startswith("GOAL-"):
        goal_dir = find_goal(ref)
        goal = read_json(goal_dir / "goal.json")
        runs = [run_summary(rd) for rd in iter_run_dirs(goal_dir)]
        data = {"goal": goal, "runs": runs}
    else:
        run_dir = find_run(ref, args.goal)
        data = run_summary(run_dir)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if "goal" in data:
        print("goal\t{0}\t{1}\t{2}".format(data["goal"]["goal_id"], data["goal"].get("status"), data["goal"].get("title")))
        for item in data["runs"]:
            run = item["run"]
            print("run\t{0}\t{1}\t{2}".format(run["run_id"], run.get("status"), run.get("reason", "")))
            for seg in item["segments"]:
                print("segment\tSEG{0:03d}\t{1}\t{2}->{3}\t{4}".format(int(seg["segment_id"]), seg["state"], seg["from_agent"], seg["to_agent"], seg.get("summary", "")))
    else:
        run = data["run"]
        print("run\t{0}\t{1}\t{2}".format(run["run_id"], run.get("status"), run.get("reason", "")))
        for seg in data["segments"]:
            print("segment\tSEG{0:03d}\t{1}\t{2}->{3}\t{4}".format(int(seg["segment_id"]), seg["state"], seg["from_agent"], seg["to_agent"], seg.get("summary", "")))


def cmd_result(args):
    run_dir = find_run(args.run, args.goal)
    segments, seg = get_segment_or_die(run_dir, args.segment)
    view = segment_status_view(seg)
    result_file = view.get("result_file", "")
    result_text = read_text_excerpt(result_file, args.excerpt)
    data = {
        "run_id": run_dir.name,
        "segment_id": int(args.segment),
        "state": view.get("state"),
        "summary": view.get("summary", ""),
        "result_file": result_file,
        "result_excerpt": result_text,
    }
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if args.path_only:
        print(result_file)
        return
    print("run\t{0}".format(run_dir.name))
    print("segment\tSEG{0:03d}\t{1}".format(int(args.segment), view.get("state")))
    print("summary\t{0}".format(view.get("summary", "")))
    print("result_file\t{0}".format(result_file))
    if result_text:
        print("result_excerpt")
        print(result_text)


def history_for_run(run_dir):
    run = read_json(run_dir / "run.json")
    events = read_events(run_dir)
    segments = build_segments(events)
    agents = {}
    if run.get("created_by_agent"):
        agents[normalize_name(run["created_by_agent"])] = {
            "name": run["created_by_agent"],
            "role": run.get("created_by_agent_snapshot", {}).get("role", ""),
            "cwd": run.get("created_by_agent_snapshot", {}).get("cwd", ""),
            "responsibilities": normalize_responsibilities(run.get("created_by_agent_snapshot", {}).get("responsibilities", {})),
        }
    for seg in segments.values():
        for field, snap_field in (("from_agent", "from_agent_snapshot"), ("to_agent", "to_agent_snapshot")):
            name = seg.get(field, "")
            snap = seg.get(snap_field, {}) or {}
            if name:
                agents[normalize_name(name)] = {
                    "name": name,
                    "role": snap.get("role", ""),
                    "cwd": snap.get("cwd", ""),
                    "responsibilities": normalize_responsibilities(snap.get("responsibilities", {})),
                }
    seg_rows = []
    for sid in sorted(segments):
        seg = segments[sid]
        attempts = []
        for attempt_id in sorted(seg.get("attempts", {})):
            att = seg["attempts"][attempt_id]
            attempts.append(
                {
                    "attempt": attempt_id,
                    "status": att.get("status", ""),
                    "wake_status": att.get("wake_status", ""),
                    "wake_exit_code": att.get("wake_exit_code"),
                    "wake_method": att.get("wake_method", ""),
                    "target_cwd": att.get("target_cwd", ""),
                    "requested_chat_name": att.get("requested_chat_name", ""),
                    "resolved_thread_id": att.get("resolved_thread_id", ""),
                    "prompt_file": att.get("prompt_file", ""),
                    "result_file": att.get("result_file", ""),
                    "wake_stdout_file": att.get("wake_stdout_file", ""),
                    "wake_stderr_file": att.get("wake_stderr_file", ""),
                }
            )
        seg_rows.append(
            {
                "segment_id": sid,
                "direction": seg.get("direction", ""),
                "parent_segment_id": seg.get("parent_segment_id"),
                "from_agent": seg.get("from_agent", ""),
                "to_agent": seg.get("to_agent", ""),
                "status": seg.get("status", ""),
                "reason": seg.get("reason", ""),
                "summary": seg.get("summary", ""),
                "handoff_file": seg.get("handoff_file", ""),
                "result_file": seg.get("result_file", ""),
                "created_at": seg.get("created_at", ""),
                "attempts": attempts,
            }
        )
    return {
        "run": run,
        "agents": list(agents.values()),
        "segments": seg_rows,
    }


def cmd_history(args):
    ref = args.ref
    if ref.startswith("GOAL-"):
        goal_dir = find_goal(ref)
        goal = read_json(goal_dir / "goal.json")
        runs = [history_for_run(rd) for rd in iter_run_dirs(goal_dir)]
        data = {"goal": goal, "runs": runs}
    else:
        run_dir = find_run(ref, args.goal)
        data = {"runs": [history_for_run(run_dir)]}
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
        return

    goal = data.get("goal")
    if goal:
        print("Goal {0}: {1} [{2}]".format(goal["goal_id"], goal.get("title", ""), goal.get("status", "")))
    all_agents = {}
    for run_item in data["runs"]:
        for agent in run_item["agents"]:
            all_agents[normalize_name(agent["name"])] = agent
    print("Agents:")
    for agent in sorted(all_agents.values(), key=lambda item: item["name"].casefold()):
        print("- {name} cwd={cwd} role={role} owns={owns}".format(
            owns=";".join(agent.get("responsibilities", {}).get("owns", [])),
            **agent,
        ))
    print("Cooperation:")
    for run_item in data["runs"]:
        run = run_item["run"]
        print("- Run {0} [{1}]: {2}".format(run["run_id"], run.get("status", ""), run.get("reason", "")))
        for seg in run_item["segments"]:
            print(
                "  - SEG{sid:03d} {direction} {from_agent} -> {to_agent} [{status}] reason={reason}".format(
                    sid=int(seg["segment_id"]),
                    direction=seg.get("direction", ""),
                    from_agent=seg.get("from_agent", ""),
                    to_agent=seg.get("to_agent", ""),
                    status=seg.get("status", ""),
                    reason=seg.get("reason", ""),
                )
            )
            if seg.get("summary"):
                print("    summary={0}".format(seg["summary"]))
            for att in seg.get("attempts", []):
                print(
                    "    ATT{attempt:03d} wake={wake_status} method={wake_method} cwd={target_cwd} chat={requested_chat_name} exit={wake_exit_code}".format(
                        attempt=int(att["attempt"]),
                        wake_status=att.get("wake_status", ""),
                        wake_method=att.get("wake_method", ""),
                        target_cwd=att.get("target_cwd", ""),
                        requested_chat_name=att.get("requested_chat_name", ""),
                        wake_exit_code=att.get("wake_exit_code"),
                    )
                )


def cmd_monitor(args):
    deadline = time.time() + args.timeout
    while True:
        run_dir = find_run(args.run, args.goal)
        data = run_summary(run_dir)
        states = [seg.get("state") for seg in data["segments"]]
        if states and not any(state == "active" for state in states):
            print("monitor_done\t{0}\t{1}".format(args.run, data["run"].get("status")))
            return
        if time.time() >= deadline:
            if args.mark_timeout:
                with file_lock(run_lock(run_dir)):
                    segments = build_segments(read_events(run_dir))
                    for sid, seg in segments.items():
                        if seg.get("status") == "active":
                            att = latest_attempt(seg) or {"attempt": 1}
                            append_event(
                                run_dir,
                                {
                                    "event": "attempt_finished",
                                    "segment_id": sid,
                                    "attempt": att["attempt"],
                                    "status": "timed_out",
                                    "summary": "monitor timeout",
                                    "result_file": att.get("result_file", ""),
                                    "updated_at": now_iso(),
                                },
                            )
                    refresh_status_files(run_dir)
                    update_run_status(run_dir)
            die("monitor timeout after {0}s".format(args.timeout))
        time.sleep(args.interval)


def cmd_wait(args):
    deadline = time.time() + args.timeout
    interval = 300
    while True:
        run_dir = find_run(args.run, args.goal)
        if args.segment is not None:
            _, seg = get_segment_or_die(run_dir, args.segment)
            view = segment_status_view(seg)
            if view.get("state") != "active":
                print("wait_done\t{0}\tSEG{1:03d}\t{2}".format(args.run, int(args.segment), view.get("state")))
                if view.get("summary"):
                    print("summary\t{0}".format(view.get("summary")))
                if view.get("result_file"):
                    print("result_file\t{0}".format(view.get("result_file")))
                return
        else:
            data = run_summary(run_dir)
            states = [seg.get("state") for seg in data["segments"]]
            if states and not any(state == "active" for state in states):
                print("wait_done\t{0}\t{1}".format(args.run, data["run"].get("status")))
                for seg in data["segments"]:
                    if seg.get("summary"):
                        print("segment\tSEG{0:03d}\t{1}\t{2}".format(int(seg["segment_id"]), seg.get("state"), seg.get("summary")))
                return
        if time.time() >= deadline:
            die("wait timeout after {0}s".format(args.timeout))
        time.sleep(interval)


def add_goal_run_args(parser):
    parser.add_argument("--goal", help="Goal id for disambiguation.")


def add_wake_selection_args(parser):
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--session",
        help="Resume this validated chat candidate thread id.",
    )
    selection.add_argument(
        "--last",
        dest="resume_last",
        action="store_true",
        help="Explicitly resume the target cwd's latest session.",
    )


def build_parser():
    parser = argparse.ArgumentParser(description="Manage co-agent handoffs.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("register")
    p.add_argument("agent")
    p.add_argument("--cwd", required=True)
    p.add_argument("--role", default="")
    p.add_argument("--alias", action="append", help="Additional exact alias. Repeat for multiple aliases.")
    p.add_argument("--owns", action="append", help="Task area owned by this agent. Repeat for multiple items.")
    p.add_argument("--handoff-when", action="append", help="Condition for handing work to another agent. Repeat for multiple items.")
    p.add_argument("--not-for", action="append", help="Task area this agent should not own. Repeat for multiple items.")
    p.add_argument("--update", action="store_true")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("rename-agent")
    p.add_argument("old_agent")
    p.add_argument("new_agent")
    p.add_argument("--alias", action="append", help="Replacement exact alias. Repeat for multiple aliases.")
    p.set_defaults(func=cmd_rename_agent)

    p = sub.add_parser("list-agents")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list_agents)

    p = sub.add_parser("resolve")
    p.add_argument("agent")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_resolve_agent)

    p = sub.add_parser("chat-candidates")
    p.add_argument("--agent")
    p.add_argument("--cwd")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_chat_candidates)

    p = sub.add_parser("resolve-cwd")
    p.add_argument("cwd")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_resolve_cwd)

    p = sub.add_parser("whoami")
    p.add_argument("--from", dest="from_agent")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("route")
    p.add_argument("--from", dest="from_agent", help="Current agent name. Defaults to COAGENT_NAME or cwd resolution.")
    p.add_argument("--task", required=True, help="Original user task text to route.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_route)

    p = sub.add_parser("start-goal")
    p.add_argument("--title", required=True)
    p.add_argument("--owner", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_start_goal)

    p = sub.add_parser("start-run")
    p.add_argument("--goal", required=True)
    p.add_argument("--from", dest="from_agent", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--previous-run", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_start_run)

    p = sub.add_parser("handoff")
    p.add_argument("--goal", required=True)
    p.add_argument("--run")
    p.add_argument("--new-run", action="store_true")
    p.add_argument("--previous-run", default="")
    p.add_argument("--from", dest="from_agent", required=True)
    p.add_argument("--to", dest="to_agent", required=True)
    p.add_argument("--context")
    p.add_argument("--context-text")
    p.add_argument("--requested-outcome", required=True)
    p.add_argument("--reason")
    p.add_argument("--artifact", action="append")
    p.add_argument("--constraint", action="append")
    p.add_argument("--parent-segment", type=int)
    add_wake_selection_args(p)
    p.add_argument("--no-wake", action="store_true", help="Write evidence but do not start codex.")
    p.add_argument("--detach", action="store_true", help="Start codex in the background and return immediately. This is the default.")
    p.add_argument("--foreground", action="store_true", help="Run codex exec in the foreground and wait for it to exit.")
    p.set_defaults(func=cmd_handoff)

    p = sub.add_parser("finish")
    add_goal_run_args(p)
    p.add_argument("--run", required=True)
    p.add_argument("--segment", required=True, type=int)
    p.add_argument("--state", choices=["finished", "blocked", "failed"], default="finished")
    p.add_argument("--summary", required=True)
    p.add_argument("--result")
    p.set_defaults(func=cmd_finish)

    p = sub.add_parser("return")
    add_goal_run_args(p)
    p.add_argument("--run", required=True)
    p.add_argument("--segment", required=True, type=int)
    p.add_argument("--summary", required=True)
    p.add_argument("--result")
    p.add_argument("--to", dest="to_agent")
    add_wake_selection_args(p)
    p.add_argument("--no-wake", action="store_true")
    p.add_argument("--detach", action="store_true", help="Start codex in the background and return immediately. This is the default.")
    p.add_argument("--foreground", action="store_true", help="Run codex exec in the foreground and wait for it to exit.")
    p.set_defaults(func=cmd_return)

    p = sub.add_parser("retry")
    add_goal_run_args(p)
    p.add_argument("--run", required=True)
    p.add_argument("--segment", required=True, type=int)
    p.add_argument("--reason", default="")
    add_wake_selection_args(p)
    p.add_argument("--no-wake", action="store_true")
    p.add_argument("--detach", action="store_true", help="Start codex in the background and return immediately. This is the default.")
    p.add_argument("--foreground", action="store_true", help="Run codex exec in the foreground and wait for it to exit.")
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("cancel")
    add_goal_run_args(p)
    p.add_argument("--run", required=True)
    p.add_argument("--segment", required=True, type=int)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("supersede")
    add_goal_run_args(p)
    p.add_argument("--run", required=True)
    p.add_argument("--segment", required=True, type=int)
    p.add_argument("--by-segment", required=True, type=int)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_supersede)

    p = sub.add_parser("unblock")
    add_goal_run_args(p)
    p.add_argument("--run", required=True)
    p.add_argument("--segment", required=True, type=int)
    p.add_argument("--reason", default="")
    add_wake_selection_args(p)
    p.add_argument("--no-wake", action="store_true")
    p.add_argument("--detach", action="store_true", help="Start codex in the background and return immediately. This is the default.")
    p.add_argument("--foreground", action="store_true", help="Run codex exec in the foreground and wait for it to exit.")
    p.set_defaults(func=cmd_unblock)

    p = sub.add_parser("reconcile")
    add_goal_run_args(p)
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("process-monitor")
    p.add_argument("--goal", required=True)
    p.add_argument("--from", dest="from_agent", required=True)
    p.set_defaults(func=cmd_process_monitor)

    p = sub.add_parser("status")
    p.add_argument("ref", help="goal id or run id")
    p.add_argument("--goal")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("result")
    add_goal_run_args(p)
    p.add_argument("run")
    p.add_argument("--segment", required=True, type=int)
    p.add_argument("--excerpt", type=int, default=4000)
    p.add_argument("--json", action="store_true")
    p.add_argument("--path-only", action="store_true")
    p.set_defaults(func=cmd_result)

    p = sub.add_parser("history")
    p.add_argument("ref", help="goal id or run id")
    p.add_argument("--goal")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeline", action="store_true")
    p.add_argument("--tree", action="store_true")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("monitor")
    add_goal_run_args(p)
    p.add_argument("run")
    p.add_argument("--interval", type=int, default=10)
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--mark-timeout", action="store_true")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("wait")
    add_goal_run_args(p)
    p.add_argument("--run", required=True)
    p.add_argument("--segment", type=int)
    p.add_argument("--timeout", type=int, default=3600)
    p.set_defaults(func=cmd_wait)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
