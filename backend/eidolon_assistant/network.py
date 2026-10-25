"""macOS system proxy discovery.

The dev machine resolves some overseas hosts through a local proxy configured in
System Settings (``scutil --proxy``); direct connections to those hosts time out
or resolve to poisoned addresses. Python HTTP clients do not honour the system
proxy by themselves, so we read it once and let services opt in.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ProxyConfig:
    http: str | None = None
    https: str | None = None
    socks: str | None = None

    @property
    def preferred(self) -> str | None:
        return self.https or self.http or self.socks


def detect_system_proxy() -> ProxyConfig:
    try:
        out = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ProxyConfig()

    def field(name: str) -> str | None:
        match = re.search(rf"^\s*{name}\s*:\s*(\S+)\s*$", out, re.M)
        return match.group(1) if match else None

    def enabled(name: str) -> bool:
        return field(name) == "1"

    http = None
    if enabled("HTTPEnable"):
        host, port = field("HTTPProxy"), field("HTTPPort")
        if host and port:
            http = f"http://{host}:{port}"
    https = None
    if enabled("HTTPSEnable"):
        host, port = field("HTTPSProxy"), field("HTTPSPort")
        if host and port:
            https = f"http://{host}:{port}"
    socks = None
    if enabled("SOCKSEnable"):
        host, port = field("SOCKSProxy"), field("SOCKSPort")
        if host and port:
            socks = f"socks5://{host}:{port}"
    return ProxyConfig(http=http, https=https, socks=socks)


def resolve_proxy(mode: str, explicit: str = "") -> str | None:
    """``mode``: ``auto`` (read macOS), ``off``, or ``manual`` with ``explicit``."""
    mode = (mode or "auto").lower()
    if mode == "off":
        return None
    if mode == "manual":
        return explicit or None
    return detect_system_proxy().preferred
