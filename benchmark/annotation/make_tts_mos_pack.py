"""包2：TTS 语音质量 MOS 标注材料包（老师意见五-7a）

用 CosyVoice 3.5（生产同款 voice/model）真实合成 18 条语音样本，混三类：
- short_precise：短精确回复（6 条，≤30 字）
- long_batch：长批量回复含点名罗列（6 条，来自 batch_length 实测样本 n≥8，60-70 字，
  老师关心的"听觉疲劳"点）
- emotion：带情绪标签的回复（6 条，标签各不相同；标签本身不朗读，生产管线同口径——
  标签被解析去驱动 Live2D，TTS 输入是标签之后的正文）

wav 存 benchmark/annotation/tts_mos/，按编号 M01-M18 命名，顺序已打乱去标识。
评分三维：自然度 / 可懂度 / 疲劳感（各 1-5）。

用法：python -X utf8 -m benchmark.annotation.make_tts_mos_pack
输出：benchmark/annotation/tts_mos/
      M01.wav ... M18.wav
      tts_mos_blind18.csv    标注表（发给同学）
      README.md              标注者说明
      .tts_mos_key.json      隐藏 key（编号→类型/文本，勿发给同学）
API：dashscope CosyVoice，18 次真实合成调用（wav 已存在则跳过，重跑零额外调用）
"""
import asyncio
import json
import os
import random
import re
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "..", "results")
OUT = os.path.join(HERE, "tts_mos")
SEED = 42
TAG_RE = re.compile(r"^\[([^\[\]]{1,8})\]\s*")


def load_texts():
    rng = random.Random(SEED)
    precise = [json.loads(l) for l in open(
        os.path.join(RES, "precise_fidelity_samples.jsonl"), encoding="utf-8") if l.strip()]

    shorts = [r for r in precise if len(TAG_RE.sub("", r["reply"])) <= 30]
    rng.shuffle(shorts)
    items = [{"cls": "short_precise", "tag": (TAG_RE.match(r["reply"]) or [None, None])[1],
              "text": TAG_RE.sub("", r["reply"]).strip()} for r in shorts[:6]]

    batches = [json.loads(l) for l in open(
        os.path.join(RES, "batch_length_samples.jsonl"), encoding="utf-8") if l.strip()]
    longs = [b for b in batches if b["n"] >= 8]
    rng.shuffle(longs)
    items += [{"cls": "long_batch", "tag": None,
               "text": TAG_RE.sub("", b["reply"]).strip()} for b in longs[:6]]

    by_tag = {}
    for r in precise:
        m = TAG_RE.match(r["reply"])
        if m:
            by_tag.setdefault(m.group(1), []).append(r)
    tags = sorted(by_tag, key=lambda t: -len(by_tag[t]))
    emo = []
    for t in tags:
        rng.shuffle(by_tag[t])
        emo.append({"cls": "emotion", "tag": t,
                    "text": TAG_RE.sub("", by_tag[t][0]["reply"]).strip()})
        if len(emo) == 6:
            break
    items += emo

    rng.shuffle(items)
    for i, s in enumerate(items, 1):
        s["mid"] = f"M{i:02d}"
    return items


def synthesize_wav(text: str, path: str):
    import dashscope
    from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat
    from config import DASHSCOPE_API_KEY, COSYVOICE_VOICE_ID, COSYVOICE_MODEL
    dashscope.api_key = DASHSCOPE_API_KEY
    dashscope.base_websocket_api_url = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

    async def _synth():
        syn = SpeechSynthesizer(model=COSYVOICE_MODEL, voice=COSYVOICE_VOICE_ID,
                                format=AudioFormat.PCM_24000HZ_MONO_16BIT)
        return syn.call(text)

    loop = asyncio.new_event_loop()
    try:
        audio_bytes = loop.run_until_complete(_synth())
    finally:
        loop.close()
    if not audio_bytes:
        raise RuntimeError(f"TTS 返回空音频: {text[:30]}")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(audio_bytes)


def main():
    os.makedirs(OUT, exist_ok=True)
    items = load_texts()
    calls = 0
    for s in items:
        path = os.path.join(OUT, f"{s['mid']}.wav")
        if os.path.exists(path):
            print(f"{s['mid']} 已存在，跳过", flush=True)
            continue
        synthesize_wav(s["text"], path)
        calls += 1
        print(f"{s['mid']} [{s['cls']}] {len(s['text'])}字 → wav", flush=True)
    print(f"真实合成调用 {calls} 次", flush=True)

    import csv
    with open(os.path.join(OUT, "tts_mos_blind18.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["编号", "自然度1-5", "可懂度1-5", "疲劳感1-5", "备注"])
        for s in items:
            w.writerow([s["mid"], "", "", "", ""])

    key = {s["mid"]: {"cls": s["cls"], "tag": s["tag"], "text": s["text"]} for s in items}
    with open(os.path.join(OUT, ".tts_mos_key.json"), "w", encoding="utf-8") as f:
        json.dump(key, f, ensure_ascii=False, indent=1)
    from collections import Counter
    print(Counter(s["cls"] for s in items))


if __name__ == "__main__":
    main()
