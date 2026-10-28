"""把现有 VTSClient 适配为业务层 AvatarPort。"""
from avatar import AvatarState
from config import VTS_IDLE_EAT_ID, VTS_IDLE_SLEEP_ID
from vts_client import VTSClient


class VTSAvatar:
    def __init__(self, client: VTSClient | None = None, skin: str = ""):
        self.client = client or VTSClient()
        self.skin = skin
        self.capabilities = {"emotion", "action", "head_pose", "debug_parameters"}
        self._emotion = "default_smile"
        self._parameters = {}

    @property
    def ready(self):
        return self.client.ready

    async def connect(self):
        return await self.client.connect()

    async def close(self):
        await self.client.close()

    async def set_emotion(self, key):
        ok = bool(await self.client.switch_expression(key))
        if ok:
            self._emotion = key
        return ok

    async def play_action(self, key):
        action_ids = {"eat": VTS_IDLE_EAT_ID, "sleep": VTS_IDLE_SLEEP_ID}
        if key not in action_ids:
            return False
        return await self.client.trigger_hotkey(action_ids[key])

    async def set_head_pose(self, x, y, z, mix=1.0):
        result = await self.client.inject_parameters([
            {"id": "FaceAngleX", "value": x, "weight": mix},
            {"id": "FaceAngleY", "value": y, "weight": mix},
            {"id": "FaceAngleZ", "value": z, "weight": mix},
        ], mode="set")
        if result:
            self._parameters.update(FaceAngleX=x, FaceAngleY=y, FaceAngleZ=z)
        return result

    async def reset_pose(self):
        return await self.set_head_pose(0, 0, 0)

    async def set_mouth_open(self, value):
        return False

    async def get_parameters(self):
        return await self.client.get_model_parameters()

    async def set_parameter(self, name, value):
        return await self.client.inject_parameters(
            [{"id": name, "value": value, "weight": 1.0}], mode="set")

    def state(self):
        return AvatarState(backend="vts", skin=self.skin, ready=self.ready,
                           emotion=self._emotion, parameters=dict(self._parameters))
