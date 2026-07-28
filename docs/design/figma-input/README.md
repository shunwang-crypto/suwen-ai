# EduAgents Figma AI 首轮输入

## 第一次只设计什么

只设计 **1440 × 1024 的全局 App Shell + 桌面对话页**，暂时不要生成练习、计划、报告和手机端。

首轮目标是确定整个产品共用的视觉语言：导航、字体、颜色、间距、按钮、输入框、会话列表和对话内容区。

## 第一次上传什么

在 Figma Design 的 `Agents → Explore visual directions` 中点击输入框左下角 `+`：

1. 必传：`01-chat-desktop-current.png`（现有功能与信息结构参考）
2. 必传：`02-chat-desktop-empty.png`（空态和推荐入口参考）
3. 可选：一张你喜欢的 ChatGPT 对话界面截图（只作审美参考）

`03-chat-mobile-current.png` 留到第二轮生成手机端时再上传，第一轮不要上传，避免 AI 同时处理太多目标。

## 首轮提示词（直接粘贴）

```text
Use the two attached EduAgents screenshots only as references for existing
features, content and information architecture. Completely redesign the
visual appearance. Do not copy the current styling.

Create three editable visual directions for a commercial-grade AI learning
platform. Design only the 1440x1024 desktop App Shell and Chat page in this
first round.

Direction 1: ChatGPT-like minimal and focused
Direction 2: Linear-like professional and information-dense
Direction 3: Notion-like neutral and calm

Preserve these product capabilities:
- Global learning navigation
- Conversation history with long-title ellipsis
- Pinned and recent conversation groups
- Three-dot menu with pin, rename and delete
- Running spinner and unread completion dot
- Knowledge-base selector
- Streaming AI conversation with mathematical formulas
- Conditional source citation panel
- Follow-up questions and bottom message composer
- New-chat empty state with next-action recommendations

Visual requirements:
- Professional, restrained, trustworthy and suitable for a real commercial product
- One restrained indigo brand color and neutral gray surfaces
- Strong typography, alignment and information hierarchy
- Consistent 8px spacing system
- Compact but comfortable information density
- 8px control radius and 12px dialog radius
- Shadows only for menus, dialogs and floating overlays
- Use realistic Simplified Chinese content from the screenshots

Avoid gradients, glassmorphism, excessive rounded cards, excessive pills,
decorative animation, emoji icons and generic AI-generated dashboard styling.

Create editable Figma frames and reusable components. Do not generate
production code. Do not design the other product pages or mobile layout yet.
```

## 生成后第二句话

先在三版中选最满意的一版，再发：

```text
Use direction 2 as the selected design direction. Refine typography, spacing,
alignment and component consistency. Reduce decorative elements further.
Create final editable frames for these five states: new chat, normal answer,
streaming answer, background completion unread state, and the conversation
three-dot menu. Keep one coherent design system across all states.
```

如果你选择的不是第二版，把 `direction 2` 改成对应数字。

## 第二轮

桌面设计确认后，上传 `03-chat-mobile-current.png`，再要求 AI 使用同一设计系统生成 390 × 844 手机端。之后才扩展练习、计划和报告页面。
