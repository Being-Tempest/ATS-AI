"""
AI VTuber 框架 — 公共配置模板

所有密钥 / 个性化信息通过环境变量或 .env 文件注入,仓库内不含任何真实密钥。
用法:
  1. 复制 .env.example 为 .env,填入你自己的密钥
  2. 或直接在系统环境变量里设置
"""

import os
from pathlib import Path


# ── 零依赖 .env 加载器 ──
def _load_dotenv() -> None:
    p = Path(__file__).with_name(".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ===== DeepSeek API(对话)=====
DEEPSEEK_API_KEY = _get("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = _get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = _get("DEEPSEEK_MODEL", "deepseek-v4-flash")

# ===== 火山引擎 Vision API(画面评论)=====
VOLCANO_API_KEY = _get("VOLCANO_API_KEY")
VOLCANO_VISION_MODEL = _get("VOLCANO_VISION_MODEL", "doubao-seed-2-0-mini-260428")
VOLCANO_BASE_URL = _get("VOLCANO_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")

# ===== B站直播间 =====
BILIBILI_ROOM_ID = int(_get("BILIBILI_ROOM_ID", "0") or 0)

# ===== CosyVoice 3.5 API(阿里云百炼 TTS)=====
DASHSCOPE_API_KEY = _get("DASHSCOPE_API_KEY")
COSYVOICE_VOICE_ID = _get("COSYVOICE_VOICE_ID")
COSYVOICE_MODEL = _get("COSYVOICE_MODEL", "cosyvoice-v3.5-flash")

# ===== 豆包端到端实时语音(可选)=====
SPEECH_LLM_API_KEY = _get("SPEECH_LLM_API_KEY")

# ===== TTS 设置 =====
TTS_VOLUME = float(_get("TTS_VOLUME", "0.6"))

# ===== 对话设置 =====
MAX_HISTORY = int(_get("MAX_HISTORY", "10"))
MAX_REPLY_LENGTH = int(_get("MAX_REPLY_LENGTH", "150"))

# ===== 过滤设置 =====
DANMAKU_MIN_LENGTH = int(_get("DANMAKU_MIN_LENGTH", "1"))
BLOCKED_KEYWORDS = []  # 屏蔽关键词,按需在代码中扩展

# ===== VTube Studio 表情映射(需在你的 Live2D 模型中对应配置)=====
VTS_EXPRESSIONS = {
    "default_smile": "default_smile.exp3.json",
    "happy":         "happy.exp3.json",
    "shy":           "shy.exp3.json",
    "touched":       "touched.exp3.json",
    "surprised":     "surprised.exp3.json",
    "thinking":      "thinking.exp3.json",
    "tsundere":      "tsundere.exp3.json",
    "angry":         "angry.exp3.json",
}

# ===== VTS 身体晃动参数(P2 追踪参数注入,叠加在表情上)=====
VTS_SWAY = {
    "happy":         {"amp_x": 5, "amp_y": 4, "amp_z": 3},
    "shy":           {"amp_x": 2, "amp_y": 2, "amp_z": 1},
    "touched":       {"amp_x": 3, "amp_y": 3, "amp_z": 2},
    "surprised":     {"amp_x": 8, "amp_y": 6, "amp_z": 5},
    "thinking":      {"amp_x": 3, "amp_y": 4, "amp_z": 5},
    "tsundere":      {"amp_x": 4, "amp_y": 3, "amp_z": 4},
    "default_smile": {"amp_x": 4, "amp_y": 3, "amp_z": 3},
}

# ===== VTS 闲置动画 hotkey IDs(在你的 VTS 里自行配置)=====
VTS_IDLE_EAT_ID = _get("VTS_IDLE_EAT_ID")
VTS_IDLE_SLEEP_ID = _get("VTS_IDLE_SLEEP_ID")
VTS_IDLE_EAT_INTERVAL = int(_get("VTS_IDLE_EAT_INTERVAL", "15"))
VTS_IDLE_SLEEP_AFTER = int(_get("VTS_IDLE_SLEEP_AFTER", "60"))
