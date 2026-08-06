"""
B站 WBI 签名 — 2025年起 getDanmuInfo 等接口强制要求 w_rid + wts
"""
import hashlib
import time
import aiohttp
from functools import reduce
from logger import get_logger

log = get_logger("wbi")

# 固定置换表（来自 B站前端 crypto）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52
]

# 缓存（全局）
_img_key = ""
_sub_key = ""
_cached_at = 0.0


def _get_mixin_key(raw: str) -> str:
    return reduce(lambda s, i: s + raw[i], MIXIN_KEY_ENC_TAB, '')[:32]


async def _refresh_keys(session: aiohttp.ClientSession):
    """从 nav 接口获取 img_key / sub_key"""
    global _img_key, _sub_key, _cached_at
    try:
        async with session.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={"User-Agent": "Mozilla/5.0 Chrome/131.0.0.0"},
            ssl=True,
        ) as resp:
            data = await resp.json()
            wbi = data.get("data", {}).get("wbi_img", {})
            _img_key = wbi.get("img_url", "").split("/")[-1].split(".")[0]
            _sub_key = wbi.get("sub_url", "").split("/")[-1].split(".")[0]
            _cached_at = time.time()
            log.info(f"🔑 WBI 密钥已刷新")
    except Exception as e:
        log.warning(f"WBI 密钥获取失败: {e}")


def sign(params: dict) -> dict:
    """原地添加 w_rid / wts，返回同一 dict"""
    if not _img_key or not _sub_key:
        return params
    mixin = _get_mixin_key(_img_key + _sub_key)
    params["wts"] = int(time.time())
    ordered = sorted(params.items(), key=lambda x: x[0])
    query = "&".join(f"{k}={v}" for k, v in ordered)
    params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return params
