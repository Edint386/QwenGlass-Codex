from __future__ import annotations

import json
import socket
import struct
import threading
import uuid
from pathlib import Path

import pytest

from glasscodex.ipc import DesktopIPC, DesktopIPCError


def _send(sock: socket.socket, message: dict) -> None:
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
    sock.sendall(struct.pack("<I", len(data)) + data)


def _recv(sock: socket.socket) -> dict:
    header = b""
    while len(header) < 4:
        header += sock.recv(4 - len(header))
    (size,) = struct.unpack("<I", header)
    data = b""
    while len(data) < size:
        data += sock.recv(size - len(data))
    return json.loads(data)


def _socket_path() -> Path:
    return Path("/tmp") / f"glasscodex-test-{uuid.uuid4().hex[:10]}.sock"


def test_send_uses_desktop_owner_and_frames_a_real_turn(tmp_path: Path):
    path = _socket_path()
    received: list[dict] = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)

    def desktop() -> None:
        connection, _ = server.accept()
        with connection:
            init = _recv(connection)
            assert init["method"] == "initialize"
            _send(connection, {
                "type": "response",
                "requestId": init["requestId"],
                "resultType": "success",
                "method": "initialize",
                "handledByClientId": "client-1",
                "result": {"clientId": "client-1"},
            })
            owner = _recv(connection)
            assert owner["method"] == "thread-owner-discovery"
            _send(connection, {
                "type": "response",
                "requestId": owner["requestId"],
                "resultType": "success",
                "method": owner["method"],
                "handledByClientId": "owner-1",
                "result": {"supportsUntrustedAppInput": True},
            })
            turn = _recv(connection)
            received.append(turn)
            _send(connection, {
                "type": "response",
                "requestId": turn["requestId"],
                "resultType": "success",
                "method": turn["method"],
                "handledByClientId": "owner-1",
                "result": {"result": {"turnId": "turn-2"}},
            })

    thread = threading.Thread(target=desktop, daemon=True)
    thread.start()
    with DesktopIPC(path, timeout=2) as ipc:
        result = ipc.send("thread-1", "/repo", "跑一下测试", model="gpt-5.3-codex", effort="high")
    server.close()
    path.unlink(missing_ok=True)
    thread.join(timeout=2)

    assert result["owner"] == "owner-1"
    turn = received[0]
    assert turn["method"] == "thread-follower-start-turn"
    assert turn["targetClientId"] == "owner-1"
    params = turn["params"]
    assert params["conversationId"] == "thread-1"
    assert params["turnStart"]["request"]["threadId"] == "thread-1"
    assert params["turnStart"]["request"]["input"][0]["text"] == "跑一下测试"
    assert params["turnStart"]["context"]["restoreMessage"]["text"] == "跑一下测试"
    assert params["turnStart"]["context"]["restoreMessage"]["cwd"] == "/repo"


def test_owner_not_open_is_reported(tmp_path: Path):
    path = _socket_path()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)

    def desktop() -> None:
        connection, _ = server.accept()
        with connection:
            init = _recv(connection)
            _send(connection, {
                "type": "response",
                "requestId": init["requestId"],
                "resultType": "success",
                "method": "initialize",
                "handledByClientId": "client-1",
                "result": {"clientId": "client-1"},
            })
            owner = _recv(connection)
            _send(connection, {
                "type": "response",
                "requestId": owner["requestId"],
                "resultType": "error",
                "method": owner["method"],
                "error": "no-client-found",
            })

    thread = threading.Thread(target=desktop, daemon=True)
    thread.start()
    with pytest.raises(DesktopIPCError, match="桌面版没有打开这个任务"):
        with DesktopIPC(path, timeout=2) as ipc:
            ipc.send("missing", "/repo", "hello")
    server.close()
    path.unlink(missing_ok=True)
    thread.join(timeout=2)
