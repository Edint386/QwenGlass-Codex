"""Mac bridge for Codex Desktop.

The bridge reads Desktop's local state and forwards the selected task. Voice
commands are sent back through Desktop's own IPC socket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets

from .cleanup import brief, clean
from .desktop import Call, DesktopReader, ThreadInfo
from .ipc import DesktopIPC, DesktopIPCError

__all__ = ["Bridge"]

log = logging.getLogger(__name__)

POLL_SECONDS = 0.7
PROGRESS_SECONDS = 2.0
RETRY_MIN_SECONDS, RETRY_MAX_SECONDS = 1.0, 30.0
DELIVERY_SECONDS = 15.0
NAME_CHARS = 48
NAME_SHOWN = 22
PROMPT_CHARS = 260


@dataclass
class Status:
    connected: bool = False
    since: float = field(default_factory=time.time)
    error: str = ""
    watching: str = ""
    command_at: float = 0.0
    command_to: str = ""
    command_chars: int = 0
    paused: bool = False


@dataclass
class _Pending:
    command: str
    text: str
    session: str
    deadline: float
    posted: bool = False


def _listing(threads: list[ThreadInfo]) -> list[dict[str, Any]]:
    names = [brief(thread.title, NAME_CHARS) for thread in threads]
    shown = [name[:NAME_SHOWN] for name in names]
    return [
        {
            "id": thread.id,
            "name": f"{name[:NAME_SHOWN - 6]}·{thread.id[:4]}" if shown.count(name[:NAME_SHOWN]) > 1 else name,
            "project": thread.project,
            "state": thread.status,
            "state_label": thread.state_label,
            "active": thread.active,
            "at": thread.updated_at,
        }
        for thread, name in zip(threads, names, strict=True)
    ]


def _call(call: Call | None) -> dict[str, Any] | None:
    if call is None:
        return None
    return {"name": call.name, "label": brief(call.label, 48), "at": call.at}


def _turn_message(thread: ThreadInfo, state: Any) -> dict[str, Any]:
    running = list(state.running)
    steps = []
    for step in state.steps:
        if step.get("k") == "text":
            steps.append({"k": "text", "t": clean(str(step.get("t", "")))})
        elif step.get("k") == "you":
            steps.append({"k": "you", "t": clean(str(step.get("t", "")))})
        elif step.get("k") == "tools":
            steps.append({
                "k": "tools",
                "n": int(step.get("n") or 0),
                "s": float(step.get("s") or 0),
                "e": float(step.get("e") or 0),
                "o": bool(step.get("o")),
            })
        elif step.get("k") == "file":
            steps.append({"k": "file", "n": int(step.get("n") or 0), "s": float(step.get("s") or 0)})
        elif step.get("k") == "agent":
            steps.append({"k": "agent", "s": float(step.get("s") or 0)})
    if state.status in ("running", "starting") and steps and steps[-1].get("k") == "tools":
        steps[-1]["o"] = True
    return {
        "type": "turn",
        "session": thread.id,
        "prompt": brief(clean(state.prompt), PROMPT_CHARS),
        "text": clean(state.text),
        "narration": clean(state.narration),
        "done": state.done,
        "status": state.status,
        "steps": steps,
        "tools": state.tools,
        "doing": _call(state.current),
        "tasks": [_call(call) for call in running[:3]],
        "plan": state.plan,
        "context": state.context,
        "window": state.window,
        "context_percent": state.context_percent,
        "plan_percent": state.plan_percent,
        "five_hour": state.five_hour,
        "seven_day": state.seven_day,
        "files": state.files,
        "added": state.added,
        "removed": state.removed,
        "diff_label": state.diff_label,
        "began": state.began,
        "last": state.last,
        "now": time.time(),
        "model": state.model,
        "effort": state.effort,
        "error": state.error,
    }


class Bridge:
    def __init__(self, url: str, token: str, home: Path | None = None,
                 poll: float = POLL_SECONDS, ipc: DesktopIPC | None = None):
        self.url = url
        self.token = token
        self.reader = DesktopReader(home)
        self.poll = poll
        self.ipc = ipc or DesktopIPC()
        self.sessions: dict[str, ThreadInfo] = {}
        self.watching: str | None = None
        self.pending: dict[str, _Pending] = {}
        self._lock = asyncio.Lock()
        self._listed: list[dict[str, Any]] | None = None
        self._shown: tuple[Any, ...] | None = None
        self._sent_at = float("-inf")
        self.status = Status()

    async def run(self) -> None:
        delay = RETRY_MIN_SECONDS
        while True:
            try:
                headers = {"Authorization": f"Bearer {self.token}"}
                async with websockets.connect(
                    self.url,
                    additional_headers=headers,
                    proxy=None,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=2**21,
                ) as ws:
                    log.info("连上云端 %s", self.url)
                    self._mark(True, "")
                    delay = RETRY_MIN_SECONDS
                    await self.serve(ws)
            except (OSError, websockets.WebSocketException, TimeoutError) as exc:
                log.warning("云端连接断了：%s，%.0f 秒后重连", exc, delay)
                self._mark(False, str(exc))
            except Exception as exc:
                log.exception("桥接出错，%.0f 秒后重连", delay)
                self._mark(False, f"出错：{exc}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX_SECONDS)

    def _mark(self, connected: bool, error: str) -> None:
        if connected != self.status.connected:
            self.status.since = time.time()
        self.status.connected, self.status.error = connected, error

    async def serve(self, ws: Any) -> None:
        self._listed = self._shown = None
        self.watching = None
        self.pending.clear()
        await self._send(ws, {"type": "hello", "version": 1})
        tasks = [asyncio.create_task(self._receive(ws)), asyncio.create_task(self._tick(ws))]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send(self, ws: Any, message: dict[str, Any]) -> None:
        await ws.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))

    async def _receive(self, ws: Any) -> None:
        async for raw in ws:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            kind = message.get("type") if isinstance(message, dict) else None
            if kind == "watch":
                async with self._lock:
                    await self._watch(ws, message.get("session"))
            elif kind == "send":
                await self._post(ws, message)

    async def _tick(self, ws: Any) -> None:
        while True:
            async with self._lock:
                await self._scan(ws)
                await self._follow(ws)
                await self._settle(ws)
            await asyncio.sleep(self.poll)

    async def _scan(self, ws: Any) -> None:
        found = {thread.id: thread for thread in await asyncio.to_thread(self.reader.list_threads, 30)}
        self.sessions = found
        listed = _listing(list(found.values()))
        key = [{k: v for k, v in item.items() if k not in ("at",)} for item in listed]
        if key != self._listed:
            self._listed = key
            await self._send(ws, {"type": "sessions", "items": listed})

    async def _watch(self, ws: Any, session: Any) -> None:
        self.watching = session if isinstance(session, str) else None
        self._shown = None
        thread = self.sessions.get(self.watching) if self.watching else None
        self.status.watching = thread.title if thread else ""
        await self._follow(ws)

    async def _follow(self, ws: Any) -> None:
        if self.watching is None:
            return
        thread = self.sessions.get(self.watching)
        if thread is None:
            return
        state = await asyncio.to_thread(self.reader.read_turn, thread)
        shown = (
            state.prompt,
            state.status,
            state.text,
            state.narration,
            tuple((s.get("k"), s.get("n"), s.get("t"), s.get("s"), s.get("e")) for s in state.steps),
            tuple((c.id, c.name, c.label) for c in state.running),
            tuple((s.get("t"), s.get("s")) for s in state.plan),
            state.context,
            state.files,
            state.added,
            state.removed,
        )
        now = time.monotonic()
        if shown == self._shown and now - self._sent_at < PROGRESS_SECONDS:
            return
        turning = self._shown is None or shown[:2] != self._shown[:2]
        if not turning and now - self._sent_at < PROGRESS_SECONDS:
            return
        self._shown, self._sent_at = shown, now
        await self._send(ws, _turn_message(thread, state))

    async def _post(self, ws: Any, message: dict[str, Any]) -> None:
        command = str(message.get("cmd") or "")
        session = str(message.get("session") or "")
        text = str(message.get("text") or "").strip()
        expires = float(message.get("expires") or 0)
        if not command or not session or not text:
            return
        if self.status.paused:
            await self._send(ws, {"type": "sent", "cmd": command, "state": "failed", "error": "电脑上暂停了"})
            return
        if expires and expires < time.time():
            await self._send(ws, {"type": "sent", "cmd": command, "state": "failed", "error": "这条指令已经过期"})
            return
        thread = self.sessions.get(session) or await asyncio.to_thread(self.reader.thread, session)
        if thread is None:
            await self._send(ws, {"type": "sent", "cmd": command, "state": "failed", "error": "桌面上找不到这个任务"})
            return
        self.status.command_at = time.time()
        self.status.command_to = thread.title
        self.status.command_chars = len(text)
        try:
            await asyncio.to_thread(
                self.ipc.send,
                thread.id,
                thread.cwd,
                text,
                model=thread.model,
                effort=thread.effort,
            )
        except (DesktopIPCError, OSError) as exc:
            await self._send(ws, {"type": "sent", "cmd": command, "state": "failed", "error": str(exc)})
            return
        self.pending[command] = _Pending(command, text, session, time.monotonic() + DELIVERY_SECONDS, posted=True)
        await self._send(ws, {"type": "sent", "cmd": command, "state": "posted"})

    async def _settle(self, ws: Any) -> None:
        for command, item in list(self.pending.items()):
            thread = self.sessions.get(item.session)
            if thread is None:
                continue
            state = await asyncio.to_thread(self.reader.read_turn, thread)
            if item.text in state.prompt:
                del self.pending[command]
                await self._send(ws, {"type": "sent", "cmd": command, "state": "delivered"})
            elif time.monotonic() > item.deadline:
                del self.pending[command]
                await self._send(ws, {"type": "sent", "cmd": command, "state": "unconfirmed"})
