import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("mss", SimpleNamespace(MSS=object))

from scheduler import Scheduler
from vision_module import VisionModule


class DummyCompletions:
    def __init__(self, payload):
        self.payload = payload
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        message = SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class VisionTests(unittest.TestCase):
    def make_vision(self, payload):
        with patch("vision_module.OpenAI", return_value=SimpleNamespace()):
            vision = VisionModule()
        completions = DummyCompletions(payload)
        vision._client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions))
        return vision, completions

    def test_skip_has_no_reply(self):
        vision, _ = self.make_vision({"should_comment": False, "reply": "", "emotion": "default_smile"})
        self.assertIsNone(vision._call_vlm(b"jpeg"))

    def test_single_multimodal_request_returns_comment(self):
        vision, completions = self.make_vision(
            {"should_comment": True, "reply": "（笑）这波打得漂亮", "emotion": "happy"})
        self.assertEqual(vision._call_vlm(b"jpeg"), ("这波打得漂亮", "happy"))
        request = completions.kwargs
        self.assertEqual(request["response_format"], {"type": "json_object"})
        image = request["messages"][1]["content"][1]["image_url"]
        self.assertTrue(image["url"].startswith("data:image/jpeg;base64,"))

    def test_scheduler_does_not_regenerate_prepared_vision_comment(self):
        scheduler = Scheduler(unified_llm=False)
        try:
            scheduler.enqueue_vision("画面", reply="已经生成", emotion="shy")
            item = scheduler.q1_d.pop_best()
            import asyncio
            item, audio, reply, emotion = asyncio.run(scheduler._prepare_next(item))
            self.assertEqual((reply, emotion), ("已经生成", "shy"))
            self.assertIsNone(audio)
        finally:
            scheduler._pool.shutdown(wait=False)


if __name__ == "__main__":
    unittest.main()
