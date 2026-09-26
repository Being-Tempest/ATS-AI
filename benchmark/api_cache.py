"""
DeepSeek API 调用缓存 — 同一消息同一函数只调一次真实 API

存储：benchmark/cache/<fn_name>.jsonl，append-only，启动时全量加载到内存。
key = sha1(消息内容)。所有策略共享同一缓存目录。
"""

import hashlib
import json
import os
import asyncio

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")


class ApiCache:
    def __init__(self, fn_name: str, real_fn, enabled: bool = True):
        """
        real_fn: async callable(text) -> result（JSON 可序列化）
        enabled=False 时只读缓存不调用真实 API（缓存缺失返回 None）
        """
        self.fn_name = fn_name
        self.real_fn = real_fn
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        self._sem = asyncio.Semaphore(8)  # 并发上限，护 API 也护本地
        os.makedirs(CACHE_DIR, exist_ok=True)
        self._path = os.path.join(CACHE_DIR, f"{fn_name}.jsonl")
        self._mem: dict[str, object] = {}
        if os.path.exists(self._path):
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rec = json.loads(line)
                        self._mem[rec["key"]] = rec["result"]

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    async def __call__(self, text: str):
        key = self._key(text)
        if key in self._mem:
            self.hits += 1
            return self._mem[key]
        if not self.enabled:
            return None
        async with self._sem:
            if key in self._mem:  # 并发下双重检查
                self.hits += 1
                return self._mem[key]
            result = await self.real_fn(text)
            self.misses += 1
            self._mem[key] = result
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"key": key, "text": text, "result": result},
                                   ensure_ascii=False) + "\n")
            return result

    def stats(self) -> str:
        return f"{self.fn_name}: 缓存命中 {self.hits}, 真实调用 {self.misses}"
