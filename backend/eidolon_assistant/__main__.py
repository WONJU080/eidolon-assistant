"""``python -m eidolon_assistant [serve|check]`` — the backend entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import sys
from logging.handlers import RotatingFileHandler
from dataclasses import replace

import uvicorn

from .config import Character, Settings
from .network import detect_system_proxy


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(host: str, preferred: int, tries: int = 30) -> int:
    if preferred and _port_free(host, preferred):
        return preferred
    for offset in range(1, tries + 1):
        candidate = preferred + offset
        if _port_free(host, candidate):
            return candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def setup_logging(settings: Settings) -> None:
    logger = logging.getLogger("eidolon")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(settings.log_dir / "eidolon.log",
                                      maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)


def serve(args: argparse.Namespace) -> int:
    settings = Settings.load()
    port = args.port or pick_port(settings.server_host, settings.server_port)
    settings = replace(settings, server_port=port)
    setup_logging(settings)
    logging.getLogger("eidolon").info("serving on %s:%d mode=%s workspace=%s",
                                      settings.server_host, port,
                                      settings.harness.permission_mode, settings.workspace_dir)
    from .server import create_app

    app = create_app(settings)
    uvicorn.run(app, host=settings.server_host, port=port, log_level=args.log_level,
                access_log=False)
    return 0


def check() -> int:
    settings = Settings.load()
    character = Character.load(settings.character_id)
    proxy = detect_system_proxy()
    kws_dir = settings.models_dir / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
    report = {
        "settings": {
            "data_dir": str(settings.data_dir),
            "workspace": str(settings.workspace_dir),
            "permission_mode": settings.harness.permission_mode,
            "dsh": settings.harness.command,
        },
        "character": {"id": character.id, "wake_words": character.wake_words},
        "services": {
            "llm": {"model": settings.llm.model, "key": settings.llm.ready},
            "stt": {"model": settings.stt.model, "key": settings.stt.ready},
            "tts": {"model": settings.tts.model, "key": settings.tts.ready},
        },
        "kws": {"dir": str(kws_dir), "exists": kws_dir.exists()},
        "proxy": {"https": proxy.https, "socks": proxy.socks},
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eidolon-assistant")
    sub = parser.add_subparsers(dest="command")
    serve_parser = sub.add_parser("serve", help="run the local API and WebSocket server")
    serve_parser.add_argument("--port", type=int, default=0)
    serve_parser.add_argument("--log-level", default="info")
    sub.add_parser("check", help="print configuration and service readiness")
    args = parser.parse_args(argv)
    if args.command == "check":
        return check()
    if args.command in (None, "serve"):
        if args.command is None:
            args.port = 0
            args.log_level = "info"
        return serve(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
