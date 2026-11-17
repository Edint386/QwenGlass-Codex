"""Cloud hub: one Codex Desktop bridge, many short-lived glasses sessions."""

from __future__ import annotations

import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from .panel import Panel
from .text import TEXT_SIZE

__all__ = ["Hub"]

log = logging.getLogger(__name__)

MAX_PANELS = 16
SEND_SECONDS = 15

Outbox = Callable[[dict[str, Any]], bool]
Delivery = list[tuple[Panel, list[dict[str, Any]]]]


class Hub:
    def __init__(self, allowed_devices: frozenset[str] = frozenset()):
        self.allowed_devices = allowed_devices
        self.panels: OrderedDict[str, Panel] = OrderedDict()
        self.sessions: list[dict[str, Any]] = []
        self.turns: dict[str, dict[str, Any]] = {}
        self.last_selected: str | None = None
        self.font_size = TEXT_SIZE
        self._outbox: Outbox | None = None
        self._watching: str | None = None

    @property
    def online(self) -> bool:
        return self._outbox is not None

    def turn(self, session: str) -> dict[str, Any] | None:
        return self.turns.get(session)

    def watch(self, session: str | None) -> None:
        if session is not None:
            self.last_selected = session
        if session == self._watching:
            return
        self._watching = session
        self._push({"type": "watch", "session": session})

    def send(self, session: str, text: str) -> str | None:
        command = uuid.uuid4().hex
        message = {
            "type": "send",
            "cmd": command,
            "session": session,
            "text": text,
            "expires": time.time() + SEND_SECONDS,
        }
        return command if self._push(message) else None

    def _push(self, message: dict[str, Any]) -> bool:
        return self._outbox is not None and self._outbox(message)

    def attach(self, session_id: str, epoch: int) -> tuple[Panel | None, str]:
        panel = self.panels.get(session_id)
        if panel is None:
            panel = Panel(session_id, self, self.allowed_devices)
            panel.epoch = epoch
            self.panels[session_id] = panel
            while len(self.panels) > MAX_PANELS:
                self.panels.popitem(last=False)
            return panel, "CREATED"
        if epoch <= panel.epoch:
            return panel, "STALE"
        panel.epoch = epoch
        self.panels.move_to_end(session_id)
        return panel, "RESUMED"

    def drop(self, session_id: str) -> None:
        self.panels.pop(session_id, None)
        self.released()

    def released(self) -> None:
        if not any(panel.socket is not None for panel in self.panels.values()):
            self.watch(None)

    def _live(self) -> list[Panel]:
        return [panel for panel in self.panels.values() if panel.socket is not None]

    def connected(self, outbox: Outbox) -> Delivery:
        self._outbox = outbox
        return [(panel, panel.on_link()) for panel in self._live()]

    def disconnected(self, outbox: Outbox) -> Delivery:
        if self._outbox is not outbox:
            return []
        self._outbox = None
        return [(panel, panel.on_link()) for panel in self._live()]

    def receive(self, message: dict[str, Any]) -> Delivery:
        kind = message.get("type")
        if kind == "hello":
            log.info("桥接程序连上了")
            return []
        if kind == "sessions":
            items = message.get("items")
            self.sessions = [item for item in items if isinstance(item, dict) and "id" in item] if isinstance(items, list) else []
            ids = {item["id"] for item in self.sessions}
            self.turns = {key: value for key, value in self.turns.items() if key in ids}
            return [(panel, panel.on_sessions()) for panel in self._live()]
        if kind == "turn":
            session = message.get("session")
            if not isinstance(session, str):
                return []
            message["heard"] = time.monotonic()
            self.turns[session] = message
            return [(panel, panel.on_turn(message)) for panel in self._live()]
        if kind == "sent":
            return [(panel, panel.on_sent(message)) for panel in self.panels.values()]
        log.info("忽略桥接消息 %r", kind)
        return []
