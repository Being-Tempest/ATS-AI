"""
离线弹幕回放器 — E1/E4 实验基础设施

用法（项目根目录）：
  python -m benchmark.replay --log logs/live-20260730.log --strategy all
  python -m benchmark.replay --combo --strategy all          # 三场拼接回放
  python -m benchmark.replay --combo --strategy full --tag e4 --fixed-threshold 50
  python -m benchmark.replay --combo --strategy keyword --no-api   # 只用缓存，不调真实 API

策略：
  fifo      先来先回
  random    空闲时随机挑一条待回
  keyword   DLIOS 式关键词路由（命中入队优先回 + 30s 冷却，未命中不回）
  fixed     LLM 评分照旧，升舱门槛固定（默认 50），关闭滑动窗口
  full      完整方案（三队列级联 + 自适应门槛，原样）

输出：
  benchmark/results/decisions_<tag>_<strategy>.jsonl   每行一条回复决策
  benchmark/results/threshold_trace.jsonl              E4 门槛时间序列（--tag e4 时）
"""

import argparse
import asyncio
import json
import os
import random
import sys
import time

RUN_START = time.time()   # 必须在 import scheduler 之前记录（import 会触发当日日志文件创建）

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler as S
from scheduler import Scheduler
from benchmark.log_parser import parse_log, combine_sessions, Event
from benchmark.api_cache import ApiCache

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")
os.makedirs(RESULTS_DIR, exist_ok=True)

# 回放日志改写到 results/replay_debug.log，不污染 logs/（否则下次 combo 会把自产日志当历史数据解析）
import logging
for _h in logging.root.handlers[:]:
    if isinstance(_h, logging.FileHandler):
        logging.root.removeHandler(_h)
        _h.close()
_fh = logging.FileHandler(os.path.join(RESULTS_DIR, "replay_debug.log"), encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
                                   datefmt="%H:%M:%S"))
logging.root.addHandler(_fh)

# 回放加速：回复句间停顿不真等（虚拟时钟下停顿无意义）
S.REPLY_GAP_MIN = S.REPLY_GAP_MAX = 0.01

# 占位回复 45 字 → 时长 9.0s/条，对齐 E3 实测服务率 μ≈6-7 条/分钟（见 preregistration.md §1）
STUB_REPLY = "（模拟回复占位文本，长度四十五字整，用于估算九秒播放时长，对齐实测服务率口径）"
# 批量回应占位：字数按真实批量生成+TTS 抽测校准（preregistration §8.2 amendment）：
# 实测批量生成均值 70 字、CosyVoice 实测 14.8s（4.72 字/s）；为保持单条 5.0 字/s 的 μ 口径统一，
# 批量占位取 74 字 → 14.8s（时长与实测对齐，语速口径与单条一致）
STUB_BATCH_REPLY = "（模拟批量回应占位文本，长度按真实批量生成与云端语音合成抽测校准：点名二到三个观众，概括大家聊的话题并且给出回应，对应十四点八秒的整段播放时长整哈）"
SETTLE_POLL = 0.01          # 真实等待粒度（秒）
SETTLE_STEP = 2.0           # 队列非空但调度器等待时的虚拟时间步长（秒）
DRAIN_TAIL = 120.0          # 最后一条事件后追加的虚拟收尾时间（秒）


class VirtualClock:
    def __init__(self, t0=0.0):
        self._t = t0

    def now(self):
        return self._t

    def set(self, t):
        if t > self._t:
            self._t = t


# ---------------- 事件预评分 ----------------

async def prescore_events(events: list[Event], score_cache: ApiCache):
    """回放前统一给所有弹幕打分（缓存共享，真实 API 每文本只调一次）。
    返回 {event_id: score}。打分失败的弹幕沿用日志原评分，再缺省给 40。"""
    tasks = {}
    for ev in events:
        if ev.type == "danmaku":
            tasks[ev.id] = asyncio.ensure_future(score_cache(ev.text))
    scores = {}
    for ev in events:
        if ev.type == "danmaku":
            s = await tasks[ev.id]
            if s is None:
                s = ev.recorded_score if ev.recorded_score is not None else 40
            scores[ev.id] = int(s)
    return scores


# ---------------- 调度器策略（full / fixed） ----------------

async def replay_scheduler(events: list[Event], scores: dict, tag: str,
                           strategy: str, fixed_threshold=None, variant=None,
                           disable_commit=False, batch_merge=False,
                           batch_q2d_min=None, batch_q2g_min=None,
                           batch_q3_trigger=None, reply_fn=None):
    variant = variant or strategy
    clock = VirtualClock(events[0].ts if events else 0.0)
    decisions = []
    threshold_trace = []

    # 弹幕评分回调按调用顺序匹配预评分表（scheduler 内部只传文本）
    pending_ids = []

    async def score_fn(text: str):
        ev_id = pending_ids.pop(0)
        return scores[ev_id]

    def on_decision(item, reply, ts, duration):
        meta = item.meta or {}
        rec = {
            "t": round(ts, 2), "type": item.item_type, "username": item.username,
            "text": item.text, "score": item.score, "duration": round(duration, 2),
            "reply": reply,
            "ev_id": meta.get("id"), "arrive_ts": meta.get("ts"),
        }
        if item.item_type == "batch":
            rec["batch_kind"] = item.text   # q2g | q2d | q3
            rec["covered"] = [{
                "type": c.item_type, "username": c.username, "text": c.text,
                "score": c.score,
                "ev_id": (c.meta or {}).get("id"),
                "arrive_ts": (c.meta or {}).get("ts"),
            } for c in (item.covered or [])]
        decisions.append(rec)

    def on_promote(kind, item, threshold, ts):
        threshold_trace.append({
            "variant": variant, "t": round(ts, 2), "event": "promote",
            "kind": kind, "threshold": round(threshold, 1),
            "item_score": item.score, "username": item.username,
        })

    async def stub_reply(username, text):
        if username.startswith("（批量/"):
            return (STUB_BATCH_REPLY, "default_smile")
        return (STUB_REPLY, "default_smile")

    sched = Scheduler(chatbot=None, tts=None, vts=None,
                      time_fn=clock.now, score_fn=score_fn,
                      reply_fn=reply_fn or stub_reply,
                      on_decision=on_decision, on_promote=on_promote,
                      fixed_threshold=fixed_threshold,
                      disable_commit=disable_commit,
                      batch_merge=batch_merge,
                      batch_q2d_min=batch_q2d_min,
                      batch_q2g_min=batch_q2g_min,
                      batch_q3_trigger=batch_q3_trigger)
    sched._tick = 0.01   # 回放加速：主循环节拍 0.1s → 0.01s（虚拟时钟下不影响语义）

    last_threshold = [None]

    def sample_threshold():
        th = round(sched._threshold, 1)
        if th != last_threshold[0]:
            last_threshold[0] = th
            threshold_trace.append({
                "variant": variant, "t": round(clock.now(), 2), "event": "tick",
                "kind": "danmaku", "threshold": th,
                "window_n": len(sched._danmaku_window),
                "window_avg": round(sum(sched._danmaku_window) / len(sched._danmaku_window), 1)
                if sched._danmaku_window else None,
            })

    async def pump(t_limit=None):
        """推进仿真直到稳定空闲。播放跳播完，后台任务等落地，
        队列有货但调度器在等级联等待时按 SETTLE_STEP 推虚拟时钟。
        t_limit 给定后，虚拟时钟不越过该时刻（下一事件到达时间）——
        播放/等待跨过 t_limit 是真实情况，直接返回让下一事件入队。
        预算是真实时间（真实 API 延迟会烧迭代次数，见 §8.2 fidelity 调试）。"""
        import time as _time
        deadline = _time.monotonic() + 900.0
        stable = 0
        while True:
            if _time.monotonic() > deadline:
                raise RuntimeError(
                    f"pump 未收敛: t_limit={t_limit} now={clock.now():.1f} "
                    f"playing_until={sched.playing_until:.1f} preparing={sched.preparing is not None} "
                    f"next_ready={sched.next_ready is not None} committed={sched._committed_item is not None} "
                    f"reserved={sched._committed_reserved} decisions={len(decisions)} "
                    f"queues={[len(q) for q in (sched.q1_d, sched.q1_g, sched.q2_d, sched.q2_g, sched.q3_d, sched.q3_g)]}")
            now = clock.now()
            if sched.playing_until > now:
                target = sched.playing_until + 0.01
                if t_limit is not None and target > t_limit:
                    return
                clock.set(target)
                await asyncio.sleep(SETTLE_POLL)
                stable = 0
                continue
            if sched.preparing is not None or sched._committed_reserved \
                    or sched.next_ready is not None or sched._committed_item is not None:
                await asyncio.sleep(0.05)
                stable = 0
                continue
            queues_nonempty = any(len(q) for q in
                                  (sched.q1_d, sched.q1_g, sched.q2_d,
                                   sched.q2_g, sched.q3_d, sched.q3_g))
            n_dec = len(decisions)
            await asyncio.sleep(SETTLE_POLL * 3)
            if len(decisions) != n_dec or sched.playing_until > clock.now():
                stable = 0
                continue
            if queues_nonempty:
                if t_limit is not None and clock.now() + SETTLE_STEP > t_limit:
                    return
                clock.set(clock.now() + SETTLE_STEP)   # 推进级联等待 8s/20s
                stable = 0
                continue
            stable += 1
            if stable >= 3:
                return

    run_task = asyncio.create_task(sched.run())
    try:
        for i, ev in enumerate(events):
            clock.set(ev.ts)
            if ev.type == "danmaku":
                pending_ids.append(ev.id)
                await sched.enqueue_danmaku(ev.username, ev.text,
                                            meta={"id": ev.id, "ts": ev.ts})
            elif ev.type in ("gift", "sc"):
                if not ev.gift_name:
                    ev.gift_name = "(未知礼物)"
                sched.enqueue_gift(ev.username, ev.gift_name,
                                   ev.price or 0, ev.num,
                                   score_override=None if ev.price is not None
                                   else ev.recorded_score,
                                   meta={"id": ev.id, "ts": ev.ts})
            elif ev.type == "entry":
                sched.enqueue_entry(ev.username)
            sample_threshold()
            # 真实语义：两个事件之间的空闲期里调度器照常消化队列
            next_ts = events[i + 1].ts if i + 1 < len(events) else None
            await pump(next_ts)
        await pump()                            # 事件流结束后统一收尾
    finally:
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    return decisions, threshold_trace, sched


# ---------------- 单通道基线（fifo / random / keyword） ----------------

KEYWORD_RULES = {
    "address":  ["佳宁", "主播", "老婆", "宝贝", "@"],
    "question": ["?", "？", "吗", "什么", "怎么", "为什么", "哪里", "谁"],
    "greeting": ["你好", "早上好", "晚上好", "下午好", "哈喽", "hello", "hi"],
    "gift":     ["送", "舰长", "醒目", "SC"],
}
KEYWORD_COOLDOWN = 30.0  # DLIOS 式：每次回复后 30s 内不再路由新弹幕


def keyword_hit(ev: Event) -> str | None:
    if ev.type in ("gift", "sc"):
        return "gift"          # 付费事件在任何路由里都优先
    text = ev.text or ""
    for cat, kws in KEYWORD_RULES.items():
        if cat == "gift" and ev.type not in ("gift", "sc"):
            continue
        if any(kw in text for kw in kws):
            return cat
    if len(text) >= 15:
        return "story"
    return None


def _gift_score(ev: Event) -> int:
    """礼物分：价格已知走 score_gift 确定性赋分，否则用日志原评分兜底"""
    if ev.price is not None:
        from scorer import score_gift
        return score_gift(ev.gift_name or "(未知礼物)", ev.price)
    return ev.recorded_score if ev.recorded_score is not None else 70


# 基线模型的"主播注意力窗口"：与调度器三队列总容量（5+5+10+10+20+20=70，弹幕侧 35）对齐。
# 超出即遗忘（弹幕滚屏流走），否则稀疏流下 greedy/fifo 退化成"全回"，无选择压力。
PENDING_CAP = 35


async def replay_baseline(events: list[Event], scores: dict, strategy: str):
    """单通道模型：一次只回一条，回复时长与调度器策略同一口径（4s 占位）
    greedy：LLM 评分 + 每轮取当前最高分（无队列/级联/自适应门槛），红审剥离基线"""
    pending: list[Event] = []
    decisions = []
    free_at = 0.0            # 频道何时空闲（虚拟时间）
    last_reply_end = -1e9
    rng = random.Random(42)

    def ev_score(item):
        return scores.get(item.id, 0) if item.type == "danmaku" else _gift_score(item)

    def push_pending(ev):
        if len(pending) < PENDING_CAP:
            pending.append(ev)
            return
        if strategy == "greedy":
            worst = min(pending, key=ev_score)
            if ev_score(ev) > ev_score(worst):   # 单优先级池：挤掉最低分
                pending.remove(worst)
                pending.append(ev)
        else:
            pending.pop(0)                       # 滚屏遗忘最旧
            pending.append(ev)

    def pick():
        if strategy in ("fifo", "keyword"):
            return pending.pop(0)
        if strategy == "greedy":
            item = max(pending, key=ev_score)   # 分数最高，同分取先到
            pending.remove(item)
            return item
        return pending.pop(rng.randrange(len(pending)))  # random

    def reply_at(item, start):
        nonlocal free_at, last_reply_end
        duration = max(2, len(STUB_REPLY) / 5)
        decisions.append({
            "t": round(start, 2), "type": "gift" if item.type in ("gift", "sc") else item.type,
            "username": item.username,
            "text": item.text or f"(送了{item.gift_name})",
            "score": ev_score(item),
            "duration": round(duration, 2), "reply": STUB_REPLY,
            "ev_id": item.id, "arrive_ts": item.ts,
            "kw_category": keyword_hit(item) if strategy == "keyword" else None,
        })
        free_at = start + duration
        last_reply_end = free_at

    for ev in events:
        t = ev.ts
        # 消化 t 之前到达且频道已能处理的回复
        while pending and free_at <= t:
            reply_at(pick(), free_at)
        if strategy == "keyword":
            if keyword_hit(ev) and t - last_reply_end >= KEYWORD_COOLDOWN:
                push_pending(ev)   # 命中且在冷却外 → 排队
        else:
            push_pending(ev)
        # 频道空闲且刚入新货 → 立即起一条
        if pending and free_at <= t:
            reply_at(pick(), t)
    # 收尾：剩余缓冲按节奏播完
    while pending:
        reply_at(pick(), free_at)
    return decisions


# ---------------- 入口 ----------------

def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def load_events_file(path: str) -> list[Event]:
    """从 jsonl 读事件流（合成流或保存的历史流）"""
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                events.append(Event(**{k: d.get(k) for k in
                                       ("ts", "type", "username", "text", "gift_name",
                                        "num", "price", "recorded_score", "session", "id")}))
    events.sort(key=lambda e: e.ts)
    return events


async def run(args):
    if args.events_file:
        events = load_events_file(args.events_file)
        src = os.path.basename(args.events_file).replace(".jsonl", "")
    elif args.combo:
        from benchmark.log_parser import list_session_logs
        paths = list_session_logs(RUN_START)
        events = combine_sessions(paths)
        src = "combo"
    else:
        events = parse_log(args.log, include_entry=args.include_entry)
        src = os.path.basename(args.log).replace("live-", "").replace(".log", "")
    events = [e for e in events if e.type in ("danmaku", "gift", "sc")
              or (args.include_entry and e.type == "entry")]
    if not events:
        print("事件流为空")
        return
    print(f"事件流: {src}, {len(events)} 条 "
          f"(弹幕 {sum(1 for e in events if e.type=='danmaku')}, "
          f"礼物/SC {sum(1 for e in events if e.type in ('gift','sc'))})")
    write_jsonl(os.path.join(RESULTS_DIR, f"events_{src}.jsonl"),
                [e.to_dict() for e in events])

    from scorer import score_danmaku
    score_cache = ApiCache("score_danmaku", score_danmaku,
                           enabled=not args.no_api)
    scores = await prescore_events(events, score_cache)
    print(score_cache.stats())

    strategies = ["fifo", "random", "keyword", "greedy", "fixed", "full"] \
        if args.strategy == "all" else [args.strategy]
    all_threshold_traces = []
    for st in strategies:
        commits = enqueued = None
        if st in ("full", "fixed"):
            ft = None if st == "full" else args.fixed_threshold
            name = st if st == "full" else f"fixed{int(args.fixed_threshold)}"
            decisions, trace, sched = await replay_scheduler(
                events, scores, args.tag, st, fixed_threshold=ft, variant=name)
            all_threshold_traces.extend(trace)
            commits = sched.stats["commit"]
            enqueued = sched.stats["enqueue_danmaku"] + sched.stats["enqueue_gift"]
        else:
            decisions = await replay_baseline(events, scores, st)
            name = st
        out = os.path.join(RESULTS_DIR, f"decisions_{args.tag}_{src}_{name}.jsonl")
        write_jsonl(out, decisions)
        n_gift = sum(1 for d in decisions if d["type"] in ("gift", "sc"))
        print(f"[{name}] 回复 {len(decisions)} 条（含礼物 {n_gift}）"
              + (f"，占坑 {commits}/{enqueued}" if commits is not None else "")
              + f" → {out}")
        # 运行元数据（占坑统计等），metrics.py 读取
        with open(os.path.join(RESULTS_DIR, "run_meta.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "tag": args.tag, "src": src, "strategy": name,
                "commits": commits, "enqueued": enqueued,
                "decisions": len(decisions),
                "scorer_model": "deepseek-v4-flash",   # scorer.py 内打分模型
                "reply_model": None,                   # E1 回复生成关闭（占位）
                "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False) + "\n")

    if all_threshold_traces:
        trace_path = os.path.join(RESULTS_DIR, f"threshold_trace_{args.tag}_{src}.jsonl")
        done_file = os.path.join(RESULTS_DIR, f".trace_written_{args.tag}_{src}")
        mode = "a" if os.path.exists(done_file) else "w"
        with open(trace_path, mode, encoding="utf-8") as f:
            for r in all_threshold_traces:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        open(done_file, "w").close()
        print(f"门槛轨迹 {len(all_threshold_traces)} 行 → {trace_path}")


def _cleanup_self_log():
    """删除本次运行自产的当日日志（回放日志不是真实直播数据，防止下次 combo 自吞）"""
    import glob
    import logging
    logging.shutdown()
    for p in glob.glob("logs/live-*.log"):
        try:
            if os.path.getmtime(p) >= RUN_START - 5:
                os.remove(p)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="logs/live-20260730.log")
    ap.add_argument("--combo", action="store_true", help="三场日志拼接回放")
    ap.add_argument("--events-file", help="回放指定 jsonl 事件流（如 benchmark/data/synthetic_*.jsonl）")
    ap.add_argument("--strategy", default="all",
                    choices=["all", "fifo", "random", "keyword", "greedy", "fixed", "full"])
    ap.add_argument("--fixed-threshold", type=float, default=50.0)
    ap.add_argument("--tag", default="e1")
    ap.add_argument("--no-api", action="store_true", help="只用缓存，不调用真实 API")
    ap.add_argument("--include-entry", action="store_true", help="回放进房欢迎事件")
    args = ap.parse_args()
    try:
        asyncio.run(run(args))
    finally:
        _cleanup_self_log()


if __name__ == "__main__":
    main()
