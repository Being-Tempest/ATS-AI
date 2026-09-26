"""
合成密集弹幕流生成器 — 解决真实弹幕量不足（97 条）导致策略区分度出不来的问题

三条流（文件名带 synthetic_ 前缀，仅用于回放调度对比，不与真实数据混合统计）：
  synthetic_steady.jsonl   平稳中流量（3-5s/条）
  synthetic_bursty.jsonl   高峰涌入（含 2 次弹幕风暴 ~30 条/分钟）
  synthetic_shift.jsonl    冷清→涌入突变（前半 ~20s/条，后半 ~2s/条）

文本由 DeepSeek 批量生成（每流 2 次调用，共 6 次）；时间间隔/用户池/礼物穿插由
本程序按 profile 确定性生成（ seeded ）。用户池 40 个虚拟 ID，含 5 个高频"熟人"。

用法：python -m benchmark.synth_gen [--n 700] [--seed 42]
"""

import argparse
import asyncio
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

GEN_PROMPT = """你在为一个B站虚拟主播直播间模拟弹幕语料。参考真实弹幕风格：
{examples}

请生成 {n} 条模拟弹幕，每行一个 JSON 对象（JSONL，不要数组、不要代码块）：
{{"text": "弹幕内容", "tag": "类别"}}

tag 类别与占比要求：
- "normal" 普通互动/聊天（约35%）
- "address" @主播或带称呼"佳宁"（约15%）
- "question" 提问（约15%）
- "meme" 梗/复读/接话（约10%）
- "spam" 无意义灌水如"666""哈哈哈""来了"（约15%）
- "ad" 广告/低质/引战（约5%）
- "story" 讲故事/分享经历的长弹幕20字以上（约5%）

要求：口语化、长度2-40字、内容多样不重复、涉及游戏/学习/生活/感情等话题。只输出 JSONL。"""

GIFT_TABLE = [  # (名称, 电池数, 权重)
    ("小心心", 10, 40), ("粉丝团灯牌", 100, 25), ("打call", 50, 15),
    ("棒棒糖", 10, 10), ("小花花", 10, 5), ("舰长", 13800, 1),
]
USERS = [f"观众{i:02d}" for i in range(35)] + \
        ["星空下的猫", "爱笑的桃子", "路过的风", "芝士拌饭", "熬夜冠军"]
REGULARS = ["老哥本哥", "佳宁的小跟班", "天天来看", "阿伟", "弹幕姬本姬"]  # 高频熟人


async def gen_texts(n: int, seed_tag: str) -> list[dict]:
    """调 DeepSeek 批量生成弹幕文本（每批 ~350 条，不够再调一次）"""
    from openai import AsyncOpenAI
    from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL
    from benchmark.log_parser import list_session_logs, parse_log
    examples = []
    for p in list_session_logs():
        examples += [e.text for e in parse_log(p) if e.type == "danmaku"]
    random.Random(0).shuffle(examples)
    examples = "\n".join(examples[:40])

    client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    out: list[dict] = []
    seen = set()
    calls = 0
    while len(out) < n and calls < 14:
        calls += 1
        want = min(300, n - len(out) + 20)
        resp = await client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content":
                       GEN_PROMPT.format(examples=examples, n=want)
                       + f"\n\n批次编号：{seed_tag}-{calls}（与之前批次内容不要重复）"}],
            max_tokens=8000,
            temperature=1.0,
            extra_body={"thinking": {"type": "disabled"}},  # 省 token 给正文
        )
        text = resp.choices[0].message.content or ""
        got = 0
        for line in text.splitlines():
            line = line.strip().strip("`").strip(",")
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
                t = str(d.get("text", ""))[:60]
                if t and d.get("tag") and t not in seen:
                    seen.add(t)
                    out.append({"text": t, "tag": str(d["tag"])})
                    got += 1
            except json.JSONDecodeError:
                continue
        print(f"  [{seed_tag}] 第{calls}次调用 → 本批 {got} 条，累计 {len(out)} 条")
    return out[:n]


def build_stream(name: str, texts: list[dict], n: int, seed: int) -> list[dict]:
    """按流量 profile 分配时间戳/用户/礼物穿插，输出事件 dict 列表"""
    rng = random.Random(seed)
    n = min(n, len(texts))
    events = []
    t = 0.0
    storm_at = []
    if name == "bursty":
        total_est = n * 8
        storm_at = [total_est * 0.35, total_est * 0.7]  # 两次风暴中心
    gift_pool = []
    for g, p, w in GIFT_TABLE:
        gift_pool += [(g, p)] * w

    i = 0
    storm_left = 0
    while i < n:
        # ── 到达间隔 ──
        if name == "steady":
            gap = rng.uniform(3, 5)
        elif name == "bursty":
            if storm_left > 0:
                gap = rng.uniform(1, 3)          # 风暴中 ~30条/分钟
                storm_left -= 1
            elif storm_at and t >= storm_at[0]:
                storm_at.pop(0)
                storm_left = 30
                gap = 1.0
            else:
                gap = rng.uniform(5, 12)         # 平时中低流量
        else:  # shift
            half = n * 9  # 粗估：前半 20s/条
            gap = rng.uniform(15, 25) if i < n // 2 else rng.uniform(1.2, 3)
        t += gap

        user = rng.choice(REGULARS) if rng.random() < 0.25 else rng.choice(USERS)
        d = texts[i]
        events.append({"id": f"{name}-{i:05d}", "ts": round(t, 2), "type": "danmaku",
                       "username": user, "text": d["text"], "gift_name": "", "num": 1,
                       "price": None, "recorded_score": None,
                       "session": f"synthetic_{name}", "tag": d["tag"]})
        i += 1
        # ── 礼物穿插（约 1.5% 概率）──
        if rng.random() < 0.015:
            g, p = rng.choice(gift_pool)
            events.append({"id": f"{name}-g{i:05d}", "ts": round(t + 0.5, 2),
                           "type": "gift", "username": rng.choice(USERS + REGULARS),
                           "text": "", "gift_name": g, "num": 1, "price": p,
                           "recorded_score": None, "session": f"synthetic_{name}"})
    events.sort(key=lambda e: e["ts"])
    return events


PROFILES = {"steady": 700, "bursty": 800, "shift": 600}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    for k, (name, n) in enumerate(PROFILES.items()):
        out_path = os.path.join(DATA_DIR, f"synthetic_{name}.jsonl")
        if os.path.exists(out_path):
            print(f"{out_path} 已存在，跳过（删除后可重新生成）")
            continue
        print(f"生成 {name}（目标 {n} 条弹幕）...")
        texts = await gen_texts(n, name)
        events = build_stream(name, texts, n, seed=args.seed + k)
        with open(out_path, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        span = events[-1]["ts"] / 60
        n_gift = sum(1 for e in events if e["type"] == "gift")
        print(f"  → {out_path}: {len(events)} 事件（礼物 {n_gift}），时长 {span:.0f} 分钟")


if __name__ == "__main__":
    asyncio.run(main())
