"""
创建自己的音色 — CosyVoice 3.5 声音复刻

上传一段参考音频 → 创建音色 → 拿到 voice_id。
用法:
  1. 在 .env 里配置 DASHSCOPE_API_KEY
  2. 准备一段 10-30 秒的清晰人声录音 reference.wav
  3. python create_voice.py reference.wav
"""
import os
import sys
import time

import dashscope
from dashscope import Files
from dashscope.audio.tts_v2 import VoiceEnrollmentService

from config import DASHSCOPE_API_KEY

if not DASHSCOPE_API_KEY:
    print("❌ 请在 .env 中配置 DASHSCOPE_API_KEY", file=sys.stderr)
    sys.exit(1)
dashscope.api_key = DASHSCOPE_API_KEY

# 北京地域
dashscope.base_http_api_url = "https://dashscope.aliyuncs.com/api/v1"

REF_AUDIO = sys.argv[1] if len(sys.argv) > 1 else "reference.wav"
VOICE_PREFIX = os.environ.get("VOICE_PREFIX", "myvoice")


def main():
    print("=" * 50)
    print("  创建音色 — CosyVoice 3.5 声音复刻")
    print("=" * 50)

    # 1. 上传参考音频
    print(f"\n📤 上传参考音频: {REF_AUDIO}")
    resp = Files.upload(file_path=REF_AUDIO, purpose="voice_clone")
    file_id = resp.output["uploaded_files"][0]["file_id"]
    print(f"   file_id: {file_id}")

    file_info = Files.get(file_id)
    oss_url = file_info.output["url"]
    print(f"   OSS URL: {oss_url[:60]}...")

    # 2. 创建复刻音色
    print("\n🎤 创建音色...")
    service = VoiceEnrollmentService()
    voice_id = service.create_voice(
        target_model="cosyvoice-v3.5-flash",
        prefix=VOICE_PREFIX,
        url=oss_url,
        language_hints=["zh"],
    )
    print(f"   voice_id: {voice_id}")

    # 3. 等待音色就绪
    print("\n⏳ 等待音色就绪...")
    for i in range(30):
        info = service.query_voice(voice_id=voice_id)
        status = info.get("status", "UNKNOWN")
        print(f"   [{i+1}/30] 状态: {status}")
        if status == "OK":
            print("\n✅ 音色创建成功！")
            print(f"   voice_id = {voice_id}")
            print("\n   把这一行加到 .env:")
            print(f'   COSYVOICE_VOICE_ID = "{voice_id}"')
            return voice_id
        elif status == "UNDEPLOYED":
            print("\n❌ 音色创建失败，请检查音频质量")
            return None
        time.sleep(10)

    print(f"\n⚠️ 超时，当前状态: {status}")
    print(f"   voice_id = {voice_id}(可以先记下来，手动检查状态)")
    return voice_id


if __name__ == "__main__":
    main()
