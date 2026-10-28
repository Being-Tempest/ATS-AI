import asyncio
import json
import unittest

from ai_chat import ChatBot
from scheduler import Scheduler


class UnifiedChatTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, payload):
        bot = ChatBot.__new__(ChatBot)
        bot.history = [{"role": "system", "content": "test"}]
        bot._lock = asyncio.Lock()

        async def api_call(messages, json_mode=False):
            assert json_mode
            return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

        bot._api_call = api_call
        return bot

    async def test_single_call_returns_draft_without_history_mutation(self):
        bot = self.make_bot({"score": 72, "reply": "宝贝来啦", "emotion": "happy"})
        result = await bot.score_and_reply("甲", "晚上好")
        self.assertEqual(result, (72, "宝贝来啦", "happy"))
        self.assertEqual(len(bot.history), 1)

    async def test_only_played_exchange_enters_history(self):
        bot = self.make_bot({"score": 40, "reply": "收到", "emotion": "default_smile"})
        await bot.score_and_reply("甲", "第一条")
        await bot.score_and_reply("乙", "第二条")
        self.assertEqual(len(bot.history), 1)
        bot.commit_exchange("乙", "第二条", "收到")
        self.assertEqual(bot.history[-2]["content"], "乙：第二条")
        self.assertEqual(bot.history[-1]["content"], "收到")

    async def test_invalid_json_returns_none(self):
        bot = self.make_bot("not json")
        self.assertIsNone(await bot.score_and_reply("甲", "测试"))

    async def test_scheduler_consumes_draft_without_second_generation(self):
        class DummyBot:
            def __init__(self):
                self.committed = []
                self.reply_calls = 0

            async def score_and_reply(self, username, text):
                return 72, "缓存回复", "happy"

            def commit_exchange(self, username, text, reply):
                self.committed.append((username, text, reply))

            async def reply(self, username, text):
                self.reply_calls += 1
                raise AssertionError("草稿存在时不应再次生成")

        bot = DummyBot()
        scheduler = Scheduler(chatbot=bot, unified_llm=True, disable_commit=True)
        try:
            item = await scheduler._score_with_draft("甲", "测试", None)
            result = await scheduler._generate_reply(item)
            self.assertEqual(result, ("缓存回复", "happy"))
            self.assertEqual(bot.reply_calls, 0)
            self.assertEqual(bot.committed, [("甲", "测试", "缓存回复")])
        finally:
            scheduler._pool.shutdown(wait=False)

    async def test_experiment_injection_keeps_legacy_path(self):
        async def score(text):
            return 55

        async def reply(username, text):
            return "实验回复", "thinking"

        scheduler = Scheduler(score_fn=score, reply_fn=reply)
        try:
            self.assertIsNone(scheduler._score_reply_fn)
            item = await scheduler._score_with_draft("甲", "测试", None)
            self.assertIsNone(item.reply)
            self.assertEqual(await scheduler._generate_reply(item), ("实验回复", "thinking"))
        finally:
            scheduler._pool.shutdown(wait=False)


if __name__ == "__main__":
    unittest.main()
