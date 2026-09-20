# Agent Skills

一组面向 Codex 的可复用 Skill。仓库中的版本已经移除个人用户名、内部项目名、
本机工作路径和私有服务地址；运行环境相关信息通过环境变量或配置文件提供。

Skill 不只是提示词：`SKILL.md` 负责说明适用场景、工作流和安全边界，`scripts/`
提供可重复执行的确定性操作，`references/` 保存只在特定场景下才需要加载的详细设计。

## Skills

| Skill | 作用 | 基本原理 |
| --- | --- | --- |
| [`reme-memory`](skills/reme-memory/SKILL.md) | 为跨会话任务检索并保存可复用记忆 | 本地文件存储结合向量检索与全文检索；通过 daemon 和请求队列串行写入、增量更新索引 |
| [`temp-files-mgr`](skills/temp-files-mgr/SKILL.md) | 扫描、清理并审计过期临时文件 | 从基础目录、用户和相对子目录拼装扫描根；按保留期、所有权和文件系统边界判定候选项 |
| [`usage-mgr`](skills/usage-mgr/SKILL.md) | 统计 Codex 会话和 agent 的 token 使用量 | Stop/SubagentStop hook 读取 transcript 中的累计计数，计算增量后写入按日账本 |
| [`co-agent`](skills/co-agent/SKILL.md) | 在多个 Codex agent 之间路由任务、交接结果并保留审计记录 | 以 agent registry、goal、run 和 segment 构成状态机，通过脚本驱动 handoff、wait、return 和 process monitor |
| [`ai-daily-report`](skills/ai-daily-report/SKILL.md) | 生成中文 Codex/AI 工作日报，并可选同步到远端笔记目录 | 先汇集会话、记忆、Git、Jira、计划和用量证据，再生成、校验和保存结构化 Markdown 报告 |

## `reme-memory`

### 作用

为需要跨会话连续性的任务提供本地长期记忆，例如历史决策、重复故障的根因、稳定的
解决方法和用户偏好。任务开始前可以检索相关记忆，任务完成后只保存真正可复用的经验。

### 基本原理

1. `prepare` 从 durable memory 和短期 compact memory 中检索候选项。
2. 检索同时使用向量相似度与全文索引，并用有限的时间新鲜度加权和证据链去重调整排序。
3. `finalize` 将写请求送入本地 memory bus，由 daemon 单写者完成存储和增量索引更新。
4. context capture 保存结构化交接证据；async refine 可以把短期证据提炼为长期记忆。
5. 定期维护会归档或删除过期 compact memory，但不会自动触发完整索引重建。

安全边界：不保存密钥、原始大日志、临时猜测或低置信度结论。完整索引重建是独立维护操作，
必须由用户明确批准。

主要配置：`CODEX_HOME`、`REME_HOME`、`REME_WORKDIR`、`REME_ENV_FILE`、
`REME_PYTHON` 和 embedding 相关环境变量。

## `temp-files-mgr`

### 作用

统计临时内容的数量和空间占用，识别过期条目，执行受约束的清理，并保存每次扫描和清理的
审计记录。

### 基本原理

1. 用户临时目录由 `TEMP_FILES_BASE_DIR / TEMP_FILES_USER / TEMP_FILES_USER_TMP_SUBDIR`
   拼装得到，不在代码中固定用户名或用户目录。
2. 普通内容和脚本类内容使用不同保留期；包含未到期脚本文件的目录会继续受到保护。
3. 扫描不跟随符号链接、不跨挂载点，并检查顶层条目及其后代的所有权。
4. 只有完整通过预检的顶层候选项才会被删除；隐藏条目和活跃运行时命名空间会被跳过。
5. 每次运行记录删除数量、释放空间、跳过原因和错误，便于查询历史。

权限错误只会被记录和跳过。定时清理不会自动调用 Docker 或强制删除工具；文档仅提供在
用户明确授权某个精确目标后，通过受限 Docker bind mount 处理权限问题的可选方法。

## `usage-mgr`

### 作用

按日期、原始会话、owner session、工作目录、模型或注册 agent 查询本机 Codex token 用量，
也可以向日报流程提供结构化用量汇总。

### 基本原理

1. Stop/SubagentStop hook 从 Codex transcript 的官方 `token_count` 记录读取累计用量。
2. hook 将本次累计值与保存的 baseline 比较，只把新增部分写入账本，避免重复计数。
3. 子 agent 首次记账时会排除从父会话继承的累计 token，并保留真实 raw session 与
   owner session 的归属关系。
4. 没有持久化 transcript 的 `codex exec --json` 任务，可通过稳定 event id 写入外部用量。
5. 查询脚本聚合 JSONL 账本，而不是让调用者手工解析运行状态文件。

agent 名称只是展示层信息；账本的稳定主键来自真实会话、owner、cwd 和 usage kind。

## `co-agent`

### 作用

在不同目录和职责范围的 Codex agent 之间分派任务、唤醒目标会话、等待业务结果、向上游
返回结果，并查询完整的交接历史。

### 基本原理

1. registry 保存 agent 名称、别名、cwd、角色、`owns` 和 `not_for`，但不捆绑私人 registry。
2. `route` 根据职责信息保守地给出本地处理、交接或询问用户三种结果。
3. 每个协作目标由 goal、run、segment 组成；每次 handoff、retry、unblock 和 return 都写入
   追加式证据账本。
4. handoff 前必须从经过验证的 chat candidates 中明确选择目标 session，避免唤醒错误会话。
5. `wait`、`result` 和 `status` 管理业务完成状态；顶层发起者最后通过 process monitor 等待
   所有参与进程正常退出或在宽限期后安全清理。

所有状态变更都由脚本完成，不应直接编辑 registry、segment、handoff 或 status 文件。

## `ai-daily-report`

### 作用

生成适合 Obsidian 的中文 Codex/AI 工作日报，维护长期任务和日常跟踪项，并可选择通过
SSH/SFTP 同步到配置的远端目录。

### 基本原理

1. 采集器从 Codex sessions、memory、Git、Jira、计划账本和 usage ledger 汇集目标日期证据。
2. outcome pipeline 把原始会话整理为有边界的结构化输入，避免把运行噪声直接当成成果。
3. 报告生成阶段只基于规则、模板、计划上下文和证据包总结工作，并保留来源和风险说明。
4. validator 检查章节结构、任务标识和敏感信息；成功后再把机器可读状态写回计划账本。
5. scheduled wrapper 负责工作日判断、运行状态、失败保留和可选远端同步。

默认配置不包含项目、用户、远端主机或工作目录，远端同步默认关闭。时区、Codex 命令、
日历 API、项目清单、Jira 范围、允许扫描的仓库和所有数据源路径均可配置。

## 共同设计原则

- **配置与实现分离**：用户名、路径、项目和远端服务由运行环境提供。
- **本地优先**：记忆、用量、协作状态和日报证据默认保存在本地文件中。
- **确定性脚本优先**：状态计算、清理、记账和校验由脚本执行，LLM 负责判断和总结。
- **最小权限与明确授权**：删除、远端同步、完整索引重建等高影响操作都有额外边界。
- **可审计**：关键工作流保留 JSONL、状态文件或历史记录，以便定位来源和恢复流程。
- **渐进式加载**：先通过 Skill 描述完成路由，只在需要时读取详细 references 或运行脚本。

## 目录结构

```text
skills/
├── ai-daily-report/
├── co-agent/
├── reme-memory/
├── temp-files-mgr/
└── usage-mgr/
```

每个目录的 `SKILL.md` 是入口文档。使用前请先阅读对应 Skill 的配置和安全边界；不要把
示例配置直接当作生产环境配置。
