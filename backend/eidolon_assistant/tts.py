"""Fish Audio speech synthesis and cancellable playback.

Fish Audio needs the macOS system proxy on this machine (direct connections get
poisoned DNS); the proxy is detected at call time so a user changing network
settings does not need to restart the assistant.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import httpx
import numpy as np

from .config import TTSSettings
from .network import resolve_proxy


class TTSError(RuntimeError):
    pass


class FishTTS:
    def __init__(self, settings: TTSSettings, cache_dir: Path | None = None,
                 proxy_mode: str = "auto", proxy_url: str = "") -> None:
        self.settings = settings
        self.cache_dir = cache_dir
        self.proxy_mode = proxy_mode
        self.proxy_url = proxy_url

    def _cache_path(self, text: str, voice_id: str, model: str) -> Path | None:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha256(f"{model}|{voice_id}|{self.settings.format}|{text}".encode()).hexdigest()
        path = self.cache_dir / f"{digest}.{self.settings.format}"
        return path

    async def synthesize(self, text: str, voice_id: str, *, allow_cache: bool = True) -> bytes:
        text = text.strip()
        if not text:
            return b""
        if not self.settings.ready:
            raise TTSError(f"missing API key: {self.settings.api_key_env}")
        model = self.settings.model
        cache = self._cache_path(text, voice_id, model)
        if allow_cache and cache is not None and cache.exists() and cache.stat().st_size > 0:
            return cache.read_bytes()

        proxies: list[str | None] = []
        selected = resolve_proxy(self.proxy_mode, self.proxy_url)
        for candidate in (selected, None):
            if candidate not in proxies:
                proxies.append(candidate)
        body = {
            "text": text,
            "reference_id": voice_id,
            "format": self.settings.format,
            "latency": self.settings.latency,
        }
        if self.settings.speed and abs(self.settings.speed - 1.0) > 1e-3:
            body["prosody"] = {"speed": self.settings.speed}
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
            "model": model,
        }
        audio: bytes | None = None
        errors: list[str] = []
        last_error: Exception | None = None
        for proxy in proxies:
            for attempt in range(2):
                try:
                    async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(self.settings.timeout_s)) as client:
                        resp = await client.post(f"{self.settings.base_url.rstrip('/')}/v1/tts",
                                                 json=body, headers=headers)
                    if resp.status_code != 200:
                        detail = resp.text[:400]
                        raise TTSError(f"Fish Audio HTTP {resp.status_code}: {detail}")
                    audio = resp.content
                    break
                except (httpx.HTTPError, TTSError) as exc:
                    last_error = exc
                    errors.append(f"{'proxy' if proxy else 'direct'}#{attempt + 1}: {type(exc).__name__}: {exc}")
                    await asyncio.sleep(0.4 * (attempt + 1))
            if audio:
                break
        if audio is None:
            raise TTSError("Fish Audio 连接失败：" + " | ".join(errors[-4:])) from last_error
        if not audio:
            raise TTSError("Fish Audio 返回了空音频")
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(audio)
        return audio


def trim_to_wav(audio: bytes, cache_dir: Path | None, *, fmt: str = "mp3") -> Path | None:
    """Decode ``audio``, cut leading/trailing silence, and cache a 16 kHz wav.

    Fish Audio's mp3 carries ~0.1 s of leading and often ~0.4 s of trailing
    silence. Playing it as-is delays the recording after the wake reply by that
    much (plus the cooldown), which used to swallow the first words of a command.
    Returns ``None`` when trimming is unavailable so the caller can fall back.
    """
    if not audio:
        return None
    digest = hashlib.sha256(audio).hexdigest()
    if cache_dir is not None:
        cached = cache_dir / f"play-{digest}.wav"
        if cached.exists() and cached.stat().st_size > 0:
            return cached
    try:
        with tempfile.TemporaryDirectory(prefix="eidolon-trim-") as tmp:
            source = Path(tmp) / f"in.{fmt}"
            source.write_bytes(audio)
            decoded = Path(tmp) / "out.wav"
            subprocess.run(
                ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", str(source), str(decoded)],
                check=True, capture_output=True, timeout=20,
            )
            with wave.open(str(decoded)) as handle:
                sample_rate = handle.getframerate()
                raw = handle.readframes(handle.getnframes())
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            window = max(1, int(sample_rate * 0.01))
            frames = len(samples) // window
            if frames == 0:
                return None
            rms = np.array([
                float(np.sqrt(np.mean(samples[i * window:(i + 1) * window] ** 2) + 1e-12))
                for i in range(frames)
            ])
            threshold = max(0.0035, float(rms.max()) * 0.02)
            audible = np.where(rms > threshold)[0]
            if len(audible) == 0:
                return None
            start = max(0, (int(audible[0]) - 2)) * window
            end = min(len(samples), (int(audible[-1]) + 3) * window)
            trimmed = samples[start:end]
            target_dir = cache_dir or Path(tempfile.mkdtemp(prefix="eidolon-play-"))
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"play-{digest}.wav"
            with wave.open(str(target), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(sample_rate)
                out.writeframes((np.clip(trimmed, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes())
            return target
    except (OSError, subprocess.SubprocessError, wave.Error, ValueError):
        return None


class Player:
    """Plays encoded audio through ``afplay`` so it can be stopped instantly."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._tmpdir = Path(tempfile.mkdtemp(prefix="eidolon-tts-"))
        self._cache_dir = cache_dir
        self._lock = asyncio.Lock()

    @property
    def playing(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def play_bytes(self, audio: bytes, fmt: str = "mp3") -> None:
        if not audio:
            return
        trimmed = await asyncio.to_thread(trim_to_wav, audio, self._cache_dir, fmt=fmt)
        if trimmed is not None:
            await self.play_file(str(trimmed))
            return
        target = self._tmpdir / f"reply-{os.getpid()}-{id(audio)}.{fmt}"
        target.write_bytes(audio)
        await self.play_file(str(target))

    async def play_file(self, path: str) -> None:
        async with self._lock:
            if self._proc and self._proc.returncode is None:
                await self.stop()
            proc = await asyncio.create_subprocess_exec(
                "afplay", path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._proc = proc
            try:
                await proc.wait()
            except asyncio.CancelledError:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                raise
            finally:
                if self._proc is proc:
                    self._proc = None

    async def stop(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=1.5)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def cleanup(self) -> None:
        shutil.rmtree(self._tmpdir, ignore_errors=True)
