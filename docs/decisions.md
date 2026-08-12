# 决策与偏差记录

> 状态：Active
> 本文记录长期有效的架构决定。`accepted` 表示当前仍生效，`superseded` 表示已被后续
> 实现取代，`historical` 只解释当时背景。代码与测试是当前事实，旧决定不得覆盖新实现。

## D1 · 开发环境用 conda + 国内源（2026-07-26）
用户指示。conda env `eduagents`（Python 3.12，miniforge），pip 走 TUNA 源。
锁定方式：`make lock` 生成 `requirements.lock`（pip freeze，排除 editable 包）。
本地 Makefile 直接绑定该环境并在运行前校验；部署镜像内 pip 同样走 TUNA。

## D2 · 网关新增第四种模式 stub（2026-07-26）
提示词 §9 只定义 live|record|replay。用户明确指示"开发期先不真实调用模型（时间花费大）"，
而 replay 需要先有录制。故增加 `GATEWAY_MODE=stub`：确定性开发桩，输出带
「【开发桩响应 · 非真实模型输出】」横幅，永远无法冒充真实回答（§14 反造假）。
约束：stub 仅限 dev/test；`app_env=prod` 且关闭 fallback 时全链路失败会直接报错。
黄金演示路径一律用 record→replay，stub 不出现在任何演示与评测数字中。

## D3 · 初始迁移用 metadata.create_all（2026-07-26）
`alembic/versions/0001` 用 `Base.metadata.create_all(bind)` 而非手写 20 张表的 DDL，
保证迁移与 ORM 模型零漂移；RLS（ENABLE+FORCE+策略）仍是显式 SQL。
后续所有 schema 变更一律 `alembic revision --autogenerate` 增量，禁止再用 create_all。

## D4 · 增加 chat_messages 表（2026-07-26）
提示词 §6 未列此表。理由：F1-A6 要求会话列表/历史加载/重命名/删除，需要独立于
LangGraph checkpointer 的可查询消息存储。M3 引入 checkpointer 后二者并存：
chat_messages 做展示与历史 API，checkpointer 做图执行状态。

## D5 · 本地开发用 conda 版 PostgreSQL/Redis 代替容器（historical，2026-07-26）
现实约束：本机 Docker daemon 的 DNS 被污染（docker.io 解析到 Facebook IPv6 段），
且无免密 sudo，无法当场改 `/etc/docker/daemon.json`。
处置：开发与集成测试用 conda-forge 的 postgresql/redis-server 本地实例
（`make dev-infra`）；docker-compose 仍是唯一部署路径，修复 daemon 后即可 `make up`。
后续 compose 路径已验证并保留；当前开发仍可使用 `make dev-infra`，生产部署使用 compose。

## D6 · 密码哈希直接用 bcrypt 库（2026-07-26）
不用 passlib（已停止维护，与 bcrypt>=4.1 不兼容）。cost 默认 12（.env 可配），
测试环境降为 4 以提速（tests/conftest.py）。

## D7 · usage_log 的 RLS 策略放行 is_admin（2026-07-26）
F10 运营面板需要管理员跨租户聚合。仅 usage_log 一张表的 SELECT 策略追加
`OR current_setting('app.is_admin', true) = 'true'`；写入仍强制租户匹配。

## D8 · Qdrant/MinIO 推迟到 M2 接入（superseded，2026-07-26）
该阶段性决定已完成：Qdrant 和对象存储均已接入当前摄取、附件、题目资料和数据生命周期。

## D9 · 应用数据库角色与超级用户分离（2026-07-26）
初版集成测试暴露：initdb/POSTGRES_USER 创建的 `edu` 是超级用户，**静默绕过 RLS**。
迁移 0001 现在创建 `edu_app`（NOSUPERUSER NOBYPASSRLS，开发默认密码 edu，生产必须
ALTER ROLE 改密）并授予 DML 权限；应用连接走 edu_app（DATABASE_URL），迁移走
edu（MIGRATION_DATABASE_URL）。RLS 策略统一 `NULLIF(current_setting(...), '')::uuid`
兜住事务级 set_config 结束后 GUC 残留空串的坑。

## D10 · conda libstdc++ ABI 冲突处理（2026-07-26）
pymupdf 触发加载系统 libstdc++ 后，qdrant-client 的 sqlite3/icu 需要 conda 新 ABI 而报
`CXXABI_1.3.15 not found`。处置：tests/conftest.py 顶部用 ctypes 预加载
`{sys.prefix}/lib/libstdc++.so.6`（RTLD_GLOBAL），Makefile 导出 LD_LIBRARY_PATH。
容器镜像（pip + debian slim）无此问题。

## D11 · 开发期 Qdrant 本地嵌入模式的单进程限制（2026-07-26）
`QDRANT_LOCAL_PATH` 非空时 qdrant-client 走文件锁本地模式，同一路径仅允许一个进程。
约束：`make seed`/评估 CLI 与 `make api` 不可同时运行；进程内(API+直调摄取函数)通过
qdrant_store 的客户端单例缓存共享。Qdrant 容器可用后 .env 置空 QDRANT_LOCAL_PATH
切服务端模式，该限制消失。

## D12 · stub 嵌入口径的评估数字只做管线验证（accepted，2026-07-26）
stub 哈希嵌入 + 真实 BM25 稀疏只证明管线可跑，**不得**作为质量宣称。后续 live 结果
写入明确环境的外部制品存储，不回写仓库；D12 的口径隔离仍长期有效。

## D15 · 编排形态: 四子图 + API 层路由, 无独立 Supervisor/analyst 图（2026-07-27）
偏离提示词 §5.4"顶层轻 Supervisor + 五子图"。实际形态: qa/quiz/plan/tutor **四个**
LangGraph 子图; "Supervisor 意图分类"由前端明确入口 + API 端点路由承担（用户点"练习"
就是出题意图, 无需模型再猜——省一次 LLM 调用且零误判）; "analyst 学情诊断"是纯聚合
+单次 LLM 叙述, 无多步决策, 做成 reports 服务层而非图。答辩口径: 这是"编排复杂度
应与任务复杂度匹配"的工程判断, 不是能力缺失; 自由输入的意图路由在 qa 图 router
节点内仍是真实存在的（kb/web/direct 三路）。历史 commit 信息中"五子图"措辞不准确,
以本条与 README 为准。此偏差由答辩材料诚信审计 (2026-07-27) 发现并修正。

## D14 · 机构库: owner_type 判别列 + tenant_id 语义泛化为 owner_id（2026-07-27）
（D13 由流式/缓存主线占用。）Wave3 机构共享知识库不加 `kb.org_id` 可空列，也不重命名
`tenant_id`：物理列名保留 `tenant_id`，语义泛化为 **owner_id** —— `owner_type='user'`
时为 user_id，`='org'` 时为 orgs.id。理由：①存量行 `owner_type='user'` 默认值即正确，
迁移零回填；②documents/chunks/Qdrant payload 的 tenant_id 传播链一行不改，向量零迁移；
③重命名列需改 16 张表引用与全部查询，回归面不可控，而可空 org_id 列会让所有查询/RLS/
payload 双分支 COALESCE。RLS 三 GUC：`app.tenant_id`（个人/owner 身份）+ `app.org_ids`
（成员机构逗号表，只授 kb/documents/chunks 的**读**）+ `app.is_admin`（沿用 D7，另用于
orgs 凭邀请码查询）。org 库**写**路径不走 org_ids：service 层校验 org admin 后以
`tenant_session(org_id)` 开事务（与 ARQ worker 摄取同构），成员会话对 org 行的写被 RLS
写策略（owner 匹配）拦截 —— service 与 RLS 双拦截。联合检索 chat 入参为显式
`kb_ids: list[UUID]`（不做 scope=all，保回放确定性），PG 先逐库校验可见性（不可见 403），
Qdrant 单次查询 kb_id/tenant_id 双 MatchAny 只作纵深兜底。机构配额用 `orgs.storage_quota_mb`
+ 上传事务内 SUM 预检（与个人路径同构，不引入计数列，避免计数漂移；库量级演示场景 SUM 足够）。

## D16 · 学习指南、URL 导入与内容审核（partly superseded，2026-07-27）
本条覆盖同一批落地的三件事（URL 网页导入 / 摄取后学习指南 / 内容审核中间件 S-A3）。

**① 学习指南不建表，落 `knowledge_bases.config_json["study_guide"]`。**
指南是**库级单例**（不是文档级）：用户要的是"这个库整体讲什么、从哪读起"，文档级会
碎成 N 份互不衔接的摘要，且首个文档就该有指南。它整体读写、不参与检索、不需要索引，
JSONB 一列足够；建表要多付迁移 + RLS 策略 + 级联清理三份成本却零收益。
触发：`enrich_document` 成功后统计该 kb 的 `status='enriched'` 文档数，**==1 时**入队
`build_study_guide`（后续文档不重复烧 standard 档），需要刷新走
`POST /api/kb/{id}/study-guide/rebuild`。stub 网关跳过（沿用 D2：stub 不产生可入库内容），
LLM 输出的 `kc_names` 与库内 `knowledge_components` 求交后才落库——指南上的知识点标签
必须能对上真实 KC，否则是伪造展示（§14）。

**② URL 导入的 SSRF 防线在 `parsing/web.py`，不在路由层。**
服务端代抓外链是典型 SSRF 面，四道闸：协议白名单（`file://` 拒）→ 主机 DNS 解析后
逐 IP 校验（回环/私网/链路本地含 `169.254.169.254`/保留/组播，IPv4-mapped IPv6 拆开再验）
→ 重定向**手动**跟随且每跳重验（httpx 自动跟随会绕过首次校验）→ 5MB + 20s + Content-Type
上限。抓下来的 markdown 走**现有**两阶段流水线（filename 以 `.md` 结尾即复用
MarkdownParser），`documents.parse_backend='web'` 标记来源，权限/配额与文件上传完全同构。
已知残余风险：DNS rebinding（校验用的解析结果与 httpx 实际连接的解析可能不同）；彻底修
需要把已校验 IP 钉进自定义 transport 或走出网代理白名单，生产部署建议后者，本期记档不做。
`WEB_IMPORT_ALLOW_PRIVATE_HOSTS` 仅供集成测试（本地临时 http server）打开，生产必须 False。

**③ 内容审核（S-A3）走中间件。**
审核是横切关注点，做成 `ContentModerationMiddleware` 而非在各 router 里散落调用：
对 `POST /api/chat` 与 `POST /api/tutor` 的 `message` 字段做 `check_input`，命中直接回
400 `CONTENT_REJECTED` 信封——chat 是 SSE，**在第一个字节前**拦下比在流里另造一种拒绝
事件干净得多，前端沿用既有 ApiError 分支。无 `Authorization` 头的请求跳过审核（端点自身
返回 401），免得匿名者拿这个端点当敏感词探测器。
原决定只实现输入侧；当前实现已经增加流式分段输出审核和危机提示，因此“留待 M5”部分
已被取代。规则实现仍是可替换的本地策略，生产接云审核服务时应保持 `ContentModerator`
协议和流式错误契约。

## D17 · 知识库、题目资料和错题本分离（accepted，2026-08-01）

教材、讲义、笔记等参考内容进入 `KnowledgeBase -> Document -> Chunk`，用于 RAG、引用和
讲解；试卷、练习册、作业、题单进入 `LearningResource -> ResourceQuestion`，用于拆题、
校对和真实作答；`ErrorItem` 只表示派生学习记录。题目资料可以关联知识库以解析知识点，
但题目和学生答案不自动写入普通 RAG。

理由：三者生命周期、权限、检索风险和删除语义不同。把试卷当知识库会污染回答上下文，把
错题本当资料目录会混淆来源与学习状态。

## D18 · 待核验隔离与真实作答模型（accepted，2026-08-01）

AI 识别题和不确定答案必须先由用户校对。`pending` 题、待核验错题和未最终核验作答不得
写 FSRS、掌握度、画像、报告或推荐。资源题不创建隐藏 Quiz；一次真实考试或练习使用
`AssessmentAttempt -> AssessmentAnswer`，最终核验后才生成掌握度和错题证据。

更换资源关联知识库会改变知识点语义，因此仅在没有 `ErrorItem`、`AssessmentAnswer`、
`Attempt` 或 `MasteryLog` 证据时允许，并在同一事务重映射全部 `kc_ids`。

## D19 · 文档按职责分层并自动检查（accepted，2026-08-01）

根 README 是项目入口，`docs/README.md` 是文档索引，STATUS 只记录发布状态；架构、开发、
API、数据、安全和部署分别维护自己的事实。研究、答辩、证据和历史提示词不定义当前实现。
功能变化必须在同一提交更新对应 Active 文档，并通过 `make docs-check`。
