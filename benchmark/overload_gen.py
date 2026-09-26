"""
过载 + 质量漂移合成流生成器（E1-ovl/E4-ovl，预注册见 benchmark/preregistration.md）

两阶段：
  A. 分三档意图让 DeepSeek 生成全新弹幕文本（高=有趣提问/梗/走心；中=普通聊天；低=灌水）
  B. 全量评分（共享缓存）→ 按实测分入桶 → 按预注册漂移参数组装 8 条流

用法：
  python -m benchmark.overload_gen            # 生成文本池 + 评分 + 组装 8 流
  python -m benchmark.overload_gen --skip-gen # 文本池已存在，只重新组装
输出：benchmark/data/ovl_{l3,l6,l12,l24}_{uniform,drift}.jsonl
"""

import argparse
import asyncio
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmark.synth_gen import GIFT_TABLE, USERS, REGULARS
from benchmark.api_cache import ApiCache

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
POOL_PATH = os.path.join(DATA_DIR, "ovl_text_pool.jsonl")
os.makedirs(DATA_DIR, exist_ok=True)

# 预注册参数（preregistration.md §2/§3，写死不许改）
LAMBDAS = {"l3": 3.0, "l6": 6.0, "l12": 12.0, "l24": 24.0}   # 条/分钟
DURATION_MIN = {"l3": 60, "l6": 30, "l12": 30, "l24": 30}
MIX_UNIFORM = {"high": 0.20, "mid": 0.45, "low": 0.35}
MIX_DRIFT = [  # 前 / 中(灌水期) / 后
    {"high": 0.30, "mid": 0.50, "low": 0.20},
    {"high": 0.05, "mid": 0.35, "low": 0.60},
    {"high": 0.30, "mid": 0.50, "low": 0.20},
]
BUCKET_OF = lambda s: "high" if s >= 60 else ("mid" if s >= 30 else "low")

GEN_PROMPTS = {
    "high": """生成 {n} 条B站虚拟主播直播间的【高质量】弹幕，每条大概率值得主播回复：
有趣的问题、玩梗接梗、走心的经历分享、和直播内容强相关的评论、能引出话题的提问。
每行一个 JSON：{{"text":"...","tag":"high"}}。长度10-40字，口语化，内容多样。只输出JSONL。
批次 {batch}，与之前不重复。""",
    "mid": """生成 {n} 条B站直播间的【普通】弹幕：日常问候、简单互动、普通聊天、简短附和。
不算无意义但也不特别值得回复。每行一个 JSON：{{"text":"...","tag":"mid"}}。
长度3-25字，口语化。只输出JSONL。批次 {batch}，与之前不重复。""",
    "low": """生成 {n} 条B站直播间的【灌水低质】弹幕：666、哈哈哈、来了、纯表情、单字、
复读、无意义刷屏、轻度广告。每行一个 JSON：{{"text":"...","tag":"low"}}。
长度1-10字。只输出JSONL。批次 {batch}，与之前不重复。""",
}
POOL_TARGET = {"high": 900, "mid": 1000, "low": 900}  # 文本池目标（含组装余量）


async def gen_pool():
    from openai import AsyncOpenAI
    from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL
    client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    pool = []
    seen = set()
    for intent, target in POOL_TARGET.items():
        calls = 0
        got = 0
        while got < target and calls < 12:
            calls += 1
            resp = await client.chat.completions.create(
                model="deepseek-v4-flash",
                messages=[{"role": "user", "content":
                           GEN_PROMPTS[intent].format(n=min(300, target - got + 30),
                                                      batch=f"{intent}-{calls}")}],
                max_tokens=8000, temperature=1.0,
                extra_body={"thinking": {"type": "disabled"}},
            )
            for line in (resp.choices[0].message.content or "").splitlines():
                line = line.strip().strip("`").strip(",")
                if not line.startswith("{"):
                    continue
                try:
                    d = json.loads(line)
                    t = str(d.get("text", ""))[:60]
                    if t and t not in seen:
                        seen.add(t)
                        pool.append({"text": t, "intent": intent})
                        got += 1
                except json.JSONDecodeError:
                    continue
            print(f"  [{intent}] 第{calls}次调用 → 累计 {got}/{target}")
    with open(POOL_PATH, "w", encoding="utf-8") as f:
        for r in pool:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"文本池 {len(pool)} 条 → {POOL_PATH}")
    return pool


async def score_pool(pool):
    """全量评分（共享缓存），返回 [{text,intent,score}]，按实测分入桶"""
    from scorer import score_danmaku
    cache = ApiCache("score_danmaku", score_danmaku, enabled=True)
    sem_out = []

    async def one(rec):
        rec["score"] = int(await cache(rec["text"]))
        rec["bucket"] = BUCKET_OF(rec["score"])
        sem_out.append(rec)

    await asyncio.gather(*[one(r) for r in pool])
    print(cache.stats())
    with open(POOL_PATH, "w", encoding="utf-8") as f:
        for r in pool:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # 意图 vs 实测桶分布（披露用）
    import collections
    cross = collections.Counter((r["intent"], r["bucket"]) for r in pool)
    print("意图→实测桶交叉表:", dict(cross))
    return pool


def build_streams(pool, seed=42):
    """按预注册参数组装 8 条流。桶内文本有放回抽样（真实直播存在复读）。"""
    buckets = {"high": [], "mid": [], "low": []}
    for r in pool:
        buckets[r["bucket"]].append(r["text"])
    print(f"实测桶大小: high={len(buckets['high'])} mid={len(buckets['mid'])} low={len(buckets['low'])}")

    gift_pool = []
    for g, p, w in GIFT_TABLE:
        gift_pool += [(g, p)] * w

    made = []
    for si, (lk, lam) in enumerate(LAMBDAS.items()):
        for profile in ("uniform", "drift"):
            name = f"ovl_{lk}_{profile}"
            rng = random.Random(seed + si * 10 + (0 if profile == "uniform" else 1))
            n = int(LAMBDAS[lk] * DURATION_MIN[lk])
            events = []
            t = 0.0
            for i in range(n):
                t += rng.expovariate(lam / 60.0)          # Poisson 到达
                seg = min(2, int(3 * i / n))               # 第几段（前/中/后）
                mix = MIX_UNIFORM if profile == "uniform" else MIX_DRIFT[seg]
                bucket = rng.choices(list(mix), weights=list(mix.values()))[0]
                text = rng.choice(buckets[bucket]) if buckets[bucket] else "（空桶）"
                user = rng.choice(REGULARS) if rng.random() < 0.25 else rng.choice(USERS)
                events.append({"id": f"{name}-{i:05d}", "ts": round(t, 2),
                               "type": "danmaku", "username": user, "text": text,
                               "gift_name": "", "num": 1, "price": None,
                               "recorded_score": None, "session": name,
                               "tag": bucket})              # tag=实测桶（漂移真值）
                if rng.random() < 0.015:
                    g, p = rng.choice(gift_pool)
                    events.append({"id": f"{name}-g{i:05d}", "ts": round(t + 0.3, 2),
                                   "type": "gift", "username": rng.choice(USERS + REGULARS),
                                   "text": "", "gift_name": g, "num": 1, "price": p,
                                   "recorded_score": None, "session": name})
            events.sort(key=lambda e: e["ts"])
            out = os.path.join(DATA_DIR, f"{name}.jsonl")
            with open(out, "w", encoding="utf-8") as f:
                for e in events:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            made.append((name, len(events)))
            print(f"  {out}: {len(events)} 事件，时长 {events[-1]['ts']/60:.0f} 分钟")
    return made


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-gen", action="store_true")
    args = ap.parse_args()
    if not args.skip_gen or not os.path.exists(POOL_PATH):
        pool = await gen_pool()
        pool = await score_pool(pool)
    else:
        pool = [json.loads(l) for l in open(POOL_PATH, encoding="utf-8")]
        if "bucket" not in pool[0]:
            pool = await score_pool(pool)
    build_streams(pool)


if __name__ == "__main__":
    asyncio.run(main())
