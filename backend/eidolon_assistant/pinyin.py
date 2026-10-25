"""Chinese text -> the pinyin token sequence the sherpa-onnx KWS model expects.

The wenetspeech KWS model is trained on pinyin initials and tone-marked finals
(``l ā m ǔ`` for 拉姆). pypinyin gives us tone-marked syllables; this module
splits each into the model's tokens and validates them against ``tokens.txt``
so a bad wake word fails loudly at startup instead of silently never firing.
"""

from __future__ import annotations

from pathlib import Path

from pypinyin import Style, pinyin

INITIALS = (
    "zh", "ch", "sh",
    "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h",
    "j", "q", "x", "r", "z", "c", "s", "y", "w",
)


class PinyinError(ValueError):
    pass


def is_han(char: str) -> bool:
    code = ord(char)
    return 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF


def split_syllable(syllable: str) -> list[str]:
    """``lā`` -> ``['l', 'ā']``; ``ěr`` stays ``['ěr']`` when the model has it."""
    for initial in INITIALS:
        if syllable.startswith(initial) and len(syllable) > len(initial):
            return [initial, syllable[len(initial):]]
    return [syllable]


def load_tokens(tokens_path: Path) -> set[str]:
    tokens: set[str] = set()
    for line in tokens_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        tokens.add(line.split()[0])
    return tokens


def phrase_to_tokens(text: str, tokens: set[str] | None = None) -> list[str]:
    out: list[str] = []
    for char in text:
        if not is_han(char):
            continue
        syllables = pinyin(char, style=Style.TONE, errors=lambda _: [""])
        syllable = syllables[0][0] if syllables and syllables[0] else ""
        if not syllable:
            raise PinyinError(f"无法取拼音：{char}")
        parts = split_syllable(syllable)
        if tokens is not None and any(p not in tokens for p in parts):
            # whole-syllable token, e.g. er / ěr
            if syllable in tokens:
                parts = [syllable]
            else:
                missing = [p for p in parts if p not in tokens]
                raise PinyinError(f"{char}({syllable}) 的音节不在模型词表中：{missing}")
        out.extend(parts)
    if not out:
        raise PinyinError(f"没有可用的汉字：{text!r}")
    return out


def keyword_line(text: str, tokens: set[str] | None = None) -> str:
    parts = phrase_to_tokens(text, tokens)
    return f"{' '.join(parts)} @{text}"
