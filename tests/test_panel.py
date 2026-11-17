from __future__ import annotations

from typing import Any

from glasscodex.panel import MENU, MENU_BASE, PLAN, READ, Panel


class Link:
    online = True
    sessions = [{
        "id": "t1",
        "name": "测试任务",
        "project": "acme-web",
        "state": "running",
        "state_label": "运行中",
        "at": 1,
    }]
    last_selected = "t1"
    font_size = 18

    def __init__(self) -> None:
        self.turns: dict[str, dict[str, Any]] = {}
        self.sent: list[tuple[str, str]] = []
        self.watched: list[str | None] = []

    def turn(self, session: str) -> dict[str, Any] | None:
        return self.turns.get(session)

    def watch(self, session: str | None) -> None:
        self.watched.append(session)

    def send(self, session: str, text: str) -> str | None:
        self.sent.append((session, text))
        return "cmd1"


def _turn(done: bool = True) -> dict[str, Any]:
    return {
        "type": "turn",
        "session": "t1",
        "prompt": "跑一下测试",
        "text": "测试全过了。",
        "narration": "我先运行测试。",
        "done": done,
        "status": "done" if done else "running",
        "steps": [
            {"k": "you", "t": "跑一下测试"},
            {"k": "tools", "n": 2, "s": 0, "e": 2, "o": not done},
            {"k": "text", "t": "测试全过了。" if done else "我先运行测试。"},
        ],
        "tools": 2,
        "doing": None if done else {"name": "exec_command", "label": "运行测试", "at": 2},
        "tasks": [],
        "plan": [
            {"t": "检查项目", "s": "completed"},
            {"t": "运行测试", "s": "in_progress"},
            {"t": "汇报结果", "s": "pending"},
        ],
        "context": 12000,
        "window": 100000,
        "context_percent": 12,
        "plan_percent": 33,
        "five_hour": 10,
        "seven_day": None,
        "files": 2,
        "added": 4,
        "removed": 1,
        "diff_label": "2 个文件 +4 -1",
        "began": 0,
        "last": 2,
        "now": 2,
        "model": "gpt-5.3-codex",
        "effort": "high",
    }


def test_initial_page_is_within_device_limits():
    panel = Panel("g", Link())
    messages = panel.start({"pages": [{"templateId": "1", "description": "codex-panel"}]})
    data = messages[0]["payload"]["actions"][0]["payload"]["view"]["props"]["data"]
    assert data["h"] == "测试任务"
    assert len(data) <= 64
    assert len(data["P"]) >= 1
    assert data["m"] == READ


def test_voice_draft_click_sends_and_receipt_is_shown():
    link = Link()
    panel = Panel("g", link)
    panel.start({"pages": [{"templateId": "1", "description": "codex-panel"}]})
    panel.handle({"type": "UTTERANCE", "payload": {"text": "跑一下测试"}})
    assert panel.mode == 2
    panel.handle({
        "type": "UI_ACTION",
        "payload": {"action": {"name": "send", "params": {"dv": panel.draft_version, "m": 0, "slot": 0}}},
    })
    assert link.sent == [("t1", "跑一下测试")]
    assert panel.notice == "发送中"
    panel.on_sent({"cmd": "cmd1", "state": "posted"})
    panel.on_sent({"cmd": "cmd1", "state": "delivered"})
    assert panel.notice == "Codex 已收到"


def test_live_turn_opens_two_column_and_plan_screen():
    link = Link()
    panel = Panel("g", link)
    panel.start({"pages": [{"templateId": "1", "description": "codex-panel"}]})
    panel.on_turn(_turn(done=False))
    data = panel._data()
    assert data["m"] == 3
    assert data["rp"] == 1
    assert data["d0"] == "正在执行"
    assert data["m1"] == "12"
    assert data["m2"] == "33"

    panel.menu = MENU
    panel.handle({
        "type": "UI_ACTION",
        "payload": {"action": {"name": "pick", "params": {"k": MENU_BASE + 3, "s": MENU}}},
    })
    assert panel.mode == PLAN
    assert len(panel._data()["PL"]) == 5


def test_finished_reply_and_receipt_text_are_paginated():
    panel = Panel("g", Link())
    panel.start({"pages": [{"templateId": "1", "description": "codex-panel"}]})
    panel.on_turn(_turn(done=True))
    assert panel.mode == READ
    assert "测试全过了" in "".join(panel.pages)
    assert panel._data()["rp"] == 0
