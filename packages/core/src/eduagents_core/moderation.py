"""内容审核 (S-A3, §8 第 4 条): 可插拔的输入/输出审核。

本期实现 = **规则 + 词表 stub**, 不调用任何外部服务, 零网络、可离线演示:

1. 词表放 ``eduagents_core/data/sensitive_words.txt`` (分类 + 词条 + 可选 soft 标记),
   替换词表不需要改代码;
2. 三条降误杀规则 —— ① allowlist 豁免短语优先 (``暴力法``/``暴力破解`` 等算法术语
   覆盖 ``暴力`` 的命中位置即放行); ② 拉丁词条要求词边界 (``sex`` 不命中
   ``sexagesimal``); ③ soft 词条 (学术歧义大的两字词) 只在**短文本**或**求教意图**
   上下文中判定命中, 长篇学习内容里偶然出现不拦截。

**生产替换点**: 平台侧只依赖 :class:`ContentModerator` 协议 (``check_input`` /
``check_output``)。接入云审核 (阿里云内容安全 / 腾讯天御 / 网易易盾等) 时:

    class AliyunModerator:                     # 实现同一协议即可
        async def check_input(self, text) -> ModerationResult: ...

把 :func:`get_moderator` 换成返回云审核适配器 (建议保留本地词表作为**前置快筛**,
云服务不可达时降级回本地规则), 调用方 (apps/api/middleware.py) 一行不改。
届时协议改为 async 即可 —— 当前同步实现是纯 CPU 匹配, 没有 IO。

输出侧 (``check_output``) 本期只提供函数, 未接入 SSE 流式链路, 见 decisions.md D16。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

_WORDS_FILE = Path(__file__).resolve().parent / "data" / "sensitive_words.txt"

# soft 词条的"短文本"阈值: 消息短到该词已主导语义时才判定命中
_SHORT_TEXT_CHARS = 40
# soft 词条的"求教意图"标记: 命中点附近出现即视为在索取该内容
_INTENT_MARKERS = (
    "怎么", "如何", "怎样", "教我", "教程", "步骤", "方法", "在哪买",
    "哪里买", "求推荐", "帮我搞", "帮我弄", "详细说明",
)
_INTENT_WINDOW = 24  # 命中点前后各取多少字符判断意图
_LATIN_RE = re.compile(r"^[0-9a-zA-Z._-]+$")

CATEGORY_LABELS = {
    "illegal": "违法犯罪",
    "violence": "暴力恐怖",
    "adult": "成人色情",
    "political": "政治敏感",
}


@dataclass(frozen=True)
class ModerationResult:
    """审核结论。``allowed=False`` 时 category/matched 指出命中分类与词条。"""

    allowed: bool
    category: str = ""
    matched: str = ""

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


def _parse_words_file(raw: str) -> tuple[list[_Rule], list[str]]:
    rules: list[_Rule] = []
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
        rules.append(_Rule(category=category, word=word, soft=len(parts) > 2 and parts[2] == "soft"))
    return rules, allow


class Moderator:
    """规则 + 词表审核器 (默认实现)。线程安全: 构造后只读。"""

    def __init__(self, words_path: Path | str | None = None) -> None:
        path = Path(words_path) if words_path is not None else _WORDS_FILE
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:  # 词表缺失时放行 (审核不可用不该让平台不可用), 但留日志给调用方
            raw = ""
        self._rules, self._allow = _parse_words_file(raw)
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

    # ---- 协议实现 ----

    def check_input(self, text: str) -> ModerationResult:
        """用户输入审核 (聊天/辅导消息)。soft 词条走上下文规则以免误杀学习内容。"""
        return self._scan(text, soft_as_strict=False)

    def check_output(self, text: str) -> ModerationResult:
        """模型输出审核。比输入侧更严: soft 词条一律按命中处理 (模型不该主动产出)。

        本期未接入 SSE 流式链路 (需分段缓冲后审核, 见 decisions.md D16),
        提供给非流式调用方与 M5 的流式分段审核复用。
        """
        return self._scan(text, soft_as_strict=True)


@lru_cache(maxsize=1)
def get_moderator() -> Moderator:
    """进程级单例 (词表只读一次)。替换生产审核实现时改这里的返回值即可。"""
    return Moderator()
