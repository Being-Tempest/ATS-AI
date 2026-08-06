"""
Dashboard — AI主播中控台（可视化全流程测试）
浏览器 http://localhost:5000
"""
import sys, os
sys.stdout = sys.stderr
import certifi
os.environ['SSL_CERT_FILE'] = certifi.where()

import time
import json
import os
import asyncio
import threading
import concurrent.futures
from urllib.parse import unquote

from flask import Flask, render_template
from flask_socketio import SocketIO, emit
from logger import get_logger

log = get_logger("dashboard")

app = Flask(__name__)
app.config['SECRET_KEY'] = 'jianing-dashboard'
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins="*")

# ═══════════════════════════════════════════
# 后台引擎实例（在 asyncio 线程里创建）
# ═══════════════════════════════════════════

_loop: asyncio.AbstractEventLoop | None = None      # 后台 asyncio event loop
_thread: threading.Thread | None = None              # 后台线程
_chatbot = None                                       # ChatBot
_tts = None                                           # TTSEngine
_vts = None                                           # VTSClient
_scheduler = None                                     # Scheduler
_vision = None                                        # VisionModule
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)

# B站直播相关
_live_mode = False                                    # True=正式开播, False=模拟测试
_danmaku_client = None                                # WbiBLiveClient
_danmaku_session = None                               # aiohttp.ClientSession
_entry_welcomer = None                                # EntryWelcomer
_bilibili_reconnect_task: asyncio.Task | None = None  # 断连重连后台任务

# 状态快照（后台线程写入，Flask 线程读取）
_status = {
    "live": False,
    "live_mode": False,          # True=正式开播, False=模拟测试
    "mic": False,
    "vts_connected": False,
    "tts_ready": False,
    "emotion": "default_smile",
    "queue_count": 0,
    "subtitle": "",
    "playing": False,
    "asr_text": "",
    "recent_events": [],
    # B站状态
    "bilibili_connected": False,
    "bilibili_room_id": 0,
    "cookie_valid": False,
    "cookie_expiry_days": 0,
}
_status_lock = threading.Lock()


def _read_subtitle_file():
    """读字幕文件（线程安全）"""
    try:
        p = os.path.join(os.path.dirname(__file__), "subtitle.txt")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def _update_status():
    """后台线程：更新状态快照"""
    global _scheduler, _vts, _danmaku_client
    with _status_lock:
        if _vts:
            _status["vts_connected"] = _vts.ready
        if _scheduler:
            ss = _scheduler._status()
            _status["queue_count"] = sum(ss[k] for k in ['q1_d','q2_d','q3_d','q1_g','q2_g','q3_g'])
            _status["playing"] = ss["playing"]
        _status["subtitle"] = _read_subtitle_file()
        # B站连接状态
        if _danmaku_client:
            _status["bilibili_connected"] = _danmaku_client.is_running
        _status["live_mode"] = _live_mode


def _run_async(coro):
    """从 Flask 线程安全调用后台 async 函数"""
    if _loop is None:
        return None
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return future.result(timeout=30)
    except Exception as e:
        log.error(f"async call failed: {e}")
        return None


def _fire_async(coro):
    """从 Flask 线程 fire-and-forget 调用后台 async 函数（不阻塞，但记录异常）"""
    if _loop is None:
        log.error("_fire_async: _loop is None")
        return
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    def _check(fut):
        try:
            fut.result()
        except Exception as e:
            log.error(f"_fire_async failed: {e}")
    future.add_done_callback(_check)


def _enqueue_mic_sentence(text: str):
    """从 ASR 线程 fire-and-forget 入队，每识别完一句话立即触发"""
    if _scheduler and _loop:
        add_event("mic", f"主播: {text}")
        asyncio.run_coroutine_threadsafe(_scheduler.enqueue_danmaku("主播", text), _loop)


# ═══════════════════════════════════════════
# Cookie 过期检查
# ═══════════════════════════════════════════

def _check_cookie_expiry() -> tuple[bool, int]:
    """解析 bilibili_cookies.json 的 SESSDATA 过期时间
    返回 (是否有效, 剩余天数)
    """
    cookie_file = os.path.join(os.path.dirname(__file__), "bilibili_cookies.json")
    try:
        with open(cookie_file, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        sessdata = cookies.get("SESSDATA", "")
        if not sessdata:
            return False, 0
        # SESSDATA 格式: value,timestamp,signature
        decoded = unquote(sessdata)
        parts = decoded.split(",")
        if len(parts) < 2:
            return False, 0
        expiry_ts = int(parts[1])
        now_ts = time.time()
        remaining = max(0, expiry_ts - now_ts)
        days = int(remaining / 86400)
        return remaining > 0, days
    except Exception as e:
        log.warning(f"Cookie 解析失败: {e}")
        return False, 0


# ═══════════════════════════════════════════
# B站弹幕连接（正式开播模式）
# ═══════════════════════════════════════════

def _start_bilibili(scheduler):
    """在已有的 asyncio event loop 中启动 B站弹幕监听"""
    global _danmaku_client, _danmaku_session, _entry_welcomer, _bilibili_reconnect_task

    from danmaku_bot import create_bot
    from entry_welcome import EntryWelcomer
    from config import BILIBILI_ROOM_ID

    # 进房欢迎
    _entry_welcomer = EntryWelcomer(scheduler)

    # 创建 B站客户端
    _danmaku_session, _danmaku_client, _ = create_bot(scheduler, _entry_welcomer)

    with _status_lock:
        _status["bilibili_room_id"] = BILIBILI_ROOM_ID

    # 检查 cookie
    valid, days = _check_cookie_expiry()
    with _status_lock:
        _status["cookie_valid"] = valid
        _status["cookie_expiry_days"] = days
    if valid:
        log.info(f"🍪 Cookie 有效，剩余 {days} 天")
    else:
        log.warning("⚠️ Cookie 已过期，B站连接可能失败")

    # 启动弹幕客户端（blivedm client.start() 内部跑 WebSocket 后台线程）
    _danmaku_client.start()
    log.info(f"🚀 B站弹幕监听已启动！直播间 {BILIBILI_ROOM_ID}")

    # 启动断连重连监控
    _bilibili_reconnect_task = asyncio.create_task(_monitor_bilibili())


async def _monitor_bilibili():
    """后台监控 B站连接状态，断连自动重连"""
    global _danmaku_client
    reconnect_delay = 5

    while _live_mode and _danmaku_client:
        connected = _danmaku_client.is_running if _danmaku_client else False
        with _status_lock:
            _status["bilibili_connected"] = connected

        if not connected and _live_mode:
            log.warning(f"⚠️ B站连接断开，{reconnect_delay}s 后重连...")
            add_event("system", "⚠️ B站断线，重连中...")
            try:
                _danmaku_client.start()
            except Exception as e:
                log.error(f"重连失败: {e}")
        await asyncio.sleep(reconnect_delay)


def _stop_bilibili():
    """停止 B站弹幕监听"""
    global _danmaku_client, _danmaku_session, _entry_welcomer, _bilibili_reconnect_task

    if _bilibili_reconnect_task:
        _bilibili_reconnect_task.cancel()
        _bilibili_reconnect_task = None

    if _danmaku_client:
        try:
            _danmaku_client.stop()
        except Exception:
            pass
        _danmaku_client = None

    if _danmaku_session:
        try:
            # 需要在 event loop 中关闭
            if _loop and _loop.is_running():
                asyncio.run_coroutine_threadsafe(_danmaku_session.close(), _loop)
        except Exception:
            pass
        _danmaku_session = None

    _entry_welcomer = None

    with _status_lock:
        _status["bilibili_connected"] = False
        _status["bilibili_room_id"] = 0

    log.info("👋 B站弹幕监听已停止")


def _on_vision_description(description: str):
    """从 Vision 线程桥接到 asyncio 线程 → 画面评论入队（最高优先）"""
    if _scheduler and _loop:
        add_event("vision", f"画面: {description[:40]}...")
        # enqueue_vision 是同步方法，但 ScoreQueue 非线程安全，桥接到 event loop 线程
        asyncio.run_coroutine_threadsafe(
            _async_vision_enqueue(description), _loop
        )


async def _async_vision_enqueue(description: str):
    """在 asyncio 线程里执行入队"""
    _scheduler.enqueue_vision(description)


# ═══════════════════════════════════════════
# VTS 快捷表情切换（不经过队列）
# ═══════════════════════════════════════════

@socketio.on("set_expression")
def on_set_expression(data):
    expr = data.get("expression", "default_smile")
    if _vts and _vts.ready:
        _run_async(_vts.switch_expression(expr))
    with _status_lock:
        _status["emotion"] = expr
    add_event("system", f"表情 → {expr}")


# ═══════════════════════════════════════════
# ASR 引擎（懒加载，复用实例）
# ═══════════════════════════════════════════

_asr_engine = None


def _get_asr():
    global _asr_engine
    if _asr_engine is None:
        from asr_engine import ASREngine
        _asr_engine = ASREngine()
    return _asr_engine


# ═══════════════════════════════════════════
# 后台 asyncio 线程
# ═══════════════════════════════════════════

async def _scheduler_main(mode: str = "simulated"):
    """后台线程入口：创建引擎 + 跑调度器
    mode: "simulated" = 模拟测试, "live" = 正式开播（含B站弹幕）
    """
    global _loop, _chatbot, _tts, _vts, _scheduler, _live_mode

    _loop = asyncio.get_running_loop()
    _live_mode = (mode == "live")
    log.info(f"后台线程启动，模式: {mode}")

    # ── VTS ──
    from vts_client import VTSClient
    _vts = VTSClient()
    vts_ok = await _vts.connect()
    if vts_ok:
        log.info("✅ VTS 已连接")
    else:
        log.warning("⚠️ VTS 未连接，跳过表情")

    # ── TTS ──
    from tts_engine import get_engine
    _tts = get_engine()

    # ── AI ──
    from ai_chat import ChatBot
    _chatbot = ChatBot()

    # ── Scheduler ──
    from scheduler import Scheduler
    vts_ref = _vts if vts_ok else None
    _scheduler = Scheduler(_chatbot, _tts, vts_ref)
    _scheduler._pool = _pool

    # ── 正式开播：启动 B站弹幕 ──
    if _live_mode:
        _start_bilibili(_scheduler)
        add_event("system", "🔴 正式开播 — B站直播间监听中")

    _update_status()
    log.info("✅ 引擎就绪，开始调度循环")

    try:
        await _scheduler.run()
    except asyncio.CancelledError:
        pass
    finally:
        # 清理 B站连接
        if _live_mode:
            _stop_bilibili()
        # 清理
        if vts_ok:
            await _vts.close()
        _chatbot.clear_history()
        _live_mode = False
        log.info("引擎已停止")


def _start_engine(mode: str = "simulated"):
    """启动后台 asyncio 线程
    mode: "simulated" | "live"
    """
    global _loop, _thread

    if _thread and _thread.is_alive():
        log.warning("引擎已在运行，先停止再启动")
        return

    def _run_loop():
        asyncio.run(_scheduler_main(mode))

    _thread = threading.Thread(target=_run_loop, daemon=True)
    _thread.start()
    # 等后台线程初始化完成
    time.sleep(0.5)


def _stop_engine():
    """停止后台线程（包括 B站连接）"""
    global _loop, _thread, _scheduler, _vts, _chatbot, _tts, _live_mode

    _live_mode = False

    if _loop:
        for task in asyncio.all_tasks(_loop):
            task.cancel()

    if _thread:
        _thread.join(timeout=5)
        _thread = None

    _scheduler = None
    _vts = None
    _chatbot = None
    _tts = None
    _loop = None


# ═══════════════════════════════════════════
# Flask 路由
# ═══════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


# ═══════════════════════════════════════════
# Socket.IO 事件
# ═══════════════════════════════════════════

@socketio.on("connect")
def on_connect():
    # 初始化 cookie 状态
    valid, days = _check_cookie_expiry()
    with _status_lock:
        _status["cookie_valid"] = valid
        _status["cookie_expiry_days"] = days
    emit("status", dict(_status))
    # 启动状态轮询
    socketio.start_background_task(_poll_status)


def _poll_status():
    """每 0.3s 推一次状态到前端"""
    while True:
        _update_status()
        with _status_lock:
            snapshot = dict(_status)
        socketio.emit("status", snapshot)
        time.sleep(0.3)


def _do_start(mode: str):
    """统一的启动逻辑"""
    global _status, _vision

    if _status["live"]:
        log.warning("已在直播中，先下播再切换模式")
        return

    label = "正式开播" if mode == "live" else "模拟测试"
    log.info(f" Dashboard {label}")
    add_event("system", f"{'🔴' if mode == 'live' else '🧪'} {label} — 引擎启动")

    _start_engine(mode)
    # 等引擎就绪
    time.sleep(1)
    _update_status()

    # ── 启动画面评论 ──
    from vision_module import get_vision
    _vision = get_vision()
    _vision.set_on_description(_on_vision_description)
    _vision.start()

    with _status_lock:
        _status["live"] = True
        _status["live_mode"] = (mode == "live")
        _status["tts_ready"] = _tts is not None
        _status["vts_connected"] = _vts.ready if _vts else False

    emit("status", dict(_status))


@socketio.on("start_simulated")
def on_start_simulated():
    """🧪 模拟测试"""
    _do_start("simulated")


@socketio.on("start_live")
def on_start_live():
    """🔴 正式开播"""
    # 检查 B站 cookie
    valid, days = _check_cookie_expiry()
    if not valid:
        log.warning("⚠️ B站 Cookie 已过期")
        add_event("system", "⚠️ Cookie 已过期，B站连接可能失败")
    _do_start("live")


@socketio.on("stop_live")
def on_stop_live():
    """⏹ 下播"""
    global _status, _vision
    log.info(" Dashboard 下播")
    if _status.get("mic"):
        on_toggle_mic()
    if _vision:
        _vision.stop()
        _vision = None
    _stop_engine()
    with _status_lock:
        _status["live"] = False
        _status["live_mode"] = False
        _status["vts_connected"] = False
        _status["tts_ready"] = False
        _status["bilibili_connected"] = False
        _status["bilibili_room_id"] = 0
    add_event("system", "⏹ 下播 — 引擎已停止")
    emit("status", dict(_status))


@socketio.on("toggle_mic")
def on_toggle_mic():
    try:
        with _status_lock:
            _status["mic"] = not _status["mic"]

        if _status["mic"]:
            add_event("system", "🎤 开麦")
            asr = _get_asr()
            asr._on_sentence = _enqueue_mic_sentence  # 每句话实时入队
            asr.start()
            socketio.start_background_task(_asr_monitor)
            if _vision:
                _vision.set_mic_state(True)
        else:
            # 关麦丢后台线程，避免 asr.stop() 堵键盘钩子导致被 Windows 摘掉
            threading.Thread(target=_do_stop_mic, daemon=True).start()
    except Exception as e:
        log.error(f"toggle_mic 失败: {e}")
        with _status_lock:
            _status["mic"] = False
    socketio.emit("status", dict(_status), namespace='/')


def _do_stop_mic():
    """后台执行关麦 —— 在独立线程里跑，不堵调用方"""
    try:
        asr = _get_asr()
        text = asr.stop()
        with _status_lock:
            _status["asr_text"] = text
        if not text.strip():
            add_event("mic", "(未识别到语音)")
        if _vision:
            _vision.set_mic_state(False)
        socketio.emit("status", dict(_status), namespace='/')
    except Exception as e:
        log.error(f"关麦异常: {e}")


def _asr_monitor():
    """后台：实时推送 ASR 中间结果"""
    asr = _get_asr()
    last_text = ""
    while _status.get("mic"):
        if asr.running:
            cur = asr.current_text()
            if cur != last_text:
                last_text = cur
                with _status_lock:
                    _status["asr_text"] = cur
                socketio.emit("asr_text", {"text": cur})
        time.sleep(0.2)


@socketio.on("send_text")
def on_send_text(data):
    text = data.get("text", "").strip()
    if not text: return
    add_event("chat", f"主播: {text}")
    if _scheduler:
        _fire_async(_scheduler.enqueue_danmaku("主播", text))
    else:
        add_event("system", "⚠️ 先点开播")
    emit("status", dict(_status))


@socketio.on("sim_danmaku")
def on_sim_danmaku(data):
    username = data.get("username", "观众").strip()
    text = data.get("text", "").strip()
    if not text: return
    add_event("danmaku", f"[{username}] {text}")
    if _scheduler:
        _fire_async(_scheduler.enqueue_danmaku(username, text))
    else:
        add_event("system", "⚠️ 先点开播")


@socketio.on("sim_entry")
def on_sim_entry(data):
    username = data.get("username", "观众").strip()
    if not username: return
    add_event("welcome", f"{username} 进入直播间")
    if _scheduler:
        _scheduler.enqueue_entry(username)


@socketio.on("sim_gift")
def on_sim_gift(data):
    username = data.get("username", "观众").strip()
    gift = data.get("gift", "小花花").strip()
    try:
        price = int(data.get("price", 100))
    except (ValueError, TypeError):
        price = 100
    add_event("gift", f"[{username}] {gift} ¥{price/10:.0f}")
    if _scheduler:
        _scheduler.enqueue_gift(username, gift, price, 1)


_events: list[dict] = []


def add_event(kind: str, text: str):
    print(f"[dashboard] {kind}: {text[:60]}")
    _events.insert(0, {"time": time.strftime("%H:%M:%S"), "kind": kind, "text": text[:80]})
    if len(_events) > 50:
        _events[:] = _events[:50]
    with _status_lock:
        _status["recent_events"] = _events[:10]
    socketio.emit("events", {"events": _events[:10]}, namespace='/')


# ═══════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════

def run_dashboard(host="0.0.0.0", port=5000):

    # ── 注册全局快捷键 Ctrl+Alt+M → 开/关麦 ──
    try:
        import keyboard
        keyboard.add_hotkey('ctrl+alt+m', on_toggle_mic, suppress=False)
        print("[Hotkey] Ctrl+Alt+M → 开/关麦")
    except Exception as e:
        print(f"[Hotkey] 快捷键注册失败（可能需要管理员权限）: {e}")

    # 打印 cookie 状态
    valid, days = _check_cookie_expiry()
    cookie_info = f"有效({days}天)" if valid else "已过期"
    print(f"""
╔══════════════════════════════════════╗
║       AI主播 中控台 v1.0            ║
╠══════════════════════════════════════╣
║  中控台: http://localhost:{port}     ║
║  Cookie: {cookie_info}                    ║
╚══════════════════════════════════════╝
""")
    socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    run_dashboard()
