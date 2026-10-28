import audioop
import queue
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd
from dashscope.audio.tts_v2 import AudioFormat, ResultCallback, SpeechSynthesizer

from config import COSYVOICE_MODEL, COSYVOICE_VOICE_ID, TTS_VOLUME
from logger import get_logger

log = get_logger("tts.stream")


class ContinuousResampler:
    def __init__(self, source_rate: int, target_rate: int):
        self.source_rate = source_rate
        self.target_rate = target_rate
        self._state = None

    def feed(self, pcm: bytes) -> bytes:
        if not pcm or self.source_rate == self.target_rate:
            return pcm
        converted, self._state = audioop.ratecv(
            pcm, 2, 1, self.source_rate, self.target_rate, self._state)
        return converted

    def flush(self) -> bytes:
        if self.source_rate == self.target_rate:
            return b""
        converted, self._state = audioop.ratecv(
            b"", 2, 1, self.source_rate, self.target_rate, self._state)
        return converted


class _ChunkCallback(ResultCallback):
    def __init__(self, owner):
        self.owner = owner

    def on_data(self, data: bytes):
        self.owner._on_chunk(data)

    def on_error(self, message):
        self.owner._on_error(RuntimeError(str(message)))


class PlaybackHandle:
    def __init__(self, text: str, device_resolver, fallback_player,
                 audio_tap=None, sample_rate: int = 24000,
                 volume: float = TTS_VOLUME, prebuffer_seconds: float = 0.12,
                 synthesizer_factory=None, first_chunk_timeout: float = 30.0):
        self.text = text
        self.sample_rate = sample_rate
        self.volume = volume
        self.prebuffer_seconds = prebuffer_seconds
        self.first_chunk_timeout = first_chunk_timeout
        self.first_chunk_delay = None
        self.first_audio_delay = None
        self._created_at = time.perf_counter()
        self._device_resolver = device_resolver
        self._fallback_player = fallback_player
        self._audio_tap = audio_tap
        self._synthesizer_factory = synthesizer_factory or self._default_synthesizer
        self._synthesizer = None
        self._queue = queue.Queue()
        self._raw_chunks = []
        self._producer_done = threading.Event()
        self._started = threading.Event()
        self._done = threading.Event()
        self._stop_requested = threading.Event()
        self._first_audio = threading.Event()
        self._stream = None
        self._stream_lock = threading.Lock()
        self._fallback_active = threading.Event()
        self._error = None
        self._playback_thread = None
        self._producer = threading.Thread(
            target=self._produce, name="tts-synthesis", daemon=True)
        self._producer.start()

    @staticmethod
    def _default_synthesizer(callback):
        return SpeechSynthesizer(
            model=COSYVOICE_MODEL,
            voice=COSYVOICE_VOICE_ID,
            format=AudioFormat.PCM_24000HZ_MONO_16BIT,
            callback=callback,
        )

    @property
    def done(self):
        return self._done.is_set()

    @property
    def error(self):
        return self._error

    @property
    def duration(self):
        if self._producer_done.is_set() and self._raw_chunks:
            return sum(map(len, self._raw_chunks)) / (self.sample_rate * 2)
        return max(2.0, len(self.text) / 5.0)

    def _on_chunk(self, data):
        if self._stop_requested.is_set() or not data:
            return
        data = bytes(data)
        if not self._raw_chunks:
            self.first_chunk_delay = time.perf_counter() - self._created_at
        self._raw_chunks.append(data)
        self._queue.put(data)

    def _on_error(self, error):
        if self._error is None:
            self._error = error

    def _produce(self):
        try:
            if self._stop_requested.is_set():
                return
            self._synthesizer = self._synthesizer_factory(_ChunkCallback(self))
            self._synthesizer.streaming_call(self.text)
            self._synthesizer.streaming_complete()
        except Exception as exc:
            self._on_error(exc)
        finally:
            self._producer_done.set()
            self._queue.put(None)

    def start(self):
        if self._started.is_set():
            return self
        self._started.set()
        self._playback_thread = threading.Thread(
            target=self._play, name="tts-playback", daemon=True)
        self._playback_thread.start()
        return self

    def wait(self, timeout=None):
        self.start()
        if not self._done.wait(timeout):
            raise TimeoutError("TTS 播放等待超时")
        if self._error is not None:
            raise RuntimeError(f"TTS 播放失败: {self._error}")
        return self.duration

    def stop(self):
        self._stop_requested.set()
        self._emit_level(b"")
        synthesizer = self._synthesizer
        if synthesizer is not None and not self._producer_done.is_set():
            try:
                synthesizer.streaming_cancel(1000)
            except Exception:
                pass
        with self._stream_lock:
            stream = self._stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
        elif self._fallback_active.is_set():
            sd.stop()
        if not self._started.is_set():
            self._done.set()

    def _emit_level(self, pcm):
        if self._audio_tap is None:
            return
        try:
            if pcm:
                pcm = audioop.mul(pcm, 2, self.volume)
                level = min(1.0, audioop.rms(pcm, 2) / 32768.0 * 8.0)
            else:
                level = 0.0
            self._audio_tap(level)
        except Exception as exc:
            log.warning("音频口型回调失败: %s", exc)
            self._audio_tap = None

    def _mark_first_audio(self):
        if not self._first_audio.is_set():
            self._first_audio.set()
            self.first_audio_delay = time.perf_counter() - self._created_at
            log.info("TTS 首块音频: %.3fs（首包 %.3fs）",
                     self.first_audio_delay, self.first_chunk_delay or 0.0)

    def _play(self):
        stream = None
        frames_written = 0
        try:
            device_id, device_sr = self._device_resolver()
            target_rate = device_sr if device_id is not None else self.sample_rate
            resampler = ContinuousResampler(self.sample_rate, target_rate)
            prebuffer = bytearray()
            source_ended = False
            prebuffer_bytes = int(self.sample_rate * 2 * self.prebuffer_seconds)

            while (not prebuffer or len(prebuffer) < prebuffer_bytes) and not source_ended:
                if self._stop_requested.is_set():
                    return
                try:
                    chunk = self._queue.get(timeout=self.first_chunk_timeout)
                except queue.Empty:
                    raise TimeoutError("等待首个 TTS 音频块超时")
                if chunk is None:
                    source_ended = True
                else:
                    prebuffer.extend(chunk)

            if not prebuffer:
                if self._error is not None:
                    raise self._error
                return

            try:
                stream = sd.RawOutputStream(
                    samplerate=target_rate,
                    blocksize=int(target_rate * 0.02),
                    device=device_id,
                    channels=1,
                    dtype="int16",
                    latency="low",
                )
                with self._stream_lock:
                    self._stream = stream
                stream.start()
            except Exception:
                if stream is not None:
                    stream.close()
                self._play_fallback()
                return

            pending = bytearray()
            inputs = deque([bytes(prebuffer)])
            flushed = False
            block_bytes = int(target_rate * 0.02) * 2
            while not self._stop_requested.is_set():
                if inputs:
                    chunk = inputs.popleft()
                elif not source_ended:
                    try:
                        chunk = self._queue.get(timeout=0.1)
                    except queue.Empty:
                        self._emit_level(b"")
                        continue
                    if chunk is None:
                        source_ended = True
                        chunk = b""
                else:
                    chunk = b""

                if chunk:
                    pending.extend(resampler.feed(chunk))
                if source_ended and not flushed:
                    pending.extend(resampler.flush())
                    flushed = True

                while pending and (len(pending) >= block_bytes or flushed):
                    size = min(block_bytes, len(pending))
                    size -= size % 2
                    block = bytes(pending[:size])
                    del pending[:size]
                    stream.write(block)
                    frames_written += len(block) // 2
                    self._mark_first_audio()
                    self._emit_level(block)

                if source_ended and flushed and not pending:
                    break

            if self._stop_requested.is_set():
                stream.abort()
            else:
                stream.stop()
        except Exception as exc:
            if frames_written == 0 and not self._stop_requested.is_set():
                try:
                    log.warning("流式播放失败，回退整段播放: %s", exc)
                    self._play_fallback()
                except Exception as fallback_exc:
                    self._on_error(fallback_exc)
            else:
                self._on_error(exc)
        finally:
            with self._stream_lock:
                self._stream = None
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            self._emit_level(b"")
            self._done.set()

    def _play_fallback(self):
        if not self._producer_done.wait(60):
            raise TimeoutError("等待完整 TTS 音频超时")
        if not self._raw_chunks:
            if self._error is not None:
                raise self._error
            return
        pcm = b"".join(self._raw_chunks)
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        self._fallback_active.set()
        try:
            duration = self._fallback_player(audio)
            block_bytes = int(self.sample_rate * 0.02) * 2
            started = time.monotonic()
            for offset in range(0, len(pcm), block_bytes):
                if self._stop_requested.is_set():
                    sd.stop()
                    break
                self._mark_first_audio()
                self._emit_level(pcm[offset:offset + block_bytes])
                target = started + (offset + block_bytes) / (self.sample_rate * 2)
                time.sleep(max(0, min(0.02, target - time.monotonic())))
            if not self._stop_requested.is_set():
                sd.wait()
            return duration
        finally:
            self._fallback_active.clear()
