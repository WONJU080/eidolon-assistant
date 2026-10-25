"""The assistant's turn pipeline: wake -> record -> STT -> route -> harness -> voice.

State machine (shown in the UI):
    idle → listening → transcribing → thinking → executing → waiting_confirmation
         → speaking → idle, plus cancelled and error.

Two paths reach the harness:
* the role path (judge intent, normalise the command, then summarise the result in
  character);
* **direct** (bypass the role: send the text as-is, still permission-checked,
  cancellable and logged).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import replace
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .audio import AudioEngine, AudioUnavailable
from .config import Character, Settings, update_local_settings
from .harness_bridge import HarnessBridge, HarnessStartError, TaskResult
from .kws import KwsModel, WakeSpotter
from .llm import Bilingual, RoleLLM, TaskSpec
from .pinyin import PinyinError
from .redact import clip, redact
from .stt import DashScopeSTT, STTError
from .store import Store
from .tts import FishTTS, TTSError, trim_to_wav

log = logging.getLogger("eidolon.orchestrator")

CANCEL_WORDS = {"停", "停止", "取消", "中断", "别做了", "stop", "cancel"}
ALLOW_WORDS = {"可以", "允许", "继续", "好的", "好", "行", "嗯"}
DENY_WORDS = {"不要", "不行", "拒绝", "不对", "不好"}
MAX_PENDING_CHARS = 280


@dataclass
class PendingQuestion:
    id: str
    kind: str  # "task" | "permission"
    future: asyncio.Future
    allow_words: set[str]
    deny_words: set[str]
    payload: dict[str, Any] = field(default_factory=dict)


class Orchestrator:
    def __init__(self, settings: Settings, character: Character, store: Store,
                 broadcaster: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self.settings = settings
        self.character = character
        self.store = store
        self._broadcast = broadcaster
        self.llm = RoleLLM(settings.llm, character)
        self.tts = FishTTS(settings.tts, cache_dir=settings.data_dir / "cache" / "tts")
        self.stt = DashScopeSTT(settings.stt)
        self.audio: AudioEngine | None = None
        self.bridge = HarnessBridge(
            settings.harness, settings.workspace_dir, self._on_harness_event,
            permission_timeout_s=settings.permissions.timeout_s,
            max_event_text=settings.max_event_text,
        )
        self.state = "idle"
        self.state_detail = ""
        self.voice_ready = False
        self.wake_ready = False
        self.last_error: str | None = None
        self.current_task_id: str | None = None
        self.current_task_text: str = ""
        self._turn_lock = asyncio.Lock()
        self._voice_lock = asyncio.Lock()
        self._pending: PendingQuestion | None = None
        self._clarification: dict[str, Any] | None = None
        self._cancel_requested = False
        self._voice_pipeline: asyncio.Task | None = None
        self._wake_audio: bytes | None = None
        self._character_gen = 0
        self._audio_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._starting = False

    # ------------------------------------------------------------------ setup
    async def startup(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self._emit({"type": "harness", "status": "starting"})
        asyncio.create_task(self._start_harness_safe())
        asyncio.create_task(self._prepare_wake_audio())
        # The microphone is needed for 开始听 / 按住说话 even when the wake word
        # is disabled, so audio always starts; only the spotter is optional.
        await self._start_audio()

    async def _start_harness_safe(self) -> None:
        try:
            await self.bridge.start()
        except HarnessStartError as exc:
            self.last_error = f"{exc}"
            await self._emit({"type": "message", "role": "system", "level": "error",
                              "text": f"DeepSeek Harness 启动失败：{exc}"})
            if exc.stderr_tail:
                await self._emit({"type": "message", "role": "system", "level": "error",
                                  "text": clip("\n".join(exc.stderr_tail), 1200)})

    async def _prepare_wake_audio(self, generation: int | None = None) -> None:
        generation = self._character_gen if generation is None else generation
        try:
            wake_text = self.character.wake_reply_tts or self.character.wake_reply_zh
            audio = await self.tts.synthesize(wake_text, self.character.voice_id)
            if generation != self._character_gen:
                return
            self._wake_audio = audio
            # prewarm the trimmed wav so the first wake reply is not delayed
            await asyncio.to_thread(trim_to_wav, audio,
                                    self.settings.data_dir / "cache" / "tts" / "play")
        except TTSError as exc:
            if generation != self._character_gen:
                return
            self._wake_audio = None
            self.last_error = str(exc)
            await self._emit({"type": "message", "role": "system", "level": "warn",
                              "text": f"唤醒回应语音不可用：{exc}"})

    async def _start_audio(self) -> None:
        if self.audio is not None:
            self.audio.close()
            self.audio = None
        self.wake_ready = False
        self.voice_ready = False
        spotter = None
        if self.settings.wake.enabled:
            model = KwsModel(self.settings.models_dir / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01")
            if not model.ready():
                self.last_error = f"KWS 模型缺失：{model.dir}"
                await self._emit({"type": "message", "role": "system", "level": "warn",
                                  "text": f"本地唤醒词模型缺失，语音唤醒不可用（开始听/按住说话仍可用）：{model.dir}"})
            else:
                labels: dict[str, str] = {}
                for word in self.character.wake_words:
                    labels[f"wake|{word}"] = word
                for word in self.settings.wake.interrupt_words:
                    labels[f"interrupt|{word}"] = word
                for word in self.settings.wake.confirm_words:
                    labels[f"confirm|{word}"] = word
                try:
                    spotter = WakeSpotter(model, labels, self.settings.wake,
                                          self.settings.data_dir / "kws_keywords.txt")
                except (PinyinError, FileNotFoundError) as exc:
                    self.last_error = str(exc)
                    await self._emit({"type": "message", "role": "system", "level": "warn",
                                      "text": f"唤醒词配置失败（开始听/按住说话仍可用）：{exc}"})
        loop = asyncio.get_running_loop()
        self.audio = AudioEngine(
            self.settings.audio, self.settings.recording,
            spotter=spotter, on_label=self._on_kws_label, loop=loop,
            tts_cache_dir=self.settings.data_dir / "cache" / "tts" / "play",
        )
        try:
            self.audio.start()
            self.voice_ready = True
            self.wake_ready = spotter is not None
            if spotter is not None:
                await self._emit({"type": "message", "role": "system", "level": "info",
                                  "text": f"本地唤醒词已就绪：{' / '.join(self.character.wake_words)}"})
            else:
                await self._emit({"type": "message", "role": "system", "level": "info",
                                  "text": "麦克风已就绪；本地唤醒词未启用，使用「开始听」或「按住说话」。"})
        except AudioUnavailable as exc:
            self.audio = None
            self.wake_ready = False
            self.voice_ready = False
            self.last_error = str(exc)
            await self._emit({"type": "message", "role": "system", "level": "warn",
                              "text": f"麦克风不可用，语音功能关闭：{exc}"})

    async def shutdown(self) -> None:
        if self.audio is not None:
            await self.audio.stop_playback()
            self.audio.close()
            self.audio = None
        await self.bridge.close()
        await self.llm.aclose()
        self.store.close()

    # ------------------------------------------------------------------ state
    async def _set_state(self, state: str, detail: str = "") -> None:
        if state == self.state and detail == self.state_detail:
            return
        self.state = state
        self.state_detail = detail
        await self._emit({"type": "state", "state": state, "detail": detail})

    async def _emit(self, event: dict[str, Any]) -> None:
        event.setdefault("at", time.time())
        try:
            await self._broadcast(event)
        except Exception:  # noqa: BLE001 - a broken socket must not break a turn
            pass

    async def _message(self, role: str, text: str, *, level: str = "info",
                       speak: bool = False, extra: dict | None = None) -> None:
        payload = {"type": "message", "role": role, "text": text, "level": level, "speak": speak}
        if extra:
            payload.update(extra)
        await self._emit(payload)

    def _spoken_text(self, reply: Bilingual) -> str:
        """Which line goes to TTS: the character's configured language."""
        if self.character.tts_language == "zh":
            return reply.zh or reply.ja
        return reply.ja or reply.zh

    async def _say(self, reply: Bilingual, *, speak: bool = True, tag: str = "",
                   cooldown_ms: int | None = None) -> None:
        await self._emit({"type": "message", "role": "assistant", "ja": reply.ja, "zh": reply.zh,
                          "text": reply.display, "speak": speak, "tag": tag})
        if not speak or self.audio is None:
            return
        try:
            audio = await self.tts.synthesize(self._spoken_text(reply), self.character.voice_id)
        except TTSError as exc:
            await self._emit({"type": "message", "role": "system", "level": "warn",
                              "text": f"语音合成失败（字幕仍可用）：{exc}"})
            return
        await self._set_state("speaking")
        try:
            await self.audio.speak_audio(audio, cooldown_ms=cooldown_ms)
        finally:
            if self.state == "speaking":
                await self._set_state("idle")

    # ------------------------------------------------------------- public API
    def public_state(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "detail": self.state_detail,
            "character": self.character.id,
            "voice_ready": self.voice_ready,
            "wake_ready": self.wake_ready,
            "last_error": redact(self.last_error or ""),
            "current_task_id": self.current_task_id,
            "current_task": redact(self.current_task_text),
            "harness": self.bridge.public_state(),
            "pending": self._pending_public(),
            "audio": {
                "input_devices": AudioEngine.list_input_devices() if self.audio else [],
                "level": round(self.audio.level, 4) if self.audio else 0.0,
            },
        }

    def _pending_public(self) -> dict[str, Any] | None:
        if self._pending is None:
            return None
        return {k: v for k, v in self._pending.payload.items() if k != "future"}

    async def handle_text(self, text: str, source: str = "text") -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "reason": "empty"}
        if text.lower() in CANCEL_WORDS:
            await self.cancel()
            return {"ok": True, "cancelled": True}
        clarification = self._clarification
        if (clarification is not None and source != "direct"
                and not clarification["future"].done()):
            clarification["future"].set_result(text)
            return {"ok": True, "answered": "clarification"}
        if self._pending is not None and source != "direct":
            answer = self._answer_from_text(text)
            if answer is not None:
                return {"ok": True, "answered": answer}
        if self._turn_lock.locked():
            return {"ok": False, "reason": "busy"}
        async with self._turn_lock:
            try:
                await self._turn(text, source)
                return {"ok": True}
            except Exception as exc:  # noqa: BLE001 - surface, never crash the server
                self.last_error = f"{type(exc).__name__}: {exc}"
                await self._message("system", f"处理失败：{self.last_error}", level="error")
                await self._set_state("error", self.last_error)
                await asyncio.sleep(0.5)
                await self._set_state("idle")
                return {"ok": False, "reason": self.last_error}

    async def _turn(self, text: str, source: str) -> None:
        self._cancel_requested = False
        await self._message("user", text, extra={"source": source})
        if source == "direct":
            await self._run_task(text, source="direct", spec=None)
            return
        await self._set_state("thinking")
        intent = await self.llm.judge_intent(text)
        if self._cancel_requested:
            await self._set_state("idle")
            return
        await self._emit({"type": "intent", "intent": intent.intent, "reason": intent.reason})
        if intent.intent == "chat":
            reply = await self.llm.chat(text)
            await self._say(reply, tag="chat")
            await self._set_state("idle")
            return
        if intent.intent == "ambiguous":
            reply = await self.llm.clarify(text, intent.reason)
            await self._say(reply, tag="clarify")
            await self._set_state("idle")
            return
        if self._cancel_requested:
            await self._set_state("idle")
            return
        spec = await self.llm.normalize_task(text)
        if self._cancel_requested:
            await self._set_state("idle")
            return
        await self._dispatch_task_spec(text, spec, source)

    async def _task_question(self, spec: TaskSpec) -> Bilingual:
        try:
            return await self.llm.phrase_permission("任务", spec.task, spec.notes)
        except Exception:  # noqa: BLE001 - fall back to a plain question
            return Bilingual(ja="……これを実行する。いい？", zh="……要执行这个，可以吗？")

    async def _dispatch_task_spec(self, raw: str, spec: TaskSpec, source: str,
                                  attempts: int = 0) -> None:
        await self._emit({"type": "normalized", "task": spec.task,
                          "constraints": spec.constraints,
                          "requires_confirmation": spec.requires_confirmation,
                          "missing": spec.missing,
                          "notes": redact(spec.notes)})
        if spec.missing:
            if attempts >= 2:
                await self._message("system", "……信息还是不够，先停在这里。", level="warn")
                await self._set_state("idle")
                return
            question = await self._clarification_question(spec)
            loop = asyncio.get_running_loop()
            future: asyncio.Future = loop.create_future()
            self._clarification = {"future": future}
            try:
                await self._say(question, tag="clarify", cooldown_ms=0)
                answer = await self._collect_answer(future, timeout_s=12.0)
            finally:
                if (self._clarification is not None
                        and self._clarification.get("future") is future):
                    if not future.done():
                        future.cancel()
                    self._clarification = None
            if self._cancel_requested:
                await self._set_state("idle")
                return
            if not answer or not answer.strip():
                await self._message("system", "没有听到补充信息，这次先不执行。", level="info")
                await self._set_state("idle")
                return
            combined = f"{raw}\n补充信息：{answer.strip()}"
            log.info("clarification answered (chars=%d)", len(answer))
            await self._set_state("thinking")
            spec2 = await self.llm.normalize_task(combined)
            if self._cancel_requested:
                await self._set_state("idle")
                return
            await self._dispatch_task_spec(combined, spec2, source, attempts + 1)
            return
        if spec.requires_confirmation:
            question = await self._task_question(spec)
            allowed = await self._ask(spec.task, "task", question,
                                      {"task": spec.task, "constraints": spec.constraints})
            if not allowed:
                await self._message("system", "任务未执行：用户没有确认。", level="info")
                await self._set_state("idle")
                return
        await self._run_task(raw, source=source, spec=spec)

    async def _clarification_question(self, spec: TaskSpec) -> Bilingual:
        try:
            return await self.llm.ask_missing(spec.task, spec.missing)
        except Exception:  # noqa: BLE001 - never block the question on the model
            items = "、".join(spec.missing) or "必要信息"
            return Bilingual(ja=f"……{items}を教えて。", zh=f"……告诉我{items}。")

    async def _collect_answer(self, future: asyncio.Future,
                              timeout_s: float = 12.0) -> str | None:
        """After a clarification question, take a free-form voice or typed answer."""
        if future.done():
            answer = future.result()
            self._clarification = None
            if answer:
                await self._message("user", answer, extra={"source": "text", "answer": True})
            return answer
        await self._set_state("waiting_answer", "请补充信息")
        if self.audio is None:
            try:
                return await asyncio.wait_for(future, timeout=timeout_s)
            except asyncio.TimeoutError:
                return None
            finally:
                self._clarification = None
        self.audio.suspend_wake()
        record_task = asyncio.create_task(self.audio.record_utterance())
        try:
            done, _ = await asyncio.wait({record_task, future}, timeout=timeout_s,
                                         return_when=asyncio.FIRST_COMPLETED)
            if future in done:
                answer = future.result()
                self.audio.cancel_recording()
                if not record_task.done():
                    record_task.cancel()
                if answer:
                    await self._message("user", answer,
                                        extra={"source": "text", "answer": True})
                return answer
            if record_task in done:
                pcm = record_task.result()
                if not pcm:
                    return None
                await self._set_state("transcribing")
                try:
                    text = await self.stt.transcribe_pcm(pcm, self.settings.audio.sample_rate)
                except STTError as exc:
                    await self._message("system", f"语音识别失败：{exc}", level="error")
                    return None
                if text:
                    await self._message("user", text,
                                        extra={"source": "voice", "answer": True})
                return text or None
            return None
        finally:
            if not future.done():
                future.cancel()
            self._clarification = None
            if self.audio is not None:
                if not record_task.done():
                    self.audio.cancel_recording()
                    record_task.cancel()
                self.audio.resume_wake()

    # ---------------------------------------------------------------- dispatch
    async def _run_task(self, raw: str, *, source: str, spec: TaskSpec | None) -> None:
        task_text = spec.task if spec else raw
        constraints = spec.constraints if spec else []
        requires_confirmation = spec.requires_confirmation if spec else False
        session = self.bridge.session_id or ""
        task_id = self.store.create_task(
            source=source, raw_input=raw, normalized=task_text, constraints=constraints,
            requires_confirmation=requires_confirmation, harness_session=session)
        self.current_task_id = task_id
        self.current_task_text = task_text
        await self._emit({"type": "task_created", "task_id": task_id, "source": source,
                          "raw_input": clip(redact(raw), 2000), "normalized": redact(task_text),
                          "constraints": constraints})
        self.store.add_event(task_id, "normalized", {"raw": redact(raw), "task": redact(task_text),
                                                     "constraints": constraints, "source": source})
        prompt = task_text
        if constraints:
            prompt += "\n\n约束：\n" + "\n".join(f"- {c}" for c in constraints)
        prompt = prompt[: self.settings.max_prompt_chars]
        await self._set_state("executing", task_text)
        self.store.update_task(task_id, status="running")
        started = time.time()
        try:
            await self.bridge.start()
        except HarnessStartError as exc:
            self.store.update_task(task_id, status="error", error=str(exc))
            self.store.add_event(task_id, "harness_start_error", {"error": str(exc)})
            await self._message("system", f"Harness 启动失败：{exc}", level="error")
            await self._set_state("error", str(exc))
            await self._finish_task_ui(task_id)
            return
        try:
            result = await self.bridge.prompt(prompt)
        finally:
            self.current_task_id = None
            self.current_task_text = ""
        duration_ms = int((time.time() - started) * 1000)
        self.store.update_task(task_id, status="done" if result.ok else "failed",
                               stop_reason=result.stop_reason, duration_ms=duration_ms,
                               permission_denials=result.permission_denials,
                               harness_session=self.bridge.session_id or "")
        self.store.add_event(task_id, "result", {
            "stop_reason": result.stop_reason, "error": result.error,
            "duration_s": result.duration_s, "denials": result.permission_denials,
            "messages": result.messages, "tools": [t.as_dict() for t in result.tools],
        })
        log.info("task %s finished: stop=%s ok=%s duration_ms=%d",
                 task_id, result.stop_reason, result.ok, duration_ms)
        await self._emit({"type": "task_finished", "task_id": task_id,
                          "ok": result.ok, "stop_reason": result.stop_reason,
                          "duration_ms": duration_ms})
        if source == "direct":
            self.store.update_task(task_id, status="done" if result.ok else "failed")
            await self._message("system", f"直接命令结束：{result.stop_reason}", level="info")
            await self._set_state("idle")
            self.store.add_event(task_id, "direct_finished", {"stop_reason": result.stop_reason})
            return
        if self._cancel_requested:
            log.info("task %s cancelled before summary", task_id)
            self.store.update_task(task_id, status="cancelled", stop_reason="cancelled")
            await self._set_state("idle")
            return
        await self._summarize(task_id, raw, task_text, result)

    async def _summarize(self, task_id: str, raw: str, task_text: str, result: TaskResult) -> None:
        await self._set_state("thinking", "整理结果")
        try:
            reply = await self.llm.summarize(raw, task_text, result.stop_reason,
                                             result.digest(), not result.ok)
        except Exception as exc:  # noqa: BLE001
            fallback = "……終わった。" if result.ok else "……失敗した。"
            reply = Bilingual(ja=fallback, zh=("完成了。" if result.ok else "失败了。"))
            await self._message("system", f"结果整理失败：{type(exc).__name__}", level="warn")
        self.store.update_task(task_id, summary_ja=reply.ja, summary_zh=reply.zh)
        await self._say(reply, tag="summary")
        await self._set_state("idle")

    async def _finish_task_ui(self, task_id: str) -> None:
        await self._emit({"type": "task_finished", "task_id": task_id, "ok": False,
                          "stop_reason": "error"})
        await asyncio.sleep(0.3)
        if self.state != "speaking":
            await self._set_state("idle")

    # -------------------------------------------------------------- permission
    async def _on_harness_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "permission_request":
            task_id = self.current_task_id
            if task_id:
                self.store.add_event(task_id, "permission_request", event["request"])
            await self._emit(event)
            await self._handle_permission(event["request"])
            return
        if etype == "permission_answered":
            await self._emit(event)
            return
        if etype in ("tool", "acp_event", "acp_update"):
            task_id = self.current_task_id
            if task_id:
                self.store.add_event(task_id, etype, event)
            await self._emit(event)
            return
        await self._emit(event)

    async def _handle_permission(self, request: dict[str, Any]) -> None:
        if self._cancel_requested or self.state == "cancelled":
            self.bridge.answer_permission(request["id"], "cancel")
            return
        command = request.get("command") or ""
        justification = request.get("justification") or ""
        title = request.get("title") or "操作"
        try:
            question = await self.llm.phrase_permission(title, command, justification)
        except Exception:  # noqa: BLE001
            question = Bilingual(
                ja="……これを実行してもいい？",
                zh=f"……要执行「{clip(redact(command) or title, 80)}」，可以吗？",
            )
        allowed = await self._ask(request.get("id", uuid.uuid4().hex), "permission", question,
                                 {"request": request},
                                 allow_words={"可以", "允许", "继续", "好的", "好", "行"},
                                 deny_words={"不要", "不行", "拒绝"})
        if self._cancel_requested or self.state == "cancelled":
            self.bridge.answer_permission(request["id"], "cancel")
            return
        self.bridge.answer_permission(request["id"], "allow" if allowed else "deny")
        task_id = self.current_task_id
        if task_id:
            self.store.add_event(task_id, "permission_answered", {
                "request_id": request["id"], "answer": "allow" if allowed else "deny",
                "title": title, "command": command,
            })
        if allowed:
            await self._set_state("executing", "已允许，继续执行")
        else:
            await self._set_state("executing", "已拒绝，等待 Harness 收尾")

    # ------------------------------------------------------------- ask & answer
    async def _ask(self, ask_id: str, kind: str, question: Bilingual,
                   payload: dict[str, Any], *, allow_words: set[str] | None = None,
                   deny_words: set[str] | None = None) -> bool:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        pending = PendingQuestion(
            id=ask_id, kind=kind, future=future,
            allow_words=set(allow_words or ALLOW_WORDS),
            deny_words=set(deny_words or DENY_WORDS),
            payload={**payload, "id": ask_id, "kind": kind, "ja": question.ja, "zh": question.zh},
        )
        self._pending = pending
        await self._set_state("waiting_confirmation", question.zh)
        await self._emit({"type": "confirmation_request", "id": ask_id, "kind": kind,
                          "ja": question.ja, "zh": question.zh,
                          "task": pending.payload.get("task", ""),
                          "constraints": pending.payload.get("constraints", [])})
        speak_task = None
        if self.settings.permissions.speak_question and self.audio is not None:
            speak_task = asyncio.create_task(self._speak_question(question))
        try:
            return await asyncio.wait_for(future, timeout=self.settings.permissions.timeout_s)
        except asyncio.TimeoutError:
            await self._message("system", "等待确认超时，按不执行处理。", level="warn")
            return False
        finally:
            self._pending = None
            if speak_task is not None and not speak_task.done():
                speak_task.cancel()
            await self._emit({"type": "confirmation_closed", "id": ask_id})

    async def _speak_question(self, question: Bilingual) -> None:
        if self.audio is None:
            return
        try:
            audio = await self.tts.synthesize(self._spoken_text(question), self.character.voice_id)
        except TTSError:
            return
        if self._pending is None:
            return
        await self.audio.speak_audio(audio)

    def _answer_from_text(self, text: str) -> str | None:
        pending = self._pending
        if pending is None or pending.future.done():
            return None
        normalized = text.strip().strip("。.!！?？ ")
        if any(word in normalized for word in pending.deny_words):
            pending.future.set_result(False)
            return "deny"
        if any(word in normalized for word in pending.allow_words):
            pending.future.set_result(True)
            return "allow"
        return None

    def answer_confirmation(self, ask_id: str, allow: bool) -> bool:
        pending = self._pending
        if pending is None or pending.id != ask_id or pending.future.done():
            return False
        pending.future.set_result(allow)
        return True

    # ------------------------------------------------------------------- voice
    def _on_kws_label(self, label: str) -> None:
        loop = self._loop
        if loop is None:
            return
        kind, _, word = label.partition("|")
        if kind == "wake":
            self._voice_pipeline = loop.create_task(self._on_wake(word))
        elif kind == "interrupt":
            if self.state in ("executing", "waiting_confirmation"):
                loop.create_task(self.cancel(source="voice"))
        elif kind == "confirm":
            pending = self._pending
            if pending is None or pending.future.done():
                return
            if word in pending.allow_words:
                pending.future.set_result(True)
            elif word in pending.deny_words:
                pending.future.set_result(False)

    async def _on_wake(self, word: str) -> None:
        log.info("wake: %s", word)
        if not await self._claim_voice():
            return
        try:
            await self._emit({"type": "wake", "word": word})
            await self._capture_and_turn(play_wake=True)
        finally:
            self._voice_lock.release()

    async def _claim_voice(self) -> bool:
        if self.state != "idle" or self._turn_lock.locked() or self._voice_lock.locked():
            return False
        await self._voice_lock.acquire()
        return True

    async def listen_once(self) -> dict[str, Any]:
        """The floating window's “开始听” button: same path as a wake, no wake word."""
        if not await self._claim_voice():
            return {"ok": False, "reason": "busy"}
        try:
            await self._capture_and_turn(play_wake=False)
        finally:
            self._voice_lock.release()
        return {"ok": True}

    async def ptt_start(self) -> dict[str, Any]:
        if self.audio is None:
            return {"ok": False, "reason": "no_audio"}
        if self.state != "idle" or self._turn_lock.locked():
            return {"ok": False, "reason": "busy"}
        self.audio.start_manual()
        await self._set_state("listening", "按住说话")
        return {"ok": True}

    async def ptt_stop(self) -> dict[str, Any]:
        if self.audio is None:
            return {"ok": False, "reason": "no_audio"}
        pcm = self.audio.stop_manual()
        if not pcm:
            await self._set_state("idle")
            return {"ok": False, "reason": "empty"}
        self._voice_pipeline = asyncio.create_task(self._transcribe_and_turn(pcm, source="voice"))
        return {"ok": True}

    async def _transcribe_and_turn(self, pcm: bytes, source: str = "voice") -> None:
        # A cancel from an earlier attempt must not swallow this new utterance.
        self._cancel_requested = False
        await self._set_state("transcribing")
        try:
            text = await self.stt.transcribe_pcm(pcm, self.settings.audio.sample_rate)
        except asyncio.CancelledError:
            await self._set_state("idle")
            raise
        except STTError as exc:
            await self._message("system", f"语音识别失败：{exc}", level="error")
            await self._set_state("idle")
            return
        if self._cancel_requested or self.state == "cancelled":
            log.info("voice turn aborted: cancelled during stt (chars=%d)", len(text))
            await self._message("system", "刚才那句被中断了，没有处理。", level="info")
            await self._set_state("idle")
            return
        log.info("stt done: chars=%d", len(text))
        if not text:
            await self._message("system", "没听清，再说一次？", level="info")
            await self._set_state("idle")
            return
        log.info("stt text: %s", clip(redact(text), 120))
        await self._message("user", text, extra={"source": source, "transcribed": True})
        if self._turn_lock.locked():
            await self._message("system", "忙，刚才那句先放下。", level="warn")
            await self._set_state("idle")
            return
        async with self._turn_lock:
            try:
                await self._turn_after_transcription(text)
            finally:
                if self.state not in ("error", "cancelled"):
                    await self._set_state("idle")

    async def _capture_and_turn(self, *, play_wake: bool) -> None:
        if self.audio is None:
            await self._message("system", "麦克风不可用。", level="warn")
            return
        try:
            await self._capture_and_turn_inner(play_wake=play_wake)
        finally:
            # Cancelled during the wake reply? The spotter must not stay paused.
            self.audio.resume_wake()

    async def _capture_and_turn_inner(self, *, play_wake: bool) -> None:
        if play_wake and self._wake_audio:
            await self._set_state("speaking")
            try:
                # Immediate, trimmed reply with no cooldown: recording starts the
                # instant the audible reply ends. There is deliberately no probe
                # window here — waiting before "我在" is worse than the tradeoff.
                await self.audio.speak_audio(self._wake_audio, resume=False, cooldown_ms=0)
            finally:
                await self._set_state("idle")
        await self._set_state("listening", "请说命令")
        self.audio.suspend_wake()
        pcm = await self.audio.record_utterance()
        log.info("capture finished: %s", "no speech" if pcm is None else f"{len(pcm)} bytes")
        if pcm is None:
            if not self._cancel_requested and self.state != "cancelled":
                await self._message("system", "没有听到命令。", level="info")
            await self._set_state("idle")
            return
        await self._transcribe_and_turn(pcm, source="voice")

    async def _turn_after_transcription(self, text: str) -> None:
        if text.strip().lower() in CANCEL_WORDS:
            await self.cancel(source="voice")
            return
        await self._set_state("thinking")
        intent = await self.llm.judge_intent(text)
        if self._cancel_requested:
            await self._set_state("idle")
            return
        await self._emit({"type": "intent", "intent": intent.intent, "reason": intent.reason})
        if intent.intent == "chat":
            reply = await self.llm.chat(text)
            await self._say(reply, tag="chat")
            return
        if intent.intent == "ambiguous":
            reply = await self.llm.clarify(text, intent.reason)
            await self._say(reply, tag="clarify")
            return
        spec = await self.llm.normalize_task(text)
        if self._cancel_requested:
            await self._set_state("idle")
            return
        await self._dispatch_task_spec(text, spec, "voice")

    # ------------------------------------------------------------------ cancel
    async def cancel(self, source: str = "ui") -> dict[str, Any]:
        playing = self.audio is not None and self.audio.player.playing
        if (self.state == "idle" and not self._turn_lock.locked()
                and self._voice_pipeline is None and not playing):
            return {"cancelled": False, "reason": "idle"}
        log.info("cancel requested: source=%s state=%s", source, self.state)
        self._cancel_requested = True
        pipeline = self._voice_pipeline
        # Never cancel the pipeline while a task is executing or a permission is
        # pending: those paths own DB bookkeeping and must answer the ACP request.
        if (pipeline is not None and not pipeline.done()
                and self.state in ("listening", "transcribing", "thinking", "speaking")):
            pipeline.cancel()
        await self._set_state("cancelled", f"来源：{source}")
        if self.audio is not None:
            self.audio.cancel_recording()
            await self.audio.stop_playback()
            self.audio.resume_wake()
        pending = self._pending
        if pending is not None and not pending.future.done():
            pending.future.set_result(False)
        clarification = self._clarification
        if clarification is not None and not clarification["future"].done():
            clarification["future"].set_result(None)
        bridge_state = self.bridge.status
        result: dict[str, Any] = {"cancelled": True, "source": source, "bridge": bridge_state}
        if bridge_state == "busy":
            result.update(await self.bridge.cancel())
        await self._message("system", "已中断。", level="info")
        await asyncio.sleep(0.15)
        if self.state == "cancelled":
            await self._set_state("idle")
        return result

    # -------------------------------------------------------------- operations
    async def set_auto_approve(self, enabled: bool) -> dict[str, Any]:
        mode = "danger-full-access" if enabled else "read-only"
        if mode == self.settings.harness.permission_mode:
            return {"ok": True, "mode": mode}
        await self.bridge.close()
        from .config import HarnessSettings

        self.settings = _replace_harness_mode(self.settings, mode)
        self.bridge = HarnessBridge(
            self.settings.harness, self.settings.workspace_dir, self._on_harness_event,
            permission_timeout_s=self.settings.permissions.timeout_s,
            max_event_text=self.settings.max_event_text,
        )
        _write_local_mode(mode)
        try:
            await self.bridge.start()
        except HarnessStartError as exc:
            self.last_error = str(exc)
            return {"ok": False, "error": str(exc)}
        await self._emit({"type": "harness", "status": "ready", "session": self.bridge.session_id,
                          "permission_mode": mode})
        return {"ok": True, "mode": mode}

    async def switch_character(self, character: Character) -> None:
        async with self._audio_lock:
            if (self.state != "idle" or self._turn_lock.locked() or self._voice_lock.locked()):
                raise RuntimeError("正在忙，等当前回合结束再切换角色")
            if self.audio is not None:
                self.audio.close()
                self.audio = None
            self.wake_ready = False
            self.voice_ready = False
            self.character = character
            self.llm.character = character
            self.settings = replace(self.settings, character_id=character.id)
            self._wake_audio = None
            self._character_gen += 1
            generation = self._character_gen
            update_local_settings({"app": {"character": character.id}})
            await self._emit({"type": "message", "role": "system", "level": "info",
                              "text": f"角色已切换：{character.name_zh}"})
            asyncio.create_task(self._prepare_wake_audio(generation))
            await self._start_audio()
            await self._emit({"type": "character", "character": character.to_dict()})

    async def reload_character(self) -> None:
        character = Character.load(self.character.id)
        await self.switch_character(character)

    async def apply_audio_settings(self, *, input_device: str | None = None,
                                   vad_threshold: float | None = None,
                                   silence_ms: int | None = None,
                                   wake_enabled: bool | None = None,
                                   wake_threshold: float | None = None,
                                   wake_score: float | None = None) -> dict[str, Any]:
        async with self._audio_lock:
            if self.state != "idle" or self._turn_lock.locked():
                raise RuntimeError("正在忙，等当前回合结束再改音频设置")
            return await self._apply_audio_settings_locked(
                input_device=input_device, vad_threshold=vad_threshold,
                silence_ms=silence_ms, wake_enabled=wake_enabled,
                wake_threshold=wake_threshold, wake_score=wake_score)

    async def _apply_audio_settings_locked(self, *, input_device: str | None = None,
                                           vad_threshold: float | None = None,
                                           silence_ms: int | None = None,
                                           wake_enabled: bool | None = None,
                                           wake_threshold: float | None = None,
                                           wake_score: float | None = None) -> dict[str, Any]:
        old = self.settings
        audio = replace(old.audio, input_device=old.audio.input_device if input_device is None else input_device)
        recording = replace(
            old.recording,
            vad_threshold=old.recording.vad_threshold if vad_threshold is None else vad_threshold,
            silence_ms=old.recording.silence_ms if silence_ms is None else silence_ms,
        )
        wake = replace(
            old.wake,
            enabled=old.wake.enabled if wake_enabled is None else wake_enabled,
            threshold=old.wake.threshold if wake_threshold is None else wake_threshold,
            score=old.wake.score if wake_score is None else wake_score,
        )
        restart = (
            audio.input_device != old.audio.input_device
            or wake.enabled != old.wake.enabled
            or wake.threshold != old.wake.threshold
            or wake.score != old.wake.score
            or self.audio is None
        )
        self.settings = replace(old, audio=audio, recording=recording, wake=wake)
        update_local_settings({
            "audio": {"input_device": audio.input_device},
            "recording": {"vad_threshold": recording.vad_threshold,
                          "silence_ms": recording.silence_ms},
            "wake": {"enabled": wake.enabled, "threshold": wake.threshold, "score": wake.score},
        })
        if restart:
            await self._start_audio()
        elif self.audio is not None:
            self.audio.recording = recording
        await self._emit({"type": "message", "role": "system", "level": "info",
                          "text": "音频设置已应用。"})
        return self.public_state()

    async def new_session(self) -> dict[str, Any]:
        try:
            session = await self.bridge.new_session()
            return {"ok": True, "session": session}
        except HarnessStartError as exc:
            return {"ok": False, "error": str(exc)}


def _replace_harness_mode(settings: Settings, mode: str) -> Settings:
    from dataclasses import replace

    return replace(settings, harness=settings.harness.with_mode(mode))


def _write_local_mode(mode: str) -> None:
    update_local_settings({"harness": {"permission_mode": mode}})
