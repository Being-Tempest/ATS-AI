import unittest

from avatar_factory import create_avatar
from avatar_null import NullAvatar
from avatar_vts import VTSAvatar
from config import VTS_IDLE_EAT_ID


class FakeVTSClient:
    def __init__(self):
        self.ready = False
        self.expression_result = True
        self.calls = []

    async def connect(self):
        self.ready = True
        return True

    async def close(self):
        self.ready = False

    async def switch_expression(self, key):
        self.calls.append(("emotion", key))
        return self.expression_result

    async def trigger_hotkey(self, key):
        self.calls.append(("action", key))
        return True

    async def inject_parameters(self, parameters, mode="add"):
        self.calls.append(("parameters", parameters, mode))
        return True

    async def get_model_parameters(self):
        return [{"name": "ParamMouthOpenY", "value": 0.0}]


class AvatarContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_vts_adapter_uses_semantic_operations(self):
        client = FakeVTSClient()
        avatar = VTSAvatar(client=client, skin="test-skin")
        self.assertTrue(await avatar.connect())
        self.assertTrue(await avatar.set_emotion("happy"))
        self.assertTrue(await avatar.play_action("eat"))
        self.assertTrue(await avatar.set_head_pose(1, 2, 3, mix=0.35))
        self.assertFalse(await avatar.set_mouth_open(1))
        self.assertEqual(avatar.state().emotion, "happy")
        self.assertEqual(avatar.state().skin, "test-skin")
        self.assertEqual(client.calls[0], ("emotion", "happy"))
        self.assertEqual(client.calls[1], ("action", VTS_IDLE_EAT_ID))
        self.assertEqual(client.calls[2][2], "set")
        await avatar.close()
        self.assertFalse(avatar.ready)

    async def test_failed_expression_does_not_change_reported_state(self):
        client = FakeVTSClient()
        client.expression_result = False
        avatar = VTSAvatar(client=client)
        await avatar.connect()
        self.assertFalse(await avatar.set_emotion("angry"))
        self.assertEqual(avatar.state().emotion, "default_smile")

    async def test_null_avatar_clamps_mouth_and_tracks_pose(self):
        avatar = NullAvatar()
        await avatar.set_mouth_open(2)
        await avatar.set_head_pose(1, 2, 3)
        self.assertEqual(avatar.state().parameters["mouth_open"], 1.0)
        self.assertEqual(avatar.state().parameters["z"], 3)
        await avatar.reset_pose()
        self.assertEqual(avatar.state().parameters["x"], 0)

    def test_factory_rejects_unknown_backend(self):
        with self.assertRaises(ValueError):
            create_avatar("unknown")


if __name__ == "__main__":
    unittest.main()
