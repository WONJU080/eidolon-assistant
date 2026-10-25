#!/usr/bin/env python3
"""Minimal ACP (Agent Client Protocol v1) stdio client for DeepSeek Harness.

Speaks newline-delimited JSON-RPC 2.0 over a child process' stdin/stdout.
Only the surface Eidolon Assistant needs is implemented; the framing and
request/notification dispatch here is the seed of the real HarnessBridge.

Protocol reference: @agentclientprotocol/sdk schema 1.x (protocolVersion = 1).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

PROTOCOL_VERSION = 1

DSH_APP = Path("/Applications/DeepSeek Harness.app")
DSH_CLI = DSH_APP / "Contents/Resources/runtime/cli/bin/dsh"


def clean_child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for the ACP child.

    The desktop app exports DSH_PROFILE / DSH_PROFILE_DIR / DSH_WEB_URL /
    DSH_SESSION_ID into every process it starts. Those describe *this* agent's
    shell, not a fresh ACP server, so they must not leak into the child.
    """
    env = dict(os.environ)
    for key in ("DSH_PROFILE", "DSH_PROFILE_DIR", "DSH_WEB_URL", "DSH_SESSION_ID", "DSH_EXECUTABLE"):
        env.pop(key, None)
    env.setdefault("DSH_HOME", str(Path.home() / ".dsh"))
    if extra:
        env.update(extra)
    return env


class ACPError(RuntimeError):
    def __init__(self, code: Any, message: str, data: Any = None):
        super().__init__(f"ACP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class ACPClient:
    """One ACP connection over one child process."""

    def __init__(
        self,
        argv: list[str] | None = None,
        *,
        cwd: str | Path,
        env: dict[str, str] | None = None,
        log_path: str | Path | None = None,
        on_permission: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        raw = argv if argv is not None else [str(DSH_CLI), "--profile", "acp"]
        if isinstance(raw, (str, os.PathLike)):
            raw = [str(raw), "--profile", "acp"]
        self.argv = [str(x) for x in raw]
        self.cwd = str(cwd)
        self.env = env or clean_child_env()
        self.on_permission = on_permission
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._closed = False
        self.events: list[dict[str, Any]] = []
        self.stderr_tail: list[str] = []
        self._log_path = Path(log_path) if log_path else None
        self._log = None

    # ---------------------------------------------------------------- process
    async def start(self) -> None:
        if self._log_path:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = self._log_path.open("w", encoding="utf-8")
        self._proc = await asyncio.create_subprocess_exec(
            *self.argv,
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    async def _stderr_loop(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\n")
            self.stderr_tail.append(text)
            if len(self.stderr_tail) > 400:
                del self.stderr_tail[:200]
            if self._log:
                self._log.write(f"[stderr] {text}\n")
                self._log.flush()

    # ------------------------------------------------------------------ wire
    async def _write(self, payload: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        self._proc.stdin.write(data.encode("utf-8"))
        await self._proc.stdin.drain()
        if self._log:
            self._log.write(f"[out] {json.dumps(payload, ensure_ascii=False)[:8000]}\n")
            self._log.flush()

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        while True:
            try:
                line = await self._proc.stdout.readline()
            except ValueError:
                # over-long line: protocol violation, keep draining
                continue
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if not text.strip():
                continue
            if self._log:
                self._log.write(f"[in ] {text[:20000]}\n")
                self._log.flush()
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                # stdout must carry protocol frames only; record pollution
                self.events.append({"t": time.time(), "kind": "stdout_pollution", "raw": text[:2000]})
                continue
            await self._dispatch(msg)
        self._fail_all(ConnectionError("ACP child closed stdout"))

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if "method" in msg:
            if "id" in msg:
                asyncio.create_task(self._handle_server_request(msg))
            else:
                self.events.append(
                    {"t": time.time(), "kind": "notification", "method": msg["method"], "params": msg.get("params")}
                )
        else:
            fut = self._pending.pop(msg.get("id"), None)
            if fut is None or fut.done():
                return
            if "error" in msg:
                err = msg["error"] or {}
                fut.set_exception(ACPError(err.get("code"), err.get("message", "unknown"), err.get("data")))
            else:
                fut.set_result(msg.get("result"))

    async def _handle_server_request(self, msg: dict[str, Any]) -> None:
        method = msg["method"]
        rid = msg["id"]
        params = msg.get("params") or {}
        self.events.append({"t": time.time(), "kind": "server_request", "method": method, "params": params})
        try:
            if method == "session/request_permission" and self.on_permission is not None:
                result = await self.on_permission(params)
            elif method == "session/request_permission":
                result = {"outcome": {"outcome": "cancelled"}}
            else:
                await self._write(
                    {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unsupported client method {method}"}}
                )
                return
            await self._write({"jsonrpc": "2.0", "id": rid, "result": result})
            self.events.append({"t": time.time(), "kind": "server_request_answered", "method": method, "result": result})
        except Exception as exc:  # pragma: no cover - defensive
            await self._write(
                {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}
            )

    def _fail_all(self, exc: BaseException) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def request(self, method: str, params: dict[str, Any], timeout: float = 120.0) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise TimeoutError(f"{method} timed out after {timeout}s") from None

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    # --------------------------------------------------------------- helpers
    async def initialize(self, *, name: str = "eidolon-assistant-p0", version: str = "0.0.0") -> Any:
        return await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": name, "version": version},
            },
            timeout=90,
        )

    async def new_session(self, cwd: str | Path, mcp_servers: list[Any] | None = None) -> dict[str, Any]:
        return await self.request(
            "session/new",
            {"cwd": str(Path(cwd).resolve()), "mcpServers": mcp_servers or []},
            timeout=60,
        )

    async def prompt(self, session_id: str, text: str, timeout: float = 300.0) -> dict[str, Any]:
        return await self.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
            timeout=timeout,
        )

    async def cancel(self, session_id: str) -> None:
        await self.notify("session/cancel", {"sessionId": session_id})

    async def close_session(self, session_id: str, timeout: float = 60.0) -> Any:
        return await self.request("session/close", {"sessionId": session_id}, timeout=timeout)

    def take_events(self) -> list[dict[str, Any]]:
        events, self.events = self.events, []
        return events

    # --------------------------------------------------------------- teardown
    async def stop(self, *, close_stdin: bool = True, grace: float = 5.0) -> dict[str, Any]:
        """Close stdin (the ACP app binds EOF to graceful shutdown) and reap."""
        proc = self._proc
        if proc is None:
            return {"exited": True, "code": None}
        if close_stdin and proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
        code = None
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            proc.terminate()
            try:
                code = await asyncio.wait_for(proc.wait(), timeout=grace)
            except asyncio.TimeoutError:
                proc.kill()
                code = await proc.wait()
        self._closed = True
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        if self._log:
            self._log.close()
            self._log = None
        return {"exited": True, "code": code}
