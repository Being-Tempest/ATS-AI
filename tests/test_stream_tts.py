import asyncio
import time
import unittest
from array import array
from contextlib import suppress
from unittest.mock import patch

from scheduler import Scheduler
from score_queue import QueueItem
from tts_stream import ContinuousResampler, PlaybackHandle


class FakeSynthesizer:
    def __init__(self, callback, chunks, delay=0):
        self.callback = callback
        self.chunks = chunks
        self.delay = delay

    def streaming_call(self, text):
        pass

    def streaming_complete(self):
        for index, chunk in enumerate(self.chunks):
            if index and self.delay:
                time.sleep(self.delay)
            self.callback.on_data(chunk)

    def streaming_cancel(self, timeout):
        pass


class FakeOutputStream:
    instances = []
    fail = False

    def __init__(self, **kwargs):
        if self.fail:
            raise OSError("unsupported stream")
        self.kwargs = kwargs
        self.writes = []
        self.started = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def write(self, data):
        self.writes.append(data)

    def stop(self):
        pass

    def abort(self):
        pass

    def close(self):
        pass


class StreamPlaybackTests(unittest.TestCase):
    def setUp(self):
        FakeOutputStream.instances = []
        FakeOutputStream.fail = False
        self.pcm = array("h", [12000] * 480).tobytes()

    def make_handle(self, chunks, delay=0, **kwargs):
        def synthesizer_factory(callback):
            return FakeSynthesizer(callback, chunks, delay)

        return PlaybackHandle(
            "测试",
            device_resolver=lambda: (None, None),
            fallback_player=kwargs.pop("fallback_player", lambda audio: len(audio) / 24000),
            audio_tap=kwargs.pop("audio_tap", None),
            prebuffer_seconds=kwargs.pop("prebuffer_seconds", 0),
            synthesizer_factory=synthesizer_factory,
            first_chunk_timeout=1,
            **kwargs,
        )

    def test_resampler_preserves_state_across_chunks(self):
        resampler = ContinuousResampler(24000, 48000)
        output = resampler.feed(self.pcm[:480])
        output += resampler.feed(self.pcm[480:])
        output += resampler.flush()
        self.assertIsNotNone(resampler._state)
        self.assertAlmostEqual(len(output) / 2, 960, delta=4)

    def test_first_chunk_plays_before_synthesis_completes(self):
        levels = []
        handle = self.make_handle([self.pcm, self.pcm], delay=0.35,
                                  audio_tap=levels.append)
        with patch("tts_stream.sd.RawOutputStream", FakeOutputStream):
            handle.start()
            self.assertTrue(handle._first_audio.wait(0.5))
            self.assertFalse(handle._producer_done.is_set())
            handle.wait(2)
        self.assertTrue(FakeOutputStream.instances[0].writes)
        self.assertGreater(handle.first_chunk_delay, 0)
        self.assertLess(handle.first_audio_delay, 0.35)
        self.assertEqual(levels[-1], 0)
        self.assertTrue(any(level > 0 for level in levels))
        self.assertAlmostEqual(handle.duration, 0.04, places=3)

    def test_unsupported_stream_falls_back_to_full_buffer(self):
        played = []
        levels = []

        def fallback(audio):
            played.append(audio)
            return len(audio) / 24000

        FakeOutputStream.fail = True
        handle = self.make_handle([self.pcm, self.pcm], fallback_player=fallback,
                                  audio_tap=levels.append)
        with patch("tts_stream.sd.RawOutputStream", FakeOutputStream), \
                patch("tts_stream.sd.wait", return_value=None):
            handle.wait(2)
        self.assertEqual(len(played), 1)
        self.assertEqual(len(played[0]), 960)
        self.assertEqual(levels[-1], 0)


class FakePlayback:
    def __init__(self):
        self.duration = 0.01
        self.done = False
        self.error = None
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.done = True


class SchedulerPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_estimated_deadline_does_not_interrupt_real_playback(self):
        scheduler = Scheduler(tts=object())
        playback = FakePlayback()
        item = QueueItem(score=50, item_type="danmaku", username="甲", text="测试")
        scheduler.next_ready = (item, playback, "回复", "happy")
        with patch("scheduler.REPLY_GAP_MIN", 0), patch("scheduler.REPLY_GAP_MAX", 0), \
                patch("scheduler.show_subtitle"), patch("scheduler.clear_subtitle"), \
                patch("scheduler.clear_all"):
            task = asyncio.create_task(scheduler.run())
            try:
                for _ in range(20):
                    await asyncio.sleep(0.01)
                    if playback.started:
                        break
                self.assertTrue(playback.started)
                await asyncio.sleep(0.08)
                self.assertGreater(scheduler.playing_until, 0)
                self.assertIs(scheduler.current_playback, playback)
                playback.done = True
                for _ in range(20):
                    await asyncio.sleep(0.01)
                    if scheduler.playing_until == 0:
                        break
                self.assertEqual(scheduler.playing_until, 0)
                self.assertIsNone(scheduler.current_playback)
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                await scheduler.close()
                scheduler._pool.shutdown(wait=False)


if __name__ == "__main__":
    unittest.main()
