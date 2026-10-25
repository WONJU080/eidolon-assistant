import io
import zipfile

import pytest

from eidolon_assistant.characters import (
    CharacterError,
    CharacterExists,
    delete_character,
    export_archive,
    import_archive,
    list_characters,
    migrate_legacy_characters,
    update_character,
)
from eidolon_assistant.config import Character, load_toml, update_local_settings

TOML = """id = "demo"
name = "デモ"
name_zh = "演示角色"
name_romaji = "Demo Character"
description = "测试用角色"
color = "#66aaff"
voice_id = ""
sheet = "sheet.md"
wake_words = ["演示"]
wake_reply_tts = "……はい。"
wake_reply_zh = "……在。"
tts_language = "ja"
"""

SHEET = "# 演示角色\n" + "你知道自己在协助用户操作电脑；系统操作由 Harness 执行，你不是 Harness。" * 6


def make_zip(toml: str = TOML, sheet: str = SHEET, extra: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("character.toml", toml)
        archive.writestr("sheet.md", sheet)
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_import_export_update_delete(tmp_path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    character = import_archive(config, make_zip())
    assert character.id == "demo"
    assert character.name_zh == "演示角色"
    assert [item.id for item in list_characters(config)] == ["demo"]

    blob = export_archive(config, "demo")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert {"character.toml", "sheet.md"} <= set(archive.namelist())

    updated = update_character(
        config, "demo",
        {"name_zh": "演示角色改", "wake_words": ["演示", "助手"]},
        SHEET + "\n补充设定。" * 10,
    )
    assert updated.name_zh == "演示角色改"
    assert updated.wake_words == ["演示", "助手"]
    assert "补充设定" in updated.sheet_text

    delete_character(config, "demo")
    assert list_characters(config) == []


def test_import_requires_overwrite(tmp_path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    import_archive(config, make_zip())
    with pytest.raises(CharacterExists):
        import_archive(config, make_zip())
    again = import_archive(config, make_zip(), overwrite=True)
    assert again.id == "demo"


def test_wake_words_must_be_chinese(tmp_path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    bad = TOML.replace('wake_words = ["演示"]', 'wake_words = ["demo"]')
    with pytest.raises(CharacterError):
        import_archive(config, make_zip(bad))


def test_zip_traversal_is_rejected(tmp_path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    with pytest.raises(CharacterError):
        import_archive(config, make_zip(extra={"../evil.txt": b"x"}))


def test_legacy_flat_character_is_migrated(tmp_path) -> None:
    config = tmp_path / "config"
    chars = config / "characters"
    chars.mkdir(parents=True)
    (chars / "demo.toml").write_text(TOML, encoding="utf-8")
    (chars / "demo.md").write_text(SHEET, encoding="utf-8")
    migrate_legacy_characters(config)
    assert (chars / "demo" / "character.toml").exists()
    assert not (chars / "demo.toml").exists()
    character = Character.load("demo", config)
    assert character.directory.endswith("demo")
    assert "Harness" in character.sheet_text


def test_local_settings_merge_preserves_sections(tmp_path, monkeypatch) -> None:
    import eidolon_assistant.config as config_module

    local = tmp_path / "local.toml"
    monkeypatch.setattr(config_module, "LOCAL_SETTINGS", local)
    update_local_settings({"harness": {"permission_mode": "read-only"}})
    update_local_settings({"app": {"character": "demo"}})
    update_local_settings({"recording": {"silence_ms": 2000}})
    data = load_toml(local)
    assert data["harness"]["permission_mode"] == "read-only"
    assert data["app"]["character"] == "demo"
    assert data["recording"]["silence_ms"] == 2000


def test_import_accepts_one_top_level_folder(tmp_path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("demo/character.toml", TOML)
        archive.writestr("demo/sheet.md", SHEET)
        archive.writestr("__MACOSX/._character.toml", b"junk")
    character = import_archive(config, buffer.getvalue())
    assert character.id == "demo"
    assert "Harness" in character.sheet_text
