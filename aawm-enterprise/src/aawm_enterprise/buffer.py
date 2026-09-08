"""SessionBuffer：会话级片段累积。

- key = (session_id, user_id)，与核心 Context.session_id 语义对齐
  （一个交付任务的上下文）
- append 幂等：同片段（sha256）重复 append 只存一份（agent 重试防双写），
  但仍透传返回原文——缓冲层对输出通道零干扰
- 段落以 "\\n\\n" 连接，保留文档结构（预检/嵌入按整篇进行）
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, List, Tuple, Union

SessionKey = Tuple[str, Union[int, str]]


def make_key(session_id: str, user_id: Union[int, str]) -> SessionKey:
    return (session_id, user_id)


@dataclass
class BufferedSegment:
    """一个已缓冲片段。"""

    text: str
    order: int
    fingerprint: str  # sha256 hex（幂等去重键）


@dataclass
class SessionBuffer:
    """单会话缓冲。由 AggregateWatermarker 持有，外部不直接操作。"""

    key: SessionKey
    segments: List[BufferedSegment] = field(default_factory=list)
    _seen: set = field(default_factory=set)
    last_touch: float = field(default_factory=time.monotonic)

    def append(self, text: str) -> bool:
        """追加片段。返回是否为新片段（重复片段 False，但仍透传）。"""
        self.last_touch = time.monotonic()
        fp = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if fp in self._seen:
            return False
        self._seen.add(fp)
        self.segments.append(BufferedSegment(text=text, order=len(self.segments),
                                             fingerprint=fp))
        return True

    @property
    def text(self) -> str:
        """聚合全文（段落间以空行连接，保留文档结构）。"""
        return "\n\n".join(seg.text for seg in self.segments)

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_touch

    @property
    def user_id(self) -> Any:
        return self.key[1]
