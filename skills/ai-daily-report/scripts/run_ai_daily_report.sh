#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${AI_DAILY_CONFIG_FILE:-$SCRIPT_DIR/.config.json}"

config_get() {
  local key="$1"
  local default_value="${2:-}"
  "$PYTHON_BIN" - "$CONFIG_FILE" "$key" "$default_value" <<'PY'
import json
import sys
from pathlib import Path

config_file, key, default_value = sys.argv[1:]
value = default_value
path = Path(config_file)
if path.exists():
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        current = data
        for part in key.split("."):
            current = current[part]
        if isinstance(current, bool):
            value = "1" if current else "0"
        elif current is not None:
            value = str(current)
    except Exception:
        pass
print(value)
PY
}

PYTHON_BIN="${PYTHON_BIN:-python3}"
SYSTEM_HOME="$(getent passwd "$(id -un)" | cut -d: -f6 || true)"
CODEX_USER_HOME="${CODEX_USER_HOME:-${SYSTEM_HOME:-${HOME:-}}}"
CODEX_HOME_DIR="${CODEX_HOME:-$CODEX_USER_HOME/.codex}"
export CODEX_HOME="$CODEX_HOME_DIR"
export CODEX_USER_HOME
export HOME="${AI_DAILY_HOME:-${SYSTEM_HOME:-${HOME:-$CODEX_USER_HOME}}}"
export TZ="${AI_DAILY_TIMEZONE:-${TZ:-$(config_get timezone UTC)}}"
export AI_DAILY_CALENDAR_PRIMARY_URL_TEMPLATE="${AI_DAILY_CALENDAR_PRIMARY_URL_TEMPLATE:-$(config_get calendar.primary_url_template 'https://api.jiejiariapi.com/v1/holidays/{year}')}"
export AI_DAILY_CALENDAR_SECONDARY_URL_TEMPLATE="${AI_DAILY_CALENDAR_SECONDARY_URL_TEMPLATE:-$(config_get calendar.secondary_url_template 'https://timor.tech/api/holiday/info/{date}')}"
export AI_DAILY_CALENDAR_USER_AGENT="${AI_DAILY_CALENDAR_USER_AGENT:-$(config_get calendar.user_agent 'codex-ai-daily-report/1.0')}"
export AI_DAILY_CALENDAR_HTTP_TIMEOUT_SEC="${AI_DAILY_CALENDAR_HTTP_TIMEOUT_SEC:-$(config_get calendar.http_timeout_sec 8)}"
export AI_DAILY_CALENDAR_UPGRADE_TIMEOUT_SEC="${AI_DAILY_CALENDAR_UPGRADE_TIMEOUT_SEC:-$(config_get calendar.upgrade_timeout_sec 900)}"
export AI_DAILY_CALENDAR_PIP_PACKAGE="${AI_DAILY_CALENDAR_PIP_PACKAGE:-$(config_get calendar.pip_package chinesecalendar)}"

expand_config_value() {
  local value="$1"
  value="${value//\$CODEX_HOME/$CODEX_HOME_DIR}"
  value="${value//\$CODEX_USER_HOME/$CODEX_USER_HOME}"
  value="${value//\$HOME/$CODEX_USER_HOME}"
  printf '%s\n' "$value"
}

DAILY_REPORT_DIR="${AI_DAILY_REPORT_DIR:-$CODEX_HOME_DIR/daily_report}"
NODE_BIN="${NODE_BIN:-$(expand_config_value "$(config_get codex.node_bin "")")}"
CODEX_JS="${CODEX_JS:-$(expand_config_value "$(config_get codex.codex_js "")")}"
CODEX_BIN="${CODEX_BIN:-}"
TODAY_DATE="$(date +%F)"
YESTERDAY_DATE="$(date -d "1 day ago" +%F)"
TARGET_SPEC=""
PLAN_ITEM_IDS=()
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --plan-id)
      if [[ "$#" -lt 2 || -z "$2" ]]; then
        printf '%s\n' '--plan-id requires a Task/Track ID' >&2
        exit 2
      fi
      PLAN_ITEM_IDS+=("$2")
      shift 2
      ;;
    --today|--yesterday)
      if [[ -n "$TARGET_SPEC" ]]; then
        printf 'multiple report targets are not allowed: %s %s\n' "$TARGET_SPEC" "$1" >&2
        exit 2
      fi
      TARGET_SPEC="$1"
      shift
      ;;
    --*)
      printf 'unknown option: %s\n' "$1" >&2
      exit 2
      ;;
    *)
      if [[ -n "$TARGET_SPEC" ]]; then
        printf 'multiple report targets are not allowed: %s %s\n' "$TARGET_SPEC" "$1" >&2
        exit 2
      fi
      TARGET_SPEC="$1"
      shift
      ;;
  esac
done
TARGET_MODE="explicit"
if [[ -z "$TARGET_SPEC" ]]; then
  TARGET_OFFSET_DAYS="${AI_DAILY_TARGET_OFFSET_DAYS:-1}"
  TARGET_DATE="$(date -d "${TARGET_OFFSET_DAYS} day ago" +%F)"
  TARGET_MODE="default-offset-${TARGET_OFFSET_DAYS}"
elif [[ "$TARGET_SPEC" == "--yesterday" || "$TARGET_SPEC" == "yesterday" ]]; then
  TARGET_DATE="$(date -d "1 day ago" +%F)"
  TARGET_MODE="yesterday"
elif [[ "$TARGET_SPEC" == "--today" || "$TARGET_SPEC" == "today" ]]; then
  TARGET_DATE="$TODAY_DATE"
  TARGET_MODE="today"
else
  TARGET_DATE="$TARGET_SPEC"
  TARGET_MODE="explicit-date"
fi
LOG_DIR="$DAILY_REPORT_DIR/logs"
LOG_FILE="$LOG_DIR/ai-daily-${TARGET_DATE}.log"
LAST_MESSAGE="$LOG_DIR/ai-daily-${TARGET_DATE}.last.md"
REPORT_PATH_TOOL="$SCRIPT_DIR/report_paths.py"
if [[ -f "$REPORT_PATH_TOOL" ]]; then
  REPORT_FILE="$("$PYTHON_BIN" "$REPORT_PATH_TOOL" path "$TARGET_DATE")"
else
  REPORT_FILE="$DAILY_REPORT_DIR/report_files/${TARGET_DATE:0:4}${TARGET_DATE:5:2}/${TARGET_DATE}.md"
fi
REPORT_DIR="$(dirname "$REPORT_FILE")"
BATCH_FILE="$CODEX_HOME_DIR/.tmp/obsidian-upload-${TARGET_DATE}.sftp"
WORKDAY_CHECKER="$SCRIPT_DIR/check_workday.py"
STATUS_UPDATER="$SCRIPT_DIR/update_status.py"
PLAN_STATE_TOOL="$SCRIPT_DIR/plan_state.py"
RUN_CLEANUP="$SCRIPT_DIR/cleanup_daily_report_run.sh"
PLAN_CONTEXT_FILE="$LOG_DIR/plan-state-${TARGET_DATE}.md"
EVIDENCE_COLLECTOR="$SCRIPT_DIR/collect_evidence.py"
EVIDENCE_RUNNER="${AI_DAILY_EVIDENCE_RUNNER:-$DAILY_REPORT_DIR/run_collect_evidence.sh}"
EVIDENCE_FILE="$LOG_DIR/evidence-${TARGET_DATE}.json"
EVIDENCE_SUMMARY_FILE="$LOG_DIR/evidence-${TARGET_DATE}.summary.txt"
OUTCOME_PIPELINE="$SCRIPT_DIR/outcome_pipeline.py"
OUTCOME_SUMMARY_FILE="$LOG_DIR/outcome-${TARGET_DATE}.summary.json"
OUTCOME_INPUT_FILE=""
REPORT_VALIDATOR="$SCRIPT_DIR/validate_report.py"
REPORT_NORMALIZER="$SCRIPT_DIR/normalize_report.py"
REMOTE_USER="${AI_DAILY_REMOTE_USER:-$(config_get remote.user "")}"
REMOTE_HOST="${AI_DAILY_REMOTE_HOST:-$(config_get remote.host "")}"
REMOTE_DIR="${AI_DAILY_REMOTE_DIR:-$(config_get remote.dir "")}"
REMOTE_MONTH="${TARGET_DATE:0:4}${TARGET_DATE:5:2}"
REMOTE_MONTH_DIR="$REMOTE_DIR/$REMOTE_MONTH"
REMOTE_FILE="$REMOTE_MONTH_DIR/${TARGET_DATE}.md"
SYNC_MODE="${AI_DAILY_SYNC:-$(config_get remote.sync 0)}"
SENSITIVE_PATTERN='(password|passwd|token|secret|authorization|cookie|api[_-]?key)[[:space:]]*[:=][[:space:]]*[^[:space:]]{6,}|Bearer[[:space:]]+[A-Za-z0-9._~+/-]{10,}|BEGIN .*PRIVATE KEY|AKIA[0-9A-Z]{16}|ssh-(rsa|ed25519)[[:space:]]+[A-Za-z0-9+/=]{20,}'
PLAN_LEDGER_MODE="${AI_DAILY_PLAN_LEDGER:-auto}"
USE_PLAN_LEDGER=0
SCOPED_PLAN_LEDGER=0
if [[ "${#PLAN_ITEM_IDS[@]}" -gt 0 ]]; then
  USE_PLAN_LEDGER=1
  SCOPED_PLAN_LEDGER=1
  PLAN_LEDGER_MODE="scoped"
elif [[ "$PLAN_LEDGER_MODE" == "1" || "$PLAN_LEDGER_MODE" == "true" ]]; then
  if [[ "$TARGET_MODE" == "explicit-date" && "$TARGET_DATE" != "$TODAY_DATE" && "$TARGET_DATE" != "$YESTERDAY_DATE" ]]; then
    printf 'historical ledger runs require at least one --plan-id\n' >&2
    exit 2
  fi
  USE_PLAN_LEDGER=1
elif [[ "$PLAN_LEDGER_MODE" == "auto" ]]; then
  case "$TARGET_MODE" in
    default-offset-*|yesterday|today)
      USE_PLAN_LEDGER=1
      ;;
    explicit-date)
      if [[ "$TARGET_DATE" == "$TODAY_DATE" || "$TARGET_DATE" == "$YESTERDAY_DATE" ]]; then
        USE_PLAN_LEDGER=1
      fi
      ;;
  esac
fi

mkdir -p "$LOG_DIR" "$CODEX_HOME_DIR/.tmp" "$REPORT_DIR"

RUN_COMPLETED=0
CURRENT_STAGE="preflight"

update_status() {
  if [[ -f "$STATUS_UPDATER" ]]; then
    "$PYTHON_BIN" "$STATUS_UPDATER" \
      --target-date "$TARGET_DATE" \
      --log-file "$LOG_FILE" \
      --report-file "$REPORT_FILE" \
      --remote-file "$REMOTE_FILE" \
      "$@" || true
  fi
}

field_from_line() {
  local line="$1"
  local key="$2"
  printf '%s\n' "$line" | tr ' ' '\n' | sed -n "s/^${key}=//p" | tail -n 1
}

on_exit() {
  local rc=$?
  if [[ "$rc" -ne 0 && "$RUN_COMPLETED" -eq 0 ]]; then
    local state="failed"
    if [[ "$CURRENT_STAGE" == sync* ]]; then
      state="sync_failed"
    fi
    update_status --state "$state" --reason "$CURRENT_STAGE" --set "exit_code=$rc"
  fi
}
trap on_exit EXIT

{
  printf '[%s] preflight ai daily report for %s\n' "$(date '+%F %T %z')" "$TARGET_DATE"
  update_status --reset --state preflight --reason start --set "run_date=$TODAY_DATE" --set "target_mode=$TARGET_MODE"
  if [[ -f "$WORKDAY_CHECKER" ]]; then
    set +e
    CHECK_OUTPUT="$("$PYTHON_BIN" "$WORKDAY_CHECKER" "$TARGET_DATE" 2>&1)"
    WORKDAY_RC=$?
    set -e
    printf '%s\n' "$CHECK_OUTPUT"
    WORKDAY_LINE="$(printf '%s\n' "$CHECK_OUTPUT" | sed -n 's/^calendar_check decision=/decision=/p' | tail -n 1)"
    WORKDAY_ACTION="$(field_from_line "$WORKDAY_LINE" "decision")"
    WORKDAY_SOURCE="$(field_from_line "$WORKDAY_LINE" "source")"
    WORKDAY_REASON="$(field_from_line "$WORKDAY_LINE" "reason")"
    update_status \
      --state preflight \
      --reason workday_checked \
      --set "workday.decision=${WORKDAY_ACTION:-unknown}" \
      --set "workday.source=${WORKDAY_SOURCE:-unknown}" \
      --set "workday.reason=${WORKDAY_REASON:-unknown}" \
      --set "workday.exit_code=$WORKDAY_RC"
    if [[ "$WORKDAY_RC" -eq 10 ]]; then
      printf '[%s] skip ai daily report for %s: non-workday\n' "$(date '+%F %T %z')" "$TARGET_DATE"
      update_status --state skipped --reason non_workday
      RUN_COMPLETED=1
      exit 0
    elif [[ "$WORKDAY_RC" -ne 0 ]]; then
      printf '[%s] workday checker returned rc=%s; continuing conservatively\n' "$(date '+%F %T %z')" "$WORKDAY_RC"
      update_status --state preflight --reason workday_checker_failed
    fi
  else
    printf '[%s] workday checker not found: %s; continuing conservatively\n' "$(date '+%F %T %z')" "$WORKDAY_CHECKER"
    update_status --state preflight --reason workday_checker_missing
  fi
} >>"$LOG_FILE" 2>&1

CODEX_CMD=()
if [[ -n "$CODEX_BIN" ]]; then
  CODEX_CMD=("$CODEX_BIN")
elif [[ -f "$CODEX_JS" ]]; then
  if [[ ! -x "$NODE_BIN" ]]; then
    printf 'node runtime not found: %s\n' "$NODE_BIN" >&2
    update_status --state failed --reason node_runtime_not_found
    RUN_COMPLETED=1
    exit 1
  fi
  CODEX_CMD=("$NODE_BIN" "$CODEX_JS")
else
  DETECTED_CODEX="$(command -v codex || true)"
  if [[ -n "$DETECTED_CODEX" ]]; then
    CODEX_CMD=("$DETECTED_CODEX")
  else
    printf 'codex entrypoint not found; set CODEX_BIN or CODEX_JS\n' >&2
    update_status --state failed --reason codex_entrypoint_not_found
    RUN_COMPLETED=1
    exit 1
  fi
fi

if [[ "$USE_PLAN_LEDGER" -eq 1 && -f "$PLAN_STATE_TOOL" ]]; then
  PLAN_CONTEXT_ARGS=(context --date "$TARGET_DATE")
  for PLAN_ITEM_ID in "${PLAN_ITEM_IDS[@]}"; do
    PLAN_CONTEXT_ARGS+=(--item-id "$PLAN_ITEM_ID")
  done
  if ! "$PYTHON_BIN" "$PLAN_STATE_TOOL" "${PLAN_CONTEXT_ARGS[@]}" >"$PLAN_CONTEXT_FILE"; then
    if [[ "$SCOPED_PLAN_LEDGER" -eq 1 ]]; then
      printf 'scoped plan state context generation failed for %s\n' "$TARGET_DATE" >&2
      update_status --state failed --reason scoped_plan_context_failed
      RUN_COMPLETED=1
      exit 1
    fi
    printf 'plan state context generation failed for %s\n' "$TARGET_DATE" >"$PLAN_CONTEXT_FILE"
  fi
fi

if [[ -x "$EVIDENCE_RUNNER" || -f "$EVIDENCE_COLLECTOR" ]]; then
  EVIDENCE_ARGS=("$TARGET_DATE" "--output" "$EVIDENCE_FILE" "--summary")
  if [[ "$USE_PLAN_LEDGER" -ne 1 ]]; then
    EVIDENCE_ARGS+=("--skip-long-tasks")
  fi
  for PLAN_ITEM_ID in "${PLAN_ITEM_IDS[@]}"; do
    EVIDENCE_ARGS+=("--plan-id" "$PLAN_ITEM_ID")
  done
  if [[ -x "$EVIDENCE_RUNNER" ]]; then
    EVIDENCE_CMD=("$EVIDENCE_RUNNER" "${EVIDENCE_ARGS[@]}")
  else
    EVIDENCE_CMD=("$PYTHON_BIN" "$EVIDENCE_COLLECTOR" "${EVIDENCE_ARGS[@]}")
  fi
  if "${EVIDENCE_CMD[@]}" >"$EVIDENCE_SUMMARY_FILE" 2>&1; then
    update_status \
      --state preflight \
      --reason evidence_collected \
      --set "evidence.file=$EVIDENCE_FILE" \
      --set "evidence.summary=$EVIDENCE_SUMMARY_FILE"
  else
    printf '[%s] evidence collection failed for %s; continuing without structured evidence\n' "$(date '+%F %T %z')" "$TARGET_DATE" >>"$EVIDENCE_SUMMARY_FILE"
    update_status --state preflight --reason evidence_collection_failed
  fi
else
  printf '[%s] evidence collector not found: %s\n' "$(date '+%F %T %z')" "$EVIDENCE_COLLECTOR" >"$EVIDENCE_SUMMARY_FILE"
  update_status --state preflight --reason evidence_collector_missing
fi

if [[ -f "$OUTCOME_PIPELINE" ]]; then
  set +e
  OUTCOME_JSON="$("$PYTHON_BIN" "$OUTCOME_PIPELINE" --date "$TARGET_DATE" --json 2>>"$LOG_FILE")"
  OUTCOME_RC=$?
  set -e
  if [[ "$OUTCOME_RC" -eq 0 && -n "$OUTCOME_JSON" ]]; then
    printf '%s\n' "$OUTCOME_JSON" >"$OUTCOME_SUMMARY_FILE"
    OUTCOME_RUN_DIR="$(printf '%s\n' "$OUTCOME_JSON" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("run_dir",""))' 2>/dev/null || true)"
    if [[ -n "$OUTCOME_RUN_DIR" && -s "$OUTCOME_RUN_DIR/turn_analysis_input.jsonl" ]]; then
      OUTCOME_INPUT_FILE="$OUTCOME_RUN_DIR/turn_analysis_input.jsonl"
    fi
    update_status \
      --state preflight \
      --reason outcome_pipeline_prepared \
      --set "outcome.summary=$OUTCOME_SUMMARY_FILE" \
      --set "outcome.input=$OUTCOME_INPUT_FILE"
  else
    printf '[%s] outcome pipeline failed for %s rc=%s; continuing without turn bundles\n' "$(date '+%F %T %z')" "$TARGET_DATE" "$OUTCOME_RC" >>"$LOG_FILE"
    update_status --state preflight --reason outcome_pipeline_failed
  fi
else
  printf '[%s] outcome pipeline not found: %s\n' "$(date '+%F %T %z')" "$OUTCOME_PIPELINE" >>"$LOG_FILE"
  update_status --state preflight --reason outcome_pipeline_missing
fi

PROMPT="使用 \$ai-daily-report 生成 ${TARGET_DATE} 的中文 Codex/AI 工作日报。当前 wrapper 运行日期是 ${TODAY_DATE}，target_mode=${TARGET_MODE}；正文中的“今日/当日/本日”都必须指日报目标日期 ${TARGET_DATE}，不是 cron 实际运行日期。必须读取 skill 的 report-rules、report-template 和 scheduled-run references；默认只汇总 Codex。scheduled wrapper 已经完成 workday、plan、evidence、Jira 和 workspace git 状态收集；请信赖这些结构化输入，不要重新执行 collect_evidence.py、check_workday.py、plan_state.py、python -m py_compile、git status/log/branch/rev-parse、Jira CLI、SSH/SFTP 或任何写文件命令。只允许读取规则文件、plan context、evidence 文件以及 evidence 明确指向的本地文本来源；如果 evidence 缺少信息，把缺口写入风险或来源说明，不要自行补跑采集命令。不要把本次 wrapper 运行、证据口径、日报规则或报告生成过程写成 ${TARGET_DATE} 的工作成果；除非结构化 outcome/evidence/git commit 明确显示这些工作实际发生在 ${TARGET_DATE}。新建 Task ID 和 Track ID 的日期前缀必须基于日报目标日期 ${TARGET_DATE}；Task ID 仅用于长期/阶段性任务，Track ID 使用 TRK-YYYYMMDD-N，仅用于日常未闭环跟踪事项。最终答复必须是可直接保存为 Markdown 文件的日报正文，不要加解释性前后文，不要使用代码块包裹；wrapper 会负责保存到 ${REPORT_FILE}，并仅在配置启用时执行远端同步。"
PROMPT="${PROMPT} 项目进展只能使用已注册且 active 的项目；如果没有可用项目证据，项目进展 section 保留表头即可，不要写“暂无证据”这类占位项目行。"
if [[ "$USE_PLAN_LEDGER" -eq 1 && -s "$PLAN_CONTEXT_FILE" ]]; then
  PROMPT="${PROMPT} 在撰写前必须读取 ${PLAN_CONTEXT_FILE}，这是计划主账本生成的精简上下文；Long-Term Tasks 用于项目进展，Tracking Items 用于跟踪事项。不要输出单独的前次计划回顾或下一步计划；旧事项和新事项统一写入跟踪事项，通过 ID 日期区分。项目进展必须放在跟踪事项前面，只能围绕 Registered Projects 列出的活跃项目和长期任务组织，不要临时发散新项目；项目进展必须按任务/指标维度写清目标指标、最新进展、验证状态和下一步计划；日报末尾 ai-daily-state 需要写 task_updates、next_long_tasks、tracking_updates 和 next_tracking。"
  if [[ "$SCOPED_PLAN_LEDGER" -eq 1 ]]; then
    PLAN_ID_LIST=""
    for PLAN_ITEM_ID in "${PLAN_ITEM_IDS[@]}"; do
      if [[ -n "$PLAN_ID_LIST" ]]; then
        PLAN_ID_LIST+=", "
      fi
      PLAN_ID_LIST+="$PLAN_ITEM_ID"
    done
    PROMPT="${PROMPT} 本次是按 ID 限定的历史账本运行，允许读取和回写的 ID 只有：${PLAN_ID_LIST}。不要创建或输出名单外的 Task/Track ID。"
  fi
fi
if [[ -s "$EVIDENCE_FILE" ]]; then
  PROMPT="${PROMPT} 在撰写前必须优先读取 ${EVIDENCE_FILE}，这是脚本收集的结构化证据；可先看 ${EVIDENCE_SUMMARY_FILE} 了解覆盖范围。Jira 证据只允许使用 evidence.jira.created_on_target_date 和 evidence.jira.closed_on_target_date；不要把对话中提到但并非目标日期创建/关闭的 Jira 写成当日进展或跟踪事项。TRK 历史只使用 plan context 和 evidence.plan_state.active_tracking_items 作为待判断的历史列表；是否新增、更新或关闭 TRK 由你根据 outcome 证据判断。项目进展和做了哪些事必须按 Registered Projects 或 evidence.long_tasks.projects 中的 report_profile.focus/signals 筛选和排序：每个项目都必须严格使用其 report_profile.focus/signals 决定证据优先级，不得使用未配置的项目特例。不要因为某类 evidence 字段存在就写入正文；只有当 target-day outcome、git、long_tasks 或 report_profile 明确命中时才使用。必须增加 token使用 section，使用 evidence.token_usage.by_agent 按 agent 列出当日 token 消耗，只显示 total_tokens > 0 的 agent，没有记录时写暂无 token 使用记录；有记录时表尾必须用 evidence.token_usage.total 增加总计行；token 数不要写精确整数，>=1亿用亿，否则用万，最多保留小数点后2位并去掉末尾0，例如14.35亿、305万、0.48万；最后一列必须叫 session/subagent/approvals，内容用 owner_count/subagent_count/approval_count，例如2/4/1；权限审批 token 已由 usage-mgr 计入对应 agent，不要单独列出权限审批行；token 使用表的 Markdown 源码必须按列补空格对齐，Agent 左对齐，其他列右对齐。来源 section 中 Codex sessions 和 Repositories 只写数量，Memory files 只写文件名。"
  if [[ "$USE_PLAN_LEDGER" -eq 1 ]]; then
    PROMPT="${PROMPT} 长期任务以 evidence.long_tasks 为准做定向跟踪；项目进展优先使用 evidence.long_tasks.projects 聚合结果；只写一到两句成果性/状态性进展，不要复述匹配片段或命令过程。"
  else
    PROMPT="${PROMPT} 本次未启用计划主账本；不要从当前 plan_state 或 evidence.long_tasks 推断项目进展，避免历史日期被当前长期任务污染。"
  fi
fi
if [[ -n "$OUTCOME_INPUT_FILE" ]]; then
  PROMPT="${PROMPT} 若 evidence 对当日实际工作进展描述不足，必须读取 ${OUTCOME_INPUT_FILE} 作为补充；这是 outcome pipeline 从原始 transcript 提取的 bounded turn bundle 输入，配套摘要在 ${OUTCOME_SUMMARY_FILE}。只从这些 turn 中提炼 mission/result/decision/blocker/follow_up，不要把生成日报本身当作业务成果。"
fi

{
  printf '[%s] start ai daily report for %s\n' "$(date '+%F %T %z')" "$TARGET_DATE"
  CURRENT_STAGE="codex_exec"
  update_status --state running_codex --reason start --set codex.started=true
  set +e
  "${CODEX_CMD[@]}" \
    --ask-for-approval never \
    exec \
    --cd "$CODEX_HOME_DIR" \
    --sandbox read-only \
    --skip-git-repo-check \
    --output-last-message "$LAST_MESSAGE" \
    "$PROMPT"
  CODEX_RC=$?
  set -e
  update_status --state running_codex --reason codex_exit --set "codex.exit_code=$CODEX_RC" --set "codex.last_message=$LAST_MESSAGE"
  if [[ "$CODEX_RC" -ne 0 ]]; then
    printf '[%s] codex exec failed rc=%s\n' "$(date '+%F %T %z')" "$CODEX_RC" >&2
    update_status --state failed --reason codex_exec_failed
    RUN_COMPLETED=1
    exit "$CODEX_RC"
  fi
  if [[ ! -s "$LAST_MESSAGE" ]]; then
    printf '[%s] codex produced no final report output\n' "$(date '+%F %T %z')" >&2
    update_status --state failed --reason empty_codex_output
    RUN_COMPLETED=1
    exit 1
  fi

  CURRENT_STAGE="local_report"
  cp "$LAST_MESSAGE" "$REPORT_FILE"
  if [[ -f "$REPORT_NORMALIZER" ]]; then
    "$PYTHON_BIN" "$REPORT_NORMALIZER" "$REPORT_FILE"
  fi
  update_status --state generated --reason report_written

  if rg -n -i "$SENSITIVE_PATTERN" "$REPORT_FILE" >/dev/null; then
    printf '[%s] sensitive content detected in %s; keeping local file and skipping sync\n' "$(date '+%F %T %z')" "$REPORT_FILE" >&2
    update_status --state validation_failed --reason sensitive_content --set validation.passed=false --set 'validation.errors=["sensitive_content"]'
    RUN_COMPLETED=1
    exit 1
  fi
  update_status --state generated --reason sensitive_scan_passed --set validation.passed=true
  if [[ -f "$REPORT_VALIDATOR" ]]; then
    if "$PYTHON_BIN" "$REPORT_VALIDATOR" "$REPORT_FILE"; then
      update_status --state generated --reason report_validation_passed --set validation.report_schema=true
    else
      printf '[%s] report validation failed for %s\n' "$(date '+%F %T %z')" "$REPORT_FILE" >&2
      update_status --state validation_failed --reason report_validation_failed --set validation.passed=false --set validation.report_schema=false
      RUN_COMPLETED=1
      exit 1
    fi
  fi
  if [[ "$USE_PLAN_LEDGER" -eq 1 && -f "$PLAN_STATE_TOOL" ]]; then
    PLAN_IMPORT_ARGS=(import-report "$REPORT_FILE" --date "$TARGET_DATE" --replace-active)
    for PLAN_ITEM_ID in "${PLAN_ITEM_IDS[@]}"; do
      PLAN_IMPORT_ARGS+=(--item-id "$PLAN_ITEM_ID")
    done
    if "$PYTHON_BIN" "$PLAN_STATE_TOOL" "${PLAN_IMPORT_ARGS[@]}"; then
      update_status --state generated --reason plan_state_updated
    else
      if [[ "$SCOPED_PLAN_LEDGER" -eq 1 ]]; then
        printf '[%s] scoped plan state import failed for %s; keeping local report and skipping sync\n' "$(date '+%F %T %z')" "$REPORT_FILE" >&2
        update_status --state failed --reason scoped_plan_state_update_failed
        RUN_COMPLETED=1
        exit 1
      fi
      printf '[%s] plan state import failed for %s; continuing sync\n' "$(date '+%F %T %z')" "$REPORT_FILE" >&2
      update_status --state generated --reason plan_state_update_failed
    fi
  else
    printf '[%s] plan state import skipped for %s mode=%s today=%s\n' "$(date '+%F %T %z')" "$TARGET_DATE" "$PLAN_LEDGER_MODE" "$TODAY_DATE"
  fi

  if [[ "$SYNC_MODE" == "0" || "$SYNC_MODE" == "false" ]]; then
    if [[ -f "$RUN_CLEANUP" ]]; then
      bash "$RUN_CLEANUP" "$TARGET_DATE"
      update_status --state generated --reason cleanup_completed --set cleanup.completed=true
    fi
    printf '[%s] finished ai daily report for %s in local-only mode\n' "$(date '+%F %T %z')" "$TARGET_DATE"
    update_status --state success --reason local_only --set sync.attempted=false --set sync.passed=false
    RUN_COMPLETED=1
    exit 0
  fi

  if [[ -z "$REMOTE_USER" || -z "$REMOTE_HOST" || -z "$REMOTE_DIR" ]]; then
    printf '[%s] remote sync is enabled but remote.user, remote.host, or remote.dir is empty\n' "$(date '+%F %T %z')" >&2
    update_status --state failed --reason remote_config_incomplete --set sync.attempted=false --set sync.passed=false
    RUN_COMPLETED=1
    exit 1
  fi

  CURRENT_STAGE="sync"
  update_status --state syncing --reason start --set sync.attempted=true
  printf 'put %s "%s"\n' "$REPORT_FILE" "$REMOTE_FILE" >"$BATCH_FILE"
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE_USER@$REMOTE_HOST" "mkdir -p \"$REMOTE_MONTH_DIR\""
  sftp -o BatchMode=yes -o ConnectTimeout=5 -b "$BATCH_FILE" "$REMOTE_USER@$REMOTE_HOST"
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE_USER@$REMOTE_HOST" "ls -l \"$REMOTE_FILE\""
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE_USER@$REMOTE_HOST" "sed -n '1,80p' \"$REMOTE_FILE\""
  if [[ -f "$RUN_CLEANUP" ]]; then
    bash "$RUN_CLEANUP" "$TARGET_DATE"
    update_status --state syncing --reason cleanup_completed --set cleanup.completed=true
  else
    printf '[%s] cleanup script not found: %s\n' "$(date '+%F %T %z')" "$RUN_CLEANUP" >&2
    update_status --state syncing --reason cleanup_script_missing --set cleanup.completed=false
  fi
  printf '[%s] finished ai daily report for %s\n' "$(date '+%F %T %z')" "$TARGET_DATE"
  update_status --state success --reason synced --set sync.passed=true
  RUN_COMPLETED=1
} >>"$LOG_FILE" 2>&1
