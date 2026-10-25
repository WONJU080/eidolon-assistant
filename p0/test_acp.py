#!/usr/bin/env python3
"""P0-A: prove DeepSeek Harness `dsh --profile acp` can be driven end to end.

Scenarios
  smoke       initialize -> session/new -> session/prompt(ls) -> session/close
  permission  a write task; the client rejects, then allows, and checks the file
  cancel      a long shell task; session/cancel must settle the prompt fast
  all         smoke then permission then cancel (default)

Run from the project root; writes logs under p0/out/.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acp_client import ACPClient, ACPError, DSH_CLI  # noqa: E402

ROOT = Path(__file__).resolve().parent
WORKDIR = ROOT / "workdir"
OUT = ROOT / "out"

PERMISSION_PROBE = "p0-permission-probe.txt"


class Recorder:
    """Pretty-print ACP traffic while keeping every frame for the report."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.started = time.time()

    def add(self, events: list[dict[str, Any]]) -> None:
        now = time.time()
        for ev in events:
            ev = dict(ev)
            ev["rel"] = round(ev.get("t", now) - self.started, 3)
            self.frames.append(ev)
            kind = ev["kind"]
            if kind == "notification":
                p = ev.get("params") or {}
                if ev["method"] == "session/update":
                    self._update(p.get("update") or {})
                else:
                    print(f"  [notify] {ev['method']} {json.dumps(p, ensure_ascii=False)[:300]}")
            elif kind == "server_request":
                print(f"  [server->client] {ev['method']}")
                print(f"    params: {json.dumps(ev['params'], ensure_ascii=False)[:1200]}")
            elif kind == "server_request_answered":
                print(f"  [answered] {json.dumps(ev['result'], ensure_ascii=False)[:300]}")
            elif kind == "stdout_pollution":
                print(f"  [!! stdout pollution] {ev['raw'][:300]}")

    def _update(self, up: dict[str, Any]) -> None:
        t = up.get("sessionUpdate")
        if t == "agent_message_chunk":
            text = (up.get("content") or {}).get("text") or ""
            print(f"  [agent] {text}", end="", flush=True)
        elif t == "agent_thought_chunk":
            text = (up.get("content") or {}).get("text") or ""
            print(f"  [thought] {text[:160]}", flush=True)
        elif t == "tool_call":
            print(f"  [tool_call] {up.get('title')} status={up.get('status')} id={up.get('toolCallId')} kind={up.get('kind')}", flush=True)
        elif t == "tool_call_update":
            print(f"  [tool_update] id={up.get('toolCallId')} status={up.get('status')}", flush=True)
        elif t == "plan":
            print("  [plan]", flush=True)
        else:
            print(f"  [{t}]", flush=True)

    def dump(self, name: str) -> Path:
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / f"{name}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for frame in self.frames:
                f.write(json.dumps(frame, ensure_ascii=False) + "\n")
        return path


class PermissionPolicy:
    """Decide what to answer for an ACP permission request."""

    def __init__(self) -> None:
        self.mode = "reject"  # reject | allow | cancel
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(params)
        options = params.get("options") or []
        print(f"\n  >>> permission requested: options={[o.get('optionId') for o in options]}")
        print(f"      toolCall={json.dumps(params.get('toolCall'), ensure_ascii=False)[:500]}")
        if self.mode == "cancel":
            return {"outcome": {"outcome": "cancelled"}}
        want_prefix = "allow" if self.mode == "allow" else "reject"
        chosen = next((o for o in options if str(o.get("optionId", "")).startswith(want_prefix)), None)
        if chosen is None and options:
            chosen = options[-1]
        if chosen is None:
            return {"outcome": {"outcome": "cancelled"}}
        print(f"      -> answering {chosen.get('optionId')} ({chosen.get('kind')})")
        return {"outcome": {"outcome": "selected", "optionId": chosen.get("optionId")}}


def children_of(pid: int) -> list[int]:
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
    return [int(x) for x in out if x.strip()]


async def run_smoke(client: ACPClient, rec: Recorder) -> dict[str, Any]:
    print("\n=== A1 smoke: initialize -> session/new -> prompt -> close ===")
    t0 = time.time()
    init = await client.initialize()
    print(f"initialize: {json.dumps(init, ensure_ascii=False)[:800]}")
    sess = await client.new_session(WORKDIR)
    sid = sess["sessionId"]
    print(f"session: {sid}  configOptions={json.dumps(sess.get('configOptions'), ensure_ascii=False)[:600]}")
    started = time.time()
    resp = await client.prompt(sid, "列出当前工作目录下的文件。只回答文件名，简短一点。", timeout=300)
    elapsed = round(time.time() - started, 2)
    print(f"\nprompt result: {resp} after {elapsed}s")
    rec.add(client.take_events())
    closed = await client.close_session(sid)
    print(f"session/close: {json.dumps(closed, ensure_ascii=False)[:300]}")
    rec.add(client.take_events())
    return {
        "scenario": "smoke",
        "initialize": init,
        "sessionId": sid,
        "prompt_seconds": elapsed,
        "stopReason": (resp or {}).get("stopReason"),
        "close": closed,
        "total_seconds": round(time.time() - t0, 2),
    }


async def run_permission(client: ACPClient, rec: Recorder, policy: PermissionPolicy) -> dict[str, Any]:
    print("\n=== A2 permission: write task, reject then allow ===")
    probe = WORKDIR / PERMISSION_PROBE
    if probe.exists():
        probe.unlink()
    sess = await client.new_session(WORKDIR)
    sid = sess["sessionId"]
    prompt_text = (
        f"请用 shell 工具在工作目录创建文件 {PERMISSION_PROBE}，内容为 hello。"
        "不要解释，直接执行。创建后回复 done。"
    )

    policy.mode = "reject"
    policy.requests.clear()
    started = time.time()
    resp = await client.prompt(sid, prompt_text, timeout=300)
    reject_seconds = round(time.time() - started, 2)
    rec.add(client.take_events())
    reject_requests = len(policy.requests)
    exists_after_reject = probe.exists()
    print(f"\nreject round: {resp} in {reject_seconds}s, permission_requests={reject_requests}, file_exists={exists_after_reject}")

    policy.mode = "allow"
    policy.requests.clear()
    started = time.time()
    resp2 = await client.prompt(sid, prompt_text, timeout=300)
    allow_seconds = round(time.time() - started, 2)
    rec.add(client.take_events())
    time.sleep(0.3)
    allow_requests = len(policy.requests)
    exists_after_allow = probe.exists()
    content = probe.read_text(encoding="utf-8", errors="replace") if exists_after_allow else None
    print(f"\nallow round: {resp2} in {allow_seconds}s, permission_requests={allow_requests}, file_exists={exists_after_allow} content={content!r}")

    await client.close_session(sid)
    rec.add(client.take_events())
    return {
        "scenario": "permission",
        "sessionId": sid,
        "reject": {"stopReason": (resp or {}).get("stopReason"), "seconds": reject_seconds,
                   "permission_requests": reject_requests, "file_created": exists_after_reject,
                   "options": [[o.get("optionId") for o in (r.get("options") or [])] for r in policy.requests]},
        "allow": {"stopReason": (resp2 or {}).get("stopReason"), "seconds": allow_seconds,
                  "permission_requests": allow_requests, "file_created": exists_after_allow, "content": content},
    }


async def run_cancel(client: ACPClient, rec: Recorder, policy: PermissionPolicy) -> dict[str, Any]:
    print("\n=== A3 cancel: long shell task interrupted by session/cancel ===")
    sess = await client.new_session(WORKDIR)
    sid = sess["sessionId"]
    policy.mode = "allow"
    policy.requests.clear()
    task = asyncio.create_task(
        client.prompt(sid, "请用 shell 工具运行命令：sleep 90。命令结束后回复 finished。", timeout=300)
    )
    saw_tool = False
    cancel_at = None
    t0 = time.time()
    while time.time() - t0 < 90:
        await asyncio.sleep(0.2)
        for ev in client.take_events():
            rec.frames.append({**ev, "rel": round(ev.get("t", time.time()) - rec.started, 3)})
            if ev["kind"] == "notification" and ev["method"] == "session/update":
                up = (ev.get("params") or {}).get("update") or {}
                if up.get("sessionUpdate") in ("tool_call", "tool_call_update") and not saw_tool:
                    saw_tool = True
                    print(f"  [tool seen] {up.get('title')} status={up.get('status')}")
        if (saw_tool or policy.requests) and cancel_at is None:
            await asyncio.sleep(2.0)
            cancel_at = time.time()
            print("\n  >>> sending session/cancel")
            await client.cancel(sid)
        if task.done():
            break
    if not task.done():
        # never saw a tool call; still exercise cancel on whatever is running
        if cancel_at is None:
            print("\n  >>> no tool call observed; sending session/cancel anyway")
            cancel_at = time.time()
            await client.cancel(sid)
        try:
            await asyncio.wait_for(task, timeout=30)
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            print(f"  prompt did not settle: {type(exc).__name__}: {exc}")
    resp: Any = None
    if task.done() and not task.cancelled():
        exc = task.exception()
        if exc:
            print(f"  prompt error: {type(exc).__name__}: {exc}")
        else:
            resp = task.result()
    settle = round(time.time() - cancel_at, 2) if cancel_at else None
    print(f"\ncancel result: {resp} settle_after_cancel={settle}s")
    rec.add(client.take_events())
    await client.close_session(sid)
    rec.add(client.take_events())
    return {
        "scenario": "cancel",
        "sessionId": sid,
        "saw_tool_call": saw_tool,
        "cancel_seconds_to_settle": settle,
        "stopReason": (resp or {}).get("stopReason") if isinstance(resp, dict) else None,
        "response": resp,
    }


async def main() -> int:
    scenario = sys.argv[1] if len(sys.argv) > 1 else "all"
    WORKDIR.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    # keep the probe workspace tidy so "ls" output is predictable
    probe = WORKDIR / PERMISSION_PROBE
    if probe.exists():
        probe.unlink()

    rec = Recorder()
    policy = PermissionPolicy()
    client = ACPClient(str(DSH_CLI), cwd=WORKDIR, log_path=OUT / "acp_wire.log", on_permission=policy)
    report: dict[str, Any] = {
        "dsh_cli": str(DSH_CLI),
        "dsh_version": subprocess.run([str(DSH_CLI), "--version"], capture_output=True, text=True).stdout.strip(),
        "cwd": str(WORKDIR),
        "python": sys.version.split()[0],
    }
    exit_code = 0
    t_start = time.time()
    try:
        print(f"spawning: {client.argv} (cwd={WORKDIR})")
        await client.start()
        print(f"child pid={client.pid}")
        report["pid"] = client.pid
        if scenario in ("smoke", "all"):
            report["smoke"] = await run_smoke(client, rec)
        if scenario in ("permission", "all"):
            report["permission"] = await run_permission(client, rec, policy)
        if scenario in ("cancel", "all"):
            report["cancel"] = await run_cancel(client, rec, policy)
    except Exception as exc:  # noqa: BLE001
        exit_code = 1
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(f"\n!! {report['error']}")
        import traceback

        traceback.print_exc()
    finally:
        rec.add(client.take_events())
        pid = client.pid
        descendants = children_of(pid) if pid else []
        report["descendants_before_stop"] = descendants
        stop = await client.stop()
        report["stop"] = stop
        await asyncio.sleep(0.5)
        alive = pid is not None and _pid_alive(pid)
        report["orphan"] = {
            "child_pid": pid,
            "child_alive_after_stop": alive,
            "descendants_alive": [p for p in descendants if _pid_alive(p)],
            "stderr_tail": client.stderr_tail[-40:],
        }
        report["total_seconds"] = round(time.time() - t_start, 2)
        events = rec.dump(scenario)
        report["events_file"] = str(events)
        (OUT / f"{scenario}_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n================ SUMMARY ================")
        print(json.dumps(report, ensure_ascii=False, indent=2)[:6000])
        print("=========================================")
    return exit_code


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
