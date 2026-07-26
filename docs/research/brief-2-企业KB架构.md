# 机构共享知识库 · 最小切片设计文档

## 0. 竞品考证结论（用于决策依据）

| 产品 | 权限模型 | 对本设计的启示 |
|---|---|---|
| Dify | Workspace 即租户，KB 上有 `permission` 字段：only_me / all_team_members / partial_members；角色 owner/admin/editor/member，member 只读使用 | "库上标可见性 + 成员角色定写权"是主流；partial_members 需要额外绑定表，属于超出最小切片的粒度 |
| RAGFlow | KB 强绑定 `tenant_id`，团队 = owner 邀请成员；KB 的 permission 从 "Only me" 切到 "Team" 即整队可见；跨租户联合检索目前仅是 feature request（#15812），未实现 | 单字段所有权 + 成员自动可见（无显式 mount 动作）是可行最小形态；连 RAGFlow 都还没做跨租户联合检索，我们做到"个人+机构联合检索"即是演示亮点 |
| Coze | 个人空间/团队空间二分；知识库不支持多人协作编辑，仅 owner 可写，成员只读 | 印证"org 库成员只读、仅管理员可写"不会显得功能残缺，反而是行业现状 |

共同模式：**库有唯一所有者主体 + 成员身份决定只读挂载 + 写权限收敛到 admin**。"个人/团队/官方"三级中的"官方库"，Dify/Coze 均有，但可以用"平台自建一个 org"零成本模拟，本期不单独建模。

---

## 1. 数据模型增量

### 唯一决定：`owner_type` 判别列 + `tenant_id` 语义泛化为 owner_id（不加 `kb.org_id` 列）

```sql
-- 新表
CREATE TABLE orgs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  invite_code TEXT NOT NULL UNIQUE,          -- 8 位随机码，邀请即凭码 join
  created_by UUID NOT NULL,                  -- 创建者，自动成为 admin
  storage_quota_bytes BIGINT NOT NULL DEFAULT 536870912,  -- 512MB
  storage_used_bytes  BIGINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE org_members (
  org_id  UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  user_id UUID NOT NULL,
  role    TEXT NOT NULL CHECK (role IN ('admin','member')),
  joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, user_id)
);
CREATE INDEX idx_org_members_user ON org_members(user_id);

-- 存量表仅加一列，零数据回填
ALTER TABLE knowledge_bases
  ADD COLUMN owner_type TEXT NOT NULL DEFAULT 'user'
  CHECK (owner_type IN ('user','org'));
-- tenant_id 语义: owner_type='user' 时为 user_id, ='org' 时为 org_id
```

理由（对比 `kb.org_id` 可空列方案）：
- 存量行 `owner_type='user'` 默认值即正确，**迁移零回填**；documents/chunks/ingest_jobs 等下游表全部沿用现有 `tenant_id` 传播链，一行不改。
- Qdrant payload 的 `tenant_id` 字段无需新增字段或双写（见第 5 节），org 库的点位直接写 `tenant_id=org_id`。
- 若用可空 `org_id` 列，所有查询、RLS、payload 都要 `COALESCE`/双分支，侵入面大一倍。
- "官方库"未来 = 平台预置 org，一行 schema 都不用加。

### RLS 扩展（兼容 asyncpg 事务级 GUC 模式）

现有 UoW 在事务开头 `SELECT set_config('app.tenant_id', $1, true)`。**扩展为再 set 两个 GUC**，不引入 SECURITY DEFINER 函数（避免 RLS 递归查 org_members，也和现有"应用层解析、GUC 下发"模式一致）：

```python
# UoW.begin() 内，成员关系每请求解析一次（org_members 极小，可加 60s Redis 缓存）
await conn.execute("SELECT set_config('app.tenant_id', $1, true)", user_id)
await conn.execute("SELECT set_config('app.org_ids', $1, true)", ",".join(member_org_ids))       # 含 admin
await conn.execute("SELECT set_config('app.org_admin_ids', $1, true)", ",".join(admin_org_ids))
```

```sql
-- knowledge_bases（documents/chunks 同构复制一份）
DROP POLICY kb_tenant_isolation ON knowledge_bases;

CREATE POLICY kb_select ON knowledge_bases FOR SELECT USING (
  tenant_id = current_setting('app.tenant_id')::uuid
  OR tenant_id::text = ANY(string_to_array(current_setting('app.org_ids', true), ','))
);

CREATE POLICY kb_write ON knowledge_bases FOR ALL USING (
  (owner_type = 'user' AND tenant_id = current_setting('app.tenant_id')::uuid)
  OR (owner_type = 'org' AND tenant_id::text = ANY(string_to_array(current_setting('app.org_admin_ids', true), ',')))
) WITH CHECK ( /* 同 USING */ );
```

要点：
- `current_setting(..., true)` 带 missing_ok，未设置返回 NULL，`string_to_array(NULL)` 为 NULL、`ANY` 不匹配——**老代码路径与 ARQ worker 不设新 GUC 也不会炸**。
- ARQ 摄取 worker 无需改动逻辑：worker 处理 org 库任务时把 `app.tenant_id` 直接设为该 kb 的 `tenant_id`（org_id），命中 select/write 策略第一分支，天然放行。
- `orgs`/`org_members` 表 RLS：`org_members` 放行 `user_id = app.tenant_id OR org_id IN app.org_ids`（成员可看同组名单）；`orgs` 放行 `id IN app.org_ids`；join-by-code 用一条 `SECURITY DEFINER` 的极小函数或 app 层 service-role 连接查 invite_code，二选一取现有代码里已有的越权查询惯例。
- 应用层仍在 service 里显式校验 org admin 角色后再走写路径，RLS 只做兜底（RLS 拒绝会抛底层错误，不适合直接当 403 用）。

---

## 2. API 面

全部复用现有路由风格，新增一个 `orgs` 路由组，KB 路由只做参数扩展：

```
POST   /orgs                       # 建 org，创建者自动 admin，返回 invite_code
POST   /orgs/join                  # body: {invite_code} → 成为 member
GET    /orgs/me                    # 我的 org 列表 + 我的 role
GET    /orgs/{org_id}/members      # 成员列表（成员可读）
DELETE /orgs/{org_id}/members/{uid}  # admin 移除成员（廉价，保留）
```

- **不做**：邮件邀请、邀请码轮换、转让 owner、member 升 admin —— 全部 P1。
- Org 库 CRUD **不新开路由**：`POST /kbs` 增加可选 `org_id` 字段，带了就校验 admin 身份后以 `owner_type='org', tenant_id=org_id` 落库；`GET /kbs` 返回个人库 + 所有已加入 org 的库，每项带 `owner_type / org_name / writable` 三个标记字段。上传/删除文档走现有端点，双层（service 校验 + RLS）拦截 member 写。
- **不做显式"挂载"动作**：入会即自动挂载（RAGFlow/Dify 同款语义），省一张 mount 表和两组端点。

### 唯一决定：chat 请求 `kb_id` 扩展为 `kb_ids: list[UUID]`（不做 `scope=all`）

理由：
- 你们有 record/replay 四模式和评估闭环，`scope=all` 的隐式范围会随 org 新增库而漂移，**破坏回放确定性和评估可复现性**；显式 `kb_ids` 是纯函数输入。
- 前端本来就要做 KB 多选 checkbox，`kb_ids` 与 UI 一一对应，引用溯源也能明确标注来自哪个库。
- `scope=all` 留作 P1 语法糖（服务端展开成 kb_ids 后走同一路径）。
- 兼容：服务端接受旧 `kb_id` 单值并归一化为单元素列表，前端一次性切换。

---

## 3. 联合检索合并策略

### 唯一决定：单次查询 + Qdrant filter `MatchAny`（不做逐库检索后 RRF 合并）

```python
qfilter = Filter(must=[
    FieldCondition(key="kb_id",     match=MatchAny(any=[str(k) for k in kb_ids])),
    FieldCondition(key="tenant_id", match=MatchAny(any=[str(user_id), *[str(o) for o in org_ids]])),  # 纵深防御
])
```

理由：
- 所有库共用同一 collection、同一 BGE-M3 嵌入，**分数天然同尺度可比**，单次全局 top-K 的排序严格优于逐库 RRF——RRF 的价值在于合并不可比分数（你们已经在 dense/BM25 两路间用它，那一层保留不动，只是把两路各自的 filter 从单 kb_id 换成 MatchAny）。
- 逐库检索是 N 次网络往返，与 P0 的真 token 流式的首包延迟目标冲突；且逐库 top-k 再合并会给小库/无关库保底名额，污染 rerank 输入。
- 现有管线改动量约为"构造 filter 的一个函数"，rerank、引用溯源全部不动。
- 前置一步：`kb_ids` 先过 PG（RLS 可见性）校验，任何不可见 id 直接 403——**Qdrant filter 是隔离兜底，不是授权来源**。
- 确认 `kb_id` payload 建有 keyword 索引（没有就在迁移脚本里 `create_payload_index` 补上）。

---

## 4. 配额与审核

- **Org 存储配额**：`orgs.storage_quota_bytes` + `storage_used_bytes` 计数列，文档创建/删除时在同一 UoW 事务内增减（避免 SUM 全表扫）；上传端点预检，超限返回 `413` + 明确的剩余额度信息。个人库配额沿用现状不动。
- **上传审核流：本期砍掉。** 决定理由：org 库唯一的写入者就是 org admin（受信角色），审核流 = 状态机 + 审核队列 + 两个前端页，评委价值近零；注入防御已在摄取阶段扫内容，安全叙事不缺。只补一个近零成本的替代：`documents` 上若无 `uploaded_by` 列则加一列，演示时可展示"谁传的"，即最小审计。

---

## 5. 向量隔离

- Qdrant payload `tenant_id` **语义与 PG 同步泛化为 owner_id**：org 库摄取时写 `tenant_id=org_id`。
- **迁移成本：零。** 存量点位全是个人库，`tenant_id=user_id` 在新语义下仍然正确，不需要任何 payload 改写脚本。
- 代码改动仅两处：摄取管线取 kb 的 `tenant_id` 原样写入（本来就是这么做的话则零改动）；检索侧 filter 从 `tenant_id == user_id` 改为第 3 节的双 MatchAny。

---

## 6. 分阶段实施清单

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| P0-1 迁移与模型 | alembic：orgs / org_members / kb.owner_type / uploaded_by；SQLAlchemy 模型 | `alembic upgrade head` 与 `downgrade -1` 干净往返；存量测试全绿 |
| P0-2 RLS + UoW | 三 GUC 下发；kb/documents/chunks 的 select/write 策略拆分 | SQL 级测试：成员可 SELECT org 库、非成员不可见、member UPDATE org 库被 RLS 拒绝；现有个人隔离测试不回归；worker 摄取 org 库任务成功 |
| P0-3 Org API | 建 org / 凭码 join / 列表 / 成员管理 | pytest 流程：A 建 org → B 凭码 join → 双方 `/orgs/me` 角色正确；错误码 join 返回 404 |
| P0-4 Org 库 CRUD + 摄取 + 配额 | `/kbs?org_id` 建库；admin 上传走完两阶段摄取；配额预检 | admin 上传→摄取完成、Qdrant 点位 `tenant_id=org_id`；member 上传→403；超配额→413 |
| P0-5 联合检索 | chat `kb_ids`；MatchAny filter；kb_ids 可见性预校验 | 答案仅存在于 org 库的问题，成员携 `kb_ids=[个人,org]` 提问 → 命中且引用指向 org 库文档；非成员传 org kb_id → 403；replay 模式结果确定 |
| P0-6 前端最小页 | org 页（建/join/邀请码复制）；KB 列表分组"个人/机构"+ 只读徽标；chat KB 多选 | 黄金路径演示脚本端到端：建 org → 拉人 → admin 传教材 → 成员联合检索提问出带溯源答案 |

依赖链：1→2→(3‖4)→5→6；3 与 4 可由两个子智能体并行。

**演示叙事建议**：这条切片让平台从"个人学习工具"升级为"机构级教学平台"——教研组长上传统一教材，全班学生的问答/出题/复习自动共享同一知识底座，且 RAGFlow 至今未实现的跨库联合检索我们已跑通。

Sources:
- [Dify: Team Members Management](https://legacy-docs.dify.ai/guides/management/team-members-management)
- [Dify Docs: Manage Members](https://docs.dify.ai/en/use-dify/workspace/team-members-management)
- [Dify Issue #31184: app-level permission control (Only me / All team members / Selected members)](https://github.com/langgenius/dify/issues/31184)
- [RAGFlow Docs: Manage team members](https://ragflow.io/docs/manage_team_members)
- [RAGFlow Docs: Join or leave a team](https://ragflow.io/docs/v0.25.5/join_or_leave_team)
- [RAGFlow Issue #15812: Cross-Tenant Knowledge Federation (feature request)](https://github.com/infiniflow/ragflow/issues/15812)
- [RAGFlow Issue #5625: How can team members share knowledge bases](https://github.com/infiniflow/ragflow/issues/5625)
- [RAGFlow Issue #10609: Permission issue of knowledge bases](https://github.com/infiniflow/ragflow/issues/10609)
- [Coze 团队空间使用（CSDN）](https://blog.csdn.net/weixin_46218781/article/details/140387737)
- [Coze 工作空间/共享空间说明（CSDN）](https://blog.csdn.net/2201_75637163/article/details/145621355)