"""
评分模块 — DS Judge 弹幕打分 + 礼物本地赋分
"""

from openai import AsyncOpenAI
from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL
from logger import get_logger

log = get_logger("scorer")

_JUDGE_PROMPT = """你是B站弹幕评分助手。给以下弹幕打分(0-80整数)，判断这条弹幕的对话价值和回复优先级。

评分标准：
- 60-80: 高价值（深度话题、真挚情感、能引出有趣回答、和主播强相关）
- 40-59: 中等（普通聊天、简单互动、一般问候、接梗）
- 20-39: 较低（敷衍、简单附和、表意不清）
- 0-19: 很低（纯表情、纯数字、无意义刷屏、人身攻击）

你只回复一个0-80的整数，不要任何解释。"""

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
        )
    return _client


async def score_danmaku(text: str) -> int:
    """DS Judge 为弹幕打分，返回 0-80"""
    if not text.strip():
        return 0

    client = _get_client()
    try:
        response = await client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[
                {"role": "system", "content": _JUDGE_PROMPT},
                {"role": "user", "content": f"弹幕内容：{text}"},
            ],
            max_tokens=200,   # 打分是简单任务，不思考：更快+更省
            extra_body={"thinking": {"type": "disabled"}},
        )
        result = response.choices[0].message.content or ""
        # v4 可能把 token 耗在推理上，content 为空时用 reasoning_content
        if not result and hasattr(response.choices[0].message, 'reasoning_content'):
            rc = response.choices[0].message.reasoning_content or ""
            result = rc.strip()
        # 提取第一个数字
        import re
        match = re.search(r'\d+', result.strip())
        if match:
            score = int(match.group())
            return max(0, min(80, score))
        return 40  # 解析失败给中位数
    except Exception as e:
        log.error(f"DS打分失败: {e}")
        return 40  # API 挂了给中位数


def score_gift(gift_name: str, price: int) -> int:
    """
    礼物本地赋分，返回 70-100。
    price: 电池数（blivedm GiftMessage.price 字段，10电池=¥1）
    """
    # 舰长/提督/总督 → 永远 100
    for kw in ["舰长", "提督", "总督"]:
        if kw in gift_name:
            return 100

    # SC 醒目留言 → 按金额 90-99
    if "醒目留言" in gift_name or gift_name.startswith("SC"):
        rmb = price / 10.0
        if rmb >= 1000:  return 99
        elif rmb >= 300: return 96
        elif rmb >= 100: return 93
        else:            return 90

    # 按价格区间映射
    rmb = price / 10.0

    if rmb >= 500:
        return min(99, 95 + int((rmb - 500) / 200))
    elif rmb >= 100:
        return min(94, 85 + int((rmb - 100) / 100))
    elif rmb >= 50:
        return min(84, 75 + int((rmb - 50) / 50))
    elif rmb >= 10:
        return min(79, 70 + int((rmb - 10) / 10))
    else:
        return 70   # 最便宜的礼物也是 70
