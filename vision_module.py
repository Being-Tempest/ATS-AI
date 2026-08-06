"""
Vision Module — 画面评论
mss截图 → 豆包VLM描述 → 回调 → DS判断+生成 → 最高优先级入队
"""
import time
import base64
import threading
import requests
import mss
from PIL import Image
from io import BytesIO
from config import VOLCANO_API_KEY, VOLCANO_VISION_MODEL, VOLCANO_BASE_URL
from logger import get_logger

log = get_logger("vision")

INTERVAL_NORMAL = 60   # 不开麦：1分钟
INTERVAL_MIC = 180     # 开麦中：3分钟

VISION_PROMPT = """用一句话（不超过30个字）描述画面中主要的内容。
规则：
- 忽略任务栏、时间、桌面图标
- 只描述直播相关的内容（游戏画面、主播动作、弹幕等）
- 如果画面没变化，回复"无变化"
- 不要带时间戳"""


class VisionModule:
    """画面截图 → VLM描述 → 回调生成评论"""

    def __init__(self):
        self._running = False
        self._thread: threading.Thread | None = None
        self._mic_open = False
        self._on_description = None  # callback(description_text)
        self._last_capture = 0.0     # 上次截图时间戳，防重入

    def set_on_description(self, callback):
        """设置回调：拿到VLM描述后调用 callback(description)"""
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
        # 1. 截图
        png_data = self._screenshot()
        if not png_data:
            return

        # 2. VLM 描述
        description = self._call_vlm(png_data)
        if not description:
            return
        log.info(f"🖼 VLM: {description[:80]}")

        # 3. 回调给外部（dashboard 负责 DS 判断 + 入队）
        if self._on_description:
            try:
                self._on_description(description)
            except Exception as e:
                log.error(f"on_description 回调异常: {e}")

    def _screenshot(self) -> bytes | None:
        """mss 截主屏 → 缩放到 720p → PNG bytes（省 token）"""
        try:
            with mss.MSS() as sct:
                monitor = sct.monitors[1]
                img = sct.grab(monitor)
                pil_img = Image.frombytes("RGB", img.size, img.rgb)
                # 缩放到高度 720，保持比例，减少 VLM token 消耗
                h = pil_img.height
                if h > 720:
                    ratio = 720 / h
                    new_w = int(pil_img.width * ratio)
                    pil_img = pil_img.resize((new_w, 720), Image.LANCZOS)
                buf = BytesIO()
                pil_img.save(buf, format="JPEG", quality=80)
                return buf.getvalue()
        except Exception as e:
            log.error(f"截图失败: {e}")
            return None

    def _call_vlm(self, png_data: bytes) -> str | None:
        """调用豆包 Responses API 描述画面"""
        b64 = base64.b64encode(png_data).decode()
        data_uri = f"data:image/png;base64,{b64}"

        headers = {
            "Authorization": f"Bearer {VOLCANO_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": VOLCANO_VISION_MODEL,
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": data_uri},
                    {"type": "input_text", "text": VISION_PROMPT},
                ]
            }],
            "thinking": {"type": "disabled"},
        }

        try:
            resp = requests.post(
                f"{VOLCANO_BASE_URL}/responses",
                headers=headers,
                json=payload,
                timeout=30,
            )
            if resp.status_code != 200:
                log.error(f"VLM API {resp.status_code}: {resp.text[:200]}")
                return None

            result = resp.json()
            # 解析 Responses API 返回
            for item in result.get("output", []):
                if item.get("type") == "message":
                    for c in item.get("content", []):
                        if c.get("type") == "output_text":
                            return c.get("text", "").strip()

            log.warning(f"VLM 返回格式未知: {str(result)[:200]}")
            return None
        except requests.exceptions.Timeout:
            log.error("VLM API 超时")
            return None
        except Exception as e:
            log.error(f"VLM API 异常: {e}")
            return None


# 全局单例
_vision: VisionModule | None = None


def get_vision() -> VisionModule:
    global _vision
    if _vision is None:
        _vision = VisionModule()
    return _vision
