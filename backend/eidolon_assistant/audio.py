"""Microphone capture, local wake-word feed, VAD recording and playback.

Threading model: PortAudio calls :meth:`AudioEngine._callback` on its own thread;
the KWS model is fed right there (a few milliseconds per 100 ms block) and any
detected label is handed back to the asyncio loop. Recording is finished by the
same callback when it sees enough trailing silence.

Echo policy: wake-word spotting is suspended for the whole of every TTS
playback plus a cooldown, which is the reliable fallback the design asks for.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd

from .config import AudioSettings, RecordingSettings
from .kws import WakeSpotter
from .tts import Player


class AudioUnavailable(RuntimeError):
    pass


class AudioEngine:
    def __init__(self, audio: AudioSettings, recording: RecordingSettings, *,
                 spotter: WakeSpotter | None, on_label, loop: asyncio.AbstractEventLoop,
                 tts_cache_dir=None) -> None:
        self.audio = audio
        self.recording = recording
        self.spotter = spotter
        self._on_label = on_label
        self._loop = loop
        self._stream: sd.InputStream | None = None
        self._paused = threading.Event()
        self._lock = threading.Lock()
        self._recording = False
        self._voice_seen = False
        self._last_voice = 0.0
        self._record_start = 0.0
        self._buffer: list[np.ndarray] = []
        self._preroll: deque[np.ndarray] = deque(maxlen=4)
        self._record_future: asyncio.Future | None = None
        self._manual = False
        self._manual_buffer: list[np.ndarray] = []
        self._manual_start = 0.0
        self.level = 0.0
        self.player = Player(cache_dir=tts_cache_dir)
        self._error: str | None = None

    # ---------------------------------------------------------------- device
    @staticmethod
    def list_input_devices() -> list[dict]:
        devices = []
        for index, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) > 0:
                devices.append({"index": index, "name": dev["name"],
                                "default": index == sd.default.device[0]})
        return devices

    def resolve_input(self) -> int | None:
        wanted = (self.audio.input_device or "").strip()
        if not wanted:
            return None
        try:
            return int(wanted)
        except ValueError:
            pass
        for dev in self.list_input_devices():
            if wanted.lower() in dev["name"].lower():
                return int(dev["index"])
        raise AudioUnavailable(f"找不到输入设备：{wanted}")

    # ---------------------------------------------------------------- stream
    def start(self) -> None:
        device = self.resolve_input()
        try:
            self._stream = sd.InputStream(
                samplerate=self.audio.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=self.audio.block_frames,
                device=device,
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001 - PortAudio raises many types
            self._error = f"{type(exc).__name__}: {exc}"
            raise AudioUnavailable(f"麦克风打不开：{self._error}") from exc

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        self.cancel_recording()

    # -------------------------------------------------------------- callback
    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        try:
            samples = indata[:, 0].astype(np.float32, copy=True)
            self.level = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
            self._preroll.append(samples)
            if self._recording:
                self._track_recording(samples)
            if self._manual:
                self._manual_buffer.append(samples)
                if time.monotonic() - self._manual_start > 30.0:
                    self._manual = False
            if not self._paused.is_set() and self.spotter is not None:
                for label in self.spotter.feed(samples, self.audio.sample_rate):
                    self._loop.call_soon_threadsafe(self._on_label, label)
        except Exception as exc:  # noqa: BLE001 - never kill the audio thread
            self._error = f"audio callback: {type(exc).__name__}: {exc}"

    def _track_recording(self, samples: np.ndarray) -> None:
        now = time.monotonic()
        rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
        if not self._voice_seen:
            if rms >= self.recording.vad_threshold:
                self._voice_seen = True
                self._last_voice = now
                self._buffer = list(self._preroll)
            elif now - self._record_start >= self.recording.start_timeout_s:
                self._finish_recording(None)
            return
        if rms >= self.recording.vad_threshold:
            self._last_voice = now
        self._buffer.append(samples)
        voiced_ms = (self._last_voice - self._record_start) * 1000
        trailing_ms = (now - self._last_voice) * 1000
        if trailing_ms >= self.recording.silence_ms and voiced_ms >= self.recording.min_speech_ms:
            self._finish_recording(self._collect())
        elif now - self._record_start >= self.recording.max_utterance_s:
            self._finish_recording(self._collect())

    def _collect(self) -> bytes:
        if not self._buffer:
            return b""
        audio = np.concatenate(self._buffer)
        pcm = np.clip(audio, -1.0, 1.0)
        return (pcm * 32767.0).astype(np.int16).tobytes()

    def _finish_recording(self, pcm: bytes | None) -> None:
        if not self._recording:
            return
        self._recording = False
        self._buffer = []
        self._voice_seen = False
        future = self._record_future
        if future is not None and not future.done():
            self._loop.call_soon_threadsafe(self._resolve_record, future, pcm)

    @staticmethod
    def _resolve_record(future: asyncio.Future, pcm: bytes | None) -> None:
        if not future.done():
            future.set_result(pcm)

    # ------------------------------------------------------------- recording
    async def record_utterance(self) -> bytes | None:
        self.cancel_recording()
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        with self._lock:
            self._record_future = future
            self._buffer = []
            self._voice_seen = False
            self._record_start = time.monotonic()
            self._last_voice = 0.0
            self._preroll.clear()
            self._recording = True
        try:
            return await future
        finally:
            with self._lock:
                self._recording = False
                self._record_future = None

    # --------------------------------------------------------------- push-to-talk
    def start_manual(self) -> None:
        with self._lock:
            self._manual_buffer = []
            self._manual_start = time.monotonic()
            self._manual = True

    def stop_manual(self) -> bytes | None:
        with self._lock:
            self._manual = False
            buffer = self._manual_buffer
            self._manual_buffer = []
        if not buffer:
            return None
        audio = np.concatenate(buffer)
        pcm = np.clip(audio, -1.0, 1.0)
        return (pcm * 32767.0).astype(np.int16).tobytes()

    def cancel_recording(self) -> None:
        with self._lock:
            self._recording = False
            self._manual = False
            self._manual_buffer = []
            self._buffer = []
            self._voice_seen = False
            future, self._record_future = self._record_future, None
        if future is not None and not future.done():
            future.set_result(None)

    # ------------------------------------------------------------- playback
    def suspend_wake(self) -> None:
        self._paused.set()

    def resume_wake(self) -> None:
        self._paused.clear()
        if self.spotter is not None:
            self.spotter.reset()

    async def speak_audio(self, audio: bytes, *, resume: bool = True,
                          cooldown_ms: int | None = None) -> None:
        if not audio:
            return
        self.suspend_wake()
        cooldown = self.audio.wake_pause_after_tts_ms if cooldown_ms is None else cooldown_ms
        try:
            await self.player.play_bytes(audio, fmt="mp3")
        finally:
            try:
                if cooldown > 0:
                    await asyncio.sleep(cooldown / 1000.0)
            finally:
                if resume:
                    self.resume_wake()

    async def speak_file(self, path: str) -> None:
        self.suspend_wake()
        try:
            await self.player.play_file(path)
        finally:
            await asyncio.sleep(self.audio.wake_pause_after_tts_ms / 1000.0)
            self.resume_wake()

    async def stop_playback(self) -> None:
        await self.player.stop()

    def close(self) -> None:
        self.stop()
        self.player.cleanup()

    @property
    def error(self) -> str | None:
        return self._error
