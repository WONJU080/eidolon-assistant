"""Speech recognition through Alibaba Cloud Model Studio (DashScope) realtime ASR.

Protocol (as used by the older eidolon-study project, model
``qwen-audio-3.1-asr-flash-streaming``): a duplex WebSocket task opened with
``run-task``, fed 16 kHz 16-bit mono PCM in 100 ms packets, closed with
``finish-task``; sentences arrive as ``result-generated`` events, final text at
``sentence_end``.

The assistant records an utterance locally and then transcribes it, so this
client streams a finished recording rather than an open microphone.
"""

from __future__ import annotations

import asyncio
import json
import time
import unicodedata
import uuid
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from .config import STTSettings

REGION_URLS = {
    "beijing": "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
    "singapore": "wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference",
}
PACKET_BYTES = 3200  # 100 ms of 16 kHz 16-bit mono


class STTError(RuntimeError):
    pass


class _Refused(Exception):
    """The service refused this key/model; another region will not help."""


def _event(raw: str | bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _has_words(text: str) -> bool:
    return any(unicodedata.category(c)[0] in "LN" for c in text)


class DashScopeSTT:
    def __init__(self, settings: STTSettings) -> None:
        self.settings = settings

    async def transcribe_pcm(self, pcm: bytes, sample_rate: int = 16000) -> str:
        if not pcm:
            return ""
        if not self.settings.ready:
            raise STTError(f"missing API key: {self.settings.api_key_env}")
        regions = [self.settings.region] + [r for r in REGION_URLS if r != self.settings.region]
        errors: list[str] = []
        for region in regions:
            try:
                return await self._transcribe_in(region, pcm, sample_rate)
            except _Refused as exc:
                errors.append(f"{region}: {exc}")
            except (OSError, TimeoutError, WebSocketException, ConnectionClosed) as exc:
                errors.append(f"{region}: {type(exc).__name__}: {exc}")
        raise STTError("语音识别失败：" + "；".join(errors))

    async def _transcribe_in(self, region: str, pcm: bytes, sample_rate: int) -> str:
        url = REGION_URLS[region]
        task_id = uuid.uuid4().hex
        headers = {"Authorization": f"bearer {self.settings.api_key}"}
        finals: list[str] = []
        last_text = ""
        try:
            # proxy=False/None: DashScope is domestic; never route it through the
            # system proxy (urllib on macOS reads System Configuration SOCKS).
            ws = await connect(url, additional_headers=headers, open_timeout=10,
                               close_timeout=3, max_size=16 * 1024 * 1024, proxy=None)
        except InvalidStatus as exc:
            status = getattr(exc.response, "status_code", 0)
            if status in (401, 403):
                raise _Refused(f"HTTP {status}") from exc
            raise
        try:
            parameters = {
                "format": "pcm",
                "sample_rate": sample_rate,
                "language_hints": list(self.settings.language_hints),
                "heartbeat": True,
                "vad_model": "near_meeting_16k",
            }
            await ws.send(json.dumps({
                "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                "payload": {
                    "task_group": "audio",
                    "task": "asr",
                    "function": "recognition",
                    "model": self.settings.model,
                    "parameters": parameters,
                    "input": {},
                },
            }))
            started = False
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                header = _event(raw).get("header", {})
                kind = header.get("event")
                if kind == "task-started":
                    started = True
                    break
                if kind == "task-failed":
                    raise _Refused(f"{header.get('error_code')}: {header.get('error_message')}")
            if not started:
                raise TimeoutError("DashScope 识别任务没有启动")

            for offset in range(0, len(pcm), PACKET_BYTES):
                await ws.send(pcm[offset:offset + PACKET_BYTES])
                await asyncio.sleep(0.005)
            await ws.send(json.dumps({
                "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
                "payload": {"input": {}},
            }))

            finish_deadline = time.monotonic() + max(10.0, len(pcm) / (sample_rate * 2) * 2)
            while time.monotonic() < finish_deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, finish_deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                event = _event(raw)
                header = event.get("header", {})
                kind = header.get("event")
                if kind == "task-finished":
                    break
                if kind == "task-failed":
                    raise _Refused(f"{header.get('error_code')}: {header.get('error_message')}")
                if kind == "result-generated":
                    sentence = event.get("payload", {}).get("output", {}).get("sentence") or {}
                    if not isinstance(sentence, dict) or sentence.get("heartbeat"):
                        continue
                    text = (sentence.get("text") or "").strip()
                    if not _has_words(text):
                        continue
                    if sentence.get("sentence_end"):
                        finals.append(text)
                        last_text = ""
                    else:
                        last_text = text
        finally:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        result = "".join(finals).strip()
        return result or last_text.strip()
