import hashlib
import json
import math
import os
import statistics

from benchmark.api_cache import ApiCache
from benchmark.overload_gen import POOL_PATH
from benchmark.metrics_ov import coverage_stats
from benchmark.replay import replay_baseline, replay_scheduler

EXPERIMENT_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(EXPERIMENT_DIR, "data")
RESULTS_DIR = os.path.join(EXPERIMENT_DIR, "results")
STRATEGIES = ["fifo", "greedy", "full_commit", "full_merge_commit", "full_merge_q3_commit"]
SEEDS = [42, 1001, 2002, 3003, 4004]


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_frozen_scorebook():
    scorebook = {}
    cache = ApiCache("score_danmaku", None, enabled=False)
    for row in read_jsonl(POOL_PATH):
        text = row["text"]
        score = int(row["score"])
        if text in scorebook:
            assert scorebook[text] == score, f"冻结池同文本评分冲突: {text}"
        scorebook[text] = score
        key = ApiCache._key(text)
        assert key in cache._mem, f"评分缓存缺失: {text}"
        assert int(cache._mem[key]) == score, f"冻结池与缓存评分不一致: {text}"
    return scorebook


def scores_for_events(events, scorebook=None):
    scorebook = scorebook or load_frozen_scorebook()
    scores = {}
    for ev in events:
        if ev.type != "danmaku":
            continue
        assert ev.text in scorebook, f"事件文本不在冻结池: {ev.id} {ev.text}"
        assert ev.id not in scores, f"重复事件 ID: {ev.id}"
        scores[ev.id] = scorebook[ev.text]
    return scores


async def run_strategy(events, scores, strategy, tag, duration_fn=None):
    if strategy in ("fifo", "greedy"):
        decisions = await replay_baseline(events, scores, strategy, duration_fn=duration_fn)
        return decisions, None
    batch_merge = strategy in ("full_merge_commit", "full_merge_q3_commit")
    q3_trigger = 8 if strategy == "full_merge_q3_commit" else None
    decisions, _, sched = await replay_scheduler(
        events, scores, tag, "full", variant=strategy,
        batch_merge=batch_merge, batch_q3_trigger=q3_trigger,
        duration_fn=duration_fn,
    )
    return decisions, sched.stats


def response_states(decisions, events):
    event_by_id = {ev["id"]: ev for ev in events}
    assert len(event_by_id) == len(events), "输入事件 ID 不唯一"

    def canonical_type(item_type):
        return "gift" if item_type in ("gift", "sc") else item_type

    precise = {}
    batch = {}
    for decision in decisions:
        if decision.get("type") == "batch":
            for covered in decision.get("covered", []):
                ev_id = covered.get("ev_id")
                assert ev_id in event_by_id, f"批量覆盖未知事件: {ev_id}"
                assert canonical_type(covered.get("type")) == canonical_type(event_by_id[ev_id]["type"]), \
                    f"批量覆盖事件类型不一致: {ev_id}"
                assert ev_id not in precise and ev_id not in batch, f"事件被重复回应: {ev_id}"
                batch[ev_id] = decision["t"]
        else:
            ev_id = decision.get("ev_id")
            assert ev_id in event_by_id, f"精回未知事件: {ev_id}"
            assert canonical_type(decision.get("type")) == canonical_type(event_by_id[ev_id]["type"]), \
                f"精回事件类型不一致: {ev_id}"
            assert ev_id not in precise and ev_id not in batch, f"事件被重复回应: {ev_id}"
            precise[ev_id] = decision["t"]
    return precise, batch


def validate_decision_scores(decisions, events, scorebook):
    event_by_id = {ev["id"]: ev for ev in events}
    for decision in decisions:
        items = decision.get("covered", []) if decision.get("type") == "batch" else [decision]
        for item in items:
            ev = event_by_id[item["ev_id"]]
            if ev["type"] == "danmaku":
                assert int(item["score"]) == scorebook[ev["text"]], \
                    f"决策分数与冻结池不一致: {item['ev_id']}"


def run_metrics(decisions, events, precise_duration, scorebook, slot_fn=None):
    precise, batch = response_states(decisions, events)
    validate_decision_scores(decisions, events, scorebook)
    stats = coverage_stats(decisions, events)
    dm_ids = {ev["id"] for ev in events if ev["type"] == "danmaku"}
    gift_ids = {ev["id"] for ev in events if ev["type"] in ("gift", "sc")}
    precise_dm = dm_ids & precise.keys()
    batch_dm = dm_ids & batch.keys()
    ignored_dm = dm_ids - precise.keys() - batch.keys()
    assert len(precise_dm) + len(batch_dm) + len(ignored_dm) == len(dm_ids)
    assert not (precise.keys() & batch.keys())
    high_ids = {ev["id"] for ev in events if ev["type"] == "danmaku" and ev.get("tag") == "high"}
    high_responded = high_ids & (precise.keys() | batch.keys())
    event_by_id = {ev["id"]: ev for ev in events}
    high_waits = [(precise.get(ev_id, batch.get(ev_id))) - event_by_id[ev_id]["ts"]
                  for ev_id in high_responded]
    responded_waits = [(precise.get(ev_id, batch.get(ev_id))) - event_by_id[ev_id]["ts"]
                       for ev_id in dm_ids & (precise.keys() | batch.keys())]
    slot_fn = slot_fn or (lambda d: math.ceil(d["duration"] / precise_duration))
    equivalent_slots = sum(slot_fn(d) for d in decisions)
    batches = [d for d in decisions if d["type"] == "batch"]
    occupied_s = sum(d["duration"] for d in decisions)
    first_arrival = min(ev["ts"] for ev in events)
    last_end = max((d["t"] + d["duration"] for d in decisions), default=first_arrival)
    span_s = max(0.0, last_end - first_arrival)
    precise_scores = [d["score"] for d in decisions if d["type"] == "danmaku"]
    stats.update({
        "n_danmaku": len(dm_ids),
        "n_gifts": len(gift_ids),
        "precise_n": len(precise_dm),
        "batch_covered_n": len(batch_dm),
        "ignored_n": len(ignored_dm),
        "gift_precise_n": len(gift_ids & precise.keys()),
        "gift_responded_n": len(gift_ids & (precise.keys() | batch.keys())),
        "mean_precise_score": statistics.mean(precise_scores) if precise_scores else None,
        "high_wait": statistics.mean(high_waits) if high_waits else None,
        "mean_wait": statistics.mean(responded_waits) if responded_waits else None,
        "generation_calls": len(decisions),
        "batch_n": len(batches),
        "batch_avg": len(batch_dm) / len(batches) if batches else 0.0,
        "occupied_s": occupied_s,
        "span_s": span_s,
        "gap_s": max(0.0, span_s - occupied_s),
        "equivalent_slots": equivalent_slots,
        "per_precise_slot_cov": len(precise_dm | batch_dm) / equivalent_slots if equivalent_slots else None,
    })
    return stats


def fano_and_peak(events, window_s):
    danmaku = [ev for ev in events if ev["type"] == "danmaku" and 0 <= ev["ts"] <= 1800]
    n_windows = math.ceil(1800 / window_s)
    counts = [0] * n_windows
    for ev in danmaku:
        index = min(n_windows - 1, int(ev["ts"] // window_s))
        counts[index] += 1
    mean = statistics.mean(counts)
    variance = statistics.variance(counts) if len(counts) > 1 else 0.0
    return {
        "counts": counts,
        "mean": mean,
        "fano": variance / mean if mean else 0.0,
        "peak_per_min": max(counts) * 60 / window_s,
    }


def mean_sd(values):
    return {
        "mean": statistics.mean(values),
        "sd": statistics.stdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "max": max(values),
    }


def sha256_file(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def sha256_rows(rows):
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
