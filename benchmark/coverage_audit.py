"""B1 审计：coverage 分母 — 每种子每臂 到达/入队/丢弃/槽数/答复数 + 两种口径 coverage

口径定义：
  到达数 N_arr  = 事件流里全部 danmaku 事件
  入队数 N_enq  = 实际进入待回缓冲的消息数（基线：PENDING_CAP=35 池；调度器：_route 落点非"丢弃"）
  丢弃数        = 溢出/置换丢失（基线：滚出 35 池；调度器：三队列全满"丢弃"或级联挤出落 🗑）
  服务槽数      = Σ decision.duration / 9.0s
  答复数        = 精回数 + 批量覆盖条数（batch 事件本身计 1 次生成动作）
  coverage_到达 = 答复数 / N_arr
  coverage_入队 = 答复数 / N_enq

输出：benchmark/results/coverage_denominator_audit.md
"""
import asyncio, json, os, sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import benchmark.replay as R
from benchmark.replay import (load_events_file, prescore_events, replay_scheduler,
                                write_jsonl, PENDING_CAP, STUB_REPLY, keyword_hit,
                                KEYWORD_COOLDOWN)
from benchmark.api_cache import ApiCache
import scheduler as S

DATA = os.path.join(os.path.dirname(__file__), "data")
RES = os.path.join(os.path.dirname(__file__), "results")
SLOT = 9.0


def baseline_audit(events, scores, strategy):
    """replay_baseline 的计数器克隆（逻辑逐行对齐）"""
    import random
    pending, decisions = [], []
    free_at, last_end = 0.0, -1e9
    rng = random.Random(42)
    n_enq = n_evict = 0

    def ev_score(item):
        if item.type == "danmaku":
            return scores.get(item.id, 0)
        from scorer import score_gift
        return score_gift(item.gift_name or "(未知礼物)", item.price) if item.price is not None \
            else (item.recorded_score or 70)

    def push(ev):
        nonlocal n_enq, n_evict
        if len(pending) < PENDING_CAP:
            pending.append(ev); n_enq += 1; return
        if strategy == "greedy":
            worst = min(pending, key=ev_score)
            if ev_score(ev) > ev_score(worst):
                pending.remove(worst); pending.append(ev); n_enq += 1; n_evict += 1
            else:
                n_evict += 1   # 没挤进去，自己丢
        else:
            pending.pop(0); pending.append(ev); n_enq += 1; n_evict += 1

    def pick():
        if strategy in ("fifo", "keyword"): return pending.pop(0)
        if strategy == "greedy":
            it = max(pending, key=ev_score); pending.remove(it); return it
        return pending.pop(rng.randrange(len(pending)))

    def reply(item, start):
        nonlocal free_at, last_end
        dur = max(2, len(STUB_REPLY) / 5)
        decisions.append({"t": start, "type": item.type, "ev_id": item.id, "duration": dur})
        free_at = start + dur; last_end = free_at

    for ev in events:
        t = ev.ts
        while pending and free_at <= t:
            reply(pick(), free_at)
        if strategy == "keyword":
            if keyword_hit(ev) and t - last_end >= KEYWORD_COOLDOWN:
                push(ev)
        else:
            push(ev)
        if pending and free_at <= t:
            reply(pick(), t)
    while pending:
        reply(pick(), free_at)
    leftover = 0  # 收尾全播完，无残留
    return decisions, n_enq, n_evict, leftover


class CountingScheduler(S.Scheduler):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.landings = Counter()

    def _route(self, item):
        dest = super()._route(item)
        self.landings[dest] += 1
        return dest


async def scheduler_audit(events, scores, **kw):
    orig = R.Scheduler
    R.Scheduler = CountingScheduler
    try:
        dec, _, sched = await replay_scheduler(events, scores, "audit", "x", **kw)
    finally:
        R.Scheduler = orig
    dropped = sched.landings.get("丢弃", 0)
    # 级联挤出最终落 🗑 的条目数 = 日志里数不到，近似：三队列全满时的丢失全在 landings["丢弃"]
    residual = sum(len(q) for q in (sched.q1_d, sched.q1_g, sched.q2_d,
                                    sched.q2_g, sched.q3_d, sched.q3_g))
    enq = sched.stats.get("enqueue_danmaku", 0) - dropped
    return dec, enq, dropped, residual


def summarize(tag, events, dec, n_enq, n_drop, n_resid):
    dm = [e for e in events if e.type == "danmaku"]
    n_arr = len(dm)
    served_ids = set()
    n_batch = 0
    slots = 0.0
    last_end = 0.0
    for d in dec:
        dur = d.get("duration", SLOT)
        slots += dur / SLOT
        last_end = max(last_end, d.get("t", 0) + dur)
        if d.get("type") == "batch":
            n_batch += 1
            for c in d.get("covered", []):
                if c.get("ev_id"): served_ids.add(c["ev_id"])
        elif d.get("ev_id"):
            served_ids.add(d["ev_id"])
    span = (dm[-1].ts - dm[0].ts) if dm else 0
    n_served = len(served_ids)
    return dict(tag=tag, n_arr=n_arr, n_enq=n_enq, n_drop=n_drop, n_resid=n_resid,
                n_reply_actions=len(dec), n_served=n_served, n_batch=n_batch,
                slots=round(slots, 1), span_min=round(span / 60, 1),
                cov_arr=n_served / n_arr if n_arr else 0,
                cov_enq=n_served / n_enq if n_enq else 0)


async def main():
    from scorer import score_danmaku
    cache = ApiCache("score_danmaku", score_danmaku, enabled=False)
    rows = []

    async def do(src, strategy, kind, **kw):
        events = load_events_file(os.path.join(DATA, f"{src}.jsonl"))
        scores = await prescore_events(events, cache)
        if kind == "base":
            dec, enq, drop, resid = baseline_audit(events, scores, strategy)
        else:
            dec, enq, drop, resid = await scheduler_audit(events, scores, **kw)
        rows.append(summarize(f"{src} / {strategy}", events, dec, enq, drop, resid))
        print(rows[-1]["tag"], "done", flush=True)

    seeds = ["", "_s1001", "_s2002", "_s3003", "_s4004"]
    # (a) λ=24 drift 选择策略：seed42 六策略 + 5种子四策略
    for st in ("fifo", "random", "keyword", "greedy"):
        await do("ovl_l24_drift", st, "base")
    await do("ovl_l24_drift", "fixed50_commit", "sched", fixed_threshold=50.0)
    await do("ovl_l24_drift", "full_commit", "sched")
    for sfx in seeds[1:]:
        for st in ("fifo", "greedy"):
            await do(f"ovl_l24_drift{sfx}", st, "base")
        await do(f"ovl_l24_drift{sfx}", "fixed50_commit", "sched", fixed_threshold=50.0)
        await do(f"ovl_l24_drift{sfx}", "full_commit", "sched")
    # (b) λ=12 drift greedy ×5
    for sfx in seeds:
        await do(f"ovl_l12_drift{sfx}", "greedy", "base")
    # (c) λ=24 drift 聚合两臂 ×5
    for sfx in seeds:
        await do(f"ovl_l24_drift{sfx}", "full_merge_commit", "sched", batch_merge=True)
        await do(f"ovl_l24_drift{sfx}", "full_merge_q3_commit", "sched",
                 batch_merge=True, batch_q3_trigger=8)

    L = ["# B1 审计：coverage 分母核对", ""]
    hdr = "| 流/策略 | 到达 | 入队 | 丢弃 | 残留 | 答复动作 | 服务条数 | 批量 | 槽数 | 跨度min | cov(到达) | cov(入队) |"
    L += [hdr, "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['tag']} | {r['n_arr']} | {r['n_enq']} | {r['n_drop']} | {r['n_resid']} | "
                 f"{r['n_reply_actions']} | {r['n_served']} | {r['n_batch']} | {r['slots']} | "
                 f"{r['span_min']} | {r['cov_arr']*100:.1f}% | {r['cov_enq']*100:.1f}% |")
    open(os.path.join(RES, "coverage_denominator_audit.md"), "w", encoding="utf-8").write(
        "\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    asyncio.run(main())
