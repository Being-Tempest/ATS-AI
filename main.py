"""
AI VTuber AI主播 — 主入口
启动弹幕监听 + 三队列调度 + AI 对话 + TTS 语音 + VTS 表情控制
"""

import sys, io, os
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
except Exception:
    pass
# 后台/管道模式下 stdout 可能已关闭，统一用 stderr 输出
sys.stdout = sys.stderr

# Fix conda SSL cert path on Windows
import certifi
os.environ['SSL_CERT_FILE'] = certifi.where()

import asyncio
from ai_chat import ChatBot
from danmaku_bot import start_bot
from tts_engine import get_engine
from scheduler import Scheduler
from vts_client import VTSClient
from entry_welcome import EntryWelcomer
from logger import get_logger, log_file_path

log = get_logger("main")


async def main():
    # 如需防止多实例打架,可自行加锁逻辑(公开版不执行任何 kill 操作)
    log.info(f"📋 日志文件: {log_file_path()}")

    # ── VTS 连接 ──
    vts = VTSClient()
    vts_connected = await vts.connect()
    if vts_connected:
        log.info("✅ VTube Studio 已连接")
    else:
        log.warning("⚠️  VTube Studio 未连接，跳过表情控制")

    # ── TTS（云端 CosyVoice 3.5，无需加载模型）──
    log.info("🎙️  TTS 引擎...")
    tts = get_engine()

    # ── AI 对话 ──
    chatbot = ChatBot()

    # ── 三队列调度器 ──
    scheduler = Scheduler(chatbot, tts, vts if vts_connected else None)

    # 启动调度器（后台流水线）
    sched_task = asyncio.create_task(scheduler.run())

    # 进房欢迎
    entry_welcomer = EntryWelcomer(scheduler)

    # 启动弹幕监听（接入调度器）
    log.info("🚀 启动弹幕监听...")
    await start_bot(scheduler, entry_welcomer=entry_welcomer)

    # 下播后清理
    sched_task.cancel()
    if vts_connected:
        await vts.close()


if __name__ == "__main__":
    asyncio.run(main())
