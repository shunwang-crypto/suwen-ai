"""LLM JSON 输出的容错解析 (唯一实现)。

此前同一逻辑存在四份逐字重复 (kc/extraction、graphs/qa_graph、graphs/quiz_graph、
apps/api/routers/reviews), 漂移风险与维护成本同源 —— 收敛到本模块, 调用方一律
import 公共名 parse_json_object。

语义契约 (与四份旧实现完全一致, 行为不变):
- 候选: 先整体 strip 后的文本, 再截取首个 "{" 到末个 "}" (容忍代码块围栏
  ```json ... ``` 与前后解释性文字);
- 仅接受 dict; list/标量一律视为解析失败;
- 任何异常 (JSONDecodeError/ValueError) 吞掉, 失败返回 None, 不向外抛;
- stub 网关的 {"stub": true} 缺业务字段, 由各调用方的字段校验兜底为解析失败。
"""

from __future__ import annotations

import json
from typing import Any


def parse_json_object(text: str) -> dict[str, Any] | None:
    """尽力解析单个 JSON 对象; 失败返回 None。"""
    candidates = [text.strip()]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None
