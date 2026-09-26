"""
有界排序队列 — 升序存储，容量满时挤掉最低分
"""

import bisect
from dataclasses import dataclass, field
from typing import Any


@dataclass(order=True)
class QueueItem:
    """队列条目，按 score 排序"""
    score: int
    item_type: str = field(compare=False)   # 'danmaku' | 'gift' | 'sc'
    username: str = field(compare=False)
    text: str = field(compare=False)        # 弹幕内容 或 礼物名称
    price: int = field(compare=False, default=0)       # 礼物电池数
    reply: str | None = field(compare=False, default=None)      # AI 回复缓存
    audio: Any = field(compare=False, default=None)             # TTS 音频缓存
    covered: list | None = field(compare=False, default=None)   # 批量回应时被 drain 的条目（item_type="batch" 专用）
    meta: dict | None = field(compare=False, default=None)      # 回放注入的事件溯源信息（id/到达时间）


class ScoreQueue:
    """有界优先队列，items[0] 最低分，items[-1] 最高分"""

    def __init__(self, capacity: int, name: str = ""):
        self.capacity = capacity
        self.name = name
        self.items: list[QueueItem] = []

    def push(self, item: QueueItem) -> QueueItem | None:
        """插入条目。如果满则挤出最低分，返回被挤出的条目或 None"""
        bisect.insort(self.items, item, key=lambda x: x.score)
        if len(self.items) > self.capacity:
            return self.items.pop(0)  # 挤掉最低分
        return None

    def pop_best(self) -> QueueItem | None:
        """取出最高分条目（队尾），没有则返回 None"""
        if self.items:
            return self.items.pop(-1)
        return None

    def pop_worst(self) -> QueueItem | None:
        """取出最低分条目（队首），没有则返回 None"""
        if self.items:
            return self.items.pop(0)
        return None

    def best(self) -> QueueItem | None:
        """查看最高分但不取出"""
        if self.items:
            return self.items[-1]
        return None

    def is_empty(self) -> bool:
        return len(self.items) == 0

    def drain_all(self) -> list[QueueItem]:
        """取出所有条目（按分数降序）"""
        result = list(reversed(self.items))
        self.items.clear()
        return result

    def __len__(self):
        return len(self.items)
