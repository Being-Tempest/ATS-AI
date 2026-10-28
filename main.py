"""
AI VTuber 框架 — 主入口
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
from avatar_factory import create_avatar
from entry_welcome import EntryWelcomer
from logger import get_logger, log_file_path

log = get_logger("main")
_instance_lock = None


def _acquire_instance_lock():
    """只阻止重复启动本程序，不影响机器上的其他 Python 进程。"""
    global _instance_lock
    import msvcrt
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".main.lock")
    handle = open(lock_path, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except (OSError, IOError):
        handle.close()
        return None
    _instance_lock = handle
    return handle


async def main():
    if _acquire_instance_lock() is None:
        log.error("程序已在运行，请勿重复启动")
        return

    log.info(f"📋 日志文件: {log_file_path()}")

    avatar = create_avatar()
    avatar_connected = await avatar.connect()
    if avatar_connected:
        log.info("Avatar 后端已连接: %s", avatar.state().backend)
    else:
        log.warning("Avatar 后端未连接，跳过形象控制: %s", avatar.state().backend)

    log.info("🎙️  TTS 引擎...")
    tts = get_engine()
    if hasattr(avatar, "submit_mouth_open"):
        tts.set_audio_tap(avatar.submit_mouth_open)
    chatbot = ChatBot()
    scheduler = Scheduler(chatbot, tts, avatar=avatar)
    sched_task = asyncio.create_task(scheduler.run())
    entry_welcomer = EntryWelcomer(scheduler)

    try:
        log.info("🚀 启动弹幕监听...")
        await start_bot(scheduler, entry_welcomer=entry_welcomer)
    finally:
        sched_task.cancel()
        await scheduler.close()
        await avatar.close()


if __name__ == "__main__":
    asyncio.run(main())
