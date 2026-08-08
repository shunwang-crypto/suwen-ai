# 安全、隐私与残余风险

> 状态：Active
> 目标读者：开发、运维、安全审查与产品负责人
> 本文区分“代码防线已经实现”和“生产运营仍需完成”，不把设计或比赛证据当成合规资质。

## 已实现的工程防线

| 防线 | 当前实现 | 主要验证 |
|---|---|---|
| 认证 | bcrypt、access token（`iat/exp` 仅允许 5 秒有界时钟漂移）、httpOnly refresh cookie 轮换、重放撤销、登出黑名单、独立限流 | auth 生命周期与限流测试 |
| 邮箱所有权与邮件滥用 | 图形验证码前置、6 位 HMAC 验证码、10 分钟 TTL、单次消费、5 次错误失效、邮箱/IP 分钟与日限额、60 秒冷却 | 邮箱验证码单元/认证集成测试 |
| 租户隔离 | 服务层所有权校验 + PostgreSQL ENABLE/FORCE RLS + 非超级应用角色 | 双账号、RLS、连接池复用测试 |
| 机构权限 | 机构成员只读共享知识库，机构管理员写；平台管理员与机构管理员分离 | org 集成测试 |
| 文件上传 | 后缀、大小、内容签名/魔数、空文件与 UTF-8 校验，文件名规范化 | RAG、错题导入、题目资料测试 |
| 原文件读取 | 本人/可见资源校验，经 API 流式返回，`nosniff` 和安全 Content-Disposition | 来源读取和越权测试 |
| SSRF | 协议白名单、DNS/IP 检查、逐跳重定向复核、响应类型/大小/超时限制 | URL 导入测试 |
| 内容审核 | 输入在 SSE 首字节前检查；输出按段审核并可截断；危机提示提供本地热线文案 | moderation 单元与流式测试 |
| 提示词注入 | 引用内容标记为资料而非指令、检索文本清洗、辅导禁答状态机 | prompt injection 与 tutor 测试 |
| 预算与滥用 | 用户/全局日预算、软硬阈值、限流、上传/文档/chunk/存储配额 | budget、quota 测试 |
| 待核验隔离 | pending 题、错题和未核验作答不进入 FSRS、掌握度、画像、报告或推荐 | error/resource/report 测试 |
| 删除一致性 | 数据库事务写 cleanup outbox，Worker 重试清理向量、对象、缓存和 checkpoint | lifecycle 测试 |
| 导出与注销 | 异步 ZIP、文件 manifest、缺失对象记录；密码复核后级联和外部清理 | account/lifecycle 测试 |

## 租户和权限模型

应用事务设置 `app.tenant_id`、`app.org_ids`、`app.is_admin`。应用数据库角色必须是
`NOSUPERUSER NOBYPASSRLS`；如果生产误用超级用户，RLS 会被静默绕过，因此部署验收必须
显式检查当前角色。

个人资源查询同时校验租户和 owner。不可见资源通常返回 404，避免攻击者根据 403 区分
“存在但无权”和“不存在”。管理员跨租户聚合只允许已列出的运营表和端点，不提供任意 SQL。

机构成员关系只扩展共享知识库读权限。机构题目资料共享尚未实现，不能因为用户属于同一
机构就放宽个人题目资料、作答、错题或画像权限。

## 文件和对象存储

### 上传

- 知识库、聊天附件、错题来源和题目资料各自使用显式格式白名单与大小上限。
- PDF、Office、图片等检查内容签名；文本要求可解码；不能只相信客户端 MIME。
- 文件名去除路径、换行和控制字符；对象 key 由服务端生成。
- Word/PPT 等不安全 inline 的类型使用 attachment；响应带 `X-Content-Type-Options: nosniff`。
- DOCX/PPTX 额外检查容器结构、成员数、解压总量、压缩比和加密成员；扫描 PDF 只渲染业务
  请求范围内的页面（辅导为用户选页，知识库为受限整份摄取），并限制总页数、视觉页数、
  DPI、像素、单图大小、并发和单页/整份超时。
- 分级辅导的图片 Base64 只存在于 worker 到平台视觉模型的一次内存请求中，不写 Redis job、
  LangGraph checkpoint、日志或 trace；用户 DeepSeek Key 不参与任何视觉请求。

### 删除

关系库是删除状态的权威来源。对象删除失败时，业务接口不得把数据库行先删掉并返回成功；
需要保留可重试状态或写 `PendingCleanup`。定时 Worker 应监控失败次数和积压。

### 导出

导出包中的 `uploads_manifest.json` 记录来源、对象是否可用和错误。缺失文件不能阻断其他
数据导出，也不能生成空文件冒充原件。导出下载必须验证当前用户，设置有效期，并防止对象
key 越权。

## 未成年人保护

当前已经具备年龄声明、监护同意字段、输入/输出安全审核、危机文案和用户数据删除能力，
但不具备真人身份或监护人身份核验，也没有完整防沉迷时段控制。

生产面向未成年人前还需要产品和法务确认：

1. 适用年龄、监护同意流程和隐私告知；
2. 云内容安全主审与人工升级流程；
3. 使用时长、夜间限制和危机事件处理；
4. 对话、画像、文件和日志的留存期限；
5. 第三方模型的数据处理协议和跨境/出境评估。

本地规则审核是第一道工程防线，不应被描述为完整的未成年人合规方案。

## 数据最小化和第三方模型

### 多供应商模型路由

启用 `MODEL_ROUTING_ENABLED=true` 后，网关按业务路由选择候选，而不是把 tier 当作供应商：
普通文本为 OpenCode Zen 免费模型 → SiliconFlow/Qwen，视觉为 OpenCode Zen/MiMo →
SiliconFlow/Qwen VL，学习计划为官方 DeepSeek → OpenCode 免费模型 → SiliconFlow/Qwen。
每个候选有独立的模型、端点、能力、熔断器和客户端缓存；视觉与学习计划始终使用平台凭据。
OpenCode 的 `public` 认证可以用于验证，但不是 OpenCode 官方保证的长期生产认证方式，生产上线前
必须实际验证并准备真实 Key。免费候选调用前不预扣钱包额度；只有准备调用付费候选时才预算预检，
最终 usage 记录实际 provider、model、token、凭据来源和成本。

视觉 JSON 请求采用“视觉模型忠实转写 Markdown → 文本模型结构化 JSON”两阶段流程，避免向不支持
JSON Mode 的 MiMo/Qwen VL 发送 `response_format`。流式请求只有首 token 前允许切换候选，已经输出
内容后不切换并按部分用量结算。

免费第三方通道可能将请求用于服务改进；平台不承诺零保留或完全私密。不要上传身份证、成绩单姓名页
或其他敏感资料。管理员可通过 `MODEL_ROUTING_ENABLED=false` 关闭该路由。

### 认证邮件与生产密钥

- 图形验证码用于防机器人，邮箱验证码用于证明邮箱所有权；普通登录不要求邮箱验证码。
- 生产 `REGISTRATION_MODE=invite|open` 缺少 SMTP 时启动失败，不会回退 `DevEmailSender`；找回密码继续复用同一
  `EmailSender`，并保持恒定防枚举响应。
- Resend 使用 SMTP：主机 `smtp.resend.com`、用户 `resend`、API Key 仅作为服务器端
  `SMTP_PASSWORD`。发件人必须来自 Verified 的 `mail.adfgt.xyz` 域名，建议配置 SPF、DKIM、
  DMARC 和 `SMTP_REPLY_TO`。
- 日志只写收件邮箱哈希、事件和异常类型，不写收件人明文、验证码、Key、密码、重置链接或
  完整正文；测试只使用 fake sender。
- 公开注册和邀请码注册只创建 `student`；教师和管理员通过受控流程产生。邀请码只保存安全哈希，
  原文仅在管理员创建响应中返回一次。
- 管理员额度调整只允许平台管理员，金额使用 Decimal 并限制为每次 -1000 至 1000、最多四位小数；
  原因和业务凭证必填。0015 的 `(user_id, ref)` 条件唯一索引与钱包账户行锁共同保证并发重试不重复入账，
  调整、流水和余额更新在同一事务内完成。

- 发送给模型的内容应限于当前任务需要的消息、检索片段和定性画像。
- 掌握度精确小数不进入第三方 LLM；提示中使用定性档位。
- 不把学生答案、错题原文件或完整画像自动写入普通知识库。
- Langfuse 等观测系统默认不记录正文，生产必须复核 `LANGFUSE_LOG_CONTENT`。
- record 磁带、故障日志和导出包可能包含敏感内容；磁带不得进入仓库，提交或共享前必须人工审查。
  用户 BYOK 和视觉/OCR 请求在网关层禁止读取或写入共享磁带；BYOK 即使显式回退平台也不例外。
- 生产密钥只来自环境/密钥系统，不能进入仓库、镜像层、日志或客户端 bundle。
- 0017 用户 DeepSeek Key 使用 AES-256-GCM 加密，AAD 绑定用户、provider 和 key version；明文只存在当前网关请求作用域，不进入 `ChatRequest`、LangGraph state、Redis job、cassette、Langfuse、usage_log、导出 ZIP 或管理员响应。生产开启 BYOK 前必须注入由随机数生成的 32 字节主密钥，缺失或非法时启动失败。
- 成功使用用户 Key 的文本调用不扣平台钱包，但仍记录 token；平台回退、视觉、Embedding、Rerank、搜索、解析和存储继续按平台额度与限流规则执行。

## 数据留存、导出与删除

`DELETE /api/account` 需要密码复核，删除用户关系数据并异步清理外部状态。`usage_log` 在
用户删除后将 user ID 置空，用于去标识化成本审计。备份中的历史数据需要依照公布留存期
自然淘汰，并限制恢复权限；恢复旧备份后必须重新执行删除事件对账，避免已注销数据复活。

FSRS ReviewLog 与统一 learning event 属于用户学习证据：写入时带 tenant/RLS 和业务 UTC
时间，随用户外键级联删除并进入导出；当前 retention worker 按 730 天清理。0022 不从历史
card 反推 review，避免把不可验证的推测伪装成学习事实。

代码中存在默认或建议留存周期不等于已经获得用户同意。正式上线前应把账户数据、模型日志、
审计日志、备份、导出包和对象垃圾的保留时间写入隐私政策和运维任务。

## 生产上线前必须完成

- [ ] 替换所有示例密码、数据库密码、MinIO 凭据和 JWT secret。
- [ ] 验证 `APP_ENV=prod`、`GATEWAY_MODE=live`，关闭 replay fallback 和私网 URL 导入开关。
- [ ] TLS、Secure Cookie、可信域名、反代真实客户端 IP和限流口径完成验证。
- [ ] 平台管理员、故障注入、时间旅行、模拟支付和 API 文档配置符合生产策略。
- [ ] PostgreSQL、对象存储、Qdrant 和 Redis 的访问控制、加密、备份及恢复演练完成。
- [ ] 接入语义级内容安全主审，建立危机和举报的人工响应流程。
- [ ] 明确隐私政策、用户协议、未成年人规则、留存期限和第三方处理者清单。
- [ ] 建立安全告警、依赖漏洞扫描、密钥轮换和数据事件响应流程。

## 残余风险

1. **DNS rebinding**：URL 导入分别验证 DNS 和实际 HTTP 连接，理论上仍可能在两次解析间
   变化。生产建议使用固定解析结果的 transport 或受控出网代理。
2. **本地审核规则上限**：拆字、编码变体和无关键词语义风险可能绕过规则；需要云审核与
   人工升级，不应只扩大词表。
3. **年龄自报**：缺少真人和监护人身份核验，必须在产品告知中如实说明。
4. **运营端高权限**：故障注入、时间旅行、封禁和评估具有较大影响，生产需要网络隔离、
   二次确认和审计告警。
5. **恢复导致删除回滚**：从旧备份恢复可能重新引入已删除数据，需要 deletion ledger 或
   恢复后对账流程。
6. **合规资质**：仓库中的工程措施不代表已经取得 ICP、等保、算法备案或其他地区资质。

## 验证入口

~~~bash
make test-unit
INTEGRATION=1 python -m pytest tests/integration/test_tenant_isolation.py -q
INTEGRATION=1 python -m pytest tests/integration/test_prompt_injection.py -q
INTEGRATION=1 python -m pytest tests/integration/test_import_guide.py -q
INTEGRATION=1 python -m pytest tests/integration/test_error_imports.py -q
INTEGRATION=1 python -m pytest tests/integration/test_learning_resources.py -q
~~~

更详细的数据关系见[数据模型](data-model.md)，生产操作见[部署手册](../deploy/README-deploy.md)。
