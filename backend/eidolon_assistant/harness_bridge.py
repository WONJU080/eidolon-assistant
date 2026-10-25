"""DeepSeek Harness over ACP: one child process, one persistent session.

The harness is an independent ``dsh --profile acp`` process speaking
newline-delimited JSON-RPC on stdin/stdout (verified in P0-A). This bridge owns
that process: start / initialize / session-new, prompt streaming, permission
requests, cancellation with a hard process-group kill fallback, and teardown
that leaves no orphan.

Permission mode is chosen per launch through ``DSH_PERMISSION_MODE``:
``read-only`` makes every write or shell escalation ask this client;
``danger-full-access`` makes the harness never ask (UI must warn).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import HarnessSettings
from .redact import clip, redact, redact_obj

PROTOCOL_VERSION = 1
DEFAULT_DSH = "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"


class HarnessError(RuntimeError):
    pass


class HarnessStartError(HarnessError):
    def __init__(self, message: str, stderr_tail: list[str] | None = None) -> None:
        super().__init__(message)
        self.stderr_tail = stderr_tail or []


@dataclass
class ToolCall:
    tool_call_id: str
    title: str = ""
    kind: str = ""
    status: str = ""
    command: str = ""
    justification: str = ""
    sandbox_permissions: str = ""
    output: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        name = self.command or self.title or self.kind or "tool"
        return clip(redact(name), 300)

    def as_dict(self) -> dict[str, Any]:
        return {
            "toolCallId": self.tool_call_id,
            "title": self.title,
            "kind": self.kind,
            "status": self.status,
            "command": redact(self.command),
            "justification": redact(self.justification),
            "sandbox_permissions": self.sandbox_permissions,
            "output": [clip(redact(x), 2000) for x in self.output[-6:]],
        }


@dataclass
class TaskResult:
    stop_reason: str
    messages: list[str]
    tools: list[ToolCall]
    error: str | None
    duration_s: float
    cancelled: bool = False
    permission_denials: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None and self.stop_reason == "end_turn"

    def digest(self, limit: int = 3500) -> str:
        lines: list[str] = []
        for message in self.messages:
            if message.strip():
                lines.append(f"[助手消息] {clip(redact(message.strip()), 900)}")
        for tool in self.tools:
            lines.append(f"[工具] {redact(tool.title or tool.kind)} 状态={tool.status}")
            if tool.command:
                lines.append(f"  命令: {clip(redact(tool.command), 400)}")
            for out in tool.output[-2:]:
                if out.strip():
                    lines.append(f"  输出: {clip(redact(out.strip()), 700)}")
        if self.error:
            lines.append(f"[错误] {redact(self.error)}")
        lines.append(f"[结束] stop_reason={self.stop_reason}")
        return clip("\n".join(lines), limit)


def _describe_input(raw_input: dict[str, Any]) -> str:
    """What a tool call is about, for a human, whichever tool it is."""
    for key in ("command", "cmd"):
        value = raw_input.get(key)
        if value:
            return str(value)
    labelled = []
    for key in ("path", "file_path", "old_path", "new_path", "url", "query", "pattern",
                "glob", "description", "prompt", "name"):
        value = raw_input.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            labelled.append(f"{key}: {value}")
    if labelled:
        return "；".join(labelled)[:800]
    rest = {k: v for k, v in raw_input.items()
            if k not in ("content", "text", "justification", "sandbox_permissions")}
    if not rest:
        return ""
    try:
        return json.dumps(rest, ensure_ascii=False)[:800]
    except (TypeError, ValueError):
        return str(rest)[:800]


class HarnessBridge:
    def __init__(
        self,
        settings: HarnessSettings,
        workspace: Path,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        permission_timeout_s: float = 120.0,
        max_event_text: int = 4000,
    ) -> None:
        self.settings = settings
        self.workspace = workspace
        self.on_event = on_event
        self.permission_timeout_s = permission_timeout_s
        self.max_event_text = max_event_text

        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr_reader: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._prompt_lock = asyncio.Lock()

        self.session_id: str | None = None
        self.status = "stopped"
        self.stderr_tail: list[str] = []
        self.last_error: str | None = None
        self.pending_permissions: dict[str, dict[str, Any]] = {}
        self._permission_futures: dict[str, asyncio.Future] = {}
        self._tools: dict[str, ToolCall] = {}
        self._messages: list[str] = []
        self._current_message_id: str | None = None
        self._denials = 0
        self._task_active = False
        self._started_at = 0.0
        self._acp_ready = False
        self._start_lock = asyncio.Lock()

    # ------------------------------------------------------------------ state
    def public_state(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "session": self.session_id,
            "pid": self._proc.pid if self._proc else None,
            "permission_mode": self.settings.permission_mode,
            "pending_permissions": [self._public_permission(p) for p in self.pending_permissions.values()],
            "last_error": redact(self.last_error or ""),
            "stderr_tail": [redact(x) for x in self.stderr_tail[-8:]],
            "active": self._task_active,
        }

    # ---------------------------------------------------------------- process
    def _argv(self) -> list[str]:
        command = self.settings.command or DEFAULT_DSH
        return [command, "--profile", self.settings.profile]

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in ("DSH_PROFILE", "DSH_PROFILE_DIR", "DSH_WEB_URL", "DSH_SESSION_ID", "DSH_EXECUTABLE"):
            env.pop(key, None)
        env["DSH_HOME"] = env.get("DSH_HOME") or str(Path.home() / ".dsh")
        env["DSH_PERMISSION_MODE"] = self.settings.permission_mode
        return env

    async def start(self) -> None:
        async with self._start_lock:
            await self._start_locked()

    async def _start_locked(self) -> None:
        if self._proc and self._proc.returncode is None and self._acp_ready:
            return
        await self._stop_process(graceful=False)
        command = self._argv()[0]
        if not Path(command).exists():
            raise HarnessStartError(f"找不到 DeepSeek Harness CLI：{command}")
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.status = "starting"
        await self.on_event({"type": "harness", "status": "starting"})
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv(),
                cwd=str(self.workspace),
                env=self._env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=16 * 1024 * 1024,
            )
        except OSError as exc:
            self.status = "error"
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise HarnessStartError(f"无法启动 Harness：{self.last_error}") from exc
        self._started_at = time.time()
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr_reader = asyncio.create_task(self._stderr_loop())
        try:
            init = await self.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                       "terminal": False},
                "clientInfo": {"name": "eidolon-assistant", "version": "0.1.0"},
            }, timeout=self.settings.startup_timeout_s)
            caps = (init or {}).get("agentCapabilities") or {}
            session = await self.request("session/new", {
                "cwd": str(self.workspace), "mcpServers": [],
            }, timeout=self.settings.startup_timeout_s)
            self.session_id = session.get("sessionId")
            self._acp_ready = True
            self.status = "ready"
            self.last_error = None
            await self.on_event({"type": "harness", "status": "ready", "session": self.session_id,
                                 "capabilities": caps,
                                 "models": self._model_options(session)})
        except Exception as exc:  # noqa: BLE001
            self.status = "error"
            self.last_error = f"{type(exc).__name__}: {exc}"
            tail = list(self.stderr_tail[-15:])
            await self._stop_process(graceful=False)
            raise HarnessStartError(f"Harness ACP 握手失败：{exc}", tail) from exc

    @staticmethod
    def _model_options(session: dict[str, Any]) -> list[dict[str, Any]]:
        for option in session.get("configOptions") or []:
            if option.get("id") == "model":
                groups = option.get("options") or []
                out: list[dict[str, Any]] = []
                for group in groups:
                    if isinstance(group, dict) and "options" in group:
                        for item in group["options"]:
                            out.append({"value": item.get("value"), "name": item.get("name")})
                    elif isinstance(group, dict):
                        out.append({"value": group.get("value"), "name": group.get("name")})
                return out
        return []

    async def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            try:
                line = await proc.stderr.readline()
            except ValueError:
                continue
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\n")
            self.stderr_tail.append(text)
            if len(self.stderr_tail) > 300:
                del self.stderr_tail[:150]

    # ------------------------------------------------------------------- wire
    async def _write(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise HarnessError("Harness 进程不在运行")
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            proc.stdin.write(data.encode("utf-8"))
            await proc.stdin.drain()

    async def request(self, method: str, params: dict[str, Any], timeout: float = 120.0) -> Any:
        self._next_id += 1
        request_id = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise HarnessError(f"{method} 超时（{timeout:.0f}s）") from None
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while True:
            try:
                line = await proc.stdout.readline()
            except ValueError:
                continue
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                await self.on_event({"type": "harness_pollution", "raw": clip(text, 500)})
                continue
            try:
                await self._dispatch(message)
            except Exception as exc:  # noqa: BLE001
                await self.on_event({"type": "harness_internal_error",
                                     "error": f"{type(exc).__name__}: {exc}"})
        # process ended
        self._fail_pending(HarnessError("Harness 进程已退出"))
        if self._acp_ready:
            self.status = "error"
            self.last_error = f"Harness 进程退出（code={proc.returncode}）"
            await self.on_event({"type": "harness", "status": "error", "error": self.last_error})

    def _fail_pending(self, exc: BaseException) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
        for future in list(self._permission_futures.values()):
            if not future.done():
                future.set_exception(exc)
        self._permission_futures.clear()
        self.pending_permissions.clear()
        self._acp_ready = False

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" in message:
            if "id" in message:
                await self._handle_server_request(message)
            else:
                await self._handle_notification(message)
            return
        future = self._pending.get(message.get("id"))
        if future is None or future.done():
            return
        if "error" in message:
            err = message.get("error") or {}
            future.set_exception(HarnessError(f"{err.get('message', 'unknown')} ({err.get('code')})"))
        else:
            future.set_result(message.get("result"))

    async def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        await self.on_event({"type": "acp_event", "method": method, "params": redact_obj(params),
                             "at": time.time()})
        if method != "session/update":
            return
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text":
                text = content.get("text") or ""
                message_id = update.get("messageId")
                if message_id and message_id != self._current_message_id:
                    self._messages.append("")
                    self._current_message_id = message_id
                if not self._messages:
                    self._messages.append("")
                self._messages[-1] += text
                await self.on_event({"type": "assistant_delta", "text": text, "at": time.time()})
        elif kind == "agent_thought_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text":
                await self.on_event({"type": "thought_delta", "text": content.get("text", "")[:1000]})
        elif kind == "tool_call":
            tool = self._tools.get(update.get("toolCallId", ""), ToolCall(update.get("toolCallId", "")))
            self._merge_tool(tool, update)
            self._tools[tool.tool_call_id] = tool
            await self._emit_tool(tool)
        elif kind == "tool_call_update":
            tool = self._tools.get(update.get("toolCallId", ""), ToolCall(update.get("toolCallId", "")))
            self._merge_tool(tool, update)
            self._tools[tool.tool_call_id] = tool
            await self._emit_tool(tool)
        elif kind == "usage_update":
            pass
        else:
            await self.on_event({"type": "acp_update", "update": redact_obj(update)})

    @staticmethod
    def _merge_tool(tool: ToolCall, update: dict[str, Any]) -> None:
        for field_name in ("title", "kind", "status"):
            value = update.get(field_name)
            if value:
                setattr(tool, field_name, str(value))
        raw_input = update.get("rawInput")
        if isinstance(raw_input, dict):
            described = _describe_input(raw_input)
            if described:
                tool.command = described
            justification = raw_input.get("justification") or ""
            if justification:
                tool.justification = str(justification)
            sandbox = raw_input.get("sandbox_permissions") or ""
            if sandbox:
                tool.sandbox_permissions = str(sandbox)
        content = update.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                inner = item.get("content") or {}
                if isinstance(inner, dict) and inner.get("type") == "text":
                    text = inner.get("text") or ""
                    if text and (not tool.output or tool.output[-1] != text):
                        tool.output.append(text)

    async def _emit_tool(self, tool: ToolCall) -> None:
        await self.on_event({"type": "tool", "tool": tool.as_dict(), "at": time.time()})

    # ------------------------------------------------------------- permission
    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if method != "session/request_permission":
            await self._write({"jsonrpc": "2.0", "id": request_id,
                               "error": {"code": -32601, "message": f"unsupported: {method}"}})
            return
        request = self._build_permission(params)
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self.pending_permissions[request["id"]] = request
        self._permission_futures[request["id"]] = future
        await self.on_event({"type": "permission_request", "request": self._public_permission(request)})
        try:
            answer = await asyncio.wait_for(future, timeout=self.permission_timeout_s + 15)
        except asyncio.TimeoutError:
            answer = "cancel"
        finally:
            self.pending_permissions.pop(request["id"], None)
            self._permission_futures.pop(request["id"], None)
        if answer == "allow":
            option = next((o for o in request["options"] if str(o.get("kind", "")).startswith("allow")), None)
            result = ({"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
                      if option else {"outcome": {"outcome": "cancelled"}})
        elif answer == "deny":
            self._denials += 1
            option = next((o for o in request["options"] if str(o.get("kind", "")).startswith("reject")), None)
            result = ({"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
                      if option else {"outcome": {"outcome": "cancelled"}})
        else:
            result = {"outcome": {"outcome": "cancelled"}}
        await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})
        await self.on_event({"type": "permission_answered", "request_id": request["id"],
                             "answer": answer})

    def _build_permission(self, params: dict[str, Any]) -> dict[str, Any]:
        call_id = (params.get("toolCall") or {}).get("toolCallId") or ""
        tool = self._tools.get(call_id)
        options = [{"optionId": o.get("optionId"), "name": o.get("name"), "kind": o.get("kind")}
                   for o in (params.get("options") or []) if isinstance(o, dict)]
        return {
            # "perm_" keeps ids out of the long-hex secret redactor
            "id": f"perm_{uuid.uuid4().hex}",
            "tool_call_id": call_id,
            "title": tool.title if tool else "工具调用",
            "command": tool.command if tool else "",
            "justification": tool.justification if tool else "",
            "sandbox_permissions": tool.sandbox_permissions if tool else "",
            "kind": tool.kind if tool else "",
            "options": options,
            "requested_at": time.time(),
        }

    @staticmethod
    def _public_permission(request: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": request["id"],
            "tool_call_id": request["tool_call_id"],
            "title": redact(request["title"]),
            "command": clip(redact(request["command"]), 1200),
            "justification": clip(redact(request["justification"]), 600),
            "sandbox_permissions": request["sandbox_permissions"],
            "kind": request["kind"],
            "options": request["options"],
            "requested_at": request["requested_at"],
        }

    def answer_permission(self, request_id: str, answer: str) -> bool:
        future = self._permission_futures.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(answer if answer in ("allow", "deny", "cancel") else "cancel")
        return True

    # ------------------------------------------------------------------ tasks
    async def prompt(self, text: str, timeout: float | None = None) -> TaskResult:
        if self.status not in ("ready", "busy") or not self._acp_ready:
            await self.start()
        if not self.session_id:
            await self.start()
        async with self._prompt_lock:
            self._task_active = True
            self.status = "busy"
            self._tools = {}
            self._messages = []
            self._current_message_id = None
            self._denials = 0
            started = time.time()
            await self.on_event({"type": "task_started", "text": clip(redact(text), 1000)})
            error: str | None = None
            stop_reason = "error"
            try:
                result = await self.request("session/prompt", {
                    "sessionId": self.session_id,
                    "prompt": [{"type": "text", "text": text}],
                }, timeout=timeout or self.settings.prompt_timeout_s)
                stop_reason = (result or {}).get("stopReason", "unknown")
            except asyncio.TimeoutError:
                error = "任务超时"
                stop_reason = "timeout"
            except HarnessError as exc:
                error = str(exc)
                stop_reason = "error"
            finally:
                self._task_active = False
                self.status = "ready" if self._proc and self._proc.returncode is None else "error"
            result = TaskResult(
                stop_reason=stop_reason,
                messages=[m for m in self._messages if m.strip()],
                tools=list(self._tools.values()),
                error=error,
                duration_s=round(time.time() - started, 2),
                cancelled=stop_reason == "cancelled",
                permission_denials=self._denials,
            )
            await self.on_event({"type": "task_finished", "result": {
                "stop_reason": result.stop_reason,
                "ok": result.ok,
                "error": redact(result.error or ""),
                "duration_s": result.duration_s,
                "denials": result.permission_denials,
                "message": clip(redact("\n".join(result.messages)), 3000),
            }})
            return result

    async def cancel(self) -> dict[str, Any]:
        """session/cancel, then wait; hard-kill the process group if it ignores us."""
        if not self._acp_ready or not self.session_id:
            return {"cancelled": False, "reason": "not_running"}
        for request_id in list(self._permission_futures):
            self.answer_permission(request_id, "cancel")
        try:
            await self.notify("session/cancel", {"sessionId": self.session_id})
        except HarnessError:
            return {"cancelled": False, "reason": "write_failed"}
        deadline = time.monotonic() + self.settings.cancel_grace_s
        while time.monotonic() < deadline:
            if not self._task_active:
                return {"cancelled": True, "reason": "settled", "forced": False}
            await asyncio.sleep(0.05)
        await self._stop_process(graceful=False)
        await self.on_event({"type": "harness", "status": "killed", "reason": "cancel_timeout"})
        return {"cancelled": True, "reason": "killed", "forced": True}

    async def new_session(self) -> str | None:
        if not self._acp_ready:
            await self.start()
            return self.session_id
        if self.session_id:
            try:
                await self.request("session/close", {"sessionId": self.session_id}, timeout=20)
            except HarnessError:
                pass
        session = await self.request("session/new", {"cwd": str(self.workspace), "mcpServers": []},
                                     timeout=self.settings.startup_timeout_s)
        self.session_id = session.get("sessionId")
        await self.on_event({"type": "harness", "status": "ready", "session": self.session_id,
                             "new_session": True})
        return self.session_id

    # ---------------------------------------------------------------- teardown
    async def _stop_process(self, *, graceful: bool) -> None:
        proc = self._proc
        self._proc = None
        self._acp_ready = False
        self.status = "stopped"
        if proc is None:
            return
        if graceful and self.session_id and proc.returncode is None:
            try:
                await self.request("session/close", {"sessionId": self.session_id},
                                   timeout=self.settings.close_timeout_s)
            except Exception:  # noqa: BLE001
                pass
        if proc.stdin is not None and not proc.stdin.is_closing():
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            self._signal_group(proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._signal_group(proc, signal.SIGKILL)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    pass
        self._signal_group(proc, signal.SIGKILL)
        for task in (self._reader, self._stderr_reader):
            if task is not None and not task.done():
                task.cancel()
        self._reader = self._stderr_reader = None
        self._fail_pending(HarnessError("Harness 已关闭"))
        self.session_id = None

    @staticmethod
    def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    async def close(self) -> None:
        session = self.session_id
        if session and self._proc and self._proc.returncode is None:
            try:
                await self.request("session/close", {"sessionId": session},
                                   timeout=self.settings.close_timeout_s)
            except Exception:  # noqa: BLE001
                pass
        await self._stop_process(graceful=True)

    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None
