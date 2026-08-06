"""
三队列弹幕调度器 — 流水线版 Phase 2b (v2: 占坑修复)

[播放 current] [合成 next] [队列排队]

设计原则：
- 管线完全空闲时第一个到的条目，不管分数多低，直接 commit。
  不参与后来者的竞争排名。评分前就占坑，防止异步评分竞速。
- 已在播放的不会被打断。sd.play() 非阻塞，音频流已送出。
"""

import asyncio
import time
import random
import math
import concurrent.futures
from collections import deque
from score_queue import ScoreQueue, QueueItem
from scorer import score_danmaku, score_gift
from ai_chat import ChatBot
from entry_welcome import random_welcome
from config import VTS_SWAY, VTS_IDLE_EAT_ID, VTS_IDLE_SLEEP_ID, VTS_IDLE_EAT_INTERVAL, VTS_IDLE_SLEEP_AFTER, DEEPSEEK_MODEL
from persona import SYSTEM_PROMPT
from reply_indicator import update_top3, show_subtitle, clear_subtitle, clear_all
from logger import get_logger

log = get_logger("scheduler")

# 队列容量
Q1_CAP = 5
Q2_CAP = 10
Q3_CAP = 20

# 级联等待（秒）
Q1_TO_Q2_WAIT = 8
Q2_TO_Q3_WAIT = 20

# 句间停顿（秒）— 模拟真人思考
REPLY_GAP_MIN = 0.5
REPLY_GAP_MAX = 1.5


class Scheduler:
    """三队列调度器 — 流水线版 + 占坑机制"""

    def __init__(self, chatbot: ChatBot, tts=None, vts=None):
        self.chatbot = chatbot
        self.tts = tts
        self.vts = vts  # VTSClient，已连接；None 则跳过表情

        # 六个队列
        self.q1_d = ScoreQueue(Q1_CAP, "Q1-D")
        self.q1_g = ScoreQueue(Q1_CAP, "Q1-G")
        self.q2_d = ScoreQueue(Q2_CAP, "Q2-D")
        self.q2_g = ScoreQueue(Q2_CAP, "Q2-G")
        self.q3_d = ScoreQueue(Q3_CAP, "Q3-D")
        self.q3_g = ScoreQueue(Q3_CAP, "Q3-G")

        # 流水线状态
        self.playing_until = 0.0               # 当前播放结束时间戳
        self.next_ready: tuple | None = None   # (audio, reply_text) 已合成待播放
        self.preparing: asyncio.Task | None = None  # 后台准备任务

        # 占坑机制：管线空闲时第一个到的弹幕直接 commit
        self._committed_item: QueueItem | None = None   # 已占坑，主循环优先取
        self._committed_reserved: bool = False           # 正在评分中（尚未入队）

        # 级联计时
        self.q1_empty_since: float | None = None
        self.q2_empty_since: float | None = None

        # 升舱门槛：滑动窗口弹幕均分，用于 Q2/Q3 → Q1 提拔
        self._danmaku_window = deque(maxlen=50)  # 只存 >=20 的弹幕分
        self._threshold = 30.0                    # 弹幕升舱门槛，初始 30

        # 礼物升舱：独立滑动窗口，5分一档，锁在[70, 85]
        self._gift_window = deque(maxlen=50)
        self._gift_threshold = 70.0               # 礼物升舱门槛，初始 70

        # TTS 线程池（单线程，保证合成顺序）
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        # 说话晃动 + 闲置动画
        self._anim_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._idle_since: float | None = None  # 开始闲置的时间戳

    # ===== 入队 =====

    def enqueue_entry(self, username: str):
        """进房欢迎入队——与其他弹幕同池排名竞争"""
        from entry_welcome import QUEUE_SCORE
        item = QueueItem(score=QUEUE_SCORE, item_type="entry", username=username,
                         text=f"(进入直播间)")
        dest = self._route(item)
        log.info(f"🚪 [{username}] 进入直播间 → {dest}")
        self._update_indicator()

    async def enqueue_danmaku(self, username: str, text: str):
        """弹幕 → DS打分 → 入队。管线空闲时评分前就占坑，防竞速。"""
        # ── 评分前就占坑 ──
        if self._should_commit():
            self._committed_reserved = True
            try:
                score = await score_danmaku(text)
            except Exception:
                self._committed_reserved = False
                return
            self._feed_danmaku_score(score)
            item = QueueItem(score=score, item_type="danmaku", username=username, text=text)
            self._committed_item = item
            self._committed_reserved = False
            log.info(f"🔒 占坑 ({item.score}分) [{username}]：{text}")
        else:
            score = await score_danmaku(text)
            self._feed_danmaku_score(score)
            item = QueueItem(score=score, item_type="danmaku", username=username, text=text)
            dest = self._route(item)
            log.info(f"📩 ({score}分) [{username}]：{text} → {dest}")
        self._update_indicator()

    def enqueue_gift(self, username: str, gift_name: str, price: int, num: int = 1):
        """礼物 → 本地赋分 → 入队。同步，无评分竞速问题。"""
        score = score_gift(gift_name, price)
        self._feed_gift_score(score)
        if num > 1:
            text = f"(送了{num}个{gift_name})"
        else:
            text = f"(送了{gift_name})"
        item = QueueItem(score=score, item_type="gift", username=username,
                         text=text, price=price)

        if self._should_commit():
            self._committed_item = item
            log.info(f"🔒 占坑 ({item.score}分) [{username}] 礼物")
        else:
            dest = self._route(item)
            rmb = price / 10
            info = f"{num}个{gift_name}" if num > 1 else gift_name
            log.info(f"🎁 ({score}分) [{username}] 送 {info} ¥{rmb:.0f} → {dest}")
        self._update_indicator()

    def enqueue_vision(self, description: str):
        """画面评论入队 — 最高优先级，跳过评分"""
        item = QueueItem(score=200, item_type="vision", username="主播",
                         text=description)
        # 不走 _route，直接插 Q1 弹幕队列最前面
        self.q1_d.push(item)
        log.info(f"🖼 画面评论入队 (最高优先)")
        self._update_indicator()

    async def _vision_comment(self, description: str) -> str | None:
        """基于画面描述 → DS生成主播评论，不值得说返回 None"""
        prompt = (
            f"你现在看着直播画面，画面内容：{description}\n"
            f"如果画面有值得评论的内容（游戏动态、有趣的事），"
            f"用你的语气简短评论（30字以内）。如果画面很普通没变化，只回复 SKIP。"
        )
        try:
            response = await self.chatbot.client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=500,
                temperature=0.9,
                extra_body={"thinking": {"type": "disabled"}},  # 画面评论是简单任务，不思考：省延迟+杜绝思维链朗读
            )
            text = (response.choices[0].message.content or "").strip()
            if not text:
                # 不朗读思维链：DS 没给出正经回复就跳过（宁可不说，也不念 reasoning）
                rc = getattr(response.choices[0].message, 'reasoning_content', None)
                if rc:
                    log.warning(f"Vision DS content为空，跳过朗读 reasoning: {str(rc)[:200]}")
                return None
            if "SKIP" in text.upper():
                return None
            return self.chatbot._clean_reply(text)
        except Exception as e:
            log.error(f"Vision DS 失败: {e}")
            return None

    def _should_commit(self) -> bool:
        """管线完全空闲 且 无人占坑 且 无待播内容"""
        return (
            self.playing_until == 0
            and self.preparing is None
            and self.next_ready is None
            and self._committed_item is None
            and not self._committed_reserved
        )

    def _feed_danmaku_score(self, score: int):
        """弹幕评分后喂入滑动窗口（>=20 才计入）"""
        if score >= 20:
            self._danmaku_window.append(score)
            if self._danmaku_window:
                avg = sum(self._danmaku_window) / len(self._danmaku_window)
                self._threshold = max(30.0, avg)

    def _threshold_val(self) -> float:
        """返回弹幕升舱门槛"""
        return self._threshold

    def _feed_gift_score(self, score: int):
        """礼物评分后喂入滑动窗口（>=20 才计入）"""
        if score >= 20:
            self._gift_window.append(score)
            if self._gift_window:
                avg = sum(self._gift_window) / len(self._gift_window)
                # 5分一档，锁在 [70, 85]
                step = int(avg / 5) * 5
                self._gift_threshold = max(70.0, min(85.0, float(step)))

    def _gift_threshold_val(self) -> float:
        """返回礼物升舱门槛"""
        return self._gift_threshold

    @staticmethod
    def _parse_emotion_tag(text: str) -> tuple[str, str]:
        """从回复开头解析 [情绪] 标签，返回 (clean_text, vts_emotion_key)"""
        import re
        m = re.match(r'^\[(开心|害羞|感动|认真|惊讶|不悦|普通)\]', text)
        if m:
            tag = m.group(1)
            tag_map = {
                "开心": "happy", "害羞": "shy", "感动": "touched",
                "认真": "thinking", "惊讶": "surprised", "不悦": "tsundere",
                "普通": "default_smile",
            }
            return text[m.end():].strip(), tag_map.get(tag, "default_smile")
        return text, "default_smile"

    def _route(self, item: QueueItem) -> str:
        """排名驱动路由：分数决定排名，排名决定队列。
        从 Q1 开始逐级挑战——挤得进去就进，挤不进去就下放；
        被挤出的条目逐级向下级联。返回落点 "Q1"/"Q2"/"Q3"/"丢弃"。"""
        if item.item_type in ("danmaku", "entry"):
            queues = [self.q1_d, self.q2_d, self.q3_d]
        else:
            queues = [self.q1_g, self.q2_g, self.q3_g]
        q_names = ["Q1", "Q2", "Q3"]

        current = item
        for i, q in enumerate(queues):
            evicted = q.push(current)
            if evicted is None:
                return q_names[i]       # 有空位，直接入
            if evicted is current:
                continue                # 打不过该队列最低分，试下一级

            # 挤进去了，被挤出的条目向下级联
            cascaded = evicted
            for j in range(i + 1, len(queues)):
                evicted2 = queues[j].push(cascaded)
                if evicted2 is None:
                    log.info(f"  ⤵ ({cascaded.score}分) [{cascaded.username}] {q_names[i]}→{q_names[j]}")
                    return q_names[i]
                if evicted2 is cascaded:
                    continue            # 也打不过这一级，继续下放
                log.info(f"  ⤵ ({cascaded.score}分) [{cascaded.username}] {q_names[i]}→{q_names[j]}")
                cascaded = evicted2     # 又挤掉了一个，继续级联
            # 三级都塞不下被挤出的条目
            log.info(f"  ⤵ ({cascaded.score}分) [{cascaded.username}] {q_names[i]}→🗑")
            return q_names[i]

        return "丢弃"

    # ===== 身体晃动（P2 参数注入，叠加在 P4 表情上）=====

    def _start_body_sway(self, duration: float, emotion_key: str = "default_smile"):
        """播放期间注入随机身体微动参数，新播放自动取消旧的"""
        if self._anim_task and not self._anim_task.done():
            self._anim_task.cancel()
        if not self.vts:
            return
        sw = VTS_SWAY.get(emotion_key, VTS_SWAY["default_smile"])
        async def _loop():
            try:
                vals = {"x": 0.0, "y": 0.0, "z": 0.0}
                targets = {"x": 0.0, "y": 0.0, "z": 0.0}
                start = time.time()
                while time.time() - start < duration - 0.1:
                    await asyncio.sleep(0.06)
                    for axis, amp in [("x", sw["amp_x"]), ("y", sw["amp_y"]), ("z", sw["amp_z"])]:
                        if abs(vals[axis] - targets[axis]) < amp * 0.2 or random.random() < 0.08:
                            targets[axis] = random.uniform(-amp, amp)
                        if random.random() < 0.03:
                            vals[axis] += random.uniform(-amp * 0.6, amp * 0.6)
                        vals[axis] += (targets[axis] - vals[axis]) * 0.35
                    await self.vts.inject_parameters([
                        {"id": "FaceAngleX", "value": vals["x"], "weight": 0.35},
                        {"id": "FaceAngleY", "value": vals["y"], "weight": 0.35},
                        {"id": "FaceAngleZ", "value": vals["z"], "weight": 0.35},
                    ], mode="set")
            except asyncio.CancelledError:
                pass
        self._anim_task = asyncio.create_task(_loop())

    def _start_idle_anims(self):
        """闲置时：微晃动 + 定时 eat/sleep"""
        if self._idle_task and not self._idle_task.done():
            return  # 已经在跑了
        if not self.vts:
            return
        self._idle_since = time.time()
        async def _loop():
            try:
                eat_last = time.time()
                while True:
                    await asyncio.sleep(0.2)
                    t = time.time() - self._idle_since
                    # 慢速正弦波叠加（像发呆/呼吸）
                    x = math.sin(t * 0.4) * 8 + math.sin(t * 0.7 + 1.2) * 3
                    y = math.sin(t * 0.5 + 0.8) * 6 + math.sin(t * 1.1) * 2
                    z = math.sin(t * 0.6 + 2.0) * 5 + math.sin(t * 0.3) * 4
                    await self.vts.inject_parameters([
                        {"id": "FaceAngleX", "value": x, "weight": 0.6},
                        {"id": "FaceAngleY", "value": y, "weight": 0.6},
                        {"id": "FaceAngleZ", "value": z, "weight": 0.6},
                    ], mode="set")
                    # 每 N 秒 eat
                    if time.time() - eat_last >= VTS_IDLE_EAT_INTERVAL:
                        eat_last = time.time()
                        await self.vts.trigger_hotkey(VTS_IDLE_EAT_ID)
                    # 60s sleep
                    if t >= VTS_IDLE_SLEEP_AFTER:
                        await self.vts.trigger_hotkey(VTS_IDLE_SLEEP_ID)
                        break
            except asyncio.CancelledError:
                pass
        self._idle_task = asyncio.create_task(_loop())

    def _stop_idle_anims(self):
        """打断闲置动画（新弹幕来了）"""
        self._idle_since = None
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            self._idle_task = None

    def _update_indicator(self):
        """更新前三名弹幕展示"""
        all_items = []
        # 优先取 committed
        if self._committed_item:
            all_items.append({"username": self._committed_item.username,
                              "danmaku": self._committed_item.text,
                              "score": self._committed_item.score,
                              "type": self._committed_item.item_type})
        # 从各队列取最高分
        for q in [self.q1_d, self.q1_g, self.q2_d, self.q2_g, self.q3_d, self.q3_g]:
            for item in q.items:
                all_items.append({"username": item.username,
                                  "danmaku": item.text,
                                  "score": item.score,
                                  "type": item.item_type})
        all_items.sort(key=lambda x: x["score"], reverse=True)
        update_top3(all_items)

    def _status(self) -> dict:
        """返回调度器状态快照（调试/模拟用）"""
        return {
            "q1_d": len(self.q1_d),
            "q2_d": len(self.q2_d),
            "q3_d": len(self.q3_d),
            "q1_g": len(self.q1_g),
            "q2_g": len(self.q2_g),
            "q3_g": len(self.q3_g),
            "next_ready": self.next_ready is not None,
            "playing": self.playing_until > 0,
            "preparing": self.preparing is not None,
        }

    # ===== 调度逻辑 =====

    def _pick_next(self) -> QueueItem | None:
        """committed 绝对优先 → 升舱(Q2/Q3弹幕>门槛填Q1) → Q1 对决 → Q2 节奏 → Q3 合并"""
        # ── committed 绝对优先 ──
        if self._committed_item is not None:
            item = self._committed_item
            self._committed_item = None
            return item

        now = time.time()
        threshold = self._threshold_val()

        # ── 弹幕升舱：Q1-D 有空位就填，Q2/Q3 弹幕分>门槛提拔 ──
        while len(self.q1_d) < Q1_CAP:
            best = None
            best_src = None
            for q in [self.q2_d, self.q3_d]:
                c = q.best()
                if c and c.score > threshold:
                    if best is None or c.score > best.score:
                        best = c
                        best_src = q
            if best is None:
                break
            best_src.pop_best()
            self.q1_d.push(best)
            log.info(f"⬆ 升舱 ({best.score}分 > 门槛{threshold:.0f}) [{best.username}] → Q1-D")

        # ── 礼物升舱：Q1-G 有空位就填 ──
        gift_threshold = self._gift_threshold_val()
        while len(self.q1_g) < Q1_CAP:
            best = None
            best_src = None
            for q in [self.q2_g, self.q3_g]:
                c = q.best()
                if c:
                    # 硬升舱 >=85 | 软升舱 >门槛
                    if c.score >= 85 or c.score > gift_threshold:
                        if best is None or c.score > best.score:
                            best = c
                            best_src = q
            if best is None:
                break
            best_src.pop_best()
            self.q1_g.push(best)
            log.info(f"🎁 升舱 ({best.score}分 > 门槛{gift_threshold:.0f}) [{best.username}] → Q1-G")

        # ── Q1 ──
        best_d = self.q1_d.best()
        best_g = self.q1_g.best()
        if best_d or best_g:
            self.q1_empty_since = None
            self.q2_empty_since = None
            if best_d and best_g:
                return self.q1_d.pop_best() if best_d.score >= best_g.score else self.q1_g.pop_best()
            return self.q1_d.pop_best() if best_d else self.q1_g.pop_best()

        # ── Q1 空，等 8s → Q2（低于门槛的弹幕 + 礼物）──
        if self.q1_empty_since is None:
            self.q1_empty_since = now
        if now - self.q1_empty_since < Q1_TO_Q2_WAIT:
            return None

        # ── Q2 ──
        best_d = self.q2_d.best()
        best_g = self.q2_g.best()
        if best_d or best_g:
            self.q2_empty_since = None
            if best_d and best_g:
                return self.q2_g.pop_best() if best_g.score >= best_d.score - 10 else self.q2_d.pop_best()
            return self.q2_d.pop_best() if best_d else self.q2_g.pop_best()

        # ── Q2 空，等 20s → Q3 ──
        if self.q2_empty_since is None:
            self.q2_empty_since = now
        if now - self.q2_empty_since < Q2_TO_Q3_WAIT:
            return None

        # ── Q3 ──
        best_d = self.q3_d.best()
        best_g = self.q3_g.best()
        if best_d or best_g:
            if best_d and best_g:
                return self.q3_g.pop_best() if best_g.score >= best_d.score else self.q3_d.pop_best()
            return self.q3_d.pop_best() if best_d else self.q3_g.pop_best()

        return None

    async def _prepare_next(self, item: QueueItem) -> tuple | None:
        """后台任务：AI生成 + 并行{TTS合成, 情绪分类} → 返回 (audio, reply_text, emotion_key)"""
        loop = asyncio.get_running_loop()
        try:
            log.debug(f"后台准备: ({item.score}分) [{item.username}] {item.text[:20]}")
            self._update_indicator()

            # ── 进房欢迎：走模板，不走 AI，省钱 ──
            if item.item_type == "entry":
                reply = random_welcome(item.username)
                emotion = "default_smile"
                audio = None
                if self.tts:
                    audio = await loop.run_in_executor(self._pool, self.tts.synthesize, reply)
                log.debug(f"后台就绪: ({item.score}分) 欢迎 \"{reply}\" → {emotion}")
                return (audio, reply, emotion)

            # ── 画面评论：VLM描述 → DS生成主播评论，跳过则返回 None ──
            if item.item_type == "vision":
                reply = await self._vision_comment(item.text)
                if reply is None:
                    log.debug("Vision: DS 判定跳过")
                    return None
                # _vision_comment 返回的 text 可能带情绪标签，解析一下
                reply, emotion = self._parse_emotion_tag(reply)
                audio = None
                if self.tts:
                    audio = await loop.run_in_executor(self._pool, self.tts.synthesize, reply)
                log.debug(f"后台就绪: ({item.score}分) 画面 \"{reply[:25]}...\" → {emotion}")
                return (audio, reply, emotion)

            reply, emotion = await self.chatbot.reply(item.username, item.text)
            if reply is None:
                return None

            if self.tts:
                audio = await loop.run_in_executor(self._pool, self.tts.synthesize, reply)
            else:
                audio = None

            log.debug(f"后台就绪: ({item.score}分) \"{reply[:25]}...\" → {emotion}")
            return (audio, reply, emotion)
        except Exception as e:
            log.error(f"后台准备失败: {e}")
            return None

    # ===== 主循环 =====

    async def run(self):
        """主调度循环 — 流水线版 + 占坑"""
        log.info("流水线启动（v2 占坑修复）")
        loop = asyncio.get_running_loop()

        while True:
            now = time.time()

            # ── 1. 播放结束标记 ──
            if self.playing_until > 0 and now >= self.playing_until:
                self.playing_until = 0
                clear_subtitle()
                continue  # 下轮统一处理

            # ── 2. 空闲：消费 next_ready，或启动后台准备 ──
            if self.playing_until == 0 and self.preparing is None:
                if self.next_ready:
                    # 有准备好的 → 切表情 + 播放
                    gap = random.uniform(REPLY_GAP_MIN, REPLY_GAP_MAX)
                    await asyncio.sleep(gap)
                    audio, reply, emotion = self.next_ready
                    self.next_ready = None
                    self._stop_idle_anims()
                    if self.vts:
                        await self.vts.switch_expression(emotion)
                    log.info(f"▶ 播放: \"{reply[:25]}...\" ({emotion})")
                    show_subtitle(reply)
                    if self.tts and audio is not None:
                        duration = self.tts.play(audio)
                    else:
                        duration = max(2, len(reply) / 5)
                    self._start_body_sway(duration, emotion)
                    self.playing_until = time.time() + duration
                    continue

                # 有人已占坑但还在评分中 → 等它
                if self._committed_reserved:
                    await asyncio.sleep(0.1)
                    continue

                # 没有就绪的 → 从队列取 → 内联生成+播放
                item = self._pick_next()
                if item is None:
                    clear_all()
                    self._start_idle_anims()
                    await asyncio.sleep(0.5)
                    continue
                self._stop_idle_anims()

                log.info(f"▶ ({item.score}分) [{item.username}] {item.text[:30]}")
                self._update_indicator()

                # ── 进房欢迎：走模板，不走 AI ──
                if item.item_type == "entry":
                    reply = random_welcome(item.username)
                    emotion = "default_smile"
                    audio = None
                    if self.tts:
                        audio = await loop.run_in_executor(self._pool, self.tts.synthesize, reply)
                # ── 画面评论：VLM描述 → DS生成主播评论 ──
                elif item.item_type == "vision":
                    reply = await self._vision_comment(item.text)
                    if reply is None:
                        continue
                    reply, emotion = self._parse_emotion_tag(reply)
                    audio = None
                else:
                    result = await self.chatbot.reply(item.username, item.text)
                    if result is None:
                        continue
                    reply, emotion = result
                    audio = None

                if audio is None and self.tts:
                    audio = await loop.run_in_executor(self._pool, self.tts.synthesize, reply)

                if self.vts:
                    await self.vts.switch_expression(emotion)
                show_subtitle(reply)
                if self.tts and audio is not None:
                    duration = self.tts.play(audio)
                else:
                    duration = max(2, len(reply) / 5)
                self._start_body_sway(duration, emotion)

                self.playing_until = time.time() + duration
                continue

            # ── 3. 播放中：后台准备下一条 ──
            if self.playing_until > 0 and self.next_ready is None and self.preparing is None:
                candidate = self._pick_next()
                if candidate:
                    self.preparing = asyncio.create_task(self._prepare_next(candidate))

            # ── 4. 检查后台准备是否完成 ──
            if self.preparing and self.preparing.done():
                result = self.preparing.result()
                self.preparing = None
                if result is not None:
                    self.next_ready = result

            await asyncio.sleep(0.1)
