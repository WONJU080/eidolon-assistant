"""Local FastAPI/WebSocket server between the Electron UI and the orchestrator."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .audio import AudioEngine
from .characters import (
    CharacterError,
    CharacterExists,
    avatar_path,
    character_ids,
    delete_character,
    export_archive,
    import_archive,
    list_characters,
    migrate_legacy_characters,
    update_character,
)
from .config import CONFIG_DIR, Character, PROJECT_ROOT, Settings, public_config
from .harness_bridge import DEFAULT_DSH
from .network import detect_system_proxy
from .orchestrator import Orchestrator
from .stt import REGION_URLS
from .store import Store
from .tts import TTSError


class Broadcaster:
    def __init__(self, history_size: int = 300) -> None:
        self._clients: set[WebSocket] = set()
        self._history: list[dict[str, Any]] = []
        self._history_size = history_size
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        for event in self._history[-80:]:
            try:
                await ws.send_json(event)
            except Exception:  # noqa: BLE001
                break

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def __call__(self, event: dict[str, Any]) -> None:
        event.setdefault("at", time.time())
        self._history.append(event)
        if len(self._history) > self._history_size:
            del self._history[: len(self._history) - self._history_size]
        dead = []
        payload = json.dumps(event, ensure_ascii=False, default=str)
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)


class TextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class ConfirmRequest(BaseModel):
    id: str
    allow: bool


class AutoApproveRequest(BaseModel):
    enabled: bool


class ListenRequest(BaseModel):
    action: str = "start"


class TaskNote(BaseModel):
    pass


class CharacterUpdate(BaseModel):
    name: str | None = None
    name_zh: str | None = None
    name_romaji: str | None = None
    description: str | None = None
    color: str | None = None
    voice_id: str | None = None
    tts_language: str | None = None
    wake_words: list[str] | None = None
    wake_reply_tts: str | None = None
    wake_reply_zh: str | None = None
    sheet_text: str | None = None


class AudioSettingsUpdate(BaseModel):
    input_device: str | None = None
    vad_threshold: float | None = None
    silence_ms: int | None = None
    wake_enabled: bool | None = None
    wake_threshold: float | None = None
    wake_score: float | None = None


class PreviewRequest(BaseModel):
    text: str = Field(default="", max_length=200)


def character_meta(character: Character) -> dict[str, Any]:
    data = character.to_dict()
    data.pop("sheet_text", None)
    return data


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    migrate_legacy_characters(CONFIG_DIR)
    character = Character.load(settings.character_id)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(settings.db_path)
        broadcaster = Broadcaster()
        orchestrator = Orchestrator(settings, character, store, broadcaster)
        app.state.settings = settings
        app.state.character = character
        app.state.store = store
        app.state.broadcaster = broadcaster
        app.state.orch = orchestrator
        app.state.tasks = set()
        print(f"EIDOLON_READY http://{settings.server_host}:{settings.server_port}", flush=True)
        startup = asyncio.create_task(orchestrator.startup())
        try:
            yield
        finally:
            startup.cancel()
            await orchestrator.shutdown()

    app = FastAPI(title="Eidolon Assistant", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
        allow_credentials=False,
    )

    def orch(request: Request) -> Orchestrator:
        return request.app.state.orch

    def spawn(request: Request, coro) -> None:
        task = asyncio.create_task(coro)
        request.app.state.tasks.add(task)
        task.add_done_callback(request.app.state.tasks.discard)

    # ------------------------------------------------------------------ basics
    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        o = orch(request)
        return {"ok": True, "state": o.state, "harness": o.bridge.status,
                "voice": o.voice_ready, "wake": o.wake_ready, "at": time.time()}

    @app.get("/api/config")
    async def config(request: Request) -> dict[str, Any]:
        o = orch(request)
        return {
            "config": public_config(o.settings, o.character),
            "state": o.public_state(),
        }

    @app.get("/api/state")
    async def state(request: Request) -> dict[str, Any]:
        return orch(request).public_state()

    @app.get("/api/diagnostics")
    async def diagnostics(request: Request) -> dict[str, Any]:
        s = orch(request).settings
        proxy = detect_system_proxy()
        dsh = Path(s.harness.command or DEFAULT_DSH)
        return {
            "dsh": {"command": str(dsh), "exists": dsh.exists(), "profile": s.harness.profile},
            "permission_mode": s.harness.permission_mode,
            "workspace": str(s.workspace_dir),
            "audio": {
                "input_devices": AudioEngine.list_input_devices(),
                "configured_input": s.audio.input_device,
                "sample_rate": s.audio.sample_rate,
                "vad_threshold": s.recording.vad_threshold,
            },
            "models": {
                "kws_dir": str(s.models_dir / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"),
                "kws_exists": (s.models_dir / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01").exists(),
            },
            "services": {
                "llm": {"model": s.llm.model, "key": bool(s.llm.ready), "base_url": s.llm.base_url},
                "stt": {"model": s.stt.model, "key": bool(s.stt.ready),
                        "endpoint": REGION_URLS.get(s.stt.region)},
                "tts": {"model": s.tts.model, "key": bool(s.tts.ready), "base_url": s.tts.base_url},
            },
            "proxy": {"http": proxy.http, "https": proxy.https, "socks": proxy.socks},
            "character": {"id": orch(request).character.id,
                          "wake_words": orch(request).character.wake_words},
            "paths": {"data": str(s.data_dir), "db": str(s.db_path), "logs": str(s.log_dir)},
        }

    # ------------------------------------------------------------------ actions
    @app.post("/api/text")
    async def text(body: TextRequest, request: Request) -> dict[str, Any]:
        o = orch(request)
        # A pending question owns the lock: typed answers must still reach it.
        if o._turn_lock.locked() and o._clarification is None and o._pending is None:
            return {"ok": False, "reason": "busy"}
        spawn(request, o.handle_text(body.text, source="text"))
        return {"ok": True, "accepted": True}

    @app.post("/api/direct")
    async def direct(body: TextRequest, request: Request) -> dict[str, Any]:
        o = orch(request)
        if o._turn_lock.locked():
            return {"ok": False, "reason": "busy"}
        spawn(request, o.handle_text(body.text, source="direct"))
        return {"ok": True, "accepted": True}

    @app.post("/api/cancel")
    async def cancel(request: Request) -> dict[str, Any]:
        return await orch(request).cancel(source="ui")

    @app.post("/api/confirm")
    async def confirm(body: ConfirmRequest, request: Request) -> dict[str, Any]:
        ok = orch(request).answer_confirmation(body.id, body.allow)
        return {"ok": ok}

    @app.post("/api/listen")
    async def listen(body: ListenRequest, request: Request) -> dict[str, Any]:
        o = orch(request)
        if o.audio is None:
            return {"ok": False, "reason": "no_audio"}
        if body.action == "start":
            spawn(request, o.listen_once())
            return {"ok": True, "accepted": True}
        return {"ok": False, "reason": "unsupported"}

    @app.post("/api/ptt")
    async def ptt(body: ListenRequest, request: Request) -> dict[str, Any]:
        o = orch(request)
        if body.action == "start":
            return await o.ptt_start()
        if body.action == "stop":
            return await o.ptt_stop()
        return {"ok": False, "reason": "unsupported"}

    @app.post("/api/auto-approve")
    async def auto_approve(body: AutoApproveRequest, request: Request) -> dict[str, Any]:
        return await orch(request).set_auto_approve(body.enabled)

    @app.post("/api/session/new")
    async def new_session(request: Request) -> dict[str, Any]:
        return await orch(request).new_session()

    # --------------------------------------------------------------- characters
    @app.get("/api/characters")
    async def characters(request: Request) -> dict[str, Any]:
        o = orch(request)
        return {
            "characters": [character_meta(item) for item in list_characters(CONFIG_DIR)],
            "active": o.character.id,
        }

    @app.get("/api/characters/{character_id}")
    async def character_detail(character_id: str, request: Request) -> dict[str, Any]:
        try:
            character = Character.load(character_id)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="角色不存在")
        return {"character": {**character_meta(character), "sheet_text": character.sheet_text}}

    @app.post("/api/characters/import")
    async def import_character(request: Request, file: UploadFile = File(...),
                               overwrite: bool = False) -> dict[str, Any]:
        payload = await file.read()
        try:
            character = import_archive(CONFIG_DIR, payload, overwrite=overwrite)
        except CharacterExists as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except CharacterError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        o = orch(request)
        if character.id == o.character.id:
            try:
                await o.reload_character()
            except RuntimeError:
                pass
        return {"ok": True, "character": character_meta(character)}

    @app.get("/api/characters/{character_id}/export")
    async def export_character(character_id: str) -> Response:
        try:
            payload = export_archive(CONFIG_DIR, character_id)
        except (CharacterError, FileNotFoundError):
            raise HTTPException(status_code=404, detail="角色不存在")
        return Response(
            content=payload,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{character_id}.eidolon"'},
        )

    @app.get("/api/characters/{character_id}/avatar")
    async def character_avatar(character_id: str) -> FileResponse:
        path = avatar_path(CONFIG_DIR, character_id)
        if path is None:
            raise HTTPException(status_code=404, detail="没有头像")
        return FileResponse(str(path))

    @app.put("/api/characters/{character_id}")
    async def edit_character(character_id: str, body: CharacterUpdate,
                             request: Request) -> dict[str, Any]:
        fields = body.model_dump(exclude_none=True)
        sheet = fields.pop("sheet_text", None)
        try:
            character = update_character(CONFIG_DIR, character_id, fields, sheet)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="角色不存在")
        except CharacterError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        applied = True
        o = orch(request)
        if character.id == o.character.id:
            try:
                await o.reload_character()
            except RuntimeError as exc:
                applied = False
                return {"ok": True, "character": character_meta(character),
                        "applied": applied, "warning": str(exc)}
        return {"ok": True, "character": character_meta(character), "applied": applied}

    @app.post("/api/characters/{character_id}/activate")
    async def activate(character_id: str, request: Request) -> dict[str, Any]:
        try:
            character = Character.load(character_id)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="角色不存在")
        try:
            await orch(request).switch_character(character)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        request.app.state.character = character
        return {"ok": True, "character": character_meta(character)}

    @app.delete("/api/characters/{character_id}")
    async def remove_character(character_id: str, request: Request) -> dict[str, Any]:
        o = orch(request)
        if character_id == o.character.id:
            raise HTTPException(status_code=409, detail="先切换到别的角色，再删除当前角色")
        if len(character_ids(CONFIG_DIR)) <= 1:
            raise HTTPException(status_code=409, detail="至少保留一个角色")
        try:
            delete_character(CONFIG_DIR, character_id)
        except (CharacterError, FileNotFoundError):
            raise HTTPException(status_code=404, detail="角色不存在")
        return {"ok": True}

    @app.post("/api/characters/{character_id}/preview")
    async def preview_character(character_id: str, body: PreviewRequest,
                                request: Request) -> Response:
        try:
            character = Character.load(character_id)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="角色不存在")
        if not character.voice_id:
            raise HTTPException(status_code=400, detail="该角色没有配置 voice_id")
        text = body.text.strip() or character.wake_reply_tts
        try:
            audio = await orch(request).tts.synthesize(text, character.voice_id, allow_cache=False)
        except TTSError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return Response(content=audio, media_type="audio/mpeg")

    # ----------------------------------------------------------------- settings
    @app.get("/api/settings")
    async def get_settings(request: Request) -> dict[str, Any]:
        o = orch(request)
        s = o.settings
        return {
            "audio": {"input_device": s.audio.input_device,
                      "devices": AudioEngine.list_input_devices()},
            "recording": {"vad_threshold": s.recording.vad_threshold,
                          "silence_ms": s.recording.silence_ms,
                          "max_utterance_s": s.recording.max_utterance_s},
            "wake": {"enabled": s.wake.enabled, "threshold": s.wake.threshold,
                     "score": s.wake.score, "threads": s.wake.threads},
            "voice_ready": o.voice_ready,
            "wake_ready": o.wake_ready,
        }

    @app.put("/api/settings")
    async def put_settings(body: AudioSettingsUpdate, request: Request) -> dict[str, Any]:
        values = body.model_dump(exclude_none=True)
        if "vad_threshold" in values and not (0.001 <= values["vad_threshold"] <= 0.2):
            raise HTTPException(status_code=400, detail="vad_threshold 必须在 0.001–0.2")
        if "silence_ms" in values and not (300 <= values["silence_ms"] <= 8000):
            raise HTTPException(status_code=400, detail="silence_ms 必须在 300–8000")
        if "wake_threshold" in values and not (0.05 <= values["wake_threshold"] <= 0.95):
            raise HTTPException(status_code=400, detail="wake_threshold 必须在 0.05–0.95")
        if "wake_score" in values and not (0.1 <= values["wake_score"] <= 5.0):
            raise HTTPException(status_code=400, detail="wake_score 必须在 0.1–5")
        if values.get("input_device"):
            devices = AudioEngine.list_input_devices()
            if not any(values["input_device"] in (item["name"], str(item["index"])) for item in devices):
                raise HTTPException(status_code=400, detail="找不到这个输入设备")
        o = orch(request)
        try:
            await o.apply_audio_settings(**values)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"ok": True, "config": public_config(o.settings, o.character)}

    @app.get("/api/audio/devices")
    async def audio_devices() -> dict[str, Any]:
        return {"devices": AudioEngine.list_input_devices()}

    # -------------------------------------------------------------------- tasks
    @app.get("/api/tasks")
    async def tasks(request: Request, limit: int = 50) -> dict[str, Any]:
        return {"tasks": request.app.state.store.list_tasks(limit=limit)}

    @app.get("/api/tasks/{task_id}")
    async def task(task_id: str, request: Request) -> dict[str, Any]:
        item = request.app.state.store.get_task(task_id)
        if item is None:
            raise HTTPException(status_code=404, detail="task not found")
        return {"task": item}

    @app.get("/api/tasks/{task_id}/events")
    async def task_events(task_id: str, request: Request, limit: int = 2000) -> dict[str, Any]:
        return {"events": request.app.state.store.get_events(task_id, limit=limit)}

    # ----------------------------------------------------------------------- ws
    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        broadcaster: Broadcaster = ws.app.state.broadcaster
        await broadcaster.connect(ws)
        try:
            o: Orchestrator = ws.app.state.orch
            await ws.send_json({"type": "hello", "config": public_config(o.settings, o.character),
                                "state": o.public_state(), "at": time.time()})
            while True:
                message = await ws.receive_text()
                if message == "ping":
                    await ws.send_text('{"type":"pong"}')
        except WebSocketDisconnect:
            broadcaster.disconnect(ws)
        except Exception:  # noqa: BLE001
            broadcaster.disconnect(ws)

    # ------------------------------------------------------------------- static
    renderer = PROJECT_ROOT / "desktop" / "dist" / "renderer"
    if renderer.exists():
        app.mount("/", StaticFiles(directory=str(renderer), html=True), name="renderer")
    else:
        @app.get("/")
        async def placeholder() -> JSONResponse:
            return JSONResponse({"ok": True, "message": "Eidolon Assistant backend is running;"
                                                        " build the renderer with npm run build:renderer."})

    return app
