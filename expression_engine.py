"""
DS 情绪标签分类
用 AI 回复文本判断该用什么表情，和 TTS 合成并行，不增加延迟。
"""

from openai import AsyncOpenAI
from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL
from logger import get_logger

log = get_logger("expression")

EMOTIONS = ["开心", "害羞", "感动", "认真", "惊讶", "不悦", "普通"]

# 情绪 → VTS_EXPRESSIONS key
EMOTION_TO_EXPRESSION = {
    "开心":   "happy",
    "害羞":   "shy",
    "感动":   "touched",
    "认真":   "thinking",
    "惊讶":   "surprised",
    "不悦":   "tsundere",
    "普通":   "default_smile",
}

_CLASSIFY_PROMPT = """你是情绪分类助手。给定主播要说的一句话，判断主播说这句话时应该用什么表情。

只回复一个词，从以下选择：开心 害羞 感动 认真 惊讶 不悦 普通

判断依据：
- 开心：活泼高兴、在笑、说有趣的事
- 害羞：被夸了、不好意思、小羞涩
- 感动：被感动到、温暖、真心感谢
- 认真：思考问题、回答正经话题、解释事情
- 惊讶：震惊、意外、没想到
- 不悦：嘴上不承认但其实开心、小傲娇、吐槽
- 普通：平常聊天、一般寒暄回复"""

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
        )
    return _client


async def classify(text: str) -> str:
    """判断回复文本的情绪 → 返回 VTS expression key (如 'happy')"""
    if not text.strip():
        return "default_smile"

    client = _get_client()
    try:
        response = await client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[
                {"role": "system", "content": _CLASSIFY_PROMPT},
                {"role": "user",   "content": f"主播要说的话：{text}"},
            ],
            max_tokens=200,
        )
        result = response.choices[0].message.content or ""
        result = result.strip()
        # v4 可能 content 为空，fallback 到 reasoning_content
        if not result and hasattr(response.choices[0].message, 'reasoning_content'):
            rc = response.choices[0].message.reasoning_content or ""
            result = rc.strip()

        for e in EMOTIONS:
            if e in result:
                expr_key = EMOTION_TO_EXPRESSION[e]
                log.debug(f"情绪: {e} → {expr_key}")
                return expr_key

        log.debug(f"无法识别情绪 '{result}'，用 default_smile")
        return "default_smile"

    except Exception as e:
        log.error(f"情绪分类失败: {e}")
        return "default_smile"
