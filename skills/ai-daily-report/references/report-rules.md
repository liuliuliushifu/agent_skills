# AI 日报规则

本文件是 AI 日报 workflow 的唯一规则源。`$AI_DAILY_REPORT_DIR/AI_DAILY_RULES.md` 和 Obsidian 中的 `日报规则.md` 只是导出或指针，不应成为另一份手工维护的规则副本。

## 目标

每天将 Codex 当日工作整理成一篇 Markdown 笔记，并可选择同步到配置的远端目录。日报不是聊天记录转储，而是面向复盘的工作摘要。

## 输出位置

- 远端目录：由 `remote.dir` 或 `AI_DAILY_REMOTE_DIR` 配置；默认不启用远端同步。
- 远端布局：`YYYYMM/YYYY-MM-DD.md`
- 文件名：`YYYY-MM-DD.md`
- 本地备份目录：`$AI_DAILY_REPORT_DIR/report_files/YYYYMM`

## 语言规则

- 使用中文撰写。
- 允许保留必要英文词汇，例如 API、SDK、commit、branch、token、SSH、Obsidian、Codex。
- 命令、文件路径、函数名、宏名、日志关键字、错误码必须按原文保留。
- 不机械翻译技术名词；优先保证准确。

## 排版与风格

- 日报要像复盘文档，不像操作流水账。
- 顶部使用 Obsidian callout 写一屏概览：
  - `> [!summary] 今日概览`
  - 概览只保留 `关键成果`、`主要风险`、`下一步` 三项。
- `做了哪些事` 使用成果导向写法：
  - 主体使用有序列表，每条以加粗主题开头，例如 `1. **日报链路**：...`。
  - 描述结果和价值，不展开排查过程、命令尝试顺序或中间对话细节。
  - 文件、命令、路径只作为必要证据出现；不要把执行过程写成清单。
  - 不要把生成本日报的 wrapper、证据口径、prompt 约束或报告生成过程写成目标日期的工作成果；除非目标日期的 outcome/evidence/git commit 明确显示当天实际修改了日报链路。
- `学习到了哪些事` 使用有序列表：
  - 每条以加粗主题开头，例如 `1. **性能数据口径**：...`。
  - 记录可复用结论、方法或约定，不写泛泛感想。
- `token使用` 放在 `学习到了哪些事` 后、`风险与阻塞` 前：
  - 使用表格列出 `Agent`、`总 token`、`输入 token`、`缓存输入 token`、`输出 token`、`推理输出 token`、`session/subagent/approvals`。
  - token 数字不要显示精确整数；按近似单位展示，`>= 1 亿` 用 `亿`，否则用 `万`，最多保留小数点后 2 位并去掉末尾 0，例如 `14.35亿`、`305万`、`0.48万`。
  - `session/subagent/approvals` 使用 `owner_count/subagent_count/approval_count`，例如 `2/4/1`；`session` 表示 owner session 数，`subagent` 表示业务 subagent session 数，`approvals` 表示权限审批 guardian 次数。
  - Markdown 源码中的表格必须按列补空格对齐：`Agent` 左对齐，其他列右对齐，分隔行保留右对齐冒号。
  - 数据来源优先使用结构化证据包 `evidence.token_usage.by_agent` 和 `evidence.token_usage.total`；证据包只允许来自 usage ledger，不允许使用 Stop-hook probe fallback。
  - agent 归属由 usage-mgr 按 co-agent registry 的 cwd/name/alias 归并；外部固定 agent 记录（如 `ReMe`）保留 usage ledger 原本的 agent 名，未命中 co-agent 的记录保留原始 agent 名或 `unknown`。
  - 日报生成自身的 `$ai-daily-report` `codex exec` 记录由 usage-mgr 固定显示为 `Daily Report`，不要按 cwd 归并到 `skill agent`。
  - 权限审批 guardian 开销由 usage-mgr 计入对应 owner agent，不单独展示权限审批 agent 行；日报只展示 approvals 计数。
  - 只显示日报目标日期有 token ledger 记录且 `total_tokens > 0` 的 agent；当日没有产生对话/token 的 agent 不显示。
  - 表格最后增加 `总计` 行，使用 `evidence.token_usage.total` 汇总当天全部 token 消耗。
  - 如果当天没有可用 token 记录，写 `暂无 token 使用记录。`
- `风险与阻塞` 必须突出优先级：
  - 使用表格列出 `优先级`、`状态`、`问题`、`影响`、`下一步`。
  - `P0` 表示当前阻塞，`P1` 表示高风险或需要尽快处理，`P2` 表示普通风险或观察项。
  - 如果没有 `P0/P1`，明确写 `暂无 P0/P1 阻塞`。
- `来源` 使用折叠 callout，避免影响正文阅读：
  - `> [!info]- 来源`
  - 默认只列 Codex sessions、Memory files、Repositories。
  - 不暴露 `Other artifacts` 或内部辅助文件清单；必要文件应在正文成果中自然提及，而不是作为来源单独展开。
- 报告末尾必须追加折叠的 `ai-daily-state` callout：
  - 标题固定为 `> [!info]- ai-daily-state`，默认收起，避免正文阅读时直接展示机器数据。
  - JSON 放在 callout 内的 `json` 代码块中，每一行都必须保持 callout 引用前缀 `>`。
  - 内容必须是严格 JSON，不写 JSON 注释，不留尾逗号。
  - 长期/阶段性任务字段使用 `task_id` 和 `text`；`task_id` 格式固定为 `YYYYMMDD-N`，例如 `20260507-1`。
  - 日常未闭环跟踪事项字段使用 `track_id` 和 `text`；`track_id` 格式固定为 `TRK-YYYYMMDD-N`，例如 `TRK-20260507-1`。
  - `task_updates` 和 `next_long_tasks` 只记录长期/阶段性任务。
  - `tracking_updates` 和 `next_tracking` 只记录日常跟踪事项。
  - 不要在机器可读 JSON 中写入敏感信息。
- 计划主账本：
  - `$AI_DAILY_REPORT_DIR/plan_state.json` 是长期事实账本，保留 `done` 和 `dropped` 记录。
  - 读取计划上下文时使用 `scripts/plan_state.py context --date YYYY-MM-DD`，不要把完整 JSON 直接塞进上下文。`$AI_DAILY_REPORT_DIR/plan_state.py` 只作为兼容入口。
  - 精简上下文分成 `Long-Term Tasks` 和 `Tracking Items` 两张表。
  - `Long-Term Tasks` 进入 `项目进展`，描述阶段性成果和指标变化。
  - `Tracking Items` 进入 `跟踪事项`，合并旧事项和新事项；新旧通过 ID 日期判断，不再拆成“前次计划回顾”和“下一步计划”两个 section。
  - 历史回填默认不读写主账本。需要账本上下文时，为每个既有事项传入 `--plan-id ID`；脚本用同一 ID 集合限制 context、evidence 和 import-report，禁止写回名单外事项。
- `项目进展` 放在 `今日概览` 后、`跟踪事项` 前：
  - 只能使用注册项目下的任务/指标视角说明“指标目标、最新进展、验证状态、下一步计划”，避免只写当天做了哪些动作。
  - 必须使用表格列出 `注册项目`、`Task ID`、`任务/指标`、`目标指标/目标状态`、`最新进展`、`验证状态`、`下一步计划`。
  - 本节不能使用 bullet、段落、三级标题或临时专题来补充未注册项目；未注册事项应放入 `做了哪些事`、`风险与阻塞` 或先登记为注册项目。
  - 任务/指标建议写具体能力或指标，例如 `接收性能优化`、`吞吐达到目标值`、`完整回归通过`。
  - 注册项目可以定义 `report_profile.focus` 和 `report_profile.signals`。日报应按项目 profile 选择证据和排序，不要把某个具体 skill、daemon 或工具字段写成永久关注点。
  - 每个项目的关注点必须来自其 `report_profile` 配置，不得把某个私有项目类型固化到规则中。
  - 验证状态建议使用 `设计中`、`实现中`、`待编译`、`待验证`、`验证中`、`已验证`、`阻塞`。
  - 用户显式提供的项目总结是高优先级输入，必须和会话、git、Jira 证据合并后写入这里。
  - 例如性能调优应体现代码重构、热路径收敛、查表次数下降、缓存或内存优化以及验证状态，而不是只列提交或命令。
- `跟踪事项` 放在 `项目进展` 后、`做了哪些事` 前：
  - 使用表格列出 `跟踪 ID`、`关联 Task ID`、`事项`、`状态`、`今日进展/原因`、`下一步/完成判据`、`优先级`。
  - `跟踪 ID` 使用 `TRK-YYYYMMDD-N`；这是日常未闭环事项的稳定 ID。
  - `状态` 只能使用 `已完成`、`部分完成`、`未推进`、`调整/取消`、`阻塞`。
  - `下一步/完成判据` 必须写清下一动作和做到什么算完成。

## 必须收集的数据源

- 当日所有 Codex 会话记录：`$CODEX_HOME/sessions/YYYY/MM/DD/*.jsonl`
- 当前仍在进行的会话：如果尚未完整落盘，需要结合当前对话上下文补齐。
- 当日相关记忆文件：
  - `$CODEX_HOME/memories/*.md`
  - `$CODEX_HOME/memories/**/*.md`
  - `$CODEX_HOME/memories/reme-memory/memory_workflow_events.jsonl`
  - `$CODEX_HOME/memories/reme-memory/handoffs/*`
- 当日相关仓库状态：
  - 任务涉及的 git 仓库分支、提交、工作区变更。
  - 只记录与当日工作有关的仓库，不机械列出无关仓库。
  - scheduled wrapper 运行时由 `collect_evidence.py` 收集 workspace 下仓库状态；LLM 只读取 evidence 结果，不再自行执行 `git status`、`git log`、`git branch` 或 `git rev-parse`。
- 计划主账本精简上下文：
  - `scripts/plan_state.py context --date YYYY-MM-DD`
  - 正常同日 scheduled wrapper 会预先写入 `$AI_DAILY_REPORT_DIR/logs/plan-state-YYYY-MM-DD.md`，可优先读取该文件；历史回填默认跳过主账本上下文。
- 结构化证据包：
  - 正常 scheduled wrapper 会预先执行 `scripts/collect_evidence.py YYYY-MM-DD --output $AI_DAILY_REPORT_DIR/logs/evidence-YYYY-MM-DD.json --summary`。
  - 报告生成时优先读取 `$AI_DAILY_REPORT_DIR/logs/evidence-YYYY-MM-DD.json`；`logs/evidence-YYYY-MM-DD.summary.txt` 只用于快速了解覆盖范围。
  - 证据包中的 Jira 只表示目标日期当天创建或关闭的事项：`jira.created_on_target_date` 可作为当日新建 Jira 证据，`jira.closed_on_target_date` 可作为当日闭环 Jira 证据。对话里提到但不是目标日期创建/关闭的 Jira 不应写入当日进展。
  - 证据包中的 `plan_state.active_tracking_items` 是历史 TRK 列表，只作为 LLM 判断新增、更新或关闭跟踪事项的上下文；脚本不再把未关闭 Jira 自动转成 TRK。
  - 证据包中的运行类字段（例如 memory/refine/capture 运行证据）只是结构化上下文。只有当目标日期 outcome、git、long task 或 project `report_profile` 明确命中时，才把它写入项目进展或做了哪些事；普通例行运行噪声不应被夸大成工程成果。
  - 证据包中的 `long_tasks` 是长期任务的定向证据。使用它时只能提炼成果性进展，不要引用大段片段；优先把结论写入 `项目进展` 或 `跟踪事项`，只在存在明确影响时升级到 `风险与阻塞`。
  - 证据包中的 `token_usage.by_agent` 和 `token_usage.total` 是 `token使用` 章节的数据源；只列出其中 `total_tokens > 0` 的 agent，并在表尾写 `总计`。
  - 证据包在 scheduled run 中是可信黑盒输入；不要检查或执行 `collect_evidence.py`、`check_workday.py`、`plan_state.py`，不要执行 `python -m py_compile`，也不要用 Jira CLI 或 git 命令重新采集。信息缺失时写入 `风险与阻塞` 或 `来源`，不要临时补跑采集逻辑。
  - 证据包是临时 prompt 输入；日报成功 SFTP 同步并读回验证后，由 `scripts/cleanup_daily_report_run.sh` 删除，失败时保留用于诊断。`$AI_DAILY_REPORT_DIR/cleanup_daily_report_run.sh` 只作为兼容入口。

## 可选数据源

- Cursor `agent-transcripts` 默认不纳入 AI 日报。
- 只有在用户明确要求“跨 agent 汇总”、或调度配置显式启用 Cursor 输入时，才读取 `$CURSOR_HOME/projects/<project>/agent-transcripts`。
- 如果启用 Cursor，项目列表以 `$AI_DAILY_REPORT_DIR/config.json` 的 `cursor_projects` 为准，并在 `来源` 中列明。

## 记忆文件筛选

- 优先收集当日新建或修改的记忆文件。
- 对长期项目记忆，即使不是当日修改，只要当日任务依赖它，也要纳入摘要来源。
- ReMe bus 的大量中间 JSON/status 文件一般不直接写入日报；只有在调试 memory daemon 或请求链路时才摘要引用。
- 不把 daemon pid、临时锁文件、纯运行日志当作核心内容，除非它们解释了当天的问题。

## 核心内容要求

- `跟踪事项`：使用计划主账本精简上下文里的 `Tracking Items`，并合并 evidence 中新发现的未完成 Jira 或用户明确要求跟踪的事项。不要再输出单独的 `前次计划回顾` 或 `下一步计划` section。`track_id` 日期即可看出事项首次进入跟踪的时间；旧事项、新事项都放在同一张表里。没有可用跟踪事项时写 `暂无未闭环跟踪事项。`
- `项目进展`：只围绕注册项目下的长期任务/阶段性任务写，不按当天零散事项临时发散新项目。注册项目来自 `$AI_DAILY_REPORT_DIR/projects.json` 或 `scripts/projects.json`，scheduled run 中优先看 plan context 的 `Registered Projects` 和 evidence 的 `long_tasks.projects`。每一行必须落到任务/指标级别，写清“目标指标/目标状态、最新进展、验证状态、下一步计划”。例如：`RX 性能优化 | 目标 ACK/Provision 1280k pps | 最新进展 xxx | 下一步 xxx`。
- `项目和任务关系`：长期任务属于项目内部的当前焦点，使用 `task_id` 记录；跟踪事项属于日常未闭环工作，使用 `track_id` 记录。没有挂到注册项目的零散事项可以在 `做了哪些事`、`跟踪事项` 或 `风险与阻塞` 简要记录，但不要升格为项目。
- `长期任务跟踪`：从结构化证据包的 `long_tasks` 读取脚本定向匹配结果。长期任务的进展只写一到两句“成果性/状态性”描述，例如性能达到什么水平、能力完成到什么阶段、验证还差什么；不要把 session 片段、命令、文件改动流水展开成过程日志。没有明确成果时写“未看到新的成果性进展”，并在 `项目进展` 里保留下一步计划，必要时关联 `跟踪事项`。
- `强结果数据优先`：当 evidence、记忆或用户补充中出现明确的量化结果、性能矩阵、拐点结论、通过率、PPS/Entry-s、错误数归零、回归结果等强数据时，必须优先写入 `今日概览`、`项目进展.最新进展` 或 `做了哪些事` 的结果描述；不要只写“完成策略梳理”“推进验证”等弱过程描述。若 evidence.long_tasks.tasks[].metric_tables 存在，优先使用其中的完整矩阵数据。若同一主题有 3 行以上可比较数据，至少保留关键行、峰值、分界点和验证口径，例如 `4-entry V3 高于 V1`、`64-entry 恢复到 xxx pps`、`127-entry 达到 xxM Entry/s`。
- `做了哪些事`：按项目或主题归类，使用 `1. 2. 3.` 有序列表输出；每项突出可复用成果、交付物和当前状态，避免详细过程。
- `学习到了哪些事`：使用 `1. 2. 3.` 有序列表输出；记录技术结论、排障经验、项目约定、用户偏好、后续可复用的方法。
- `token使用`：按 co-agent registry 可识别的 agent 汇总当日 token 消耗；未命中 co-agent 的记录按原始 agent 名或 `unknown` 显示；只显示当日有记录的 agent，不列出 0 token agent；token 数量用 `亿/万` 近似展示；`session/subagent/approvals` 用 `owner_count/subagent_count/approval_count`；表尾必须有 `总计`。
- `Jira 跟踪`：只从结构化证据包读取目标日期当天创建或关闭的 Jira。`jira.created_on_target_date` 可写为新建事项或背景证据；`jira.closed_on_target_date` 可写为闭环成果。历史未闭环事项来自计划主账本的 Tracking Items 或 `evidence.plan_state.active_tracking_items`，是否新增、更新或关闭由 outcome 证据决定。
- `风险与阻塞`：优先记录 `P0/P1`，再记录 `P2`；每项都要有影响和下一步。
- `来源`：折叠展示摘要，不展开长路径清单。Codex sessions 和 Repositories 只写数量，例如 `Codex sessions: 6 个`、`Repositories: 6 个`；Memory files 写文件名，例如 ``Memory files: `2026-05-15.md` ``。不列普通 artifacts。
- `ai-daily-state`：折叠 callout JSON 中的 `task_updates[].task_id` 覆盖项目进展中的长期任务；`tracking_updates[].track_id` 和 `next_tracking[].track_id` 覆盖正文 `跟踪事项` 的有效行动项。

## 计划闭环规则

- 主账本优先：
  - 主账本路径：`$AI_DAILY_REPORT_DIR/plan_state.json`。
  - 日报生成前优先读取 `plan_state.py context --date YYYY-MM-DD` 的输出；该输出把 `Long-Term Tasks` 和 `Tracking Items` 分开。
  - 该输出还包含 `Registered Projects`；`项目进展` 只能围绕这些 active project 下的长期任务/阶段性任务组织。
  - 不直接把完整主账本 JSON 作为日报上下文，避免长期完成项造成噪音。
  - 正常同日 scheduled wrapper 会在报告通过敏感扫描后执行 `plan_state.py import-report $AI_DAILY_REPORT_DIR/report_files/YYYYMM/YYYY-MM-DD.md --date YYYY-MM-DD --replace-active`，用本次报告的 `ai-daily-state.task_updates`、`next_long_tasks`、`tracking_updates` 和 `next_tracking` 更新主账本。
  - 历史回填默认不读写主账本，避免旧报告覆盖当前未闭环事项。需要账本上下文时，使用 `run_ai_daily_report.sh YYYY-MM-DD --plan-id ID [--plan-id ID ...]`；未知 ID 或报告中的名单外 ID 必须失败，不得静默写回。
  - 因此 `task_updates` 必须覆盖本次涉及的长期 Task ID；`tracking_updates` 和 `next_tracking` 必须覆盖仍需后续跟进的新旧 `track_id`。
- 前次日报选择：
  - 在 `$AI_DAILY_REPORT_DIR/report_files` 中递归查找目标日期之前最近的 `YYYY-MM-DD.md`。
  - 只使用已经生成的日报；不要因为中间缺少非工作日日报而在 `风险与阻塞` 中报错。
  - 如果主账本和前次日报都没有可跟踪 ID，写 `暂无未闭环跟踪事项。`
  - 优先读取前次日报末尾 `> [!info]- ai-daily-state` callout 中的 `task_updates`、`next_long_tasks`、`tracking_updates` 和 `next_tracking`。
  - 只读取折叠 `> [!info]- ai-daily-state` callout；当前 skill 仍在开发阶段，不保留旧版 HTML 注释块兼容逻辑。
- 跟踪事项判断：
  - `已完成`：前次计划在当日已有明确结果或验证闭环。
  - `部分完成`：有实质推进，但仍有剩余验证、提交、同步或落地动作。
  - `未推进`：当日没有看到实质动作，需说明原因，例如优先级调整、等待环境、时间不足。
  - `调整/取消`：计划本身被新信息改变或不再需要，需说明调整依据。
  - `阻塞`：计划因明确外部条件、权限、网络、构建失败或设备不可用无法推进。
- 风险联动：
  - 连续多次 `未推进` 且影响明确的事项，应进入 `风险与阻塞`，通常标为 `P2`。
  - 影响自动日报、构建验证、数据同步或关键代码闭环的阻塞，按影响升级为 `P1` 或 `P0`。
  - 不要把所有未完成计划都写成风险；只有存在影响或阻塞条件时才升级。

## 机器可读状态块

每篇日报末尾必须追加一个默认折叠的 Obsidian callout，格式如下：

```markdown
> [!info]- ai-daily-state
> ```json
> {
>   "version": 1,
>   "date": "YYYY-MM-DD",
>   "task_updates": [
>     {
>       "task_id": "YYYYMMDD-N",
>       "text": "长期任务或阶段性任务内容",
>       "status": "done",
>       "priority": "P1",
>       "completed_date": "YYYY-MM-DD",
>       "source": "carryover",
>       "project": "glb_dev_validation",
>       "metric": "任务级指标名，例如 RX ACK/Provision 性能",
>       "target_metric": "目标值或目标状态，例如 1280k pps",
>       "note": "状态变化或保留原因"
>     }
>   ],
>   "tracking_updates": [
>     {
>       "track_id": "TRK-YYYYMMDD-N",
>       "text": "跟踪事项内容",
>       "status": "open",
>       "priority": "P1",
>       "source": "carryover",
>       "project": "glb_dev_validation",
>       "related_task_id": "YYYYMMDD-N",
>       "completion_criteria": "完成判据"
>     }
>   ],
>   "next_tracking": [
>     {
>       "track_id": "TRK-YYYYMMDD-N",
>       "text": "下一轮仍需跟踪的事项",
>       "status": "open",
>       "priority": "P1",
>       "source": "risk",
>       "project": "glb_dev_validation",
>       "related_task_id": "YYYYMMDD-N",
>       "completion_criteria": "完成判据"
>     }
>   ],
>   "next_long_tasks": [
>     {
>       "task_id": "YYYYMMDD-N",
>       "text": "下一轮继续跟踪的长期任务",
>       "status": "in_progress",
>       "priority": "P1",
>       "source": "carryover",
>       "project": "glb_dev_validation",
>       "metric": "任务级指标名",
>       "target_metric": "目标值或目标状态",
>       "tracking": {
>         "mode": "long_term",
>         "progress_style": "outcome"
>       }
>     }
>   ]
> }
> ```
```

字段规则：

- `version` 固定为 `1`。
- `date` 必须等于目标日期。
- `task_updates` 记录本次日报已经更新的长期/阶段性 Task ID，包括完成、取消、继续阻塞、延后或仍在推进的事项。
- `task_updates[].completed_date` 仅在 `status` 为 `done` 或 `dropped` 时填写，表示闭环日期。
- `next_long_tasks` 记录下一轮仍需继续跟踪的长期/阶段性任务。
- `tracking_updates` 记录本次日报已经更新的日常跟踪事项。
- `next_tracking` 记录下一轮仍需后续跟进的日常跟踪事项。
- `task_id` 使用 `YYYYMMDD-N` 数字格式，例如 `20260507-1`；`YYYYMMDD` 是任务首次进入主账本的日期，`N` 是当天新增任务序号。
- `track_id` 使用 `TRK-YYYYMMDD-N` 格式，例如 `TRK-20260507-1`；用于日常未闭环跟踪事项。
- `text` 是任务或跟踪事项的中文具体内容；正文、`task_updates`、`tracking_updates`、`next_tracking` 都必须输出对应 ID 加 Text。
- `status` 只能使用 `open`、`in_progress`、`blocked`、`deferred`、`done`、`dropped`；`next_tracking` 中通常只输出未闭环状态，完成或取消项放入 `tracking_updates`。
- `priority` 使用 `P0`、`P1`、`P2`。
- `source` 使用 `report`、`risk`、`carryover`、`user_request`、`jira` 中的一个；旧账本里的 `next_plan` 仅作为兼容来源保留，不要在新日报中继续输出。
- 可选字段：`area`、`target_date`、`note`、`tracking`；只在能提升后续跟踪质量时填写，避免把机器状态块写得过长。
- 可选字段 `project` 应使用注册项目 key，例如 `glb_dev_validation`、`codex_skill_management`。长期任务必须尽量带 `project`，否则脚本会按别名和关键词推断。
- 长期任务可在 `tracking` 中写入 `mode: "long_term"`、`progress_style: "outcome"`、`keywords`、`jira_keys`、`repo_paths`、`evidence_patterns`、`outcome_hint`。`progress_style: "outcome"` 表示后续日报只更新成果性进展，不写过程流水。

## 写作质量要求

- 先综合，再列证据；避免逐条复制聊天原文。
- 对同一主题的多轮会话要合并成一个结论。
- 对失败和绕路要保留工程价值：失败原因、如何处理、是否留下残余风险。
- 不夸大结果；未完成的事项明确写为未完成。
- 能用一句话说明的事情不要写成长段。
- 每个 section 优先控制在 3 到 6 条，高密度但可扫读。

## 安全与隐私

- 不写入明文密码、token、私钥、cookie、完整认证头。
- 如果对话中出现敏感信息，只写“已配置临时凭据/已建立免密登录”等脱敏描述。
- 不把大段原始日志、完整会话内容、密钥材料复制到 Obsidian。
- 对外部机器路径和内网 IP 可以记录，但不要记录登录密码。
- 源最小化：只摘要和当天工作相关的信息，不复制原始 tool output、完整环境变量、完整配置文件或无关日志。
- 写入前必须对草稿做敏感信息扫描。至少检查这些模式：`password`、`passwd`、`token`、`secret`、`Authorization`、`cookie`、`BEGIN .*PRIVATE KEY`、`AKIA`、`ssh-rsa`、`ssh-ed25519`。
- 扫描命中时，不把命中的原文写入日报；只在 `风险与阻塞` 中写“已发现并脱敏敏感信息”，必要时描述敏感信息类型。

## 验证规则

- 写入 Obsidian 后，必须读回文件前部或检查文件大小，确认上传成功。
- 如果远端不可达，先生成本地日报并在最终回复中说明未同步原因。

## 分步执行规则

远端写入必须拆成本地准备和远端同步两段，避免复杂 shell 命令导致权限规则无法命中。

1. 本地准备：生成 Markdown 文件和 SFTP batch 文件。该阶段只读写 `$CODEX_HOME` 和 `$AI_DAILY_REPORT_DIR` 下文件，不访问网络。
2. 远端建目录：从目标日期派生 `YYYYMM`，使用配置的远端身份单独执行 `mkdir -p`；每次都创建，确保每月第一篇日报无需人工准备目录。
3. 远端上传：使用固定 SFTP 前缀执行 batch 上传。
4. 远端验证：使用相同的配置身份单独执行 `ls` 和 `sed`，必要时再执行 `wc`。

命令必须从配置读取用户、主机和远端目录，例如：

```bash
ssh -o BatchMode=yes -o ConnectTimeout=5 "$AI_DAILY_REMOTE_USER@$AI_DAILY_REMOTE_HOST" \
  "mkdir -p \"$AI_DAILY_REMOTE_DIR/YYYYMM\""
```

```bash
sftp -o BatchMode=yes -o ConnectTimeout=5 \
  -b "$CODEX_HOME/.tmp/report-upload-YYYY-MM-DD.sftp" \
  "$AI_DAILY_REMOTE_USER@$AI_DAILY_REMOTE_HOST"
```

SFTP batch 文件必须使用日期化文件名，例如：

```text
$CODEX_HOME/.tmp/report-upload-YYYY-MM-DD.sftp
```

batch 内容只能包含目标日期的一条 `put`：

```text
put $AI_DAILY_REPORT_DIR/report_files/YYYYMM/YYYY-MM-DD.md "$AI_DAILY_REMOTE_DIR/YYYYMM/YYYY-MM-DD.md"
```

执行 SFTP 前必须确认 batch 中的本地文件名、远端月份目录和远端文件名都匹配目标日期，避免 stale batch 上传错文件。

不要把本地文件生成、远端目录创建、上传和验证合并成一个带管道、重定向、 heredoc 或多个 shell 分隔符的命令。
