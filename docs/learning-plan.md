# 可执行学习计划

当前计划链路是“目标 → 诊断 → 知识库检索 → 可编辑草案 → 服务端硬校验 → 原子保存”。每个任务都包含：

`title`、`objective`、`instructions`（2–5 步）、`completion_criteria`、`minutes`、`kc_names`、`action_type`、`practice_count`、`difficulty`、`task_key` 和 `done`。`action_type` 为 `read`、`practice`、`tutor`、`review_errors` 或 `mixed`。

## 资源保存与权限

草案阶段的 `resource_ref` 只是检索结果数组中的临时索引。确认时，API 会在同一事务中重新验证当前用户可访问的知识库、文档和分块，并创建 `plan_resources` 快照；任务保存稳定的 `resource_id`。网页资源只接受安全搜索链路返回的公网 `http/https` 地址。Plan 与 PlanResource 任一保存失败都会回滚。

知识库删除后，计划的 `kb_id`、PlanResource 的 `kb_id/doc_id/chunk_id` 按 `SET NULL` 处理，资源标题仍可用于历史任务展示；无法恢复的历史 `resource_ref` 不会被猜测为新资源。

旧计划没有新字段时，读取层会补 `task_key`、`done`，任务详情仍可展示；没有稳定资源就不显示阅读按钮。计划任务详情在 `/plans` 和 `/today` 使用同一个抽屉组件，执行入口使用已有页面参数：知识库 `/kb?kb=...`（资料页暂不支持精确 chunk，明确降级）、练习 `/quiz?topic=...&kb=...&count=...&difficulty=...`、辅导 `/tutor?problem=...&kb=...`、错题 `/errors?kb_id=...` 或无库时 `/errors`。

“今日”继续使用最近创建的计划作为联动计划；当前没有计划状态字段。

## 边界与后续

本阶段已实现任务详情、稳定资源关联、任务执行入口、plans/today 联动和学情补强任务详情。以下能力明确未实现：

- 多计划 `active/paused/completed` 状态机、跨计划合并与冲突调度；
- 练习 Attempt 与 plan task 的强归因，以及完成练习后自动完成计划任务；
- 实际学习时长自动统计。

练习、错题复习和辅导总结仍沿用既有 mastery/learner insights 链路；pending 错题不会参与掌握度、画像或计划调整。
