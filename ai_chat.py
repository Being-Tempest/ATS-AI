"""
AI主播 AI 对话模块 — DeepSeek API（带重试）
"""

import asyncio
import re
from openai import AsyncOpenAI
from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, MAX_HISTORY, MAX_REPLY_LENGTH
from persona import SYSTEM_PROMPT
from logger import get_logger

log = get_logger("ai_chat")

MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # 首次重试等 2s, 然后 4s, 8s

# 括号内容正则：中文括号（）英文括号 () 以及里面的内容
_PAREN_RE = re.compile(r'[（(][^）)]*[）)]')

# 情绪标签正则：匹配 [开心] [害羞] 等，只匹配开头的标签
_TAG_RE = re.compile(r'^\[(开心|害羞|感动|认真|惊讶|不悦|普通)\]')

# 中文情绪标签 → VTS expression key
_TAG_TO_VTS = {
    "开心": "happy",
    "害羞": "shy",
    "感动": "touched",
    "认真": "thinking",
    "惊讶": "surprised",
    "不悦": "tsundere",
    "普通": "default_smile",
}


class ChatBot:
    """AI主播 AI 对话引擎"""

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
            max_retries=0,  # 我们自己控制重试
        )
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._lock = asyncio.Lock()

    async def _api_call(self, messages: list) -> str:
        """API 调用 + 重试，返回 reply_text。失败抛异常。"""
        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await self.client.chat.completions.create(
                    model=DEEPSEEK_MODEL,
                    messages=messages,
                    max_tokens=1024,
                    temperature=0.9,
                    extra_body={"thinking": {"type": "disabled"}},  # 简单任务不思考：省延迟+省token+杜绝思维链泄漏
                )
                msg = response.choices[0].message
                reply_text = (msg.content or "").strip()
                if not reply_text and hasattr(msg, 'reasoning_content') and msg.reasoning_content:
                    reasoning = msg.reasoning_content.strip()
                    reply_text = reasoning.split('\n')[-1].strip()
                    log.debug("fallback: 使用 reasoning_content")
                return reply_text
            except Exception as e:
                last_err = e
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    log.warning(f"API 第{attempt+1}次失败, {delay}s后重试: {e}")
                    await asyncio.sleep(delay)
        raise last_err  # type: ignore

    def _parse_emotion(self, text: str) -> tuple[str, str]:
        """从回复开头解析情绪标签，返回 (clean_text, vts_emotion_key)"""
        m = _TAG_RE.match(text)
        if m:
            tag = m.group(1)
            text = text[m.end():].strip()
            return text, _TAG_TO_VTS.get(tag, "default_smile")
        return text, "default_smile"

    def _clean_reply(self, text: str) -> str:
        """清洗 DS 输出：去括号动作描写、去首尾空白、合并多余空格"""
        text = _PAREN_RE.sub('', text)        # 去掉（笑）（挥手）等
        text = re.sub(r'\s+', ' ', text)      # 合并多余空白
        text = text.strip()
        return text

    async def reply(self, username: str, message: str) -> tuple[str, str] | None:
        """根据一条弹幕生成回复，返回 (reply_text, vts_emotion_key) 或 None"""
        user_msg = f"{username}：{message}"
        self.history.append({"role": "user", "content": user_msg})

        if len(self.history) > MAX_HISTORY + 1:
            self.history = [self.history[0]] + self.history[-(MAX_HISTORY):]

        try:
            async with self._lock:
                reply_text = await self._api_call(self.history)

            if not reply_text:
                return None

            # 先扒情绪标签（洗括号前），再洗括号
            reply_text, emotion = self._parse_emotion(reply_text)
            reply_text = self._clean_reply(reply_text)
            if not reply_text:
                return None

            # 智能截断：超过配长才截
            if len(reply_text) > MAX_REPLY_LENGTH:
                for cut in range(MAX_REPLY_LENGTH, MAX_REPLY_LENGTH - 15, -1):
                    if reply_text[cut - 1] in "。！？~…！？.!?~":
                        reply_text = reply_text[:cut]
                        break
                else:
                    reply_text = reply_text[:MAX_REPLY_LENGTH]
                log.debug(f"截断: ({len(reply_text)}字) {reply_text}")

            # 注意：存入历史的是洗过的版本（不带情绪标签）
            self.history.append({"role": "assistant", "content": reply_text})
            return reply_text, emotion

        except Exception as e:
            log.error(f"AI 回复失败(已重试{MAX_RETRIES}次): {e}")
            return None

    def clear_history(self):
        """清空对话历史（下播时调用）"""
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]
