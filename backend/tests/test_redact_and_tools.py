import json

from eidolon_assistant.harness_bridge import _describe_input
from eidolon_assistant.redact import redact, redact_obj


def test_redact_bearer_and_key_value() -> None:
    text = "Authorization: Bearer abcdefgh12345678 api_key=sk-abcdefghijklmnop"
    cleaned = redact(text)
    assert "abcdefgh12345678" not in cleaned
    assert "sk-abcdefghijklmnop" not in cleaned
    assert "***" in cleaned


def test_redact_home_path() -> None:
    assert "/Users/ramlethal/Library" not in redact("/Users/ramlethal/Library/Application Support")


def test_redact_long_hex() -> None:
    secret = "a" * 32
    assert secret not in redact(f"key {secret}")


def test_redact_obj_nested_secret_keys() -> None:
    payload = {"api_key": "abcdef123456", "nested": {"token": "xyz123456"}, "safe": "hello"}
    cleaned = redact_obj(payload)
    assert cleaned["api_key"] == "***"
    assert cleaned["nested"]["token"] == "***"
    assert cleaned["safe"] == "hello"


def test_redact_obj_keeps_short_session_ids() -> None:
    payload = {"sessionId": "c1f26946-7bb6-4454-8f93-ccbe162ef74d"}
    cleaned = redact_obj(payload)
    assert cleaned["sessionId"].startswith("c1f26946")


def test_describe_input_command() -> None:
    assert _describe_input({"command": "ls -la", "description": "list"}) == "ls -la"


def test_describe_input_path() -> None:
    described = _describe_input({"file_path": "/tmp/a.txt", "content": "hello"})
    assert "file_path: /tmp/a.txt" in described
    assert "hello" not in described


def test_describe_input_generic_json() -> None:
    described = _describe_input({"custom": 1})
    assert json.loads(described) == {"custom": 1}
