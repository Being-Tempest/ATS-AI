"""
回复指示器 — 排队榜 + 字幕文件，直播姬实时读取
"""
import os

_DIR = os.path.dirname(__file__)
QUEUE_FILE = os.path.join(_DIR, "reply_queue.txt")
SUBTITLE_FILE = os.path.join(_DIR, "subtitle.txt")
MAX_LEN = 30  # 弹幕过长截断

# 初始化：确保文件始终存在，直播软件不会因文件丢失报错
for _init_f in [QUEUE_FILE, SUBTITLE_FILE]:
    if not os.path.exists(_init_f):
        with open(_init_f, "w", encoding="utf-8") as _fh:
            _fh.write("")


def _truncate(text: str, n: int = MAX_LEN) -> str:
    return text if len(text) <= n else text[:n] + "…"


def update_top3(items: list[dict]):
    """更新排队榜
    items = [{"username": "A", "danmaku": "...", "score": 85, "type": "danmaku"}, ...]
    """
    lines = ["即将回复", "回复排队"]
    medals = ["①", "②", "③"]
    for i, item in enumerate(items[:3]):
        tag = "[礼]" if item.get("type") == "gift" else ""
        danmaku = _truncate(item["danmaku"])
        lines.append(f"  {medals[i]} {tag} {item['username']}：{danmaku}  ({item['score']}分)")
    if len(items) == 0:
        lines.append("  暂无")
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def show_subtitle(text: str):
    """主播当前说的话（字幕），按标点分段多行显示"""
    lines = []
    current = ""
    for ch in text:
        current += ch
        if ch in "。！？~…！？" and len(current) >= 8:
            lines.append(current)
            current = ""
        elif len(current) >= 28:
            lines.append(current + "…")
            current = ""
    if current:
        lines.append(current)
    display = "\n".join(lines)
    with open(SUBTITLE_FILE, "w", encoding="utf-8") as f:
        f.write(display)


def clear_subtitle():
    """清空字幕（保留文件，直播软件不会报错）"""
    with open(SUBTITLE_FILE, "w", encoding="utf-8") as f:
        f.write("")


def clear_all():
    """清空所有"""
    for f in [QUEUE_FILE, SUBTITLE_FILE]:
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("")
