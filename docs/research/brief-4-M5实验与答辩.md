# 知识库+学习类 AI 交互设计调研简报 (2025-2026)

## 一、Google NotebookLM

1. **引用三段式交互 (行业标杆)**: 引用以**灰色圆角数字角标**内嵌在回答句中; **悬停**弹出浮层显示被引原文片段(不离开聊天区即可核对); **点击**则左侧来源查看器自动打开对应文档、**滚动到被引段落并高亮**。角标在"保存的笔记"里同样可点。
2. **来源面板 = 范围控制器**: 每个来源前有**复选框**, 勾选/取消即时限定本轮问答检索范围; 点开单个来源自动生成 **Source guide**(摘要 + "关键主题"按钮组, 点主题按钮即发起对应提问)。上限 50 来源/笔记本、单来源 50 万词, 支持 PDF/TXT/Markdown/Google Docs/Slides/URL/YouTube。
3. **Studio 一键生成物矩阵**: Audio Overview(双主播播客, 可"Join"插话提问)、Video Overview(讲解视频)、Mind Map(节点可点击继续提问)、Reports(学习指南/简报 Briefing/FAQ/时间线)、Flashcards、Quiz——全部是"点一个按钮、后台异步生成、生成后驻留在 Studio 栏"的模式, 2025 年扩展到 80+ 语言。
4. **建议问题 chips**: 输入框上方常驻针对当前来源自动生成的建议问题, 点击即发送; 回答尾部固定 **Save to note** 按钮, 存为蓝色"Saved Response"笔记(可改标题不可改内容)。
5. **Discover sources**: 输入主题自动全网发现候选来源, 用户勾选后批量导入——把"冷启动没资料"变成引导流程。

## 二、腾讯 ima / 元宝、字节豆包

1. **ima 采集入口矩阵**: 微信公众号文章长按分享→"用 ima 打开"一键入库; 网页 **URL 粘贴到"导入链接"输入框自动解析正文**; 浏览器插件剪藏; 本地拖拽(19 种格式含 PDF/Office/图片/音频); 微信聊天文件经小程序导入。核心是"在内容消费现场一步入库"。
2. **ima 问答范围语法**: 输入框键入 **@ 选择指定知识库**, 回答 = 全网内容 + 私域知识库混合, 且所有回答标注出处、一键跳转溯源; 支持**直接修改上一条提问重新生成**(不必重开会话)。
3. **ima 划词唤醒**: 阅读/笔记界面选中任意文字浮出 AI 菜单(解读/翻译/续写/润色); 笔记面板的 AI 写作可**引用知识库文献作参考**并多轮追问。
4. **ima 共享知识库+广场**: 个人库/共享库两级, 微信邀请成员并设编辑/查看权限, 可发布到"知识库广场"公开订阅(百万成员规模, 不占个人空间)——个人→机构共享的现成范式。
5. **元宝/豆包**: 元宝联网回答带**信源角标(公众号名+发布日期)可点击溯源**, 信源即生态内容(2B+ 公众号文章); 豆包电脑版主打**全局划词 AI**(任意应用内选中文字→搜索/翻译/解释/总结)和 **"AI 看网站"**(对当前网页一键生成总结/播客/脑图), 对话内原生输出思维导图/流程图。

## 三、Coze(扣子) / Dify

1. **Dify 召回测试页**: 知识库详情左侧栏独立"召回测试"入口: 输入模拟 query → 返回命中分段卡片列表(**每段显示相似度分数、分段全文、来源文档名**); 页内可临时切换检索模式/top-k/score 阈值, **仅对当前测试会话生效**不影响线上; "记录"区留存所有检索事件(含线上应用真实请求)可逐条回放。
2. **Dify 引用与归因开关**: 应用编排里打开"引用和归因"后, 用户端回答下方折叠展示命中分段+所属文档, 点击展开原文——引用是应用级可配置项而非写死。
3. **分段预览可编辑**: Coze/Dify 上传文档时实时预览切片结果, 支持自定义分段(分隔符/最大长度/重叠), **单个分段可手动编辑、禁用/启用**——把"清洗数据"做成了 UI 操作。
4. **知识库分级权限**: Dify 每个知识库有可见性设置(仅自己/全部团队成员/指定成员); Coze 按个人空间/团队空间隔离资源——"个人库 vs 团队库"是空间+单库权限两层, 不是两套系统。

## 四、Khanmigo / 多邻国 Max

1. **Khanmigo 分层提示、绝不直出答案**: 典型链路: "先看题目要我们求什么?"→学生作答→"我们已知哪些信息? 试试某公式"→学生卡住可主动点**要提示**→给一个小 hint 或**一道相似的已解例题**(worked example), 始终不给本题答案; 写作类同理(学生要太阳的比喻, 它只给肯定性反馈+引导问题)。
2. **嵌入练习现场**: 辅导不在独立聊天页, 而是内嵌在题目页侧栏, AI 天然拿到题目上下文——"在做题处辅导"而非"去聊天处问"。
3. **教师/家长可见性**: 全部对话记录可回看; 仪表盘自动汇总"谁掌握了/谁需要帮助"并推荐练习; 越界内容触发通知家长与教师的警报——安全感是采纳前提。
4. **Duolingo "Explain My Answer"**: 答题后出现入口按钮, 点击进入与 Duo 的聊天卡片, 解释这道题为何对/错, 可继续要更多例子——**解释入口长在错题上**, 不是让用户自己去描述问题。
5. **Duolingo Video Call with Lily 的"事后复盘"设计**: 对话进行中**刻意不纠错不打断**(降低开口焦虑), 结束后给复盘页: 完整转写稿 + 做得好/待改进清单 + 可查看对方台词翻译; Lily 按学习者水平自适应并记住历史对话。

## 五、对本项目的 8 条落地建议

| # | 建议 | 成本 | 感知价值 |
|---|------|------|----------|
| 1 | **引用交互照抄 NotebookLM 三段式** (P0): 数字角标内嵌句中 + 悬停浮层显示 chunk 原文 + 点击右侧抽屉打开源文档按 chunk 偏移量/页码滚动并高亮。摄取时已有 chunk 元数据, 关键是给 chunk 存 char offset/页码。演示时评委必点 | 中 | **高(演示杀手锏)** |
| 2 | **追问 chips** (P0): 问答子图最后一节点顺带结构化输出 3 个追问(同次调用, 零额外成本), 渲染为输入框上方 chips 点击即发; 加一个学情驱动的固定 chip "考考我这个知识点"(直连出题子图), 体现五子图联动 | 低 | 高 |
| 3 | **摄取完成自动生成"来源指南"** (P1 提前): 对齐 NotebookLM Source guide——摄取完成回调触发 ARQ 任务生成摘要 + KC 知识点 chips(词表已有, 点击即提问); Studio 式按钮"学习指南/简报/测验"复用出题子图, 一键异步生成 | 中 | 高 |
| 4 | **来源级 checkbox + @知识库范围控制**: 前端来源列表加复选框, 问答请求带 source_ids 走 Qdrant payload 过滤(现有 tenant 过滤同机制, 改动极小) | 低 | 中 |
| 5 | **检索命中测试页** (照 Dify): 管理端输入 query → 展示 BM25/向量/RRF/rerank 各阶段分数与排名变化的命中分段列表。你的混合检索链路比 Dify 复杂, 这一页就是向清北评委展示技术深度的窗口, 与成本面板并列 | 低-中 | **高(评委向)** |
| 6 | **辅导前端做 Khanmigo 式提示阶梯** (P0): 辅导子图内置提示层级状态机(引导问题→小提示→大提示→相似例题), 前端常驻"要个提示"按钮, 每次点击升一级, 永不直出答案; 答对时给肯定语 + BKT 掌握度上涨的可视化动画(数值跳动/进度条), 把已有 BKT 变成即时奖励 | 中 | 高 |
| 7 | **辅导会话结束复盘卡** (照 Lily): 会话中不打断纠错, 结束时生成复盘: 本次覆盖 KC、暴露的错因、掌握度变化曲线、FSRS 计算的下次复习时间——把 BKT+FSRS+辅导三条线在一张卡上闭环, 直接作为答辩叙事 | 中 | 中-高 |
| 8 | **URL 导入先行、剪藏插件后置** (P1): 粘贴 URL→readability 抽正文→走现有两阶段摄取, 导入时展示解析预览与分段预览(可编辑/禁用单分段, 照 Coze); 浏览器插件工作量大且演示增益小, 竞赛周期内不做 | 低(URL)/高(插件) | 中 |

优先级排序建议: #1、#2、#5 是"低成本高演示回报"组合, 应最先做; #6、#7 构成辅导页的差异化叙事; #3、#4、#8 视 M5 前剩余时间取舍。

Sources:
- [FSU: How do NotebookLM's inline citations work](https://servicecenter.fsu.edu/s/article/How-do-NotebookLM-s-inline-citations-work-and-why-are-they-important)
- [DataCamp NotebookLM Tutorial](https://www.datacamp.com/tutorial/notebooklm)
- [Learn Prompting: NotebookLM Guide](https://learnprompting.org/blog/notebooklm-guide)
- [Nanobits: NotebookLM Ultimate User Guide](https://nanobits.beehiiv.com/p/notebook-lm-the-ultimate-user-guide-with-real-life-examples)
- [Yahoo Tech: NotebookLM Discover Sources](https://tech.yahoo.com/articles/notebooklm-now-help-discover-sources-191535900.html)
- [53AI: 腾讯 IMA Copilot 深度解析](https://www.53ai.com/news/zhishiguanli/2025071526940.html)
- [AIGC工具导航: ima 知识库全方位解析](https://www.aigc.cn/ima-knowledge-base)
- [知乎: 2025 腾讯IMA实测评测](https://zhuanlan.zhihu.com/p/1977413402281398572)
- [53AI: 腾讯产品接入 DeepSeek 实测](https://www.53ai.com/news/LargeLanguageModel/2025021824719.html)
- [量子位: 豆包电脑版](https://www.qbitai.com/2024/12/237208.html)
- [Dify Docs: Knowledge Base](https://docs.dify.ai/en/guides/knowledge-base/readme)
- [知乎: 扣子 Coze 知识库全能指南](https://zhuanlan.zhihu.com/p/1934004233906128565)
- [Medium: Dify vs Coze Studio 对比](https://medium.com/@cyan747/comparison-of-ai-development-tools-key-difference-facts-between-dify-and-coze-studio-open-source-3a3657b0a60c)
- [AVID Open Access: Khanmigo as AI Personal Tutor](https://avidopenaccess.org/resource/khanmigo-as-an-ai-personal-tutor-and-assistant/)
- [NeuralClass: Khanmigo in the Classroom](https://neuralclass.uk/articles/khanmigo-in-the-classroom-what-teachers-actually-get)
- [Class Central: ChatGPT on Duolingo & Khan Academy](https://www.classcentral.com/report/chatgpt-on-duolingo-khan-academy/)
- [duoplanet: Duolingo Video Call](https://duoplanet.com/duolingo-video-call/)
- [Talkpal: Duolingo Video Call with Lily](https://talkpal.ai/how-to-ace-your-duolingo-video-call-with-lily-top-tips-and-tricks/)