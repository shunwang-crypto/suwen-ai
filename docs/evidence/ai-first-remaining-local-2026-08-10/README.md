# AI-first 本地收口证据（2026-08-10）

本目录只记录本次本地验收。`browser-results.json` 明确区分自动化通过、确定性 seeded fixture 验收和 live 模型验收状态；它不包含密码、token、Cookie、Authorization header 或资料正文。

## 结果

- 自动化：通过。单测 `741 passed`，integration `184 passed, 15 skipped`，Web `114 passed`；DAG 定向 Web 单测 `2 passed`，TypeScript、lint、docs-check 和 `git diff --check` 通过。
- 本地 live：被阻断，未声称通过。
- 阻断原因：OpenCode Zen 文本路由连续两次返回 `UPSTREAM_TIMEOUT`。未切换 provider，也未绕过 KC enrichment barrier。
- API 因此真实返回 `status=preparing`、`QUIZ_KC_PREPARING`；没有生成题目，也没有伪造 Quiz 结果。
- 计划 DAG：使用当前本地测试用户的确定性 seeded fixture，通过真实 `Plan`、`KnowledgeComponent`、`KcEdge`、计划结构和 `GET /api/plans/{id}` 验收；3 层、4 节点、共享前置 A 出现 1 次，D 任务抽屉可打开，阻塞说明可见。
- DAG 响应式验收：桌面和移动页面水平溢出均为 0；移动 DAG 区域为 `overflow-x: auto` 且实际可横向滚动。该截图不是 live 模型生成结果。

## 截图说明

- `quiz-recommendation.png`：本次 live 账号的推荐页，显示资料已基础摄取但 semantic enrichment 尚未完成。
- `profile-after-quiz.png`：本次账号在 Quiz 被 barrier 阻断时的 blocked/empty profile 快照；没有成功 Quiz，不暗示画像闭环已完成。
- `plan-kc-dag-desktop.png`、`plan-kc-dag-mobile.png`：上述确定性本地 seeded DAG 的真实页面快照，不是空状态截图。
- `quiz-generated.png`：仅为本地 seeded demo 账号打开的既有题目，用于检查页面渲染；不是本次 live 生成结果。
- `quiz-result.png` 未生成：demo 账号的既有题目提交被当前 live KC barrier 返回 `QUIZ_KC_REQUIRED`，没有结果页可证明。

生产验证仍需部署新代码后另行进行。
