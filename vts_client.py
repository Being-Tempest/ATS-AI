"""
VTube Studio WebSocket 客户端
控制表情切换 + 微动作 + 心跳保活 + 自动重连
"""

import asyncio
import json
import os
import uuid
import aiohttp
from logger import get_logger

log = get_logger("vts")

VTS_WS_URL = "ws://localhost:8001"
PLUGIN_NAME = "AI主播"
PLUGIN_DEV = "xjn"
HEARTBEAT_INTERVAL = 20  # 心跳间隔（秒），用轻量 StatisticsRequest


class VTSClient:
    """VTube Studio WebSocket API 封装 — 心跳保活 + 断开自动重连"""

    def __init__(self):
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._auth_token: str | None = None
        self._ready = False
        self._current_expr: str | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._reconnecting: bool = False
        self._lock = asyncio.Lock()  # 防止并发调用竞争 WebSocket 响应

    async def connect(self) -> bool:
        """连接 VTS 并认证。返回成功与否。"""
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(
                VTS_WS_URL,
                heartbeat=15,
            )
        except Exception as e:
            log.error(f"连接失败: {e}")
            return False

        # 请求 token（VTS 会弹授权窗，或自动通过）
        log.info("🔑 请求 VTS 授权...")
        resp = await self._send({
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": str(uuid.uuid4()),
            "messageType": "AuthenticationTokenRequest",
            "data": {"pluginName": PLUGIN_NAME, "pluginDeveloper": PLUGIN_DEV}
        })
        if not resp or resp.get("messageType") != "AuthenticationTokenResponse":
            log.error(f"获取 token 失败（请在 VTS 点允许）: {resp}")
            return False
        self._auth_token = resp["data"].get("authenticationToken", "")

        # 认证
        resp = await self._send({
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": str(uuid.uuid4()),
            "messageType": "AuthenticationRequest",
            "data": {
                "pluginName": PLUGIN_NAME,
                "pluginDeveloper": PLUGIN_DEV,
                "authenticationToken": self._auth_token,
            }
        })
        if resp and resp.get("data", {}).get("authenticated"):
            self._ready = True
            log.info("✅ VTS 已连接认证")
            self._start_heartbeat()
            return True
        log.error(f"认证失败: {resp}")
        return False

    def _start_heartbeat(self):
        """启动心跳协程，防止 VTS 超时断开"""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self):
        """每 HEARTBEAT_INTERVAL 秒发一个轻量请求"""
        while self._ready:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if not self._ready:
                break
            try:
                await self._send({
                    "apiName": "VTubeStudioPublicAPI",
                    "apiVersion": "1.0",
                    "requestID": str(uuid.uuid4()),
                    "messageType": "StatisticsRequest",
                    "data": {}
                })
            except Exception:
                log.warning("💔 VTS 心跳失败，WebSocket 可能已断开")
                self._ready = False
                # 尝试自动重连
                if not self._reconnecting:
                    asyncio.create_task(self._auto_reconnect())

    async def _auto_reconnect(self):
        """自动重连：优先用旧 token 免弹窗认证，失效才走完整流程"""
        self._reconnecting = True
        log.info("🔄 尝试重连 VTS...")
        await self._cleanup_connection()
        self._ready = False
        await asyncio.sleep(1)

        # 新建 WebSocket，优先用旧 token 认证（不弹窗）
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(VTS_WS_URL, heartbeat=15)
        except Exception as e:
            log.error(f"重连失败: {e}")
            self._reconnecting = False
            return

        if self._auth_token:
            resp = await self._send({
                "apiName": "VTubeStudioPublicAPI",
                "apiVersion": "1.0",
                "requestID": str(uuid.uuid4()),
                "messageType": "AuthenticationRequest",
                "data": {
                    "pluginName": PLUGIN_NAME,
                    "pluginDeveloper": PLUGIN_DEV,
                    "authenticationToken": self._auth_token,
                }
            })
            if resp and resp.get("data", {}).get("authenticated"):
                self._ready = True
                self._start_heartbeat()
                log.info("🔄 VTS 重连成功（旧 token 有效）")
                self._reconnecting = False
                await asyncio.sleep(0.3)  # 等 VTS 就绪
                if self._current_expr:
                    await self._restore_expression()
                return

        # 旧 token 失效，走完整认证（插件已授权通常自动通过）
        ok = await self.connect()
        if ok:
            await asyncio.sleep(0.3)
            if self._current_expr:
                await self._restore_expression()
        log.info("🔄 VTS 重连完成" if ok else "❌ VTS 重连失败")
        self._reconnecting = False

    async def _restore_expression(self):
        """恢复当前表情，确保文件名以 .exp3.json 结尾"""
        from config import VTS_EXPRESSIONS
        expr = self._current_expr
        if not expr:
            return
        if not expr.endswith(".exp3.json"):
            expr = VTS_EXPRESSIONS.get(expr, expr)
        await self.set_expression(expr, True)

    async def _cleanup_connection(self):
        """清理旧连接资源"""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        try:
            if self._ws:
                await self._ws.close()
        except Exception:
            pass
        self._ws = None
        # 不关 session，reconnect 会新建

    async def _send(self, data: dict) -> dict | None:
        """发送请求并等待响应（线程安全，防止心跳和业务竞争）"""
        if not self._ws:
            return None
        async with self._lock:
            try:
                await self._ws.send_json(data)
                msg = await self._ws.receive_json(timeout=5)
                if msg.get("messageType") == "APIError":
                    log.error(f"API错误: {msg['data'].get('message', msg)}")
                    return None
                return msg
            except asyncio.TimeoutError:
                log.warning("VTS API 超时")
                return None
            except Exception as e:
                log.error(f"请求异常: {e}")
                return None

    async def trigger_hotkey(self, hotkey_id: str) -> bool:
        """触发指定 hotkey"""
        resp = await self._send({
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": str(uuid.uuid4()),
            "messageType": "HotkeyTriggerRequest",
            "data": {"hotkeyID": hotkey_id},
            "authenticationToken": self._auth_token,
        })
        return resp is not None and resp.get("messageType") == "HotkeyTriggerResponse"

    async def get_hotkeys(self) -> list[dict]:
        """获取当前模型所有已配置的 hotkey 列表"""
        resp = await self._send({
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": str(uuid.uuid4()),
            "messageType": "HotkeysInCurrentModelRequest",
            "data": {},
            "authenticationToken": self._auth_token,
        })
        if resp and resp.get("messageType") == "HotkeysInCurrentModelResponse":
            return resp["data"].get("availableHotkeys", [])
        return []

    async def set_expression(self, expression_file: str, active: bool) -> bool:
        """激活/停用一个表达式文件"""
        resp = await self._send({
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": str(uuid.uuid4()),
            "messageType": "ExpressionActivationRequest",
            "data": {
                "expressionFile": expression_file,
                "active": active,
            },
            "authenticationToken": self._auth_token,
        })
        return resp is not None and resp.get("messageType") == "ExpressionActivationResponse"

    async def switch_expression(self, new_key: str):
        """切换表情：先用简称查 VTS_EXPRESSIONS 映射，再发请求。断连自动重连重试。"""
        from config import VTS_EXPRESSIONS
        new_file = VTS_EXPRESSIONS.get(new_key, new_key)
        log.info(f"🎭 切表情: {new_key} → {new_file}  (ready={self._ready}, current={self._current_expr})")
        if self._current_expr == new_file:
            log.info(f"  ⏭ 已是当前表情，跳过")
            return
        if not self._ready:
            await self._auto_reconnect()
            if not self._ready:
                return
        try:
            if self._current_expr:
                await self.set_expression(self._current_expr, False)
                await asyncio.sleep(0.05)
            ok = await self.set_expression(new_file, True)
            if ok:
                self._current_expr = new_file
            else:
                self._ready = False  # 标记断连，下次自动重连
                log.warning(f"切表情失败: {new_file}")
        except Exception as e:
            self._ready = False
            log.warning(f"切表情异常: {e}")

    async def clear_expressions(self):
        """清除当前激活的表情"""
        if self._current_expr:
            await self.set_expression(self._current_expr, False)
            self._current_expr = None

    async def current_model(self) -> dict | None:
        """获取当前模型信息"""
        resp = await self._send({
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": str(uuid.uuid4()),
            "messageType": "CurrentModelRequest",
            "data": {}
        })
        return resp.get("data") if resp else None

    async def inject_parameters(self, params: list[dict], mode: str = "add") -> bool:
        """注入参数值（P2 优先级，可叠加 P3/P4 表情）
        params = [{"id": "FaceAngleX", "value": 1.5, "weight": 0.5}, ...]
        mode: "add" 累加, "set" 覆盖
        """
        resp = await self._send({
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": str(uuid.uuid4()),
            "messageType": "InjectParameterDataRequest",
            "data": {
                "faceFound": False,
                "mode": mode,
                "parameterValues": params,
            },
            "authenticationToken": self._auth_token,
        })
        return resp is not None and resp.get("messageType") == "InjectParameterDataResponse"

    async def get_model_parameters(self) -> list[dict]:
        """获取模型所有 Live2D 参数列表"""
        resp = await self._send({
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": str(uuid.uuid4()),
            "messageType": "Live2DParameterListRequest",
            "data": {},
        })
        if resp and "parameters" in resp.get("data", {}):
            return resp["data"]["parameters"]
        return []

    async def create_tracking_param(self, name: str, explanation: str = "",
                                     min_val: float = -30, max_val: float = 30,
                                     default_val: float = 0) -> bool:
        """创建自定义追踪参数（可在 VTS 里映射到 Live2D 参数）"""
        resp = await self._send({
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": str(uuid.uuid4()),
            "messageType": "ParameterCreationRequest",
            "data": {
                "parameterName": name,
                "explanation": explanation or f"Auto-created by {PLUGIN_NAME}",
                "min": min_val,
                "max": max_val,
                "defaultValue": default_val,
            },
            "authenticationToken": self._auth_token,
        })
        return resp is not None and resp.get("messageType") == "ParameterCreationResponse"

    @property
    def ready(self) -> bool:
        return self._ready

    async def close(self):
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        await self._cleanup_connection()
        if self._session:
            await self._session.close()
        self._ready = False
        log.info("👋 VTS 已断开")
