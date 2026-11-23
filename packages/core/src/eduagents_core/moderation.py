"""内容审核 (S-A3, §8 第 4 条): 可插拔的输入/输出审核 + 流式分句缓冲 + 未成年模式。

本期实现 = **规则 + 词表 stub**, 不调用任何外部服务, 零网络、可离线演示:

1. 词表放 ``eduagents_core/data/sensitive_words.txt`` (分类 + 词条 + 可选 soft 标记),
   替换词表不需要改代码;
2. 三条降误杀规则 —— ① allowlist 豁免短语优先 (``暴力法``/``暴力破解`` 等算法术语
   覆盖 ``暴力`` 的命中位置即放行); ② 拉丁词条要求词边界 (``sex`` 不命中
   ``sexagesimal``); ③ soft 词条 (学术歧义大的两字词) 只在**短文本**或**求教意图**
   上下文中判定命中, 长篇学习内容里偶然出现不拦截。

危机语义 (P0-5): 词表 ``crisis`` 分类覆盖自残/轻生的常见口语表达。策略上与其他
分类**方向相反**:

- ``check_input`` 命中危机类 **不拦截**, 只在结果上置 ``crisis=True`` —— 对话要继续,
  API 层据此注入关怀提示 (拦截一个正在求助的人是最坏的处置);
- ``check_output`` 命中危机类 **必拦**, ``StreamModerationBuffer`` 会把兜底文案切换为
  危机干预文案 (含心理援助热线 12356)。
- 降误杀: 紧邻的夸张口语前缀 (``累得不想活``) 默认豁免; 但规则做不到完美时
  **宁可多拦 + 给热线**, 未成年 strict 模式下夸张口语也不豁免。

fail-closed (P2-6): 词表缺失或清空时构造 :class:`Moderator` 直接抛 ``RuntimeError``
(启动即炸), 绝不静默放行; 仅测试可传 ``allow_missing=True`` 得到降级空审核器。

**生产替换点**: 平台侧只依赖 :class:`ContentModerator` 协议 (``check_input`` /
``check_output``)。接入云审核 (阿里云内容安全 / 腾讯天御 / 网易易盾等) 时:

    class AliyunModerator:                     # 实现同一协议即可
        async def check_input(self, text) -> ModerationResult: ...

把 :func:`get_moderator` 换成返回云审核适配器 (建议保留本地词表作为**前置快筛**,
云服务不可达时降级回本地规则), 调用方 (apps/api/middleware.py) 一行不改。
届时协议改为 async 即可 —— 当前同步实现是纯 CPU 匹配, 没有 IO。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Protocol

_WORDS_FILE = Path(__file__).resolve().parent / "data" / "sensitive_words.txt"
# 仓库根 prompts/ 下的未成年安全约束段 (与 workflows/qa.py 的 _PROMPTS_DIR 同约定);
# 打包部署下文件不存在时回退到内置文案, 不炸。
_MINOR_GUARD_FILE = Path(__file__).resolve().parents[4] / "prompts" / "minor_safety_guard.md"

# soft 词条的"短文本"阈值: 消息短到该词已主导语义时才判定命中
_SHORT_TEXT_CHARS = 40
# soft 词条的"求教意图"标记: 命中点附近出现即视为在索取该内容
_INTENT_MARKERS = (
    "怎么", "如何", "怎样", "教我", "教程", "步骤", "方法", "在哪买",
    "哪里买", "求推荐", "帮我搞", "帮我弄", "详细说明",
)
_INTENT_WINDOW = 24  # 命中点前后各取多少字符判断意图
_LATIN_RE = re.compile(r"^[0-9a-zA-Z._-]+$")

# 危机词条紧邻的夸张口语前缀 (2 字副词补语): "累得不想活了" 是高频夸张表达而非
# 求助信号, 默认豁免该命中; strict (未成年) 模式不豁免 —— 宁可多拦 + 给热线。
_HYPERBOLE_PREFIXES = frozenset(
    {"累得", "累的", "困得", "困的", "忙得", "忙的", "热得", "饿得",
     "烦得", "烦的", "笑得", "笑的", "急得", "气得", "冻得", "吓得"}
)

# 流式缓冲: 句末标点集合与强制切块阈值 (StreamModerationBuffer 契约, 勿改)
_SENTENCE_ENDS = frozenset("。！？.!?\n")
_MAX_PENDING_CHARS = 80

# 流式拦截兜底文案 (chat.py 接入契约, 勿改字面)
SAFE_MESSAGE_BLOCKED = "内容已被安全策略拦截。如果你正在经历困扰, 可拨打心理援助热线 12356。"
# 危机类命中时的干预文案 (必含热线)
SAFE_MESSAGE_CRISIS = (
    "听起来你现在可能正处在很难受的时刻。你不是一个人, 也请不要独自硬扛——"
    "可以先和信任的家人、老师或朋友聊聊, 也可以拨打 24 小时心理援助热线 12356 "
    "获得专业支持。如果情况紧急, 请立即拨打 110 或 120。"
)

CATEGORY_LABELS = {
    "illegal": "违法犯罪",
    "violence": "暴力恐怖",
    "adult": "成人色情",
    "political": "政治敏感",
    "crisis": "危机干预",
}


@dataclass(frozen=True)
class ModerationResult:
    """审核结论。``allowed=False`` 时 category/matched 指出命中分类与词条。

    ``crisis``: 命中危机语义 (自残/轻生表达)。输入侧 ``allowed`` 可以同时为 True
    (危机不拦截, 只标记, API 层注入关怀提示); 输出侧危机命中必为 ``allowed=False``。
    """

    allowed: bool
    category: str = ""
    matched: str = ""
    crisis: bool = False

    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category)


class ContentModerator(Protocol):
    """审核器协议 —— 生产接云审核服务时实现这两个方法即可整体替换。"""

    def check_input(self, text: str) -> ModerationResult: ...

    def check_output(self, text: str) -> ModerationResult: ...


@dataclass(frozen=True)
class _Rule:
    category: str
    word: str
    soft: bool


def _parse_words_file(raw: str) -> tuple[list[_Rule], list[_Rule], list[str]]:
    """解析词表 -> (常规分类规则, 危机规则, 豁免短语)。危机规则单独成列表:
    输入/输出两侧对它的处置方向不同 (标记 vs 必拦), 不能混进常规扫描。"""
    rules: list[_Rule] = []
    crisis_rules: list[_Rule] = []
    allow: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p for p in re.split(r"[\t ]+", line) if p]
        if len(parts) < 2:
            continue
        category, word = parts[0], parts[1]
        if category == "allow":
            allow.append(word)
            continue
        if category not in CATEGORY_LABELS:
            continue  # 未知分类忽略, 换表时不炸
        rule = _Rule(category=category, word=word, soft=len(parts) > 2 and parts[2] == "soft")
        if category == "crisis":
            crisis_rules.append(rule)
        else:
            rules.append(rule)
    return rules, crisis_rules, allow


class Moderator:
    """规则 + 词表审核器 (默认实现)。线程安全: 构造后只读。

    fail-closed (P2-6): 词表文件缺失或解析后无任何词条时抛 ``RuntimeError``,
    让进程启动即失败, 而不是静默放行全部内容。``allow_missing=True`` 仅供测试
    构造降级空审核器 (什么都不拦), 生产禁用。
    """

    def __init__(
        self, words_path: Path | str | None = None, *, allow_missing: bool = False
    ) -> None:
        path = Path(words_path) if words_path is not None else _WORDS_FILE
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            if not allow_missing:
                raise RuntimeError(
                    f"内容审核词表缺失: {path} — 拒绝以空审核规则启动 (fail-closed, P2-6)"
                ) from exc
            raw = ""
        self._rules, self._crisis_rules, self._allow = _parse_words_file(raw)
        if not self._rules and not self._crisis_rules and not allow_missing:
            raise RuntimeError(
                f"内容审核词表为空: {path} — 拒绝以空审核规则启动 (fail-closed, P2-6)"
            )
        # 长豁免短语优先匹配 (先匹配"校园暴力预防"再匹配"暴力法")
        self._allow.sort(key=len, reverse=True)

    # ---- 内部 ----

    def _allow_spans(self, text: str) -> list[tuple[int, int]]:
        """豁免短语在文本中的覆盖区间 (学术/技术语境命中即豁免)。"""
        spans: list[tuple[int, int]] = []
        for phrase in self._allow:
            start = text.find(phrase)
            while start != -1:
                spans.append((start, start + len(phrase)))
                start = text.find(phrase, start + 1)
        return spans

    @staticmethod
    def _covered(spans: list[tuple[int, int]], at: int, end: int) -> bool:
        return any(s <= at and end <= e for s, e in spans)

    @staticmethod
    def _has_word_boundary(text: str, at: int, end: int) -> bool:
        """拉丁词条要求两侧非字母数字 (中文词条恒为 True: 中文无空格分词)。"""
        before = text[at - 1] if at > 0 else ""
        after = text[end] if end < len(text) else ""
        return not (before.isalnum() or after.isalnum())

    def _soft_hits(self, text: str, at: int, end: int) -> bool:
        """soft 词条的上下文规则: 短文本 或 命中点附近有求教意图标记。"""
        if len(text) <= _SHORT_TEXT_CHARS:
            return True
        window = text[max(0, at - _INTENT_WINDOW) : end + _INTENT_WINDOW]
        return any(marker in window for marker in _INTENT_MARKERS)

    def _scan(self, text: str, *, soft_as_strict: bool) -> ModerationResult:
        if not text or not text.strip():
            return ModerationResult(allowed=True)
        spans = self._allow_spans(text)
        lowered = text.lower()
        for rule in self._rules:
            needle = rule.word.lower()
            latin = bool(_LATIN_RE.match(rule.word))
            at = lowered.find(needle)
            while at != -1:
                end = at + len(needle)
                if not self._covered(spans, at, end) and (
                    not latin or self._has_word_boundary(lowered, at, end)
                ):
                    if soft_as_strict or not rule.soft or self._soft_hits(text, at, end):
                        return ModerationResult(
                            allowed=False, category=rule.category, matched=rule.word
                        )
                at = lowered.find(needle, at + 1)
        return ModerationResult(allowed=True)

    def _crisis_hit(self, text: str, *, hyperbole_exempt: bool = True) -> str:
        """危机词条扫描: 返回命中词条 (未命中返回空串)。

        降误杀两条: ① allow 豁免短语依然生效 ("想死你了" 覆盖 "想死" 即放行);
        ② ``hyperbole_exempt=True`` 时紧邻的夸张口语前缀豁免 ("累得不想活")。
        规则识别不了的边界情形按命中处理 —— 宁可多标/多拦 + 给热线。
        """
        if not text or not text.strip():
            return ""
        spans = self._allow_spans(text)
        lowered = text.lower()
        for rule in self._crisis_rules:
            needle = rule.word.lower()
            at = lowered.find(needle)
            while at != -1:
                end = at + len(needle)
                if not self._covered(spans, at, end) and not (
                    hyperbole_exempt and lowered[max(0, at - 2) : at] in _HYPERBOLE_PREFIXES
                ):
                    return rule.word
                at = lowered.find(needle, at + 1)
        return ""

    # ---- 协议实现 ----

    def check_input(self, text: str) -> ModerationResult:
        """用户输入审核 (聊天/辅导/出题/计划入参)。soft 词条走上下文规则以免误杀学习内容。

        危机语义**不拦截只标记** (P0-5): 单纯的求助表达返回 ``allowed=True, crisis=True``,
        让对话继续、API 层注入关怀提示; 若同时命中其他分类的硬词条 (如"自杀方法"属
        violence), 仍拦截, 但 ``crisis=True`` 让拒绝文案带上热线而非冷冰冰的违规提示。
        """
        crisis_word = self._crisis_hit(text)
        verdict = self._scan(text, soft_as_strict=False)
        if not verdict.allowed:
            return replace(verdict, crisis=bool(crisis_word)) if crisis_word else verdict
        if crisis_word:
            return ModerationResult(
                allowed=True, category="crisis", matched=crisis_word, crisis=True
            )
        return verdict

    def check_output(self, text: str, *, strict: bool = False) -> ModerationResult:
        """模型输出/外部内容审核。比输入侧更严: soft 词条一律按命中处理
        (模型不该主动产出), 危机语义**必拦** (``crisis=True``, 调用方换危机干预文案)。

        ``strict=True`` (未成年模式) 阈值再收紧: 危机词条的夸张口语前缀豁免关闭
        ("累得不想活" 也拦)。豁免短语 (allowlist) 两档都生效, 否则助教没法给
        未成年人讲"暴力枚举"算法。协议调用方 (不传 strict) 行为与旧版完全一致。
        """
        crisis_word = self._crisis_hit(text, hyperbole_exempt=not strict)
        if crisis_word:
            return ModerationResult(
                allowed=False, category="crisis", matched=crisis_word, crisis=True
            )
        return self._scan(text, soft_as_strict=True)


class StreamModerationBuffer:
    """SSE 流式输出的分句缓冲审核器 (P0-5): 攒整句 → 过审 → 放行, 命中即截流。

    契约 (chat.py 接入, 勿改):

    - ``feed(text) -> list[str]``: 累积上游 token; 攒到句末标点 (。！？.!?\\n) 或
      ≥80 字符时把完整句段送 ``moderator.check_output``, 通过的句段按序作为放行块
      返回 (一次可多块); 未攒满返回 ``[]``。某句段命中即置 ``tripped``, 该句段与
      之后的所有输入不再放行 (命中前已过审的句段仍随本次返回)。
    - ``flush() -> str``: 流结束时冲洗残余; 过审返回残余文本, 不过审返回空串并置
      ``tripped``。
    - ``tripped``: 只读属性, 是否已截流。
    - ``safe_message``: 截流后应下发给用户的兜底文案; 默认为通用拦截文案,
      危机类命中时自动切换为危机干预文案 (含心理援助热线 12356)。
    - ``strict=True``: 未成年模式, 用更严阈值 (见 :meth:`Moderator.check_output`)。

    chat.py 接入示例::

        buf = StreamModerationBuffer(get_moderator(), strict=is_minor(user.birth_year))
        async for delta in upstream:               # 上游逐 token
            for piece in buf.feed(delta.text):
                yield sse_token(piece)             # 只下发过审句段
            if buf.tripped:
                break
        if not buf.tripped and (tail := buf.flush()):
            yield sse_token(tail)
        if buf.tripped:
            yield sse_token(buf.safe_message)      # 截流兜底 (危机类自动带热线)
            # 落库的助手消息也应替换为 buf.safe_message, 不落原文

    已知限制 (词表 stub 档): 80 字强制切块可能把一个词条切到两个句段而漏检;
    接云审核后以全文异步复检兜底。
    """

    def __init__(self, moderator: Moderator, strict: bool = False) -> None:
        self._moderator = moderator
        self._strict = strict
        self._pending = ""
        self._tripped = False
        self.safe_message: str = SAFE_MESSAGE_BLOCKED

    @property
    def tripped(self) -> bool:
        return self._tripped

    def feed(self, text: str) -> list[str]:
        if self._tripped:
            return []
        if text:
            self._pending += text
        released: list[str] = []
        while (segment := self._next_segment()) is not None:
            verdict = self._moderator.check_output(segment, strict=self._strict)
            if not verdict.allowed:
                self._trip(verdict)
                break
            released.append(segment)
        return released

    def flush(self) -> str:
        if self._tripped:
            return ""
        segment, self._pending = self._pending, ""
        if not segment:
            return ""
        verdict = self._moderator.check_output(segment, strict=self._strict)
        if not verdict.allowed:
            self._trip(verdict)
            return ""
        return segment

    # ---- 内部 ----

    def _next_segment(self) -> str | None:
        """取下一个完整句段: 优先句末标点, 其次 80 字强制切块; 攒不满返回 None。"""
        for i, ch in enumerate(self._pending):
            if ch in _SENTENCE_ENDS:
                segment, self._pending = self._pending[: i + 1], self._pending[i + 1 :]
                return segment
        if len(self._pending) >= _MAX_PENDING_CHARS:
            segment = self._pending[:_MAX_PENDING_CHARS]
            self._pending = self._pending[_MAX_PENDING_CHARS:]
            return segment
        return None

    def _trip(self, verdict: ModerationResult) -> None:
        self._tripped = True
        self._pending = ""  # 命中后丢弃残余, 后续 feed 一律不放行
        if verdict.crisis or verdict.category == "crisis":
            self.safe_message = SAFE_MESSAGE_CRISIS


# ---- 未成年模式 (P0-5) ----

_ADULT_AGE = 18

_MINOR_GUARD_FALLBACK = (
    "## 未成年人安全约束（系统级，优先级最高）\n"
    "当前用户可能是未成年人，必须遵守：拒绝输出任何成人/色情内容、暴力细节描写、"
    "自残自杀方式、赌博与违法活动的具体做法，相关话题只做安全教育层面的正面引导；"
    "不引导线下见面，不索取住址、学校、联系方式等个人信息；察觉到情绪困扰或自伤"
    "倾向时温和回应，建议求助信任的成年人，并告知可拨打心理援助热线 12356；"
    "连续学习较久时提醒适当休息、保护视力；用语适龄、正向，不使用制造焦虑或"
    "诱导沉迷的表述。"
)


def is_minor(birth_year: int | None) -> bool:
    """按出生年判定未成年 (周岁近似: 当前年份 - 出生年 < 18)。

    ``None`` = 用户未声明, 按成年处理 (注册侧另有年龄声明引导)。异常值 (出生年
    在未来) 按未成年保守处理。刻意用真实系统时钟而非虚拟时钟 timectl —— 演示
    time-travel 不该把未成年人"催熟"成年。
    """
    if birth_year is None:
        return False
    return date.today().year - birth_year < _ADULT_AGE


@lru_cache(maxsize=1)
def minor_system_guard() -> str:
    """未成年安全约束提示词段, 追加到 system prompt 末尾 (qa_graph generate 等)。

    正文维护在仓库 ``prompts/minor_safety_guard.md``; 文件不存在 (如打包部署)
    时回退内置文案, 保证约束永不缺位。
    """
    try:
        text = _MINOR_GUARD_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return _MINOR_GUARD_FALLBACK
    return text or _MINOR_GUARD_FALLBACK


@lru_cache(maxsize=1)
def get_moderator() -> Moderator:
    """进程级单例 (词表只读一次)。替换生产审核实现时改这里的返回值即可。

    词表缺失/为空时首个调用方即抛 RuntimeError (fail-closed) —— API 在 lifespan
    里应主动调用一次以便启动即炸, 而不是等第一条消息才炸。
    """
    return Moderator()
