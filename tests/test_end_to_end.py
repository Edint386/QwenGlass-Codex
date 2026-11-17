from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
import websockets

from glasscodex.bridge import Bridge
from glasscodex.desktop import ThreadInfo, TurnState
from glasscodex.server import create_app

AGENT, BRIDGE = "agent-token", "bridge-token"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def server():
    port = _free_port()
    instance = uvicorn.Server(uvicorn.Config(create_app(AGENT, BRIDGE), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    while not instance.started:
        time.sleep(0.01)
    yield f"ws://127.0.0.1:{port}"
    instance.should_exit = True
    thread.join(timeout=5)


class FakeReader:
    def __init__(self) -> None:
        self.message = ""
        self.done = False

    def list_threads(self, limit: int = 30) -> list[ThreadInfo]:
        return [ThreadInfo(
            id="desktop-1",
            title="桌面版代码任务",
            cwd="/repo/acme-web",
            model="gpt-5.3-codex",
            effort="high",
            updated_at=1,
            status="done" if self.done else "running",
            active=not self.done,
        )]

    def thread(self, thread_id: str) -> ThreadInfo | None:
        return self.list_threads()[0] if thread_id == "desktop-1" else None

    def read_turn(self, thread: ThreadInfo) -> TurnState:
        return TurnState(
            thread_id=thread.id,
            prompt=self.message,
            text="测试全过了。" if self.done else "",
            narration="正在运行测试。" if not self.done else "",
            status="done" if self.done else "running",
            steps=(
                [{"k": "you", "t": self.message}, {"k": "tools", "n": 1, "s": 0, "e": 2, "o": False}, {"k": "text", "t": "测试全过了。"}]
                if self.done
                else [{"k": "you", "t": self.message}, {"k": "text", "t": "正在运行测试。"}, {"k": "tools", "n": 1, "s": 0, "e": 2, "o": True}]
            ),
            tools=1,
            current=None if self.done else __import__("glasscodex.desktop", fromlist=["Call"]).Call("c", "exec_command", "运行测试", 2),
            plan=[{"t": "运行测试", "s": "completed" if self.done else "in_progress"}],
            context=12000,
            window=100000,
            files=1,
            added=3,
            removed=0,
            began=0,
            last=2,
            model="gpt-5.3-codex",
            effort="high",
        )


class FakeIPC:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, str, str]] = []

    def send(self, thread_id: str, cwd: str, text: str, *, model: str = "", effort: str = "") -> dict:
        self.sent.append((thread_id, cwd, text, model, effort))
        return {"owner": "desktop", "message_id": "m1"}


def _platform(session: str, kind: str, payload: dict) -> str:
    return json.dumps({"specVersion": "1.0", "sessionId": session, "type": kind, "payload": payload}, ensure_ascii=False)


async def _next_data(ws, predicate, seconds: float = 6.0) -> dict:
    merged: dict = {}
    async with asyncio.timeout(seconds):
        async for raw in ws:
            message = json.loads(raw)
            if message.get("type") != "OUTPUT":
                continue
            for action in message["payload"]["actions"]:
                if action["type"] != "SHOW_UI":
                    continue
                data = action["payload"]["view"]["props"]["data"]
                merged.update(data)
                merged["_page"] = action["payload"]["pageUuid"]
            if predicate(merged):
                return merged
    raise AssertionError


def test_voice_reaches_desktop_and_reply_returns(server):
    reader = FakeReader()
    ipc = FakeIPC()

    async def main() -> None:
        bridge = Bridge(f"{server}/bridge", BRIDGE, home=Path("/nonexistent"), poll=0.05, ipc=ipc)
        bridge.reader = reader
        task = asyncio.create_task(bridge.run())
        headers = {"Authorization": f"Bearer {AGENT}", "X-Request-Id": "r1"}
        async with websockets.connect(f"{server}/agent", additional_headers=headers) as ws:
            assert ws.response.headers["X-Request-Id"] == "r1"
            await ws.send(_platform("g", "SESSION_INIT", {
                "connectionEpoch": 1,
                "pages": [{"templateId": "9", "description": "codex-panel"}],
            }))
            data = await _next_data(ws, lambda d: d.get("h") == "桌面版代码任务")
            assert data["m"] in (0, 3)

            await ws.send(_platform("g", "UTTERANCE", {"text": "跑一下测试"}))
            data = await _next_data(ws, lambda d: d.get("m") == 2 and "跑一下测试" in d.get("dr", ""))
            await ws.send(_platform("g", "UI_ACTION", {
                "pageUuid": data["_page"],
                "action": {"name": "send", "params": {"dv": data["dv"], "m": 0, "slot": 0}},
            }))
            await _next_data(ws, lambda d: d.get("ft") in ("发送中", "已发出", "Codex 已收到"))
            reader.message = "跑一下测试"
            await _next_data(ws, lambda d: d.get("ft") == "Codex 已收到", seconds=8)
            assert ipc.sent == [("desktop-1", "/repo/acme-web", "跑一下测试", "gpt-5.3-codex", "high")]

            reader.done = True
            final = await _next_data(
                ws,
                lambda d: "测试全过了" in "".join(page["t"] for page in d.get("P", []))
                and str(d.get("ft", "")).startswith("已完成"),
            )
            assert final["ft"].startswith("已完成")
        task.cancel()

    asyncio.run(main())


def test_wrong_tokens_are_refused(server):
    async def main() -> None:
        for path in ("/agent", "/bridge"):
            with pytest.raises(websockets.InvalidStatus) as caught:
                async with websockets.connect(server + path, additional_headers={"Authorization": "Bearer nope"}):
                    pass
            assert caught.value.response.status_code == 401

    asyncio.run(main())
