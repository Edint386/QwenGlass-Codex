"""Send a turn into the running Codex Desktop through its local IPC socket.

Desktop uses 4-byte little-endian length-prefixed JSON. An external client
initializes, discovers the renderer that owns a task, then asks that owner to
start a turn. The task itself remains a Desktop task; no second Codex process
is started.
"""

from __future__ import annotations

import json
import socket
import struct
import time
import uuid
from pathlib import Path
from typing import Any

__all__ = ["DesktopIPC", "DesktopIPCError"]

MAX_FRAME = 0x10000000


class DesktopIPCError(RuntimeError):
    pass


class DesktopIPC:
    def __init__(self, socket_path: Path | None = None, timeout: float = 5.0):
        self.socket_path = socket_path or Path.home() / ".codex" / "ipc" / "ipc.sock"
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._buffer = b""
        self.client_id = ""

    def __enter__(self) -> DesktopIPC:
        self.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def connect(self) -> None:
        if not self.socket_path.exists():
            raise DesktopIPCError("Codex Desktop 没有在运行，找不到 ipc.sock")
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(str(self.socket_path))
        except OSError as exc:
            raise DesktopIPCError(f"连不上 Codex Desktop：{exc}") from exc
        self._socket = sock
        response = self.request("initialize", {"clientType": "glasscodex"}, source="initializing-client")
        result = response.get("result")
        client_id = str(result.get("clientId") or response.get("handledByClientId") or "") if isinstance(result, dict) else ""
        if not client_id:
            self.close()
            raise DesktopIPCError("Codex Desktop 没有返回客户端 ID")
        self.client_id = client_id

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None
                self.client_id = ""

    def _send(self, message: dict[str, Any]) -> None:
        if self._socket is None:
            raise DesktopIPCError("没有连上 Codex Desktop")
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
        if not payload or len(payload) > MAX_FRAME:
            raise DesktopIPCError("IPC 消息太大")
        self._socket.sendall(struct.pack("<I", len(payload)) + payload)

    def _recv(self) -> dict[str, Any]:
        if self._socket is None:
            raise DesktopIPCError("没有连上 Codex Desktop")
        while len(self._buffer) < 4:
            chunk = self._socket.recv(65536)
            if not chunk:
                raise DesktopIPCError("Codex Desktop 断开了连接")
            self._buffer += chunk
        (length,) = struct.unpack("<I", self._buffer[:4])
        if length == 0 or length > MAX_FRAME:
            raise DesktopIPCError("IPC 帧长度无效")
        while len(self._buffer) < 4 + length:
            chunk = self._socket.recv(65536)
            if not chunk:
                raise DesktopIPCError("Codex Desktop 断开了连接")
            self._buffer += chunk
        payload = self._buffer[4:4 + length]
        self._buffer = self._buffer[4 + length:]
        try:
            message = json.loads(payload)
        except ValueError as exc:
            raise DesktopIPCError("Codex Desktop 返回了无效 JSON") from exc
        if not isinstance(message, dict):
            raise DesktopIPCError("Codex Desktop 返回了非对象消息")
        return message

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        target: str | None = None,
        timeout_ms: int | None = None,
        source: str = "",
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        message = {
            "type": "request",
            "requestId": request_id,
            "sourceClientId": source or self.client_id or "initializing-client",
            "version": 1,
            "method": method,
            "params": params,
        }
        if target:
            message["targetClientId"] = target
        if timeout_ms is not None:
            message["timeoutMs"] = timeout_ms
        self._send(message)
        deadline = time.monotonic() + max(self.timeout, (timeout_ms or 0) / 1000 + 1)
        while time.monotonic() < deadline:
            response = self._recv()
            if response.get("type") == "request":
                self._send({
                    "type": "response",
                    "requestId": response.get("requestId"),
                    "resultType": "error",
                    "method": response.get("method"),
                    "error": "glasscodex does not handle Desktop requests",
                })
                continue
            if response.get("type") != "response" or response.get("requestId") != request_id:
                continue
            if response.get("resultType") != "success":
                error = str(response.get("error") or "Codex Desktop 拒绝了请求")
                if error == "no-client-found":
                    error = "桌面版没有打开这个任务"
                raise DesktopIPCError(error)
            return response
        raise DesktopIPCError("Codex Desktop 没有及时回应")

    def owner(self, thread_id: str) -> str:
        response = self.request(
            "thread-owner-discovery",
            {"hostId": "local", "conversationId": thread_id},
            timeout_ms=5000,
        )
        owner = str(response.get("handledByClientId") or "")
        if not owner:
            raise DesktopIPCError("桌面版没有打开这个任务")
        return owner

    def send(
        self,
        thread_id: str,
        cwd: str,
        text: str,
        *,
        model: str = "",
        effort: str = "",
    ) -> dict[str, Any]:
        owner = self.owner(thread_id)
        message_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        restore = {
            "id": message_id,
            "text": text,
            "context": {
                "prompt": text,
                "addedFiles": [],
                "fileAttachments": [],
                "ideContext": None,
                "imageAttachments": [],
                "workspaceRoots": [cwd] if cwd else [],
            },
            "cwd": cwd or "/",
            "createdAt": now_ms,
        }
        request: dict[str, Any] = {
            "threadId": thread_id,
            "turnTrigger": "composer",
            "input": [{"type": "text", "text": text}],
            "clientUserMessageId": message_id,
        }
        context: dict[str, Any] = {
            "restoreMessage": restore,
            "attachments": [],
        }
        if model:
            request["model"] = model
        if effort:
            request["effort"] = effort
        response = self.request(
            "thread-follower-start-turn",
            {
                "conversationId": thread_id,
                "turnStart": {"request": request, "context": context},
            },
            target=owner,
            timeout_ms=15000,
        )
        return {"owner": owner, "message_id": message_id, "response": response}
