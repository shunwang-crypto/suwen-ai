# EduAgents 个性化学习者模型与自适应机制设计

> 目标：为 EduAgents 的个性化讲解、学习问答、学习计划和自适应练习提供统一的用户建模与决策机制。  
> 核心思想：系统不是维护一份静态“用户画像”，而是维护一个会随学习行为持续变化的 **Dynamic Learner Model（动态学习者模型）**，并根据不同场景按需读取不同状态。

---

## 1. 为什么需要 Dynamic Learner Model

普通 LLM 个性化通常只做：

- 记住用户喜欢代码还是图解；
- 记住用户的背景和目标；
- 将历史摘要塞进 Prompt。

这只能实现“表达方式个性化”，还不是真正的“教学自适应”。

教育场景真正需要系统持续回答：

1. 用户现在会什么、不会什么？
2. 用户为什么不会：没学过、前置缺失，还是存在错误理解？
3. 哪些知识已经掌握，但可能因为长时间未练习而需要复习？
4. 当前最适合学习哪个知识点？
5. 当前应该完整讲解、给例子、给提示，还是只做理解检查？
6. 下一道题应该练什么、难度多大？
7. 用户当前是否连续出错、依赖提示，需要临时降低认知负荷？
8. 用户长期更适合代码、图解、类比还是简洁解释？

因此，用户不应该只对应一个 `profile.json`，而应该对应一个动态状态：

\[
LearnerModel_t =
Profile + Goal + KnowledgeState_t + Misconception_t + Preference_t + SessionState_t
\]

同时保留原始交互证据：

\[
Evidence = InteractionEvents
\]

系统通过新证据持续更新学习者状态：

\[
L_{t+1}=Update(L_t,E_t,DomainModel)
\]

---

# 2. 整体架构

整个个性化系统分为三层：

```text
┌──────────────────────────────────────────────┐
│                Domain Model                  │
│                                              │
│ KC 定义 / prerequisite / transfer / KST-lite │
│ Item Bank / Q-matrix / Difficulty            │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│              Dynamic Learner Model           │
│                                              │
│ Profile / Goal                               │
│ KC Mastery / Confidence / Recency             │
│ Misconception                                │
│ Preference                                   │
│ Session State                                │
│ Semantic Memory                              │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│              Adaptive Engine                 │
│                                              │
│ 学什么 / 怎么讲 / 给多难 / 是否复习 / 下一题 │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
                     LLM
                       │
                       ▼
                    Student
                       │
                       ▼
               Interaction Events
                       │
                       └──────→ 更新 Learner Model
```

关键原则：

> **Adaptive Engine 决策，LLM 负责教学内容的生成和表达。**

不能完全依赖 LLM 自己猜测“用户会什么、该讲什么、难度多少、是否已经掌握”。

---

# 3. Domain Model：知识本身的模型

Domain Model 不是用户数据，而是所有用户共享的知识结构。

## 3.1 Knowledge Component（KC）

每个可诊断、可学习的知识单元定义成一个 KC。

例如：

```text
Transformer
├── vector
├── matrix_multiplication
├── dot_product
├── softmax
├── attention
├── self_attention
├── multi_head_attention
└── transformer_architecture
```

## 3.2 KC 关系

至少支持：

```text
prerequisite    前置关系
related         相关关系
transfer        可迁移关系
part_of         组成关系
```

例如：

```text
matrix_multiplication
        ↓ prerequisite
attention
        ↓
self_attention
        ↓
multi_head_attention
```

跨领域迁移：

```text
Java OOP
   ── transfer ──→
Python OOP
```

但 transfer 只能提供先验，不能直接认为用户已经掌握目标 KC。

---

# 4. KST-lite 在系统中的作用

不建议第一版完整枚举 Knowledge Space，而是采用：

```text
KC DAG
+
prerequisite relation
+
用户 mastery
+
reachable frontier
```

也就是 KST-lite。

它主要回答：

> **根据用户当前已经掌握的知识，现在“可以继续学什么”？**

例如：

```text
function
├── recursion
├── closure
└── OOP

closure
└── decorator
```

如果用户已经掌握 `function`，但还没掌握 `closure`，那么：

```text
当前可学习 frontier：

recursion
closure
OOP
```

`decorator` 虽然 mastery 可能最低，但因为 `closure` 前置没有满足，不应该优先学习。

因此：

```text
KST / prerequisite
→ 决定“能学什么”

Goal
→ 决定“哪些值得先学”

Mastery
→ 决定“哪些最需要学”

Recency
→ 决定“哪些需要复习”
```

---

# 5. Dynamic Learner Model 包含什么

## 5.1 Learner Profile

只保存相对稳定的信息。

```json
{
  "user_id": "u123",
  "level": "undergraduate",
  "background": [
    "python_basic",
    "linear_algebra_basic"
  ],
  "language": "zh-CN"
}
```

不应该在这里长期维护：

```text
weak = ["recursion"]
known = ["function"]
```

因为这些应该由 Knowledge State 动态推导。

---

## 5.2 Goal Model

回答：

> 用户现在为什么学？

例如：

```json
{
  "goal_id": "g01",
  "target": "两周内理解 Transformer 并能阅读基础实现",
  "deadline": null,
  "target_kcs": [
    "attention",
    "self_attention",
    "multi_head_attention",
    "transformer_architecture"
  ]
}
```

一个用户可以同时存在多个学习目标。

---

## 5.3 Knowledge State

这是整个 Learner Model 最核心的部分。

建议每个用户 × KC 保存：

```json
{
  "kc_id": "python.recursion",
  "mastery": 0.63,
  "confidence": 0.82,
  "status": "learning",

  "last_practiced_at": "2026-08-09T20:32:00",
  "last_assessed_at": "2026-08-09T20:32:00",
  "last_evidence_at": "2026-08-09T20:32:00",

  "evidence_count": 14,

  "correct_count": 9,
  "incorrect_count": 5,

  "model_type": "bkt",
  "model_version": "v1"
}
```

其中必须区分：

### Mastery

```text
mastery = 0.80
```

表示：

> 当前模型认为学生掌握这个 KC 的程度。

### Confidence

```text
confidence = 0.25
```

表示：

> 系统对这个 mastery 判断有多确定。

例如：

```text
mastery = .90
confidence = .20
```

意味着：

> 看起来会，但是证据很少，应该安排一次快速验证，而不是永久跳过。

---

# 6. known / weak 不再独立维护

不要同时维护：

```text
student_profile:
weak = ["recursion"]

mastery:
recursion = .82
```

否则很容易冲突。

推荐：

```text
mastery
   ↓ derive
status
```

例如：

```text
mastery < .40
→ weak

.40 <= mastery < .75
→ learning

mastery >= .75
→ mastered
```

实际阈值以后可以根据模型调整。

因此：

> `known / weak / mastered / learning` 是 Knowledge State 的派生视图，不是另一套 source of truth。

---

# 7. Misconception Model

只知道：

```text
recursion mastery=.35
```

还不够。

因为低 mastery 可能来自：

```text
没学过
前置知识不足
稳定错误理解
题目过难
一次粗心
```

因此需要显式维护 misconception。

例如：

```json
{
  "kc_id": "python.recursion",
  "misconception_id": "missing_base_case",
  "description": "认为递归调用会自然停止",
  "confidence": 0.91,
  "status": "active",
  "evidence_count": 3,
  "first_seen_at": "...",
  "last_seen_at": "..."
}
```

这会直接影响教学策略。

普通弱项：

```text
→ 多练习 / 补基础
```

稳定 misconception：

```text
→ 反例
→ 认知冲突
→ 针对性解释
→ 再验证 misconception 是否消失
```

---

# 8. Preference Model

Preference 负责：

> **怎样呈现内容更适合这个用户。**

例如：

```json
{
  "code": {
    "score": 0.82,
    "confidence": 0.76
  },
  "diagram": {
    "score": 0.61,
    "confidence": 0.55
  },
  "concise_first": {
    "score": 0.73,
    "confidence": 0.68
  }
}
```

不建议：

```json
{
  "prefers_code": true
}
```

因为用户偏好不是绝对的。

长期多次请求代码：

```text
code preference:
.61 → .68 → .74
```

而一次：

> “今天别给代码。”

优先只影响当前 session，不应该立即把长期偏好改成 false。

重要优先级：

```text
Pedagogical Need
>
Task Suitability
>
User Preference
```

即：

> 用户喜欢代码，不意味着所有知识都强行代码化。

---

# 9. Session State

Session State 表示：

> **用户此时此刻的学习状态。**

例如：

```json
{
  "current_goal_id": "g01",
  "current_topic": "attention",
  "current_kc": "attention.scaled_dot_product",

  "recent_accuracy": 0.40,
  "recent_error_streak": 3,
  "hint_requests": 2,

  "estimated_confusion": 0.72,
  "estimated_load": 0.67,

  "scaffold_level": 3
}
```

注意：

```text
今天连续做错 3 道
```

不能直接变成：

```text
长期 weak
```

因此：

```text
长期状态 → Knowledge State

短期状态 → Session State
```

需要严格区分。

---

# 10. Semantic Memory

除了结构化 Learner Model，还可以维护 LLM 可检索的长期语义记忆，例如：

```text
“用户之前通过 Java interface 很快理解了 Python Protocol。”

“解释 attention 时，小矩阵数值例子效果很好。”

“用户在实际项目中使用过 FastAPI。”
```

Semantic Memory 主要用于：

```text
类比
历史项目上下文
有效解释方式
长期学习经历
```

它不应该承担精确状态：

```text
recursion mastery=.63
```

精确 mastery 必须使用结构化数据库。

---

# 11. Interaction Events：所有更新的证据源

Interaction Event 建议 append-only。

例如：

```json
{
  "event_type": "ITEM_ANSWERED",
  "user_id": "u123",
  "session_id": "s001",
  "kc_id": "recursion",
  "item_id": "q103",
  "timestamp": "...",
  "payload": {
    "correct": false,
    "response_time_ms": 24000,
    "hint_count": 1,
    "attempt": 2
  }
}
```

其他事件：

```text
QUESTION_ASKED
EXPLANATION_DELIVERED
ITEM_PRESENTED
ITEM_ANSWERED
HINT_REQUESTED
CODE_SUBMITTED
PLAN_CREATED
PLAN_STEP_COMPLETED
CHECK_UNDERSTANDING
GOAL_CHANGED
```

原始 Event 永远保留。

因此以后：

```text
BKT → AKT
重新计算 preference
重新训练 difficulty
分析学习行为
```

都能基于同一套原始证据重新计算。

---

# 12. 时间如何影响 Learner Model

Mastery 不能被理解成永久不变：

\[
M_{u,k}=p
\]

应该理解成：

\[
M_{u,k}(t)
\]

数据库保存：

```text
mastery_raw = .82
last_evidence_at = 2026-05-01
```

当 2026-08-09 再使用时：

```text
Temporal Resolver
        ↓
考虑距离最后一次练习的时间
        ↓
effective mastery / review risk
```

例如：

```text
raw mastery       .82
last_seen         100 days ago
forgetting risk   high
```

最终：

```text
state = needs_refresh
```

### 不建议

每天后台：

```text
.82 → .81 → .80 → .79
```

不断修改数据库。

### 推荐

保存：

```text
上次可信状态
+
时间戳
```

然后在：

```text
生成计划
生成练习
知识讲解
进入相关 KC
```

时按需计算：

```text
effective_mastery
review_priority
```

后台任务只需要用于：

```text
生成到期复习任务
统计
提醒
```

---

# 13. 不同功能到底读取什么

这是个性化系统最核心的调用规则。

| 数据 | 学习计划 | 练习题 | 知识讲解 | 普通问答 |
|---|---:|---:|---:|---:|
| Profile | ✓ | △ | ✓ | △ |
| Goal | **✓✓** | ✓ | ✓ | △ |
| KC State | **✓✓** | **✓✓** | **✓✓** | 按需 |
| Misconception | ✓ | **✓✓** | **✓✓** | 按需 |
| Preference | ✓ | △ | **✓✓** | ✓ |
| Session State | △ | **✓✓** | **✓✓** | 按需 |
| Semantic Memory | ✓ | △ | ✓ | **✓✓** |
| Recent Events | 不直接全读 | 最近窗口 | 最近窗口 | 最近窗口 |
| KC / KST Graph | **✓✓** | **✓✓** | ✓ | 按需 |
| Item / Q-matrix | △ | **✓✓** | △ | — |

说明：

```text
✓✓  核心输入
✓   正常读取
△   条件读取
—   一般不需要
```

原则：

> **不同功能按需读取 Learner Model，而不是每个请求把所有用户信息全部塞给 LLM。**

---

# 14. 场景一：生成学习计划

例如用户说：

> 给我生成一个两周 Transformer 学习计划。

重点读取：

```text
Goal
+
KC / KST Graph
+
Learner KC State
+
Recency / Forgetting
+
Misconception
+
Preference
+
Relevant Semantic Memory
```

调用流程：

```text
User Goal
   ↓
解析 Target KCs
   ↓
查 KC Graph
   ↓
求 prerequisite closure
   ↓
读取用户相关 KC State
   ↓
分类：

mastered
needs_refresh
learning
weak
unseen
   ↓
计算 KST reachable path
   ↓
Goal Priority
   ↓
Preference 调整任务形式
   ↓
LLM 组织成最终学习计划
```

例如：

```text
matrix multiplication
mastery=.91
last_seen=3 days
→ 跳过

softmax
mastery=.85
last_seen=180 days
→ 快速检测

attention
mastery=.25
→ 重点学习

self_attention
前置 attention 未满足
→ 后置
```

### 生成计划以后写什么？

写：

```text
Goal
Plan
Planned KCs
PLAN_CREATED Event
```

不能因为：

> “生成了 attention 学习计划”

就：

```text
attention mastery .20 → .30
```

只有真实学习证据才能更新 mastery。

---

# 15. 场景二：生成练习题

例如：

> 给我出几道递归题。

重点读取：

```text
KC State
+
Misconception
+
Session State
+
KC Graph
+
Item / Q-matrix
+
Difficulty
+
Goal
```

首先判断：

```text
Assessment Mode
还是
Practice Mode
```

### Assessment Mode

目标：

> 尽快搞清楚学生到底会不会。

如果：

```text
mastery=.80
confidence=.20
```

说明：

> 看起来会，但证据不足。

应该出诊断信息量较高的题。

### Practice Mode

如果：

```text
mastery=.40
confidence=.85
```

说明：

> 系统已经比较确定用户不会。

这时目标不是继续测，而是促进学习。

---

练习题生成流程：

```text
GenerateExercise
      ↓
读取 KC State
      ↓
读取 Misconception
      ↓
读取 Session State
      ↓
读取 Item / Q-matrix
      ↓
确定：

target_kc
difficulty
question_type
diagnostic/practice
misconception_target
scaffold_level
      ↓
从题库选择 / LLM 生成
```

给 LLM 的最终输入最好结构化成：

```json
{
  "target_kc": "recursion.base_case",
  "mode": "practice",
  "difficulty": 0.45,
  "misconception_target": "missing_base_case",
  "question_type": "code_debug",
  "scaffold_level": 1
}
```

而不是：

```text
“根据用户情况随便出一道合适的题。”
```

---

# 16. 用户答题之后怎么更新

用户答题：

```text
Student Response
      ↓
Interaction Event
      ↓
Evidence Extractor
      ↓
┌───────────────┬────────────────┬──────────────┐
↓               ↓                ↓
Mastery       Misconception     Session
Updater       Updater           Updater
```

例如：

```text
mastery
.45 → .41

missing_base_case confidence
.68 → .82

recent_error_streak
1 → 2
```

不能统一：

```python
correct:
    p += 0.1
incorrect:
    p -= 0.1
```

长期应由 BKT / KT 等模型更新。

---

# 17. 场景三：个性化知识点讲解

这是目前个性化讲解最核心的场景。

例如用户问：

> 给我讲一下 Attention。

重点读取：

```text
Target KC State
+
Prerequisite States
+
Misconception
+
Session State
+
Preference
+
Goal
+
Relevant Semantic Memory
```

调用流程：

```text
Explain(KC)
     ↓
KC Mapper
     ↓
查 prerequisite graph
     ↓
读取 prerequisite mastery
     ↓
读取 target KC state
     ↓
读取 misconception
     ↓
读取 session state
     ↓
Tutor Policy
     ↓
决定 pedagogical action
     ↓
Preference + Memory
     ↓
LLM 生成讲解
```

---

# 18. 讲解首先适应“会不会”

例如：

### mastery 很低

```text
attention mastery=.08
```

采用：

```text
EXPLAIN
+
WORKED_EXAMPLE
+
CHECK_UNDERSTANDING
```

### mastery 中等

```text
attention mastery=.48
```

采用：

```text
PARTIAL_EXPLANATION
+
GUIDED_QUESTION
+
PRACTICE
```

### mastery 较高

```text
attention mastery=.84
```

采用：

```text
MINIMAL_REVIEW
+
SOCRATIC_QUESTION
+
CHALLENGE
```

即：

> 同一句“讲一下 Attention”，不同学习者得到的内容深度和教学动作应该不同。

---

# 19. 讲解还要适应前置知识

例如 Attention 的前置：

```text
vector
matrix multiplication
dot product
softmax
```

用户：

```text
vector                  .94
matrix multiplication   .89
dot product              .86
softmax                  .32
```

那么系统不能直接长篇解释 Q/K/V。

Tutor Policy 应该发现：

```text
softmax prerequisite gap
```

决定：

```text
先补 softmax
→ 再回到 attention
```

这就是 KST / prerequisite-aware tutoring。

---

# 20. 讲解还要适应 misconception

例如系统知道用户存在：

```text
misconception:
QK 是逐元素乘法
confidence=.88
```

则 Attention 的讲解重点应该变成：

```text
矩阵乘法 vs element-wise multiplication
↓
小矩阵 worked example
↓
要求用户预测 QKᵀ
↓
验证 misconception 是否消失
```

而不是继续讲 Transformer 历史。

---

# 21. Preference 最后决定“怎么呈现”

例如：

```text
prefers_code=.82
prefers_diagram=.67
```

Tutor Policy 先决定：

```text
WORKED_EXAMPLE
```

然后表达层再决定：

```text
使用代码
+
必要时配图
```

正确顺序：

```text
知识需求
↓
Pedagogical Action
↓
表达形式
```

不是：

```text
喜欢代码
↓
所有东西都给代码
```

---

# 22. 讲完知识点不能直接提高 mastery

例如：

```text
系统讲完 Attention
```

不能直接：

```text
attention mastery:
.30 → .50
```

因为：

> “看过解释”不是“掌握证据”。

可以记录：

```text
EXPLANATION_DELIVERED
CONTENT_VIEWED
```

然后追加一个理解检查：

> 为什么 QKᵀ 后要除以 √d？

用户回答后：

```text
CHECK_UNDERSTANDING
↓
产生 Evidence
↓
再更新 Mastery
```

---

# 23. 场景四：普通问答

普通问答不能每次都加载：

```text
全部 KC
全部 mastery
全部 KST
全部历史记录
```

成本高，而且会制造无关上下文。

应该首先：

```text
User Query
    ↓
Intent Router
```

判断：

```text
普通问题？
还是学习型问题？
```

---

## 23.1 非学习型普通问题

例如：

> Docker 怎么查看容器日志？

默认读取：

```text
Recent Context
+
Relevant Semantic Memory
+
Preference
```

必要时加：

```text
Profile / Project Context
```

通常不读：

```text
KST
Mastery
Misconception
```

---

## 23.2 学习型问答

例如：

> Attention 为什么要除以 √d？

Intent Router 判断：

```text
educational = true
```

然后：

```text
KC Mapper
→ attention.scaling
```

再升级到 Tutor Context：

```text
Target KC State
+
Prerequisite
+
Misconception
+
Session
+
Preference
```

因此普通问答和知识点讲解可以共享底层 Tutor Policy。

---

# 24. 系统应该有统一的 Context Builder

不同 Agent 不应该自己随意查：

```text
profile
mastery
memory
...
```

应该统一：

```text
User Request
      ↓
Intent Router
      ↓
Context Builder
      ↓
Adaptive Engine
      ↓
LLM
```

例如：

```text
build_context(
    user_id,
    scenario="plan_generation"
)
```

自动加载：

```text
goal
kc_state
kst
recency
preference
```

---

# 25. 四套推荐 Context Schema

## 25.1 PlanContext

```json
{
  "profile": {},
  "goal": {},
  "target_kcs": [],
  "kc_states": [],
  "prerequisite_graph": {},
  "review_candidates": [],
  "preferences": {},
  "relevant_memory": []
}
```

---

## 25.2 PracticeContext

```json
{
  "goal": {},
  "target_kc": {},
  "mastery": {},
  "misconceptions": [],
  "session": {},
  "candidate_items": [],
  "item_parameters": {}
}
```

---

## 25.3 TutorContext

```json
{
  "target_kc": {},
  "mastery": {},
  "prerequisite_states": [],
  "misconceptions": [],
  "session": {},
  "preferences": {},
  "relevant_memory": []
}
```

---

## 25.4 QAContext

```json
{
  "recent_context": [],
  "preferences": {},
  "relevant_memory": [],
  "educational_context": null
}
```

如果识别为学习型问答，再动态加载：

```text
mastery
misconception
prerequisite
session
```

---

# 26. Context Builder 还负责时间解析

例如数据库：

```text
mastery_raw=.82
last_evidence_at=2026-05-01
```

用户在 8 月生成学习计划：

```text
Context Builder
      ↓
Temporal State Resolver
      ↓
effective mastery
review urgency
```

因此 LLM 看到的是：

```text
mastery_raw=.82
effective_state=needs_refresh
```

而不是直接认为：

```text
mastered
```

---

# 27. 什么情况下真正更新长期 Learner Model

建议严格控制长期写入。

## 27.1 新学习证据

例如：

```text
答题
理解检查
测验
代码任务
自主解释
```

更新：

```text
mastery
confidence
misconception
recency
```

---

## 27.2 重复偏好证据

例如：

```text
多次主动要求代码例子
多次跳过图解
多次要求简洁版本
```

才慢慢更新：

```text
Preference
```

---

## 27.3 用户目标变化

例如：

> “我现在不准备考研，改成准备算法面试。”

更新：

```text
Goal Model
```

---

# 28. 哪些事件不要直接更新长期模型

以下行为先只进入：

```text
Interaction Event
+
Session State
```

例如：

```text
收到一次讲解
看了一篇材料
问了一个普通问题
一次答错
今天暂时不想看代码
```

不能直接推断：

```text
长期不会
长期不喜欢代码
长期能力下降
```

---

# 29. 推荐的工程存储方式

正式系统不建议继续使用多个 JSON 文件作为 source of truth。

## PostgreSQL：长期结构化真实状态

建议核心表：

```text
learner_profiles
learning_goals

knowledge_components
kc_relations

learner_kc_states
learner_misconceptions
learner_preferences

interaction_events

items
item_kcs
item_attempts
```

其中最关键：

```text
learner_kc_states
```

建议字段：

```text
user_id
kc_id

mastery_probability
mastery_confidence
status

evidence_count

last_seen_at
last_practiced_at
last_assessed_at
last_evidence_at

model_type
model_version

created_at
updated_at
```

---

## Redis：当前 Session State

例如：

```text
learner-session:{user_id}
```

保存：

```json
{
  "current_kc": "attention",
  "recent_accuracy": 0.4,
  "recent_error_streak": 3,
  "hint_requests": 2,
  "scaffold_level": 2
}
```

设置合理 TTL。

---

## Qdrant / Vector DB：Semantic Memory

只保存需要语义检索的信息：

```text
有效类比
历史学习经历
项目背景
过去成功解释
```

不要存：

```text
mastery=.63
```

这类精确数据。

因此：

```text
PostgreSQL
= truth

Redis
= current session

Qdrant
= semantic retrieval
```

---

# 30. `user_profile.md` 的定位

仍然可以保留，但它不是真实数据库，而是：

> **LLM-readable derived memory**

例如：

```markdown
## 当前学习状态

目标：
理解 Transformer 并能阅读基础代码。

已有基础：
- Python 熟练
- 矩阵乘法掌握稳定

近期重点：
- Attention mastery 中等
- scaling factor 理解仍不稳定

当前问题：
- 曾混淆矩阵乘法与逐元素乘法

有效教学方式：
- 小矩阵数值例子效果好
- 代码示例接受度较高

近期：
- 连续两次 Attention score 计算正确
```

如果 Markdown 和数据库冲突：

> **数据库为准。**

---

# 31. 最终 Adaptive Engine 做什么

Dynamic Learner Model 只负责：

> “描述这个学生现在是什么状态。”

Adaptive Engine 根据状态做决策：

\[
A_t=\pi(L_t,D,G_t)
\]

输出：

```json
{
  "next_kc": "...",
  "pedagogical_action": "...",
  "difficulty": 0.5,
  "item_type": "...",
  "scaffold_level": 2,
  "modality": "code+text",
  "review_or_new": "new"
}
```

LLM 再根据这个决策生成最终教学内容。

---

# 32. 推荐的 Pedagogical Action

第一版不需要复杂 RL，可以先定义固定动作集合：

```text
EXPLAIN
WORKED_EXAMPLE
PARTIAL_EXAMPLE
HINT
SOCRATIC_QUESTION
COUNTEREXAMPLE
ANALOGY
REVIEW_PREREQUISITE
PRACTICE
CHECK_UNDERSTANDING
CHALLENGE
SUMMARIZE
```

例如：

```text
mastery=.15
→ EXPLAIN + WORKED_EXAMPLE

mastery=.45
→ PARTIAL_EXAMPLE + HINT

mastery=.70
→ SOCRATIC_QUESTION + PRACTICE

mastery=.88
→ CHALLENGE

active misconception
→ COUNTEREXAMPLE + CHECK_UNDERSTANDING
```

这样比完全让 LLM 自由发挥更稳定。

---

# 33. 当前版本建议做到什么程度

不建议第一版直接堆：

```text
完整 KST
+ DKT
+ AKT
+ IRT
+ RL
+ 情感识别
```

推荐先做：

## V1

```text
1. KC + prerequisite graph
2. learner_kc_state
3. mastery + confidence + recency
4. misconception
5. session_state
6. preference
7. interaction event
8. Context Builder
9. 固定 Tutor Policy
10. 时间驱动 review / effective mastery
```

也就是：

```text
KST-lite
+
BKT/规则式 KT
+
Misconception
+
Session
+
Adaptive Tutor Policy
```

先把完整闭环跑通：

```text
Observe
↓
Diagnose
↓
Decide
↓
Teach
↓
Assess
↓
Update
```

---

# 34. 一句话说明我们的个性化设计

可以对外这样介绍：

> **EduAgents 不把个性化理解为简单的用户画像或 Prompt 记忆，而是维护一个随学习行为持续更新的 Dynamic Learner Model。系统结合用户目标、KC 掌握度、前置知识关系、错误认知、时间遗忘、当前会话状态和长期偏好，在学习计划、知识讲解、问答和练习场景中按需构造不同上下文，并由 Adaptive Engine 决定“学什么、怎么教、练什么、给多难”，LLM 负责具体教学内容的生成。**

更简短：

> **同一个问题，不同用户不是只得到“不同措辞”，而是可能得到不同知识路径、不同讲解深度、不同教学动作和不同后续练习。**

---

# 35. 四个核心场景一句话总结

### 学习计划

```text
Goal + KST + Mastery + Recency
→ 决定学什么、按什么顺序学
```

### 自适应练习

```text
Mastery + Misconception + Session + Item Model
→ 决定现在练什么、练多难
```

### 个性化讲解

```text
Mastery + Prerequisite + Misconception + Session + Preference
→ 决定现在应该怎么教
```

### 普通问答

```text
默认：
Relevant Memory + Preference + Recent Context

如果识别为学习型问题：
再加载对应 KC Learner State
```

---

# 36. 最终模块划分

代码层面建议最终形成四个核心组件：

```text
IntentRouter
     ↓
ContextBuilder
     ↓
AdaptivePolicy
     ↓
LLM / Agent
     ↓
EvidenceUpdater
```

其中：

### IntentRouter

判断：

```text
学习计划
练习
知识讲解
学习型问答
普通问答
```

### ContextBuilder

根据场景按需加载：

```text
Profile
Goal
KC State
Misconception
Session
Preference
Memory
KST
Item
```

### AdaptivePolicy

决定：

```text
next KC
pedagogical action
difficulty
scaffold
review/new
modality
```

### EvidenceUpdater

负责：

```text
Interaction Event
→ Mastery Update
→ Misconception Update
→ Session Update
→ Preference Update
```

---

## 最终核心

整个个性化系统真正需要实现的不是：

```text
“给 LLM 更多用户数据”
```

而是：

```text
动态学习者状态
        ↓
场景化 Context
        ↓
明确的教学决策
        ↓
LLM 教学执行
        ↓
新学习证据
        ↓
状态再次更新
```

也就是：

\[
\boxed{
Observe
\rightarrow
Diagnose
\rightarrow
Decide
\rightarrow
Teach
\rightarrow
Assess
\rightarrow
Update
}
\]

这才是 EduAgents 个性化讲解、问答、学习计划和自适应练习应该共享的统一底座。
