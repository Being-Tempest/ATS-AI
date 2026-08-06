"""
进房欢迎 — 模板化欢迎 + 直接入队（无批次）
"""
import time
import random
from logger import get_logger

log = get_logger("entry")

TEMPLATES = [
    "{name}来啦~",
    "{name}来了呀，欢迎欢迎",
    "呀，{name}回来了",
    "{name}晚上好~",
    "欢迎{name}~",
    "哎{name}来啦~今天怎么样",
    "{name}！好久不见呀",
    "哦{name}来啦，快坐快坐",
]

COOLDOWN = 300   # 同一人冷却 5 分钟
QUEUE_SCORE = 50  # 入队分数（Q1 级）


def random_welcome(name: str) -> str:
    """随机抽一条欢迎模板，填充名字"""
    return random.choice(TEMPLATES).format(name=name)


class EntryWelcomer:
    """进房欢迎 — 直接入队，不攒批次"""

    def __init__(self, scheduler):
        self.scheduler = scheduler
        self._last_welcome: dict[str, float] = {}  # name -> last welcome time

    def on_entry(self, name: str):
        """有人进房 → 直接入队"""
        now = time.time()
        if name in self._last_welcome:
            if now - self._last_welcome[name] < COOLDOWN:
                return
        self._last_welcome[name] = now

        self.scheduler.enqueue_entry(name)
