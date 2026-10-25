"""Configuration loading for Eidolon Assistant.

Settings come from ``config/settings.toml`` (tracked) merged with
``config/local.toml`` (gitignored) and process environment. Secrets live only in
the environment or ``config/local.env``; nothing here logs a secret.
"""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"
# A packaged app keeps writable config under Application Support; a dev run uses ./config.
CONFIG_DIR = Path(os.environ.get("EIDOLON_CONFIG_DIR") or DEFAULT_CONFIG_DIR).expanduser()
DEFAULT_SETTINGS = CONFIG_DIR / "settings.toml"
LOCAL_SETTINGS = CONFIG_DIR / "local.toml"
LOCAL_ENV = CONFIG_DIR / "local.env"


def _seed_config_dir() -> None:
    """First packaged run: copy the shipped defaults into the writable config dir."""
    if CONFIG_DIR == DEFAULT_CONFIG_DIR or not DEFAULT_CONFIG_DIR.exists():
        return
    if (CONFIG_DIR / "settings.toml").exists():
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(DEFAULT_CONFIG_DIR, CONFIG_DIR, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("local.env", "local.toml"))


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML value: {type(value)!r}")


def _dump_toml(data: dict[str, Any], prefix: tuple[str, ...] = ()) -> str:
    lines: list[str] = []
    sections: list[tuple[str, dict[str, Any]]] = []
    for key, value in data.items():
        if isinstance(value, dict):
            sections.append((key, value))
        elif value is not None:
            lines.append(f"{key} = {_toml_value(value)}")
    for key, value in sections:
        lines.append("")
        header = ".".join((*prefix, key))
        lines.append(f"[{header}]")
        for child_key, child in value.items():
            if isinstance(child, dict):
                lines.extend(_dump_toml({child_key: child}, (*prefix, key)).splitlines())
            elif child is not None:
                lines.append(f"{child_key} = {_toml_value(child)}")
    return "\n".join(lines).strip() + "\n"


def update_local_settings(updates: dict[str, Any]) -> dict[str, Any]:
    """Merge overrides into ``config/local.toml`` (the user layer)."""
    current = load_toml(LOCAL_SETTINGS)
    merged = _merge(current, updates)
    LOCAL_SETTINGS.write_text(_dump_toml(merged), encoding="utf-8")
    return merged


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    model: str
    api_key_env: str
    temperature: float
    timeout_s: float
    max_tokens: int

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "").strip()

    @property
    def ready(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class STTSettings:
    provider: str
    model: str
    api_key_env: str
    region: str
    language_hints: list[str]
    timeout_s: float

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "").strip()

    @property
    def ready(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class TTSSettings:
    provider: str
    base_url: str
    model: str
    api_key_env: str
    format: str
    latency: str
    speed: float
    timeout_s: float

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "").strip()

    @property
    def ready(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class HarnessSettings:
    command: str
    profile: str
    permission_mode: str
    startup_timeout_s: float
    prompt_timeout_s: float
    cancel_grace_s: float
    close_timeout_s: float

    @property
    def auto_approve(self) -> bool:
        return self.permission_mode == "danger-full-access"

    def with_mode(self, mode: str) -> "HarnessSettings":
        return HarnessSettings(
            command=self.command,
            profile=self.profile,
            permission_mode=mode,
            startup_timeout_s=self.startup_timeout_s,
            prompt_timeout_s=self.prompt_timeout_s,
            cancel_grace_s=self.cancel_grace_s,
            close_timeout_s=self.close_timeout_s,
        )


@dataclass(frozen=True)
class AudioSettings:
    input_device: str
    output_device: str
    sample_rate: int
    block_ms: int
    wake_pause_after_tts_ms: int

    @property
    def block_frames(self) -> int:
        return self.sample_rate * self.block_ms // 1000


@dataclass(frozen=True)
class WakeSettings:
    enabled: bool
    threshold: float
    score: float
    threads: int
    interrupt_words: list[str]
    confirm_words: list[str]


@dataclass(frozen=True)
class RecordingSettings:
    vad_threshold: float
    silence_ms: int
    min_speech_ms: int
    max_utterance_s: float
    start_timeout_s: float


@dataclass(frozen=True)
class PermissionSettings:
    timeout_s: float
    speak_question: bool


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    workspace_dir: Path
    db_path: Path
    log_dir: Path
    harness: HarnessSettings
    llm: LLMSettings
    stt: STTSettings
    tts: TTSSettings
    audio: AudioSettings
    wake: WakeSettings
    recording: RecordingSettings
    permissions: PermissionSettings
    max_prompt_chars: int
    max_event_text: int
    character_id: str
    server_host: str
    server_port: int
    app_name: str
    models_dir: Path = PROJECT_ROOT / "models"

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls) -> "Settings":
        _seed_config_dir()
        if LOCAL_ENV.exists():
            load_dotenv(LOCAL_ENV, override=False)
        raw = _merge(load_toml(DEFAULT_SETTINGS), load_toml(LOCAL_SETTINGS))

        app = raw.get("app", {})
        paths = raw.get("paths", {})
        data_dir = Path(
            os.environ.get("EIDOLON_DATA_DIR")
            or paths.get("data_dir")
            or (Path.home() / "Library/Application Support/EidolonAssistant")
        ).expanduser()
        workspace_dir = Path(
            os.environ.get("EIDOLON_WORKSPACE_DIR")
            or paths.get("workspace_dir")
            or (data_dir / "workspace")
        ).expanduser()

        h = raw.get("harness", {})
        l = raw.get("llm", {})
        s = raw.get("stt", {})
        t = raw.get("tts", {})
        a = raw.get("audio", {})
        w = raw.get("wake", {})
        r = raw.get("recording", {})
        p = raw.get("permissions", {})
        lim = raw.get("limits", {})
        srv = raw.get("server", {})

        mode = os.environ.get("EIDOLON_PERMISSION_MODE") or h.get("permission_mode", "read-only")
        if mode not in ("read-only", "danger-full-access"):
            mode = "read-only"

        settings = cls(
            data_dir=data_dir,
            workspace_dir=workspace_dir,
            db_path=data_dir / "eidolon.sqlite3",
            log_dir=data_dir / "logs",
            harness=HarnessSettings(
                command=os.environ.get("EIDOLON_DSH_COMMAND") or h.get("command", ""),
                profile=h.get("profile", "acp"),
                permission_mode=mode,
                startup_timeout_s=float(h.get("startup_timeout_s", 90)),
                prompt_timeout_s=float(h.get("prompt_timeout_s", 900)),
                cancel_grace_s=float(h.get("cancel_grace_s", 8)),
                close_timeout_s=float(h.get("close_timeout_s", 20)),
            ),
            llm=LLMSettings(
                base_url=l.get("base_url", "https://api.deepseek.com/v1"),
                model=l.get("model", "deepseek-chat"),
                api_key_env=l.get("api_key_env", "CHAT_LLM_API_KEY"),
                temperature=float(l.get("temperature", 0.7)),
                timeout_s=float(l.get("timeout_s", 60)),
                max_tokens=int(l.get("max_tokens", 900)),
            ),
            stt=STTSettings(
                provider=s.get("provider", "dashscope"),
                model=s.get("model", "qwen-audio-3.1-asr-flash-streaming"),
                api_key_env=s.get("api_key_env", "DASHSCOPE_API_KEY"),
                region=os.environ.get("EIDOLON_STT_REGION") or s.get("region", "beijing"),
                language_hints=list(s.get("language_hints", ["zh", "ja"])),
                timeout_s=float(s.get("timeout_s", 30)),
            ),
            tts=TTSSettings(
                provider=t.get("provider", "fish"),
                base_url=t.get("base_url", "https://api.fish.audio"),
                model=t.get("model", "drama-3"),
                api_key_env=t.get("api_key_env", "FISH_AUDIO_API_KEY"),
                format=t.get("format", "mp3"),
                latency=t.get("latency", "normal"),
                speed=float(t.get("speed", 1.0)),
                timeout_s=float(t.get("timeout_s", 60)),
            ),
            audio=AudioSettings(
                input_device=a.get("input_device", ""),
                output_device=a.get("output_device", ""),
                sample_rate=int(a.get("sample_rate", 16000)),
                block_ms=int(a.get("block_ms", 100)),
                wake_pause_after_tts_ms=int(a.get("wake_pause_after_tts_ms", 600)),
            ),
            wake=WakeSettings(
                enabled=bool(w.get("enabled", True)),
                threshold=float(w.get("threshold", 0.25)),
                score=float(w.get("score", 1.5)),
                threads=int(w.get("threads", 2)),
                interrupt_words=list(w.get("interrupt_words", ["停", "取消", "中断"])),
                confirm_words=list(w.get("confirm_words", ["可以", "允许", "继续", "好的", "不要", "不行", "拒绝"])),
            ),
            recording=RecordingSettings(
                vad_threshold=float(r.get("vad_threshold", 0.012)),
                silence_ms=int(r.get("silence_ms", 900)),
                min_speech_ms=int(r.get("min_speech_ms", 250)),
                max_utterance_s=float(r.get("max_utterance_s", 15)),
                start_timeout_s=float(r.get("start_timeout_s", 8)),
            ),
            permissions=PermissionSettings(
                timeout_s=float(p.get("timeout_s", 120)),
                speak_question=bool(p.get("speak_question", True)),
            ),
            max_prompt_chars=int(lim.get("max_prompt_chars", 4000)),
            max_event_text=int(lim.get("max_event_text", 4000)),
            character_id=app.get("character", "ramlethal"),
            server_host=srv.get("host", "127.0.0.1"),
            server_port=int(srv.get("port", 8765)),
            app_name=app.get("name", "Eidolon Assistant"),
        )
        settings.ensure_dirs()
        return settings

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.workspace_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Character:
    id: str
    name: str
    name_zh: str
    name_romaji: str
    color: str
    voice_id: str
    sheet: str
    wake_words: list[str]
    wake_reply_tts: str
    wake_reply_zh: str
    tts_language: str = "ja"
    sheet_text: str = field(default="", compare=False)
    description: str = ""
    avatar: str = ""
    directory: str = ""

    @classmethod
    def load(cls, character_id: str, config_dir: Path = CONFIG_DIR) -> "Character":
        """Load a character from ``characters/<id>/`` (new) or ``characters/<id>.toml`` (legacy)."""
        base = config_dir / "characters" / character_id
        toml_path = base / "character.toml"
        legacy_path = config_dir / "characters" / f"{character_id}.toml"
        if toml_path.exists():
            data = load_toml(toml_path)
            root = base
        elif legacy_path.exists():
            data = load_toml(legacy_path)
            root = config_dir
        else:
            raise FileNotFoundError(f"character config not found: {character_id}")
        if not data:
            raise FileNotFoundError(f"character config is empty: {character_id}")
        sheet_value = data.get("sheet", "sheet.md" if root == base else f"characters/{character_id}.md")
        sheet_path = Path(sheet_value)
        if not sheet_path.is_absolute():
            sheet_path = root / sheet_path
        if not sheet_path.exists() and root == config_dir:
            fallback = config_dir / "characters" / f"{character_id}.md"
            if fallback.exists():
                sheet_path = fallback
        sheet_text = sheet_path.read_text(encoding="utf-8") if sheet_path.exists() else ""
        avatar_value = data.get("avatar", "assets/avatar.png" if root == base else "")
        avatar_path = Path(avatar_value)
        if avatar_value and not avatar_path.is_absolute():
            avatar_path = root / avatar_path
        return cls(
            id=str(data.get("id", character_id)),
            name=str(data.get("name", character_id)),
            name_zh=str(data.get("name_zh", data.get("name", character_id))),
            name_romaji=str(data.get("name_romaji", "")),
            color=str(data.get("color", "#d4a24c")),
            voice_id=str(data.get("voice_id", "")),
            sheet=str(sheet_path),
            wake_words=[str(word) for word in data.get("wake_words", [])],
            wake_reply_tts=str(data.get("wake_reply_tts", "……ここにいる。")),
            wake_reply_zh=str(data.get("wake_reply_zh", "我在。")),
            tts_language=str(data.get("tts_language", "ja")),
            sheet_text=sheet_text,
            description=str(data.get("description", "")),
            avatar=str(avatar_path) if avatar_value and avatar_path.exists() else "",
            directory=str(base) if root == base else "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "name_zh": self.name_zh,
            "name_romaji": self.name_romaji,
            "description": self.description,
            "color": self.color,
            "voice_id": self.voice_id,
            "wake_words": self.wake_words,
            "wake_reply_tts": self.wake_reply_tts,
            "wake_reply_zh": self.wake_reply_zh,
            "tts_language": self.tts_language,
            "avatar": bool(self.avatar),
            "sheet_text": self.sheet_text,
        }


def public_config(settings: Settings, character: Character) -> dict[str, Any]:
    """What the renderer may see: never a secret."""
    return {
        "app_name": settings.app_name,
        "character": {
            "id": character.id,
            "name": character.name,
            "name_zh": character.name_zh,
            "color": character.color,
            "wake_words": character.wake_words,
            "wake_reply_zh": character.wake_reply_zh,
        },
        "harness": {
            "command": settings.harness.command,
            "profile": settings.harness.profile,
            "permission_mode": settings.harness.permission_mode,
            "auto_approve": settings.harness.auto_approve,
        },
        "workspace": str(settings.workspace_dir),
        "services": {
            "llm": {"model": settings.llm.model, "ready": settings.llm.ready},
            "stt": {"model": settings.stt.model, "ready": settings.stt.ready},
            "tts": {"model": settings.tts.model, "ready": settings.tts.ready},
            "wake": {"words": settings.wake.interrupt_words, "enabled": settings.wake.enabled},
        },
        "limits": {
            "max_prompt_chars": settings.max_prompt_chars,
            "permission_timeout_s": settings.permissions.timeout_s,
        },
    }
