"""不渲染形象的测试/降级后端。"""
from avatar import AvatarState


class NullAvatar:
    def __init__(self):
        self.capabilities = set()
        self._state = AvatarState(backend="null", ready=True)

    @property
    def ready(self):
        return self._state.ready

    async def connect(self):
        self._state.ready = True
        return True

    async def close(self):
        self._state.ready = False

    async def set_emotion(self, key):
        self._state.emotion = key
        return True

    async def play_action(self, key):
        return True

    async def set_head_pose(self, x, y, z, mix=1.0):
        self._state.parameters.update(x=x, y=y, z=z, mix=mix)
        return True

    async def reset_pose(self):
        return await self.set_head_pose(0, 0, 0)

    async def set_mouth_open(self, value):
        self._state.parameters["mouth_open"] = max(0.0, min(1.0, value))
        return True

    async def get_parameters(self):
        return []

    async def set_parameter(self, name, value):
        self._state.parameters[name] = value
        return True

    def state(self):
        return self._state
