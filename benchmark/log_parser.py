"""
历史直播日志解析器 — 从 logs/live-*.log 提取弹幕/礼物事件流

日志行格式:  HH:MM:SS [INFO ] logger_name: 消息
事件来源行（danmaku 模块记到达，scheduler 记评分/路由，二者需去重合并）：
  danmaku:   📩 [user]：text                        —— 弹幕到达（无分数）
  scheduler: 📩 (65分) [user]：text → Q2            —— 弹幕到达+评分（路由入队）
  scheduler: 🔒 占坑 (65分) [user]：text            —— 弹幕到达+评分（占坑直commit）
  danmaku:   🎁 [user] 送了 2 个 小心心              —— 礼物到达（有名无数，无价格）
  scheduler: 🎁 (70分) [user] 送 2个小心心 ¥10 → Q1  —— 礼物到达+评分+价格(¥人民币)
  scheduler: 🔒 占坑 (100分) [user] 礼物            —— 礼物占坑（只有分数，无名称价格）
  danmaku:   💰 [user] SC ¥30：留言                 —— 醒目留言
  scheduler: 🚪 [user] 进入直播间 → Q1              —— 进房（可选）
注意：scheduler 的 "▶ (N分) [user] ..." 是原直播的播放决策行，回放要重新模拟，忽略。
"""

import os
import re
from dataclasses import dataclass, field

LINE_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2}) \[\w+\s*\] ([\w.]+): (.*)$")

DM_ARRIVE_RE = re.compile(r"^📩 \[(?P<u>.+?)]：(?P<t>.*)$")                       # danmaku 模块
DM_SCORED_RE = re.compile(r"^📩 \((?P<s>\d+)分\) \[(?P<u>.+?)]：(?P<t>.*?) →")     # scheduler 路由
DM_COMMIT_RE = re.compile(r"^🔒 占坑 \((?P<s>\d+)分\) \[(?P<u>.+?)]：(?P<t>.*)$")  # scheduler 占坑
GIFT_ARRIVE_RE = re.compile(r"^🎁 \[(?P<u>.+?)] 送了 (?P<n>\d+) 个 (?P<g>.+)$")     # danmaku 模块
GIFT_SCORED_RE = re.compile(r"^🎁 \((?P<s>\d+)分\) \[(?P<u>.+?)] 送 (?P<info>.+?) ¥(?P<rmb>[\d.]+) →")  # scheduler
GIFT_COMMIT_RE = re.compile(r"^🔒 占坑 \((?P<s>\d+)分\) \[(?P<u>.+?)] 礼物$")       # scheduler 占坑礼物
SC_RE = re.compile(r"^💰 \[(?P<u>.+?)] SC ¥(?P<rmb>[\d.]+)：(?P<t>.*)$")
ENTRY_RE = re.compile(r"^🚪 \[(?P<u>.+?)] 进入直播间")

_MERGE_WINDOW = 10.0  # 同一事件在 danmaku/scheduler 两处记录的时间差上限（秒）


@dataclass
class Event:
    ts: float                 # 相对会话开始的秒数
    type: str                 # 'danmaku' | 'gift' | 'sc' | 'entry'
    username: str
    text: str = ""
    gift_name: str = ""
    num: int = 1
    price: int | None = None          # 电池数（10电池=¥1），None=日志缺价格信息
    recorded_score: int | None = None  # 原直播时 DS 评分（仅供对照/兜底）
    session: str = ""
    id: str = ""

    def to_dict(self):
        return {k: getattr(self, k) for k in
                ("id", "ts", "type", "username", "text", "gift_name", "num",
                 "price", "recorded_score", "session")}


def _merge(candidates):
    """同一事件多条日志合并：取最早时间戳，补齐字段"""
    base = candidates[0]
    for c in candidates[1:]:
        for k in ("gift_name", "text"):
            if not getattr(base, k) and getattr(c, k):
                setattr(base, k, getattr(c, k))
        if base.price is None and c.price is not None:
            base.price = c.price
        if base.recorded_score is None and c.recorded_score is not None:
            base.recorded_score = c.recorded_score
        base.num = max(base.num, c.num)
    return base


def parse_log(path: str, include_entry: bool = False) -> list[Event]:
    """解析单个日志文件，返回按时间排序的事件流"""
    session = re.search(r"live-(\d{8})", str(path))
    session = session.group(1) if session else "unknown"

    raw: list[Event] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LINE_RE.match(line)
            if not m:
                continue
            hh, mm, ss, logger, msg = m.groups()
            ts = int(hh) * 3600 + int(mm) * 60 + int(ss)
            msg = msg.rstrip("\n")

            ev = None
            if logger == "danmaku":
                if (g := DM_ARRIVE_RE.match(msg)):
                    ev = Event(ts, "danmaku", g["u"], text=g["t"])
                elif (g := GIFT_ARRIVE_RE.match(msg)):
                    ev = Event(ts, "gift", g["u"], gift_name=g["g"], num=int(g["n"]))
                elif (g := SC_RE.match(msg)):
                    ev = Event(ts, "sc", g["u"], text=g["t"],
                               gift_name="醒目留言", price=int(float(g["rmb"]) * 10))
            elif logger == "scheduler":
                if (g := DM_SCORED_RE.match(msg)) or (g := DM_COMMIT_RE.match(msg)):
                    ev = Event(ts, "danmaku", g["u"], text=g["t"],
                               recorded_score=int(g["s"]))
                elif (g := GIFT_SCORED_RE.match(msg)):
                    info = g["info"]
                    num = 1
                    nm = re.match(r"^(\d+)个(.+)$", info)
                    if nm:
                        num, info = int(nm.group(1)), nm.group(2)
                    ev = Event(ts, "gift", g["u"], gift_name=info, num=num,
                               price=int(float(g["rmb"]) * 10),
                               recorded_score=int(g["s"]))
                elif (g := GIFT_COMMIT_RE.match(msg)):
                    ev = Event(ts, "gift", g["u"], recorded_score=int(g["s"]))
                elif include_entry and (g := ENTRY_RE.match(msg)):
                    ev = Event(ts, "entry", g["u"])
            if ev:
                raw.append(ev)

    # 去重合并：同类型+同用户+时间差<=窗口 视为同一事件（danmaku 模块与 scheduler 双记录）
    raw.sort(key=lambda e: e.ts)
    merged: list[Event] = []
    for ev in raw:
        found = None
        for prev in reversed(merged[-5:]):
            if (prev.type == ev.type and prev.username == ev.username
                    and abs(prev.ts - ev.ts) <= _MERGE_WINDOW
                    and (not prev.text or not ev.text or prev.text == ev.text)):
                found = prev
                break
        if found is not None:
            _merge([found, ev])
        else:
            merged.append(ev)

    # ts 归零 + 分配 id
    if merged:
        t0 = merged[0].ts
        for i, ev in enumerate(merged):
            ev.ts -= t0
            ev.session = session
            ev.id = f"{session}-{i:04d}"
    return merged


def list_session_logs(start_ts: float | None = None) -> list[str]:
    """列出有效历史日志；排除空文件、回放进程自产日志（mtime >= start_ts）
    以及当天日期的日志（当天文件可能是正在直播/实验中的活日志，不是历史数据）"""
    import glob
    import datetime
    today = datetime.date.today().strftime("%Y%m%d")
    paths = []
    for p in sorted(glob.glob("logs/live-*.log")):
        if os.path.getsize(p) == 0 or today in os.path.basename(p):
            continue
        if start_ts is not None and os.path.getmtime(p) >= start_ts - 5:
            continue   # 本次运行自己产生的日志，防自吞
        paths.append(p)
    return paths


def combine_sessions(log_paths: list[str], gap: float = 120.0,
                     include_entry: bool = False) -> list[Event]:
    """多场日志拼接成一条事件流（保持场内时间间隔，场间间隔 gap 秒）"""
    all_events: list[Event] = []
    offset = 0.0
    n = 0
    for p in log_paths:
        evs = parse_log(p, include_entry=include_entry)
        for ev in evs:
            ev.ts += offset
            ev.id = f"combo-{n:05d}"
            n += 1
        if evs:
            offset += evs[-1].ts + gap
        all_events.extend(evs)
    return all_events


if __name__ == "__main__":
    import sys, collections
    for p in sys.argv[1:]:
        evs = parse_log(p)
        c = collections.Counter(e.type for e in evs)
        span = evs[-1].ts / 60 if evs else 0
        print(f"{p}: {dict(c)} 时长{span:.0f}分钟")
