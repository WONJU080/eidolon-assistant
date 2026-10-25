import json
import os
from pathlib import Path

from eidolon_assistant.config import Character, Settings, public_config


def test_character_loads_sheet_and_wake_words() -> None:
    character = Character.load("ramlethal")
    assert character.id == "ramlethal"
    assert "拉姆" in character.wake_words
    assert "协助" in character.sheet_text


def test_settings_are_writable_under_override(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EIDOLON_DATA_DIR", str(tmp_path))
    settings = Settings.load()
    assert settings.data_dir == tmp_path
    assert settings.workspace_dir == tmp_path / "workspace"
    assert (tmp_path / "workspace").exists()


def test_public_config_never_contains_secrets(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EIDOLON_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHAT_LLM_API_KEY", "sk-should-not-appear-123456")
    settings = Settings.load()
    character = Character.load(settings.character_id)
    payload = json.dumps(public_config(settings, character), ensure_ascii=False)
    assert "should-not-appear" not in payload
    assert "api_key" not in payload.lower()
    assert payload.count("ready") >= 3


def test_default_permission_mode_is_confirm(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EIDOLON_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("EIDOLON_PERMISSION_MODE", raising=False)
    monkeypatch.setattr("eidolon_assistant.config.load_toml", lambda path: {} if path.name == "local.toml" else _real_load(path))
    settings = Settings.load()
    assert settings.harness.permission_mode in ("read-only", "danger-full-access")
    assert settings.harness.auto_approve == (settings.harness.permission_mode == "danger-full-access")


def _real_load(path: Path):
    import tomllib

    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def test_local_overrides_character_and_audio(monkeypatch, tmp_path) -> None:
    import shutil
    from pathlib import Path

    import eidolon_assistant.config as config_module

    root = Path(__file__).resolve().parents[2]
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copyfile(root / "config" / "settings.toml", config_dir / "settings.toml")
    (config_dir / "local.toml").write_text(
        '[app]\ncharacter = "demo"\n[recording]\nsilence_ms = 1900\n'
        '[wake]\nenabled = false\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "DEFAULT_SETTINGS", config_dir / "settings.toml")
    monkeypatch.setattr(config_module, "LOCAL_SETTINGS", config_dir / "local.toml")
    monkeypatch.setattr(config_module, "LOCAL_ENV", config_dir / "local.env")
    monkeypatch.setenv("EIDOLON_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("EIDOLON_PERMISSION_MODE", raising=False)

    settings = Settings.load()
    assert settings.character_id == "demo"
    assert settings.recording.silence_ms == 1900
    assert settings.wake.enabled is False
