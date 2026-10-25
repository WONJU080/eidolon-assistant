"""Redaction for anything that leaves memory: logs, SQLite rows, the raw panel.

Rules of the house: no API key, token, password, or full home path is ever shown
in the UI, written to a task record, or pasted into a prompt.
"""

from __future__ import annotations

import re
from typing import Any

_SECRET_KEY_HINT = re.compile(r"(api[_-]?key|token|secret|password|passwd|authorization|cookie)", re.I)
_KEY_VALUE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd)"
    r"(\s*[:=]\s*)(['\"]?)([A-Za-z0-9_\-./+=]{8,})\3"
)
_BEARER = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9_\-./+=]{8,})")
_SK_STYLE = re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_\-]{12,}\b")
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_HOME = re.compile(r"/Users/[^/\s]+")


def redact(text: Any) -> str:
    """A string safe to log, store, or show raw."""
    if text is None:
        return ""
    s = str(text)
    s = _BEARER.sub(lambda m: m.group(1) + "***", s)
    s = _KEY_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", s)
    s = _SK_STYLE.sub(lambda m: m.group(1) + "-***", s)
    s = _LONG_HEX.sub("***", s)
    s = _HOME.sub("~", s)
    return s


def redact_value(key: str, value: Any) -> Any:
    if isinstance(value, dict):
        return {k: redact_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(key, v) for v in value]
    if isinstance(value, str):
        if value.startswith(("http://", "https://")) and "@" not in value:
            return value
        return redact(value)
    return value


def redact_obj(obj: Any) -> Any:
    """Redact every string inside a decoded JSON value."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if _SECRET_KEY_HINT.search(str(key)) and isinstance(value, str) and len(value) >= 6:
                out[key] = "***"
            else:
                out[key] = redact_obj(value)
        return out
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


def clip(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，共 {len(text)} 字符）"
