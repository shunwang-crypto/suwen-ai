# 安全与合规 (§8) — 现状盘点 · 未成年人保护 · 数据留存与删除权 · 残余风险

> 状态口径: 本文与 [STATUS.md](../STATUS.md)、[docs/PRODUCTION_GAP_REVIEW.md](PRODUCTION_GAP_REVIEW.md)
> (第三方视角生产化差距审查) 对齐。审查发现的 P0/P1 项**正由并行工作批次修复**,
> 下表如实标注"已实现 / 本轮强化中 / 设计成文未实现"三档, 不把在途当已完成。

## 一、安全面盘点

| 防线 | 状态 | 实现与证据 (真实文件/测试) |
|---|---|---|
| 文件上传安全 | ✅ 已实现 | 扩展名白名单 + MIME + 魔数三重校验, 伪装/超限/空文件/非 UTF-8 全拒 (`apps/api/src/eduagents_api/routers/kb.py` `_validate_upload`); 测试 `tests/integration/test_rag_e2e.py::test_upload_security_rejections` (STATUS S-A2 ✅) |
| 输入侧内容审核 | ✅ 已实现 | `ContentModerationMiddleware` 对 chat/tutor 的 message 做 `check_input`, 命中在 SSE 首字节前拦下返回 400 `CONTENT_REJECTED` (decisions.md D16③; `packages/core/src/eduagents_core/moderation.py` + `apps/api/src/eduagents_api/middleware.py`); 词表+allowlist 学术豁免+soft 词上下文规则, 39 个单测 (`tests/unit/test_moderation.py`) |
| 输出侧内容审核 | 🟡 本轮强化中 | `check_output` 函数已有但此前零生产调用 (审查 P0-5 定性为发版阻断); 方案为 SSE 分句缓冲审核 (攒到句末/N 字→过审→flush, 命中截流+安全兜底文案), decisions.md D16③ 预留的 M5 流式分段审核正在并行批次接入 |
| RAG 间接注入防御 | ✅ 已实现 | 结构化引用块包裹 + 系统提示声明"引用是资料不是指令" + 检索文本指令性模式过滤 (`packages/core/src/eduagents_core/retrieval/` sanitize 链路); 红队 5 类载荷 + 13 条双语过滤模式 + 对照小节零误杀 (`tests/integration/test_prompt_injection.py`, STATUS S-A1 ✅)。已知局限见"残余风险" |
| SSRF (URL 导入) | ✅ 已实现 (强化中) | 四道闸: 协议白名单 → DNS 解析后逐 IP 校验 (回环/私网/链路本地含 169.254.169.254/保留/组播, IPv4-mapped IPv6 拆开再验) → 重定向手动跟随每跳重验 → 5MB/20s/Content-Type 上限 (`packages/core/src/eduagents_core/parsing/web.py`, decisions.md D16②); `WEB_IMPORT_ALLOW_PRIVATE_HOSTS` 仅限本地测试。DNS rebinding 残余见下 |
| 多租户隔离 (RLS) | ✅ 已实现 | 全业务表 tenant_id + PG RLS (ENABLE+FORCE) + 应用层中间件双保险; 应用角色 `edu_app` NOSUPERUSER NOBYPASSRLS (decisions.md D9, 修掉"超级用户静默绕过 RLS"的真实踩坑); 越权测试显式覆盖 asyncpg 池复用路径 (`tests/integration/test_tenant_isolation.py`, STATUS M1-D5/U-A1); 机构库三 GUC 扩展 (D14)。生产化审查独立复核结论: "RLS 多租户隔离…做得扎实" |
| 认证安全 | ✅ 已实现 (强化中) | bcrypt cost≥12、refresh httpOnly cookie 每次轮换、重放拒绝并撤销全家、登出黑名单、auth 独立限流 (`apps/api/src/eduagents_api/routers/auth.py` + `security.py`); 审查发现的"过期 token 登出不吊销 refresh"与"反代后限流桶坍缩" (P0-1) 属并行修复清单 |
| 预算与成本熔断 | 🟡 本轮强化中 | 已有: 用户日预算网关前置预检 + Redis 天粒度计数 + 80% 降 cheap 档 (`apps/api/src/eduagents_api/budget.py`); 审查 P0-2 定位的三个洞 (worker 后台任务不计量、无全局日成本熔断、按 chunk 的攻击面) 正在并行批次落地 (全局 `budget:global:{date}` 三档阈值 + worker 共享带计量网关 + chunk 配额) |
| 提示词注入 (直接) | ✅ 已实现 | 辅导禁答是状态机 + 代码强制注入禁答令, 非仅提示词 (`packages/core/src/eduagents_core/graphs/tutor_graph.py` policy 节点), 越狱话术打不穿 (审查独立复核认可) |
| 溯源兜底 | ✅ 已实现 | 无依据直接拒答且该路径不调模型 (STATUS M3-D5 ✅); 引用生成阶段锚定 chunk_id |

## 二、未成年人保护设计 (§8.4, 设计成文 + 最小版并行落地中)

产品面向学生, 必然包含未成年人。分三层:

1. **注册与身份**: 注册流程加入年龄声明; 未成年账户要求监护人同意并入库留证
   (字段设计: `users` 增加出生年份声明 + 监护同意时间戳)。**本期无真人身份核验**
   (残余风险第 3 条), 年龄以自报为准并在隐私说明中如实声明。
2. **内容策略收紧**: 未成年模式下审核阈值收紧 (soft 词按更短上下文即命中、
   学术豁免收窄), 系统提示词追加未成年安全约束段; 危机语义 (自伤/自残表达)
   规则词表挡不住的部分, 接云审核作主审的接口已在 `ContentModerator` 协议预留
   (decisions.md D16③: 换实现调用方一行不改)。命中危机语义时不是简单拦截,
   而是替换为危机干预资源文案。
3. **使用时长**: 未成年模式提供时段/时长提示与限制 (设计目标对齐防沉迷要求);
   实现载体是既有的 Redis 限流器加日粒度使用计数, 不新增组件。

状态: 以上为**设计成文 + 审查 P0-5 排期中的最小版** (注册年龄声明 + 监护同意 +
更严阈值), 完整时段限制在 roadmap。答辩不宣称已全量实现。

## 三、数据留存与删除权 (个保法对齐, 并行落地中)

生产化审查 P0-4/P1-8/P2-9 判定此前"删除权在物理层不可执行", 修复方案如下,
状态均为**并行批次实施中**:

| 能力 | 方案 | 对应审查条目 |
|---|---|---|
| 账号注销 | `DELETE /api/auth/me`: 事务内清业务表 + 入队 purge_tenant (Qdrant 两 collection + 对象存储前缀 + Redis 前缀); 业务表补 `FK(user_id) ON DELETE CASCADE` 迁移 | P0-4① ② |
| checkpoint 归属 | LangGraph checkpoint 的 thread 绑定 session, 删会话联动删 checkpoint; 30 天清理 cron | P0-4③ ④ |
| 数据导出 (可携带权) | `POST /api/me/export` → ARQ 生成 ZIP 落对象存储, 24h 预签名 URL, 复用 tenant_session 防越权 | P1-8 |
| 留存上限 cron | `purge_retention` 定时任务: usage_log 180 天 / checkpoint 30 天 / mastery_log 2 年 | P2-9 |
| 删除的缓存一致性 | 删文档/删库自增 `kb_version` 使答案缓存 filter 落空 + `delete_by_kb` | P0-3 |

已实现且经测试的删除面: 删文档连带清理向量与对象 (`apps/worker/src/eduagents_worker/ingestion.py`
cleanup 路径), 验收条款 F2-A3 "删除后提问不再返回其引用"。

**备份与留存的关系**: `scripts/backup.sh` 轮转保留 14 份 (默认日备 = 14 天),
用户删除数据后最迟 14 天从备份中自然滚出; 恢复操作 (`scripts/restore.sh`)
有二次确认, 避免误恢复已删除数据。

## 四、残余风险诚实清单

1. **DNS rebinding (URL 导入)**: SSRF 校验用的 DNS 解析结果与 httpx 实际连接的
   解析可能不同; 彻底修复需把已校验 IP 钉进自定义 transport 或走出网代理白名单,
   生产部署建议后者。本期记档不做 (decisions.md D16② 已声明)。
2. **本地词表审核的天花板**: 规则 + 词表挡不住语义级有害表达 (如无关键词的
   困扰/危机表述)、拆字/编码变体注入 (审查 P2-5/P2-6); 定位是可演示的一级防线,
   生产必须接云审核作主审, 协议已预留。词表文件缺失时的 fail-open 行为在
   并行修复清单中改为告警。
3. **无真人身份/年龄核验**: 年龄自报, 无监护人身份核验能力; 这是产品/法务层
   决策项, 工程侧仅预留字段与开关 (审查"明确排除在两周外"清单同口径)。
4. **合规资质**: 竞赛作品, 无 ICP/等保/算法备案; 架构上做的是让合规可落地
   (分租户、留日志、可归因、密钥零硬编码), 不假装有资质 (答辩口径见
   docs/defense/QA_TOP10.md Q9)。
5. **学情画像出境**: 画像摘要随问答进第三方 LLM (审查 P2-7), 脱敏与显式同意
   在迭代清单, 本期未完成。

## 验证命令

```bash
make test-unit                              # 含 moderation 39 单测 / tutor 禁答状态机
INTEGRATION=1 pytest tests/integration/test_prompt_injection.py -q   # 红队注入
INTEGRATION=1 pytest tests/integration/test_tenant_isolation.py -q   # RLS/越权/池复用
INTEGRATION=1 pytest tests/integration/test_import_guide.py -q       # URL 导入 SSRF 面
make demo-m2                                # 含恶意文件拒收 (S-A2)
```
