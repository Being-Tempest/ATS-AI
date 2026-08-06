"""
AI主播 TTS 引擎 — CosyVoice 3.5 云端 API（阿里云百炼）
接口保持 synthesize() + play() 不变，内部走 dashscope WebSocket 流式合成
"""
import time
import numpy as np
import sounddevice as sd
import scipy.signal
import dashscope
from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat
from config import DASHSCOPE_API_KEY, COSYVOICE_VOICE_ID, COSYVOICE_MODEL, TTS_VOLUME
from logger import get_logger

log = get_logger("tts")

# 初始化 DashScope
dashscope.api_key = DASHSCOPE_API_KEY
dashscope.base_websocket_api_url = 'wss://dashscope.aliyuncs.com/api-ws/v1/inference'

# VB-Cable 虚拟声卡（TTS → CABLE Input → CABLE Output → 直播姬）
# 优先 DirectSound / MME，避开 WASAPI（线程兼容性问题）
_CABLE_DEVICE_ID: int | None = None
_CABLE_DEVICE_SR: int | None = None


def _get_cable_device() -> tuple[int | None, int | None]:
    """查找 VB-Audio Virtual Cable 输出设备"""
    global _CABLE_DEVICE_ID, _CABLE_DEVICE_SR
    if _CABLE_DEVICE_ID is not None:
        return _CABLE_DEVICE_ID, _CABLE_DEVICE_SR

    candidates = []
    for i, d in enumerate(sd.query_devices()):
        name = d["name"]
        if d["max_output_channels"] == 0:
            continue
        if "CABLE Input" not in name and "CABLE In" not in name:
            continue
        if "Cable" not in name and "VB-Audio" not in name:
            continue

        hostapi = sd.query_hostapis(d["hostapi"])["name"]
        if "DirectSound" in hostapi:
            priority = 0
        elif "MME" in hostapi:
            priority = 1
        else:
            priority = 2
        candidates.append((priority, i, name, int(d["default_samplerate"])))

    if candidates:
        candidates.sort(key=lambda x: x[0])
        _, dev_id, name, sr = candidates[0]
        _CABLE_DEVICE_ID = dev_id
        _CABLE_DEVICE_SR = sr
        log.info(f"VB-Cable: [{dev_id}] {name} ({sr}Hz)")
        return dev_id, sr

    log.warning("VB-Cable not found, fallback to default speakers")
    return None, None


class TTSEngine:
    """AI主播语音引擎 — CosyVoice 3.5 云端流式"""

    def __init__(self):
        self.sample_rate = 24000  # CosyVoice 3.5 默认采样率
        log.info("CosyVoice 3.5 云端 TTS 就绪 (model=%s)", COSYVOICE_MODEL)

    def synthesize(self, text: str) -> np.ndarray:
        """云端合成，同步返回完整音频 → numpy (samples,) float32
        线程安全：每次调用创建独立事件循环（支持从线程池调用）"""
        import asyncio

        t0 = time.time()

        async def _synth():
            synthesizer = SpeechSynthesizer(
                model=COSYVOICE_MODEL,
                voice=COSYVOICE_VOICE_ID,
                format=AudioFormat.PCM_24000HZ_MONO_16BIT,
            )
            return synthesizer, synthesizer.call(text)

        # dashscope 需要事件循环；线程池里没有，每次创建新的
        loop = asyncio.new_event_loop()
        try:
            synthesizer, audio_bytes = loop.run_until_complete(_synth())
        finally:
            loop.close()

        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        dur = len(audio) / self.sample_rate
        delay = synthesizer.get_first_package_delay()
        rtf = (time.time() - t0) / dur if dur > 0 else 0
        log.debug(f"synthesized {len(text)} chars -> {dur:.1f}s, 首包={delay:.0f}ms, RTF={rtf:.2f}")
        return audio

    def play(self, audio: np.ndarray) -> float:
        """播放音频到 CABLE，返回时长（秒）"""
        audio = audio * TTS_VOLUME

        device_id, device_sr = _get_cable_device()
        target_sr = device_sr if device_id is not None else self.sample_rate

        sd.stop()

        # 重采样到设备原生采样率（polyphase 滤波器，比 FFT 干净）
        if self.sample_rate != target_sr:
            import fractions
            ratio = fractions.Fraction(target_sr, self.sample_rate)
            audio = scipy.signal.resample_poly(audio, ratio.numerator, ratio.denominator)

        duration = len(audio) / target_sr

        try:
            sd.play(audio, target_sr, device=device_id, latency='low')
            label = f'CABLE [{device_id}]' if device_id is not None else 'speakers'
            log.debug(f"playing {duration:.1f}s to {label} @ {target_sr}Hz")
        except Exception as e:
            log.error(f"play failed: {e}, retrying with default device")
            sd.play(audio, target_sr, latency='low')
        return duration

    def speak(self, text: str) -> float:
        """合成 + 播放（一体式，向后兼容）"""
        audio = self.synthesize(text)
        return self.play(audio)


# 全局单例
_engine: TTSEngine | None = None


def get_engine() -> TTSEngine:
    """获取 TTS 引擎单例（云端，无需加载模型）"""
    global _engine
    if _engine is None:
        _engine = TTSEngine()
    return _engine
