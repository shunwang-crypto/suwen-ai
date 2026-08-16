"""thought_filter: Gemma 内联推理段剥离 (整段/跨 chunk/未闭合)。"""

from __future__ import annotations

from eduagents_core.gateway.thought_filter import ThoughtFilter, strip_thought


def test_strip_whole_block() -> None:
    assert strip_thought("<thought>abc</thought>\n答案") == "答案"
    assert strip_thought("前<thought>a</thought>中<thought>b</thought>后") == "前中后"


def test_no_tag_passthrough() -> None:
    assert strip_thought("普通回答 $x^2$") == "普通回答 $x^2$"


def test_stream_cross_chunk() -> None:
    tf = ThoughtFilter()
    out = (
        tf.feed("<thou")
        + tf.feed("ght>推理")
        + tf.feed("过程</thou")
        + tf.feed("ght>\n答案")
        + tf.flush()
    )
    assert out == "答案"


def test_stream_no_tags() -> None:
    tf = ThoughtFilter()
    assert tf.feed("abc") + tf.feed("def") + tf.flush() == "abcdef"


def test_stream_unclosed_thought_dropped() -> None:
    tf = ThoughtFilter()
    assert tf.feed("<thought>没写完") + tf.flush() == ""
