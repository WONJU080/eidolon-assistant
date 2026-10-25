from eidolon_assistant.llm import _extract_json, parse_bilingual
from eidolon_assistant.pinyin import PinyinError, phrase_to_tokens, split_syllable


def test_split_syllable_with_initial() -> None:
    assert split_syllable("lā") == ["l", "ā"]
    assert split_syllable("mǔ") == ["m", "ǔ"]
    assert split_syllable("zhōng") == ["zh", "ōng"]
    assert split_syllable("lěi") == ["l", "ěi"]


def test_split_syllable_zero_initial() -> None:
    assert split_syllable("ěr") == ["ěr"]
    assert split_syllable("āi") == ["āi"]


def test_phrase_tokens_for_wake_word() -> None:
    assert phrase_to_tokens("拉姆") == ["l", "ā", "m", "ǔ"]
    assert phrase_to_tokens("拉姆蕾萨尔")[:4] == ["l", "ā", "m", "ǔ"]


def test_phrase_rejects_unknown_syllable_when_tokens_given() -> None:
    try:
        phrase_to_tokens("拉姆", tokens={"l", "ā"})
    except PinyinError as exc:
        assert "m" in str(exc) or "ǔ" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected PinyinError")


def test_phrase_skips_non_han() -> None:
    assert phrase_to_tokens("拉姆！") == ["l", "ā", "m", "ǔ"]


def test_parse_bilingual_tags() -> None:
    reply = parse_bilingual("……そう。\n<ja>……そう。</ja><zh>……这样啊。</zh>")
    assert reply.ja == "……そう。"
    assert reply.zh == "……这样啊。"


def test_parse_bilingual_missing_zh_falls_back() -> None:
    reply = parse_bilingual("<ja>……わかった。</ja>")
    assert reply.ja == "……わかった。"
    assert reply.zh == "……わかった。"


def test_parse_bilingual_plain_text() -> None:
    reply = parse_bilingual("……わからない。")
    assert reply.ja == "……わからない。"
    assert reply.zh == "……わからない。"


def test_extract_json_fenced_and_noisy() -> None:
    assert _extract_json('```json\n{"intent":"task"}\n```')["intent"] == "task"
    assert _extract_json('好的：{"intent":"chat","reason":"x"} 以上')["reason"] == "x"
    assert _extract_json("not json") == {}
