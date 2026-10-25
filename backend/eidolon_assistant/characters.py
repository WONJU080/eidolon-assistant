"""Character packages: one directory per character, import/export as ``.eidolon`` zips.

Layout (inside ``config/characters/<id>/``)::

    character.toml      required metadata
    sheet.md            required persona sheet
    assets/avatar.png   optional (png/jpeg/webp/gif, <=5MB)
    README.md           optional

Legacy flat files (``characters/<id>.toml`` + ``<id>.md``) are still readable and
are migrated on startup. Import rejects absolute paths, ``..``, symlinks, and any
member that is not part of the documented layout.
"""

from __future__ import annotations

import io
import logging
import re
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any

from .config import Character, _dump_toml, load_toml, update_local_settings
from .pinyin import is_han

log = logging.getLogger("eidolon.characters")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_AVATAR_BYTES = 5 * 1024 * 1024
MAX_MEMBERS = 120

IMAGE_SIGNATURES = {
    b"\x89PNG\r\n\x1a\n": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"RIFF": ".webp",  # checked further below
    b"GIF87a": ".gif",
    b"GIF89a": ".gif",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class CharacterError(ValueError):
    """A package/file is not a valid character."""


class CharacterExists(CharacterError):
    """Import collided with an existing id and overwrite was not requested."""


def characters_dir(config_dir: Path) -> Path:
    path = config_dir / "characters"
    path.mkdir(parents=True, exist_ok=True)
    return path


def character_dir(config_dir: Path, character_id: str) -> Path:
    return characters_dir(config_dir) / character_id


def slugify(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    text = text[:40].strip("-")
    if not text:
        text = f"character-{uuid.uuid4().hex[:8]}"
    if text[0].isdigit():
        text = f"char-{text}"
    return text


def character_ids(config_dir: Path) -> list[str]:
    root = characters_dir(config_dir)
    ids: list[str] = []
    for item in root.iterdir():
        if item.is_dir() and (item / "character.toml").exists():
            ids.append(item.name)
        elif item.suffix == ".toml" and item.is_file():
            ids.append(item.stem)
    return sorted(set(ids))


def list_characters(config_dir: Path) -> list[Character]:
    characters: list[Character] = []
    for character_id in character_ids(config_dir):
        try:
            characters.append(Character.load(character_id, config_dir))
        except (FileNotFoundError, ValueError, OSError) as exc:
            log.warning("character %s skipped: %s", character_id, exc)
    return sorted(characters, key=lambda item: item.name_zh)


def validate_character(data: dict[str, Any], sheet_text: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    name = str(data.get("name", "")).strip()
    name_zh = str(data.get("name_zh", "")).strip()
    if not name:
        errors.append("缺少 name（角色原名）")
    if not name_zh:
        errors.append("缺少 name_zh（中文名）")
    voice_id = str(data.get("voice_id", "")).strip()
    if not voice_id:
        warnings.append("voice_id 为空：角色可以对话，但 TTS 会失败；请填 Fish Audio 的 reference id")
    raw_words = data.get("wake_words") or []
    if isinstance(raw_words, str):
        raw_words = [raw_words]
    words = [str(word).strip() for word in raw_words if str(word).strip()]
    if not words:
        errors.append("至少需要一个唤醒词")
    for word in words:
        if len(word) > 8:
            errors.append(f"唤醒词过长：{word!r}（最多 8 个字）")
        if not all(is_han(char) for char in word):
            errors.append(f"唤醒词必须是中文汉字（本地 KWS 使用拼音）：{word!r}")
    if not str(data.get("wake_reply_zh", "")).strip():
        errors.append("缺少 wake_reply_zh（唤醒回应的中文字幕）")
    if not str(data.get("wake_reply_tts", "")).strip():
        warnings.append("wake_reply_tts 为空，唤醒时不会播放语音回应")
    color = str(data.get("color", "#d4a24c"))
    if not COLOR_RE.match(color):
        errors.append(f"color 必须是 #RRGGBB：{color!r}")
    language = str(data.get("tts_language", "ja"))
    if language not in ("ja", "zh"):
        errors.append("tts_language 只能是 ja 或 zh")
    if len(sheet_text.strip()) < 80:
        errors.append("sheet.md 太短（至少 80 个字符的人设描述）")
    return errors, warnings


def _character_toml(character: Character, *, sheet: str = "sheet.md") -> str:
    data: dict[str, Any] = {
        "id": character.id,
        "name": character.name,
        "name_zh": character.name_zh,
        "name_romaji": character.name_romaji,
        "description": character.description,
        "color": character.color,
        "voice_id": character.voice_id,
        "sheet": sheet,
        "wake_words": character.wake_words,
        "wake_reply_tts": character.wake_reply_tts,
        "wake_reply_zh": character.wake_reply_zh,
        "tts_language": character.tts_language,
    }
    if character.avatar:
        data["avatar"] = f"assets/{Path(character.avatar).name}"
    return _dump_toml(data)


def _write_character(directory: Path, character: Character) -> Character:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "character.toml").write_text(_character_toml(character), encoding="utf-8")
    (directory / "sheet.md").write_text(character.sheet_text, encoding="utf-8")
    return Character.load(character.id, directory.parent.parent)


def save_character(config_dir: Path, character: Character) -> Character:
    return _write_character(character_dir(config_dir, character.id), character)


def update_character(config_dir: Path, character_id: str, updates: dict[str, Any],
                     sheet_text: str | None = None) -> Character:
    current = Character.load(character_id, config_dir)
    if not current.directory:
        migrate_legacy_character(config_dir, character_id)
        current = Character.load(character_id, config_dir)
    payload = current.to_dict()
    for key in ("name", "name_zh", "name_romaji", "description", "color", "voice_id",
                "tts_language", "wake_reply_tts", "wake_reply_zh"):
        if key in updates and updates[key] is not None:
            payload[key] = str(updates[key])
    if "wake_words" in updates and updates["wake_words"] is not None:
        raw = updates["wake_words"]
        if isinstance(raw, str):
            raw = re.split(r"[,，、\s]+", raw)
        payload["wake_words"] = [str(word).strip() for word in raw if str(word).strip()]
    sheet = sheet_text if sheet_text is not None else current.sheet_text
    errors, _ = validate_character(payload, sheet)
    if errors:
        raise CharacterError("；".join(errors))
    merged = Character(
        id=current.id,
        name=str(payload.get("name", current.name)),
        name_zh=str(payload.get("name_zh", current.name_zh)),
        name_romaji=str(payload.get("name_romaji", current.name_romaji)),
        color=str(payload.get("color", current.color)),
        voice_id=str(payload.get("voice_id", current.voice_id)),
        sheet="",
        wake_words=[str(word) for word in payload.get("wake_words", current.wake_words)],
        wake_reply_tts=str(payload.get("wake_reply_tts", current.wake_reply_tts)),
        wake_reply_zh=str(payload.get("wake_reply_zh", current.wake_reply_zh)),
        tts_language=str(payload.get("tts_language", current.tts_language)),
        sheet_text=sheet,
        description=str(payload.get("description", current.description)),
        avatar=current.avatar,
        directory=current.directory,
    )
    return _write_character(character_dir(config_dir, character_id), merged)


def _image_extension(name: str, data: bytes) -> str | None:
    suffix = Path(name).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        return None
    for signature, detected in IMAGE_SIGNATURES.items():
        if data.startswith(signature):
            if detected == ".webp" and not (len(data) > 12 and data[8:12] == b"WEBP"):
                continue
            return suffix
    return None


def _safe_members(zip_file: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """Map safe member names to ZipInfo, tolerating macOS metadata and one top folder."""
    names: dict[str, zipfile.ZipInfo] = {}
    for info in zip_file.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if name.startswith("/"):
            raise CharacterError(f"不安全的路径：{name}")
        while name.startswith("./"):
            name = name[2:]
        if not name or name == ".":
            continue
        if name.startswith("__MACOSX/") or name.endswith(".DS_Store"):
            continue
        names[name] = info
    if "character.toml" not in names:
        candidates = [name for name in names if name.endswith("/character.toml")]
        if len(candidates) == 1:
            prefix = candidates[0][: -len("character.toml")]
            if all(name.startswith(prefix) for name in names):
                names = {name[len(prefix):]: info for name, info in names.items()}
    if len(names) > MAX_MEMBERS:
        raise CharacterError(f"压缩包文件太多（最多 {MAX_MEMBERS} 个）")
    total = 0
    for name, info in names.items():
        path = Path(name)
        if name.startswith("/") or ".." in path.parts or path.is_absolute():
            raise CharacterError(f"不安全的路径：{name}")
        if not (name in {"character.toml", "sheet.md", "README.md", "LICENSE", "LICENSE.md", "NOTICE"}
                or name.startswith("assets/")):
            raise CharacterError(f"不允许的文件：{name}")
        if info.file_size > MAX_AVATAR_BYTES and name.startswith("assets/"):
            raise CharacterError(f"资源文件过大：{name}")
        total += info.file_size
        if total > MAX_ARCHIVE_BYTES:
            raise CharacterError("压缩包解压后过大")
    return names


def import_archive(config_dir: Path, payload: bytes, *, overwrite: bool = False) -> Character:
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise CharacterError("文件大于 20MB")
    try:
        zip_file = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise CharacterError("不是有效的 zip / .eidolon 文件") from exc
    with zip_file:
        names = _safe_members(zip_file)
        if "character.toml" not in names:
            raise CharacterError("zip 根目录（或单一顶层文件夹）里必须有 character.toml")
        if "sheet.md" not in names:
            raise CharacterError("缺少 sheet.md")
        toml_bytes = zip_file.read(names["character.toml"])
        sheet_bytes = zip_file.read(names["sheet.md"])
        if len(toml_bytes) > MAX_TEXT_BYTES or len(sheet_bytes) > MAX_TEXT_BYTES:
            raise CharacterError("character.toml / sheet.md 过大")
        try:
            import tomllib

            data = tomllib.loads(toml_bytes.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise CharacterError(f"character.toml 不是有效的 TOML：{exc}") from exc
        sheet_text = sheet_bytes.decode("utf-8", "replace")
        errors, _ = validate_character(data, sheet_text)
        if errors:
            raise CharacterError("；".join(errors))
        character_id = str(data.get("id") or "").strip().lower()
        if not ID_RE.match(character_id):
            character_id = slugify(str(data.get("name_romaji") or data.get("name") or "character"))
        target = character_dir(config_dir, character_id)
        if (target.exists() or (characters_dir(config_dir) / f"{character_id}.toml").exists()) and not overwrite:
            raise CharacterExists(f"角色 {character_id} 已存在")

        avatar_name = ""
        avatar_bytes: bytes | None = None
        for name, info in names.items():
            if name.startswith("assets/"):
                blob = zip_file.read(info)
                extension = _image_extension(name, blob)
                if extension is None:
                    continue
                if avatar_bytes is not None:
                    continue
                avatar_bytes = blob
                avatar_name = f"assets/avatar{extension}"

        temp = characters_dir(config_dir) / f".import-{uuid.uuid4().hex}"
        temp.mkdir(parents=True, exist_ok=True)
        try:
            character = Character(
                id=character_id,
                name=str(data.get("name", character_id)),
                name_zh=str(data.get("name_zh", data.get("name", character_id))),
                name_romaji=str(data.get("name_romaji", "")),
                color=str(data.get("color", "#d4a24c")),
                voice_id=str(data.get("voice_id", "")),
                sheet="",
                wake_words=[str(word) for word in data.get("wake_words", [])],
                wake_reply_tts=str(data.get("wake_reply_tts", "……ここにいる。")),
                wake_reply_zh=str(data.get("wake_reply_zh", "我在。")),
                tts_language=str(data.get("tts_language", "ja")),
                sheet_text=sheet_text,
                description=str(data.get("description", "")),
                avatar="",
                directory="",
            )
            if avatar_bytes is not None:
                extension = _image_extension(avatar_name, avatar_bytes) or ".png"
                avatar_path = temp / "assets" / f"avatar{extension}"
                avatar_path.parent.mkdir(parents=True, exist_ok=True)
                avatar_path.write_bytes(avatar_bytes)
                character = Character(**{**character.__dict__, "avatar": str(avatar_path)})
            (temp / "character.toml").write_text(_character_toml(character), encoding="utf-8")
            (temp / "sheet.md").write_text(sheet_text, encoding="utf-8")
            for optional in ("README.md", "LICENSE", "LICENSE.md", "NOTICE"):
                if optional in names:
                    (temp / optional).write_bytes(zip_file.read(names[optional]))
            if target.exists():
                shutil.rmtree(target)
            legacy_toml = characters_dir(config_dir) / f"{character_id}.toml"
            legacy_sheet = characters_dir(config_dir) / f"{character_id}.md"
            legacy_toml.unlink(missing_ok=True)
            legacy_sheet.unlink(missing_ok=True)
            temp.replace(target)
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise
    return Character.load(character_id, config_dir)


def export_archive(config_dir: Path, character_id: str) -> bytes:
    character = Character.load(character_id, config_dir)
    if not character.directory:
        migrate_legacy_character(config_dir, character_id)
        character = Character.load(character_id, config_dir)
    directory = Path(character.directory)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("character.toml", _character_toml(character))
        archive.writestr("sheet.md", character.sheet_text)
        for optional in ("README.md", "LICENSE", "LICENSE.md", "NOTICE"):
            path = directory / optional
            if path.exists() and path.is_file():
                archive.write(path, optional)
        assets = directory / "assets"
        if assets.exists():
            for path in sorted(assets.rglob("*")):
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                    archive.write(path, str(path.relative_to(directory)))
    return buffer.getvalue()


def delete_character(config_dir: Path, character_id: str) -> None:
    directory = character_dir(config_dir, character_id)
    if directory.exists():
        shutil.rmtree(directory)
        return
    toml = characters_dir(config_dir) / f"{character_id}.toml"
    sheet = characters_dir(config_dir) / f"{character_id}.md"
    if not toml.exists():
        raise CharacterError(f"角色 {character_id} 不存在")
    toml.unlink(missing_ok=True)
    sheet.unlink(missing_ok=True)


def avatar_path(config_dir: Path, character_id: str) -> Path | None:
    character = Character.load(character_id, config_dir)
    if character.avatar and Path(character.avatar).exists():
        return Path(character.avatar)
    return None


def activate_character(config_dir: Path, character_id: str) -> None:
    update_local_settings({"app": {"character": character_id}})


def migrate_legacy_character(config_dir: Path, character_id: str) -> None:
    legacy = characters_dir(config_dir) / f"{character_id}.toml"
    if not legacy.exists() or character_dir(config_dir, character_id).exists():
        return
    character = Character.load(character_id, config_dir)
    target = character_dir(config_dir, character_id)
    target.mkdir(parents=True, exist_ok=True)
    (target / "sheet.md").write_text(character.sheet_text, encoding="utf-8")
    avatar_value = ""
    if character.avatar and Path(character.avatar).exists():
        suffix = Path(character.avatar).suffix.lower() or ".png"
        avatar_target = target / "assets" / f"avatar{suffix}"
        avatar_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(character.avatar, avatar_target)
        avatar_value = str(avatar_target)
    migrated = Character(**{**character.__dict__, "sheet": "", "directory": str(target),
                            "avatar": avatar_value})
    (target / "character.toml").write_text(_character_toml(migrated), encoding="utf-8")
    legacy.unlink(missing_ok=True)
    legacy_sheet = characters_dir(config_dir) / f"{character_id}.md"
    legacy_sheet.unlink(missing_ok=True)
    log.info("migrated legacy character %s -> %s", character_id, target)


def migrate_legacy_characters(config_dir: Path) -> None:
    for item in characters_dir(config_dir).glob("*.toml"):
        try:
            migrate_legacy_character(config_dir, item.stem)
        except (FileNotFoundError, ValueError, OSError) as exc:
            log.warning("could not migrate %s: %s", item, exc)
