"""按配置创建 Avatar 后端，供命令行和中控台共用。"""
from config import AVATAR_BACKEND, AVATAR_SKIN
from avatar_null import NullAvatar


def create_avatar(backend: str | None = None):
    backend = (backend or AVATAR_BACKEND).lower()
    if backend == "null":
        return NullAvatar()
    if backend == "vts":
        from avatar_vts import VTSAvatar
        return VTSAvatar(skin=AVATAR_SKIN)
    raise ValueError(f"未知 Avatar 后端: {backend}")
