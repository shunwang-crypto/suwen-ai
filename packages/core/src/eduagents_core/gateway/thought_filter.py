"""模型内联 <thought> 推理段的剥离 (Gemma-4 IT 经 OpenAI 兼容层混入正文)。

Gemma-4 instruct 系列在 OpenAI 兼容端点会把思维链以 <thought>…</thought>
原样放进 content。网关统一剥离, 下游 (聊天 SSE/持久化/计量) 只见净答案;
其他不输出该标签的模型零影响 (feed 原样透传)。
"""

from __future__ import annotations

import re

_THOUGHT_RE = re.compile(r"<thought>.*?</thought>\s*", flags=re.S)
_OPEN = "<thought>"
_CLOSE = "</thought>"


def strip_thought(text: str) -> str:
    """非流式: 整段剥离已闭合的 <thought> 块。"""
    return _THOUGHT_RE.sub("", text)


def _partial_suffix(buf: str, tag: str) -> str:
    """buf 的最长后缀且为 tag 前缀 (半截标签需缓冲到下一 chunk)。"""
    for k in range(min(len(buf), len(tag) - 1), 0, -1):
        if buf[-k:] == tag[:k]:
            return buf[-k:]
    return ""


class ThoughtFilter:
    """流式安全版: 开/闭标签可能被 chunk 边界切开, feed 只返回确定安全的部分。"""

    def __init__(self) -> None:
        self._buf = ""
        self._in_thought = False

    def feed(self, piece: str) -> str:
        self._buf += piece
        out: list[str] = []
        while self._buf:
            if self._in_thought:
                end = self._buf.find(_CLOSE)
                if end == -1:
                    self._buf = _partial_suffix(self._buf, _CLOSE)
                    return "".join(out)
                self._buf = self._buf[end + len(_CLOSE) :].lstrip("\n")
                self._in_thought = False
                continue
            start = self._buf.find(_OPEN)
            if start == -1:
                keep = _partial_suffix(self._buf, _OPEN)
                out.append(self._buf[: len(self._buf) - len(keep)])
                self._buf = keep
                return "".join(out)
            out.append(self._buf[:start])
            self._buf = self._buf[start + len(_OPEN) :]
            self._in_thought = True
        return "".join(out)

    def flush(self) -> str:
        """流结束: 未闭合的 thought 整段丢弃, 其余缓冲放行。"""
        rest, self._buf = self._buf, ""
        return "" if self._in_thought else rest
