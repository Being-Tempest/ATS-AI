"""
B站弹幕接收模块 — 基于 blivedm
接入三队列调度器
"""

import os
import json
import time
import base64
import asyncio
import aiohttp
from blivedm import BLiveClient, BaseHandler
from blivedm.models import DanmakuMessage, GiftMessage, SuperChatMessage
from config import BILIBILI_ROOM_ID, DANMAKU_MIN_LENGTH, BLOCKED_KEYWORDS
from scheduler import Scheduler
from logger import get_logger
import wbi_sign

log = get_logger("danmaku")


def _parse_interact_v2_uname(data: dict) -> str:
    """从 INTERACT_WORD_V2 的 pb 字段解析用户名（protobuf field 2）"""
    pb_b64 = data.get("pb", "")
    if not pb_b64:
        return ""
    try:
        raw = base64.b64decode(pb_b64)
    except Exception:
        return ""
    idx = 0
    while idx < len(raw):
        tag = raw[idx]; idx += 1
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:  # varint, skip
            while idx < len(raw) and raw[idx] & 0x80:
                idx += 1
            idx += 1
        elif wire_type == 2:  # length-delimited
            length = 0; shift = 0
            while True:
                b = raw[idx]; idx += 1
                length |= (b & 0x7f) << shift
                shift += 7
                if not (b & 0x80):
                    break
            if field_num == 2:
                try:
                    return raw[idx:idx + length].decode("utf-8")
                except Exception:
                    return ""
            idx += length
        else:
            break  # unknown wire type, stop
    return ""


_COOKIE_FILE = os.path.join(os.path.dirname(__file__), "bilibili_cookies.json")

# blivedm 的弹幕服务器 URL（和库内部一致）
DANMAKU_SERVER_CONF_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"


class WbiBLiveClient(BLiveClient):
    """BLiveClient + WBI 签名，绕过 B站 -352 风控"""

    async def _init_host_server(self):
        """重写：给 getDanmuInfo 加上 web_location + WBI 签名"""
        await wbi_sign._refresh_keys(self._session)

        params = {
            "id": self._room_id,
            "type": 0,
            "web_location": "444.8",
        }
        wbi_sign.sign(params)

        try:
            async with self._session.get(
                DANMAKU_SERVER_CONF_URL,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                                  " (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Referer": "https://live.bilibili.com/",
                },
                params=params,
                ssl=self._ssl,
            ) as res:
                if res.status != 200:
                    log.warning(
                        "room=%d _init_host_server() failed, status=%d, reason=%s",
                        self._room_id, res.status, res.reason,
                    )
                    return False
                data = await res.json()
                if data["code"] != 0:
                    log.warning(
                        "room=%d _init_host_server() failed, message=%s",
                        self._room_id, data["message"],
                    )
                    return False
                if not self._parse_danmaku_server_conf(data["data"]):
                    return False
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
            log.exception("room=%d _init_host_server() failed:", self._room_id)
            return False
        return True


class DanmakuHandler(BaseHandler):
    """弹幕处理器 — 接入调度器"""

    def __init__(self, scheduler: Scheduler, entry_welcomer=None):
        super().__init__()
        self.scheduler = scheduler
        self.entry_welcomer = entry_welcomer

    async def handle(self, client: BLiveClient, command: dict):
        """拦截进房事件（V1 + V2），其他交给父类"""
        cmd = command.get("cmd", "")
        if cmd == "INTERACT_WORD":
            data = command.get("data", {})
            uname = data.get("uname", "")
            if uname and self.entry_welcomer:
                self.entry_welcomer.on_entry(uname)
            return
        if cmd == "INTERACT_WORD_V2":
            uname = _parse_interact_v2_uname(command.get("data", {}))
            if uname and self.entry_welcomer:
                self.entry_welcomer.on_entry(uname)
            return
        await super().handle(client, command)

    async def _on_danmaku(self, client: BLiveClient, message: DanmakuMessage):
        """收到普通弹幕 → 打分入队"""
        text = message.msg.strip()

        if len(text) < DANMAKU_MIN_LENGTH:
            return
        if any(kw in text for kw in BLOCKED_KEYWORDS):
            return

        log.info(f"📩 [{message.uname}]：{text}")
        await self.scheduler.enqueue_danmaku(message.uname, text)

    async def _on_gift(self, client: BLiveClient, message: GiftMessage):
        """收到礼物 → 打分入队"""
        log.info(f"🎁 [{message.uname}] 送了 {message.num} 个 {message.gift_name}")
        self.scheduler.enqueue_gift(message.uname, message.gift_name,
                                    message.price, message.num)

    async def _on_super_chat(self, client: BLiveClient, message: SuperChatMessage):
        """收到醒目留言 → 入队"""
        log.info(f"💰 [{message.uname}] SC ¥{message.price}：{message.message}")
        # SC 价格单位：blivedm 返回的是人民币，转电池 (*10)
        price_battery = int(message.price * 10)
        gift_text = f"(醒目留言 ¥{message.price}：{message.message})"
        self.scheduler.enqueue_gift(message.uname, gift_text, price_battery)


def create_bot(scheduler: Scheduler, entry_welcomer=None):
    """创建 B站弹幕客户端（不阻塞，返回三元组）"""
    cookies = {}
    if os.path.exists(_COOKIE_FILE):
        with open(_COOKIE_FILE) as f:
            cookies = json.load(f)
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10),
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
            "Cookie": cookie_str,
            "Accept-Encoding": "gzip, deflate",
        }
    )

    handler = DanmakuHandler(scheduler, entry_welcomer=entry_welcomer)
    client = WbiBLiveClient(BILIBILI_ROOM_ID, uid=int(cookies.get("DedeUserID", 0)),
                            session=session, ssl=True)
    client.add_handler(handler)

    return session, client, handler


async def start_bot(scheduler: Scheduler, entry_welcomer=None):
    """启动弹幕监听（blocking 风格，供 main.py 使用）"""
    session, client, handler = create_bot(scheduler, entry_welcomer)

    log.info(f"🚀 AI主播已上线！监听直播间 {BILIBILI_ROOM_ID}")
    log.info("📊 三队列调度器已接入")
    log.info("按 Ctrl+C 停止")

    try:
        client.start()
        while client.is_running:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("👋 下播了，AI主播去休息啦~")
        scheduler.chatbot.clear_history()
        client.stop()
        await session.close()
