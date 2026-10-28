"""屏幕截图 → DeepSeek V4.1 Flash 单次多模态评论。"""
import base64
import json
import re
import threading
import time

from openai import OpenAI
from PIL import Image
from io import BytesIO

from config import (DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL,
                    VISION_IMAGE_DETAIL)
from persona import SYSTEM_PROMPT
from logger import get_logger

log = get_logger("vision")

INTERVAL_NORMAL = 60
INTERVAL_MIC = 180
_EMOTIONS = {"default_smile", "happy", "shy", "touched", "thinking",
             "surprised", "tsundere", "angry"}
_PAREN_RE = re.compile(r'[（(][^）)]*[）)]')
VISION_PROMPT = SYSTEM_PROMPT + """

## 画面评论任务
直接观察用户提供的当前屏幕图像，忽略任务栏、时间、桌面图标。只有游戏动态、显著变化或有趣内容才评论；普通静止画面不要打扰观众。评论使用自然口语，不超过30字，不描述你看不见的内容。
只输出 JSON：{"should_comment": true, "reply": "要说的话", "emotion": "happy"}。
不值得评论时输出 {"should_comment": false, "reply": "", "emotion": "default_smile"}。
emotion 只能是 default_smile、happy、shy、touched、thinking、surprised、tsundere、angry。
"""


class VisionModule:
    """定时截图，一次请求完成视觉判断、评论和情绪选择。"""

    def __init__(self):
        self._running = False
        self._thread: threading.Thread | None = None
        self._mic_open = False
        self._on_description = None
        self._last_capture = 0.0
        self._client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL,
                              max_retries=2)

    def set_on_description(self, callback):
        """设置 callback(reply, emotion)，保留旧方法名兼容现有入口。"""
        self._on_description = callback

    def set_mic_state(self, mic_open: bool):
        self._mic_open = mic_open
        if mic_open:
            log.info(f"🎤 开麦 → 截图间隔 {INTERVAL_MIC}s")
        else:
            log.info(f"🔇 关麦 → 截图间隔 {INTERVAL_NORMAL}s")

    def start(self):
        if self._running:
            return
        self._running = True
        self._last_capture = time.time()  # 启动后等 interval 才第一次截
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info(f"🖼 Vision 已启动（间隔 {INTERVAL_NORMAL}s）")

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)  # 等旧线程退出
        self._thread = None
        log.info("🖼 Vision 已停止")

    @property
    def running(self) -> bool:
        return self._running

    # ─── 内部 ───

    def _loop(self):
        while self._running:
            interval = INTERVAL_MIC if self._mic_open else INTERVAL_NORMAL
            # 用短 sleep 轮询，既响应 mic 状态变化，又防线程残留
            elapsed = 0
            while elapsed < interval and self._running:
                time.sleep(1)
                elapsed += 1
            if not self._running:
                break
            # 安全网：距上次截图不足 interval-5 秒则跳过
            if time.time() - self._last_capture < interval - 5:
                continue
            try:
                self._capture_and_process()
            except Exception as e:
                log.error(f"Vision 异常: {e}")

    def _capture_and_process(self):
        self._last_capture = time.time()
        jpeg_data = self._screenshot()
        if not jpeg_data:
            return
        result = self._call_vlm(jpeg_data)
        if result is None:
            return
        reply, emotion = result
        log.info(f"🖼 DeepSeek 画面评论: {reply[:80]}")
        if self._on_description:
            try:
                self._on_description(reply, emotion)
            except Exception as e:
                log.error(f"画面评论回调异常: {e}")

    def _screenshot(self) -> bytes | None:
        """截取主屏并缩放到 720p，返回 JPEG bytes。"""
        try:
            import mss
            with mss.MSS() as sct:
                monitor = sct.monitors[1]
                img = sct.grab(monitor)
                pil_img = Image.frombytes("RGB", img.size, img.rgb)
                h = pil_img.height
                if h > 720:
                    ratio = 720 / h
                    pil_img = pil_img.resize((int(pil_img.width * ratio), 720), Image.LANCZOS)
                buf = BytesIO()
                pil_img.save(buf, format="JPEG", quality=80)
                return buf.getvalue()
        except Exception as e:
            log.error(f"截图失败: {e}")
            return None

    def _call_vlm(self, jpeg_data: bytes) -> tuple[str, str] | None:
        data_uri = "data:image/jpeg;base64," + base64.b64encode(jpeg_data).decode()
        try:
            response = self._client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": VISION_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text", "text": "观察这张当前屏幕图像并按规定输出。"},
                        {"type": "image_url", "image_url": {
                            "url": data_uri, "detail": VISION_IMAGE_DETAIL}},
                    ]},
                ],
                max_tokens=300,
                temperature=0.7,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
                timeout=30,
            )
            raw = response.choices[0].message.content or ""
            data = json.loads(raw)
            if not data.get("should_comment"):
                return None
            reply = re.sub(r"\s+", " ", _PAREN_RE.sub("", str(data["reply"]))).strip()
            if not reply:
                return None
            emotion = data.get("emotion", "default_smile")
            return reply[:150], emotion if emotion in _EMOTIONS else "default_smile"
        except Exception as e:
            log.error(f"DeepSeek 视觉评论失败: {e}")
            return None


# 全局单例
_vision: VisionModule | None = None


def get_vision() -> VisionModule:
    global _vision
    if _vision is None:
        _vision = VisionModule()
    return _vision
