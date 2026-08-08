# 死代码清理记录（2026-08-08）

> 状态：Historical（一次性清理记录，不作为开发规范）
> 范围：删除全仓零引用的符号、组件与类型定义。**未改动任何业务行为。**
> 验证：见文末「验证结果」——全部测试套件在清理前后数量一致、全绿。

## 背景

清理前用 AST 遍历 `apps/api/src`、`apps/worker/src`、`packages/core/src` 的所有顶层
函数/类/常量，对每个符号在全仓（含 `tests/`、`scripts/`、`eval/`、`loadtest/`、
`alembic/`、`apps/web/`、`Makefile`）做引用计数；前端同样对所有 `export` 符号统计跨
文件引用。只删「定义处之外零引用」的符号。

## 一、已删除（21 项）

### 后端 14 项

| 符号 | 原位置 | 类型 |
|---|---|---|
| `SOFT_RATIO`、`GLOBAL_HARD_RATIO` | `apps/api/src/eduagents_api/budget.py` | 常量 |
| `get_gateway`、`get_rate_limiter` | `apps/api/src/eduagents_api/deps.py` | FastAPI 依赖函数 |
| `CODE_LENGTH`、`code_expiry` | `apps/api/src/eduagents_api/email_verification.py` | 常量 + 函数 |
| `validation_now` | `apps/api/src/eduagents_api/model_credentials.py` | 函数 |
| `PROFILE_ROLES`、`build_profile_summary` | `apps/api/src/eduagents_api/profile_summary.py` | 常量 + 函数 |
| `RESOURCE_STATUSES` | `apps/api/src/eduagents_api/resource_utils.py` | 常量 |
| `_ATTEMPT_STATUSES` | `apps/api/src/eduagents_api/routers/assessments.py` | 常量 |
| `ErrorEnvelope` | `apps/api/src/eduagents_api/schemas.py` | Pydantic 模型 |
| `LEDGER_KINDS` | `apps/api/src/eduagents_api/wallet.py` | 常量 |
| `_payload_questions` | `apps/worker/src/eduagents_worker/tutor_recognition.py` | 函数 |

连带清理的 import：`email_verification.py` 去掉 `timedelta`、`model_credentials.py`
去掉 `UTC`（删除函数后成为未使用导入）。

两个值得单独说明的：

- **`build_profile_summary`**：已被 `build_profile_context` 完全取代（前者只是取后者
  `.summary` 字段的薄包装），四条链路（chat/quiz/plans/tutor）均已直接调用后者。
- **`ErrorEnvelope`**：未作为任何路由的 `response_model`，错误响应实际由异常处理器
  直接构造 JSON。

### 前端 7 项

| 符号 / 文件 | 原位置 | 处理 |
|---|---|---|
| `fmtDate` | `apps/web/components/common.tsx` | 删函数（全仓统一用 `fmtDateTime`） |
| `SseEventName` | `apps/web/lib/sse.ts` | 删类型 |
| `StallEventData` | `apps/web/lib/sse.ts` | 删接口 |
| `EvalRunRequest` | `apps/web/lib/types.ts` | 删接口 |
| `BalanceBadge` | `apps/web/components/ui/balance-badge.tsx` | **整文件删除**（90 行） |
| `UnderConstruction` | `apps/web/components/under-construction.tsx` | **整文件删除**（24 行） |
| `WorkspaceNav`、`Workspace` | `apps/web/components/workspace-nav.tsx` | **整文件删除**（61 行） |

三个整删的组件文件，其内部符号的全部引用都在文件自身之内（自引用），无任何外部
import，删除前逐个核对过。

## 二、需连带改测试的 1 项

`set_task_done`（原 `apps/api/src/eduagents_api/plan_adjust.py`）已被 `set_task_status`
取代，生产代码零引用，仅 `tests/unit/test_plan_task_status.py` 一处调用。

删函数的同时改写该测试为直接调 `set_task_status(..., None)`：

- 测试名 `test_legacy_toggle_recovers_skipped_to_pending` 保持不变
- 断言从 `done is False` 改为 `status == "pending"`（等价，且更贴近三态语义）
- **覆盖不丢**：仍然验证「skipped 任务经无参切换回到 pending」这一行为

## 三、明确保留（易被误判为死代码，请勿删）

以下都是**当前生效的业务逻辑或必要防御**。曾在一份外部清理清单里被列为「可删」，
经核对全部是误判，特此记录以免重复踩坑。

| 保留项 | 位置 | 为什么不能删 |
|---|---|---|
| `ERROR_IMPORT_QUIZ_KIND` 的 6 处过滤 | `services.py`、`routers/reports.py`、`routers/teacher.py`、`routers/quiz.py` | 执行 [STATUS.md](../STATUS.md) 红线「错题导入不污染普通测验统计」。删掉过滤会让错题导入产生的伪 Quiz 漏进教师工作台与学情报告 |
| 计划任务 `done` 派生字段 | `plan_adjust.py`、`routers/plans.py` | 任务态存在 `structure_json` JSONB 内（决策 D-F3-1 明确「不新增表/列」），`done` 是每次写入时同步派生的镜像，读取侧靠它兼容无 `status` 的历史计划数据。**不存在也不会有 `status` 列可供「migration 回填后删除」** |
| `Organization` 类 | `models.py` | M0 遗留占位表，是 `users.org_id` 的 FK 目标（见 `decisions.md` D14）。删 ORM 类不删表会造成 schema 失配 |
| naive datetime 判断 | `routers/next_actions.py` | 防 DB 取出的 naive datetime 与 `datetime.now(UTC)` 相减抛 `TypeError`，是正确防御而非冗余 |
| `breaker_state`（单数） | `packages/core/src/eduagents_core/gateway/openai_gateway.py` | 被同文件的 `breaker_states`（复数）依赖，admin 路由调的是复数版。存在依赖链，不是孤立符号 |
| 名含 legacy/compat 的测试 | `test_metacognition.py`、`test_tutor_recognition.py`、`test_plan_task_status.py`、`test_registration_invites.py` 等 | 测的都是**活着的历史数据兼容**（老数据只有布尔预测无概率值、LLM 返回裸数组格式、老计划无 `status`、新旧注册配置冲突检测），删了会真丢覆盖 |

## 四、兼容迁移状态（代码已迁移，删除待发布观察）

这三处是真正的「新旧并存」。仓库内业务调用方已经迁移；兼容测试仍会有意调用旧入口，
删除前还必须完成发布观察和外部调用归零确认，属于行为变更，不在本次纯清理范围内。

| 兼容代码 | 位置 | 当前状态 |
|---|---|---|
| 单值 chat `kb_id`（等价 `kb_ids=[kb_id]`） | `schemas.py`、`routers/chat.py` | 业务脚本、Web chat 和压测已迁移；`test_org_kb.py` 仅保留兼容/越权契约。eval 页的单值 `kb_id` 属于单库 retrieval eval API，不是 chat 兼容调用方 |
| `PUBLIC_REGISTRATION` | `packages/core/src/eduagents_core/config.py` | 应用仍接受旧别名并检测新旧冲突；启动日志仅记录配置名来源，不记录模式值。生产部署脚本要求新名称 |
| 旧 `/api/health` 路径 | `routers/health.py` | 仓库脚本和 compose 已切换；旧路由带 OpenAPI 弃用标记、响应 header 和结构化计数事件。外部 monitor/负载均衡仍需观察期后确认归零 |

建议顺序：先改调用方 → 跑全套验证 → 再删兼容分支，每项独立提交便于回滚。

## 验证结果

清理前后逐项对比，**数量完全一致、全部通过**：

| 验证项 | 命令 | 清理前 | 清理后 |
|---|---|---|---|
| 后端单测 | `make test-unit` | 579 passed | **579 passed** |
| 集成测试 | `make test-integration` | 148 passed, 9 skipped | **148 passed, 9 skipped** |
| 迁移测试 | `MIGRATION_TESTS=1 ... -k migration` | 9 passed | **9 passed** |
| 前端单测 | `npm run test:unit` | 15 文件 88 tests | **15 文件 88 tests** |
| Python lint | `make lint` | All checks passed | **All checks passed** |
| 字节码编译 | `python -m compileall -q packages apps tests alembic` | OK | **OK** |
| 前端 lint | `npm run lint` | 零错 | **零错** |
| 前端构建 | `npm run build` | 成功 | **成功，28 静态页** |
| 文档检查 | `make docs-check` | 13 active documents | **13 active documents** |

集成/迁移测试需先 `make dev-infra`；跑任何 Python 前需
`export LD_LIBRARY_PATH=/home/shunw/miniforge3/envs/eduagents/lib`（见 `docs/development.md`）。

## 附：环境修正

清理期间发现 conda env `eduagents` 的三个 editable 安装指向了仓库副本
`/home/shunw/eduagents-impl` 而非主仓，导致 `import eduagents_api` 解析到错误代码、
4 个测试文件收集失败。已将三个 `.pth` 重新指向主仓：

~~~
$CONDA_PREFIX/lib/python3.12/site-packages/__editable__.eduagents_api-0.1.0.pth    → /home/shunw/eduagents/apps/api/src
$CONDA_PREFIX/lib/python3.12/site-packages/__editable__.eduagents_core-0.1.0.pth   → /home/shunw/eduagents/packages/core/src
$CONDA_PREFIX/lib/python3.12/site-packages/__editable__.eduagents_worker-0.1.0.pth → /home/shunw/eduagents/apps/worker/src
~~~

这是环境配置而非仓库内容，不随提交传播。若日后再出现「测试 import 到 impl 副本」，
按上表复查这三个文件。
