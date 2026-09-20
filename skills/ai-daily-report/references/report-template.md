---
title: YYYY-MM-DD AI Daily Summary
date: YYYY-MM-DD
tags:
  - daily/ai
  - codex
scope: codex
status: synced
---

# YYYY-MM-DD

下面各 section 的 bullet 是生成说明，最终日报必须替换或删除这些说明，不要原样保留。

> [!summary] 今日概览
> - **关键成果**：一句话概括当天最重要的交付结果。
> - **主要风险**：写最高优先级风险；没有 P0/P1 时写“暂无 P0/P1 阻塞”。
> - **下一步**：写最重要的后续动作。

## 项目进展

| 注册项目 | Task ID | 任务/指标 | 目标指标/目标状态 | 最新进展 | 验证状态 | 下一步计划 |
|---|---|---|---|---|---|---|
| 注册项目名 | `YYYYMMDD-N` | 例如 RX 性能优化 | 例如 ACK/Provision 1280k pps 或完整回归通过 | 按项目 `report_profile` 合并当天证据、用户项目总结和 `long_tasks` 定向证据，优先保留 PPS/Entry-s、通过率、矩阵拐点等强结果数据 | 明确已验证、待验证或缺少环境 | 下一步里程碑或验证动作 |

## 跟踪事项

| 跟踪 ID | 关联 Task ID | 事项 | 状态 | 今日进展/原因 | 下一步/完成判据 | 优先级 |
|---|---|---|---|---|---|---|
| `TRK-YYYYMMDD-N` | `YYYYMMDD-N` 或空 | 未闭环事项或用户明确要求跟踪的内容 | 已完成/部分完成/未推进/调整/取消/阻塞 | 说明今天是否推进以及原因 | 写清下一动作和做到什么算完成 | P0/P1/P2 |

如果主账本和前次日报都没有可用跟踪事项，写：`暂无未闭环跟踪事项。`

## 做了哪些事

1. **主题名**：写成果、价值和当前状态，不写详细过程。
2. **主题名**：必要时补充关键文件或命令作为证据，但不要展开尝试顺序。
3. **未完成事项**：明确标注未完成原因、当前状态和后续归属。

## 学习到了哪些事

1. **技术结论**：记录当天沉淀下来的技术判断、边界条件或验证口径。
2. **排障经验**：把能复用的方法写成可执行经验，不写泛泛感想。
3. **项目约定**：记录新的用户偏好、流程约束或后续可复用规则。

## token使用

| Agent        | 总 token | 输入 token | 缓存输入 token | 输出 token | 推理输出 token | session/subagent/approvals |
| ------------ | -------: | ---------: | -------------: | ---------: | -------------: | -----------------: |
| `agent name` |  14.35亿 |    14.3亿 |        13.8亿 |     305万 |         0.48万 |              2/1/0 |
| **总计**     |  14.35亿 |    14.3亿 |        13.8亿 |     305万 |         0.48万 |              2/1/0 |

如果当天没有可用 token 记录，写：`暂无 token 使用记录。`

## 风险与阻塞

| 优先级 | 状态 | 问题 | 影响 | 下一步 |
|---|---|---|---|---|
| P0/P1/P2 | 阻塞/风险/观察 | 问题描述 | 影响范围 | 具体动作 |

如果没有明显风险，写一行：`P2 | 观察 | 暂无 P0/P1 阻塞 | 当前无直接影响 | 继续观察下一次定时运行`。

## 来源

> [!info]- 来源
> - Codex sessions: N 个
> - Memory files: `YYYY-MM-DD.md`
> - Repositories: N 个

> [!info]- ai-daily-state
> ```json
> {
>   "version": 1,
>   "date": "YYYY-MM-DD",
>   "task_updates": [
>     {
>       "task_id": "YYYYMMDD-N",
>       "text": "长期任务或阶段性任务内容",
>       "status": "done/blocked/deferred/dropped",
>       "priority": "P1/P2",
>       "completed_date": "YYYY-MM-DD",
>       "source": "carryover/risk/user_request",
>       "project": "glb_dev_validation/codex_skill_management",
>       "metric": "任务级指标名，例如 RX ACK/Provision 性能",
>       "target_metric": "目标值或目标状态，例如 1280k pps",
>       "note": "状态变化或保留原因"
>     }
>   ],
>   "tracking_updates": [
>     {
>       "track_id": "TRK-YYYYMMDD-N",
>       "text": "和正文跟踪事项对应的未闭环事项",
>       "project": "glb_dev_validation/codex_skill_management",
>       "status": "open",
>       "priority": "P1/P2",
>       "source": "risk/carryover/user_request/jira",
>       "related_task_id": "YYYYMMDD-N",
>       "completion_criteria": "做到什么算完成"
>     }
>   ],
>   "next_tracking": [
>     {
>       "track_id": "TRK-YYYYMMDD-N",
>       "text": "仍需后续跟进的事项",
>       "project": "glb_dev_validation/codex_skill_management",
>       "status": "open",
>       "priority": "P1/P2",
>       "source": "risk/carryover/user_request/jira",
>       "related_task_id": "YYYYMMDD-N",
>       "completion_criteria": "做到什么算完成"
>     }
>   ],
>   "next_long_tasks": [
>     {
>       "task_id": "YYYYMMDD-N",
>       "text": "长期任务内容",
>       "project": "glb_dev_validation/codex_skill_management",
>       "status": "in_progress",
>       "priority": "P1/P2",
>       "source": "carryover/user_request",
>       "metric": "任务级指标名",
>       "target_metric": "目标值或目标状态",
>       "tracking": {
>         "mode": "long_term",
>         "progress_style": "outcome",
>         "keywords": ["用于定向检索的关键词"],
>         "jira_keys": ["PROJ-123"],
>         "repo_paths": ["$PROJECT_ROOT"],
>         "outcome_hint": "后续日报只写成果性进展"
>       }
>     }
>   ]
> }
> ```
