"""Glasses-side session for Codex Desktop tasks."""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterator, Protocol

from .layout import bar_data, clock, live_pages, row_data, stream_rows
from .text import BODY_WIDTH, FONT_SIZES, NARROW_WIDTH, TEXT_SIZE, paginate, wrapped

__all__ = [
    "DRAFT",
    "FONT",
    "LIST_ROWS",
    "LIVE",
    "MENU",
    "MENU_ITEMS",
    "PAGE_KEYWORD",
    "PICK",
    "PLAN",
    "QUICK",
    "QUICK_REPLIES",
    "READ",
    "SPEC_VERSION",
    "Link",
    "Panel",
]

log = logging.getLogger(__name__)

SPEC_VERSION = "1.0"
PAGE_KEYWORD = "codex-panel"
SLOTS, AHEAD, BEHIND = 6, 3, 2

READ, FONT, DRAFT, LIVE, PICK, MENU, QUICK, PLAN_MODE = 0, 1, 2, 3, 4, 5, 6, 7
PLAN = PLAN_MODE

LIVE_SECONDS = 2.0
HINT_SECONDS = 5.0
LIST_ROWS, ROW_HEIGHT, LIST_TOP = 5, 34, 44
QUICK_REPLIES = ("继续", "好的，就这么做", "先停一下", "说简单点")
MENU_ITEMS = ("返回", "切换任务", "看进度", "计划与改动", "快捷回复", "字号")
LIST_BASE = 0
QUICK_BASE = LIST_BASE + 1 + LIST_ROWS
MENU_BASE = QUICK_BASE + 1 + len(QUICK_REPLIES)
CURSORS = MENU_BASE + len(MENU_ITEMS)
MENU_BACK, MENU_SWITCH, MENU_VIEW, MENU_PLAN, MENU_QUICK, MENU_FONT = range(MENU_BASE, MENU_BASE + len(MENU_ITEMS))
_SEGMENT = {PICK: (LIST_BASE, QUICK_BASE), QUICK: (QUICK_BASE, MENU_BASE), MENU: (MENU_BASE, CURSORS)}

FOOT_NONE, FOOT_LIVE, FOOT_NEED, FOOT_QUIET = 0, 1, 2, 3
REPORT_SECONDS = 3.0
KEEPALIVE_SECONDS = 90
IDLE_LIMIT_SECONDS = 1800
MIN_DRAFT_CHARS = 2
DRAFT_SIZE, DRAFT_LINES = 24, 5
SEND_WAIT_SECONDS, RECEIPT_WAIT_SECONDS = 20.0, 25.0
RESEND_SECONDS = 5.0
BRIGHTNESS_TOOL = "change_brightness"
BRIGHTNESS_STEP, BRIGHTNESS_START = 20, 50

_WHITE, _DIM, _QUIET, _BLACK = "#FFFFFF", "#A0A0A0", "#A0A0A0", "#000000"

_NOT_CONNECTED = "电脑没有连上。看看 Mac 上的桥接程序在不在运行，电脑是不是休眠了。"
_NO_THREADS = "Codex Desktop 里没有最近任务。"
_UNSURE = "不确定发出去没有，先看电脑"
_LIST_WORDS = frozenset({"切换任务", "换个任务", "任务列表", "看看任务", "有哪些任务", "切换会话", "会话列表"})
_PLAN_WORDS = frozenset({"计划", "看计划", "计划与改动", "看看计划", "改了什么", "看看改动"})
_CANCEL_WORDS = frozenset({"取消", "不发了", "算了", "清空"})
_DIGITS = "零一二三四五六七八九十百两"
_BRIGHTNESS = re.compile(rf"(?:帮我|请)?(?:把|将|调)?亮度(?:调到|调成|调为|改成|改到|设为|设成|到|调)?(?:百分之)?([0-9{_DIGITS}]+)[%％]?吧?")
_BRIGHTER = re.compile(r"(?:帮我|请|再)?(?:把|将)?(?:亮度)?(?:调亮|调高|亮)一?[点些]吧?")
_DIMMER = re.compile(r"(?:帮我|请|再)?(?:把|将)?(?:亮度)?(?:调暗|调低|暗)一?[点些]吧?")


class Link(Protocol):
    online: bool
    sessions: list[dict[str, Any]]
    last_selected: str | None
    font_size: int

    def turn(self, session: str) -> dict[str, Any] | None: ...
    def watch(self, session: str | None) -> None: ...
    def send(self, session: str, text: str) -> str | None: ...


@dataclass
class _Outgoing:
    command: str
    text: str
    target: str
    at: float
    posted: bool = False


def _ring(step: int) -> list[dict[str, int]]:
    return [{"n": (i + step) % SLOTS} for i in range(SLOTS)]


def _cycle(count: int, step: int, size: int) -> list[dict[str, int]]:
    return [
        {"n": (i + step) % count, "d": ((i - step) % count - i) * ROW_HEIGHT} if i < count else {"n": 0, "d": 0}
        for i in range(size)
    ]


def _segments(listed: int) -> list[tuple[int, int, int]]:
    return [
        (LIST_BASE, 1 + listed, 1 + LIST_ROWS),
        (QUICK_BASE, 1 + len(QUICK_REPLIES), 1 + len(QUICK_REPLIES)),
        (MENU_BASE, len(MENU_ITEMS), len(MENU_ITEMS)),
    ]


def _cursor_table(listed: int, step: int) -> list[dict[str, int]]:
    table = [
        {"n": base + row["n"], "d": row["d"]}
        for base, count, size in _segments(listed)
        for row in _cycle(count, step, size)
    ]
    assert len(table) == CURSORS
    return table


PAGE_TABLES = {
    "nx": [{"n": (i + 1) % SLOTS} for i in range(SLOTS)],
    "pv": [{"n": (i - 1) % SLOTS} for i in range(SLOTS)],
    "tt": [{"n": LIST_TOP + i * ROW_HEIGHT} for base, _, size in _segments(LIST_ROWS) for i in range(size)],
    "F": [
        {"b": _BLACK, "c": _BLACK, "d": _BLACK},
        {"b": _BLACK, "c": _WHITE, "d": _WHITE},
        {"b": _WHITE, "c": _BLACK, "d": _WHITE},
        {"b": _BLACK, "c": _QUIET, "d": _BLACK},
    ],
    "W": [{"q": 594, "f": 608, "x": 504}, {"q": 386, "f": 408, "x": 296}],
}


def _squeeze(text: str) -> str:
    return re.sub(r"[，。！？、,.!?\s]", "", text)


def _number(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")} | {"两": 2}
    if len(text) == 1 and text in digits:
        return digits[text]
    if text in ("百", "一百"):
        return 100
    if "十" in text and "百" not in text:
        tens, _, ones = text.partition("十")
        if (not tens or tens in digits) and (not ones or ones in digits):
            return digits.get(tens, 1) * 10 + digits.get(ones, 0)
    return None


def brightness_command(spoken: str, last: int | None) -> int | None:
    if match := _BRIGHTNESS.fullmatch(spoken):
        value = _number(match.group(1))
        if value is None or value > 100:
            return None
        return value * 10 if 0 < value <= 10 else value
    base = BRIGHTNESS_START if last is None else last
    if _BRIGHTER.fullmatch(spoken):
        return min(base + BRIGHTNESS_STEP, 100)
    if _DIMMER.fullmatch(spoken):
        return max(base - BRIGHTNESS_STEP, 0)
    return None


def _short(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else f"{text[:limit - 1]}…"


def _draft_text(draft: str) -> str:
    lines = wrapped(draft, BODY_WIDTH, DRAFT_SIZE)
    if len(lines) > DRAFT_LINES:
        lines = ["…" + lines[-DRAFT_LINES].lstrip(), *lines[-DRAFT_LINES + 1:]]
    return "\n".join(lines)


def _gauge(number: int, percent: int | None) -> dict[str, Any]:
    value = min(max(percent or 0, 0), 100)
    degrees = value * 18 // 5
    return {
        f"a{number}": min(degrees, 180),
        f"e{number}": max(degrees - 180, 0),
        f"m{number}": "--" if percent is None else str(percent),
        f"u{number}": _WHITE if value >= 80 else _DIM,
    }


def _tokens(count: int) -> str:
    if not count:
        return "--"
    return f"{count / 1000:.0f}k" if count < 1_000_000 else f"{count / 1_000_000:.1f}m"


_PLAN_STATES = {"completed": "✓", "in_progress": "●", "pending": "○"}


class Panel:
    def __init__(self, session_id: str, link: Link, allowed_devices: frozenset[str] = frozenset()):
        self.id = session_id
        self.link = link
        self.allowed_devices = allowed_devices
        self.epoch = 0
        self.template_id = ""
        self.page_uuid = ""
        self.selected: str | None = None
        self.pages = [""]
        self.page = self.slot = 0
        self.loaded: tuple[Any, ...] | None = None
        self.menu = 0
        self.cursor = LIST_BASE
        self.live = False
        self.overview = False
        self.mode_sent_at = float("-inf")
        self.queued: list[tuple[str, str, float, str]] = []
        self.reported_mode: int | None = None
        self.cursor_sent_at = float("-inf")
        self.list_version = 0
        self.device_tools: set[str] = set()
        self.brightness: int | None = None
        self.font_size = link.font_size if link.font_size in FONT_SIZES else TEXT_SIZE
        self.calls = 0
        self.input_mode = "ASR_TEXT"
        self.back = 0
        self.progress: dict[str, Any] = {}
        self.held: dict[str, Any] | None = None
        self.pushed_at = float("-inf")
        self.rows: list[dict[str, Any]] = []
        self.live_rows: list[list[Any]] = [[]]
        self.draft = ""
        self.draft_version = 0
        self.draft_at = 0.0
        self.target: str | None = None
        self.notice = ""
        self.hint = ""
        self.hint_at = 0.0
        self.alert = ""
        self.alert_at = float("-inf")
        self.offline_at = 0.0
        self.outgoing: dict[str, _Outgoing] = {}
        self.sent: dict[str, Any] = {}
        self.closing = False
        self.socket: Any = None
        self.emit: Any = None
        self.resent_at = float("-inf")
        self.last_action = time.monotonic()

    # ---------------------------------------------------------------- platform

    def envelope(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"specVersion": SPEC_VERSION, "sessionId": self.id, "type": kind, "payload": payload}

    def ready(self, mode: str) -> dict[str, Any]:
        return self.envelope("SESSION_READY", {"mode": mode, "connectionEpoch": self.epoch})

    def error(self, code: str, message: str, *, fatal: bool = False) -> dict[str, Any]:
        return self.envelope("ERROR", {"scope": "SESSION", "code": code, "message": message, "fatal": fatal})

    def speak(self, text: str) -> dict[str, Any]:
        action = {"speechId": f"speech_{uuid.uuid4().hex[:8]}", "index": 1, "hasNext": False, "text": text}
        return self.envelope("OUTPUT", {"actions": [{"type": "SPEAK", "payload": action}]})

    def _show(self, mode: str, data: dict[str, Any]) -> dict[str, Any]:
        props: dict[str, Any] = {"data": data}
        if mode == "OPEN":
            props["templateId"] = self.template_id
        action = {
            "pageUuid": self.page_uuid,
            "display": {"mode": mode},
            "view": {"type": "TEMPLATE", "props": props},
        }
        return self.envelope("OUTPUT", {"actions": [{"type": "SHOW_UI", "payload": action}]})

    def start(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        identity = payload.get("identity") or {}
        device = str(identity.get("deviceOpenId") or "")
        log.info("眼镜会话 %s：设备 %r", self.id, device)
        if self.allowed_devices and device not in self.allowed_devices:
            self.closing = True
            return [self.error("FORBIDDEN_DEVICE", "这副眼镜没有绑定这台电脑", fatal=True)]
        pages = payload.get("pages") or []
        wanted = [p for p in pages if PAGE_KEYWORD in str(p.get("description", ""))]
        self.template_id = next((str(p.get("templateId", "")) for p in wanted or pages), "") or os.environ.get("GLASSCODEX_TEMPLATE_ID", "")
        if not self.template_id:
            return [self.speak("Codex 页面还没配置好，没办法显示。")]
        self.input_mode = str(payload.get("inputMode") or "ASR_TEXT")
        tools = payload.get("tools") or []
        self.device_tools = {str(t.get("name")) for t in tools if isinstance(t, dict)}
        self.page_uuid = f"page_{uuid.uuid4().hex[:12]}"
        ids = [s["id"] for s in self.link.sessions]
        active = next((s["id"] for s in self.link.sessions if s.get("active")), None)
        self._select(self.link.last_selected if self.link.last_selected in ids else active or next(iter(ids), None))
        self._reload_rows()
        if self.selected is not None:
            self.link.watch(self.selected)
        data = {**PAGE_TABLES, **self._data()}
        self.sent = dict(data)
        return [self._show("OPEN", data)]

    def resumed(self) -> list[dict[str, Any]]:
        if not self.page_uuid:
            return []
        self.sent = {key: self.sent[key] for key in ("i", "c") if key in self.sent}
        self._follow_session()
        if self.menu != PICK:
            self._reload_rows()
        self.link.watch(self.selected)
        return self._refresh()

    def handle(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        kind, payload = message.get("type"), message.get("payload") or {}
        if kind == "UTTERANCE":
            return self._utterance(str(payload.get("text", "")))
        if kind == "UI_ACTION":
            self.last_action = time.monotonic()
            return self._action(payload)
        if kind == "TOOL_RESULT":
            return self._tool_result(payload)
        if kind == "ERROR":
            log.warning("平台报错（会话 %s）：%s", self.id, payload)
        return []

    def expired(self) -> bool:
        return time.monotonic() - self.last_action > IDLE_LIMIT_SECONDS

    def keepalive(self) -> list[dict[str, Any]]:
        if not self.page_uuid:
            return []
        return [self._show("UPDATE", {"h": self.sent.get("h", "")})]

    def failed(self) -> list[dict[str, Any]]:
        return [self.error("INTERNAL", "处理这条消息时出错了")]

    # ------------------------------------------------------------------ state

    @property
    def mode(self) -> int:
        return self.menu or (DRAFT if self.draft else LIVE if self.live or self.overview else READ)

    def _session(self) -> dict[str, Any] | None:
        return next((s for s in self.link.sessions if s["id"] == self.selected), None)

    def _now(self) -> float:
        mac = float(self.progress.get("now") or 0)
        if not mac:
            return time.time()
        if not self.link.online:
            return self.offline_at or mac
        return mac + max(0.0, time.monotonic() - self.pushed_at)

    def _elapsed(self) -> str:
        began = float(self.progress.get("began") or 0)
        last = float(self.progress.get("last") or began)
        end = last if self.progress.get("done") else self._now()
        return clock(max(end - began, 0))

    def _select(self, session: str | None) -> None:
        self.selected = session
        self.page = self.slot = self.back = 0
        self.overview = False
        self.loaded = None
        self.progress = self.held = {}
        self._reload_rows()

    def _reload_rows(self) -> None:
        sessions = self.link.sessions
        chosen = next((s for s in sessions if s["id"] == self.selected), None)
        ordered = ([chosen] if chosen else []) + [s for s in sessions if s["id"] != self.selected]
        rows = [
            {
                "id": s["id"],
                "a": _short(str(s.get("name", "")), 24),
                "e": _short(f"{s.get('project', '')} · {s.get('state_label', '')}", 22),
            }
            for s in ordered[:LIST_ROWS]
        ]
        if rows != self.rows:
            self.rows = rows
            self.list_version += 1

    def _follow_session(self) -> None:
        sessions = self.link.sessions
        if self.selected is None:
            if sessions:
                self._select(sessions[0]["id"])
            return
        if self._session() is None:
            self._select(sessions[0]["id"] if sessions else None)

    def _load(self, turn: dict[str, Any] | None) -> None:
        self.progress = turn or {}
        self.live = bool(turn and not turn.get("done"))
        self.overview = self.overview and bool(turn)
        text = ""
        if turn:
            text = str(turn.get("narration") if self.live else turn.get("text") or turn.get("narration") or "")
        if not text:
            text = _NOT_CONNECTED if not self.link.online else ("还没有内容。" if turn else "选一个任务看结果。")
        width = NARROW_WIDTH if self.live or self.overview else BODY_WIDTH
        self.pages = paginate(text, width, self.font_size)
        self.page = min(self.page, len(self.pages) - 1)
        steps = list(turn.get("steps") or []) if turn else []
        for session, body, _, _ in self.queued:
            if session == self.selected:
                steps.append({"k": "you", "t": f"排队中：{body}"})
        rows = stream_rows(steps, self._now(), NARROW_WIDTH, self.font_size)
        self.live_rows = live_pages(rows, self.font_size)
        self.back = min(self.back, max(0, len(self.live_rows) - 1))
        self.loaded = (
            turn.get("prompt") if turn else None,
            self.live,
            text,
            len(self.pages),
            self.font_size,
        )

    # ------------------------------------------------------------------ data

    def _plan_rows(self) -> list[dict[str, Any]]:
        plan = list(self.progress.get("plan") or [])[:5]
        out = []
        for i, step in enumerate(plan):
            state = str(step.get("s") or "pending")
            out.append({
                "t": _short(str(step.get("t") or ""), 30),
                "s": _PLAN_STATES.get(state, "○"),
                "c": _WHITE if state == "in_progress" else _DIM if state == "completed" else _QUIET,
            })
        while len(out) < 5:
            out.append({"t": "", "s": "", "c": _BLACK})
        return out

    def _right_rows(self) -> list[tuple[str, str]]:
        plan = self.progress.get("plan") or []
        current = next((s for s in plan if s.get("s") == "in_progress"), None)
        if current:
            first = (_short(str(current.get("t") or ""), 18), "正在执行")
        else:
            last = next((s for s in reversed(plan) if s.get("s") == "completed"), None)
            first = (_short(str(last.get("t") or ""), 18), "计划") if last else ("没有计划", "Codex 会先分析再动手")
        return [
            first,
            (self.progress.get("diff_label", "暂无文件改动"), "本轮改动"),
            (_short(str((self._session() or {}).get("project", "")), 18), "工作区"),
        ]

    def _menu_view(self) -> dict[str, Any]:
        done = bool(self.progress.get("done"))
        if self.overview or done:
            return {"t": "看回复", "m": READ, "p": 0}
        if self.progress:
            return {"t": "看进度", "m": LIVE, "p": 1}
        return {"t": "看进度", "m": READ, "p": 0}

    def _data(self) -> dict[str, Any]:
        session = self._session()
        title = _short(str(session.get("name", "Codex")), 22) if session else "Codex"
        project = _short(str(session.get("project", "")), 22) if session else ""
        prompt = _short(str(self.progress.get("prompt", "")), 60)
        status = str(self.progress.get("status") or (session or {}).get("state_label", "已完成"))
        state_label = {
            "running": "运行中",
            "failed": "失败",
            "interrupted": "已中断",
            "done": "已完成",
        }.get(status, status)
        model = _short(str(self.progress.get("model") or (session or {}).get("model", "")), 24)
        effort = str(self.progress.get("effort") or "")
        if effort:
            model = f"{model}\n{effort}" if model else effort
        page = self.pages[self.page] if self.pages else ""
        rp = 1 if self.live or self.overview else 0
        right = self._right_rows()
        notice = self.alert or self.notice or self.hint
        footer = notice or ""
        if notice:
            fk = FOOT_NEED if self.alert else FOOT_QUIET
        elif not self.link.online:
            footer, fk = "电脑离线", FOOT_NEED
        elif self.live:
            current = ((self.progress.get("doing") or {}).get("label") or "Codex 正在思考")
            footer, fk = current, FOOT_LIVE
        elif self.progress.get("status") == "failed":
            footer, fk = "这一轮失败了", FOOT_NEED
        elif self.progress:
            footer, fk = f"已完成 · {self.progress.get('files', 0)} 个文件改动", FOOT_QUIET
        else:
            footer, fk = "选一个任务", FOOT_QUIET
        gauges: dict[str, Any] = {}
        gauges.update(_gauge(1, self.progress.get("context_percent")))
        gauges.update(_gauge(2, self.progress.get("plan_percent")))
        gauges.update(_gauge(3, self.progress.get("five_hour")))
        tokens = _tokens(int(self.progress.get("context") or 0))
        window = _tokens(int(self.progress.get("window") or 0))
        right[2] = ("上下文", f"{tokens} / {window}")
        data = {
            "h": title,
            "pr": project,
            "q": prompt,
            "st": state_label,
            "tm": self._elapsed() if self.progress else "0:00",
            "mo": model,
            "P": [{"t": body, "s": i + 1} for i, body in enumerate(self.pages)],
            "i": self.slot,
            "hi": (self.page + AHEAD) % SLOTS,
            "lo": (self.page - BEHIND) % SLOTS,
            "fz": self.font_size,
            "rp": rp,
            "n0": right[0][0],
            "d0": right[0][1],
            "n1": right[1][0],
            "d1": right[1][1],
            "n2": right[2][0],
            "d2": right[2][1],
            "dfx": _short(str(self.progress.get("diff_label", "暂无文件改动")), 28),
            "PL": self._plan_rows(),
            "L": self.rows + [{"id": "", "a": "", "e": ""} for _ in range(LIST_ROWS - len(self.rows))],
            "lv": self.list_version,
            "c": self.cursor,
            "m": self.mode,
            "mv": self._menu_view(),
            "dr": _draft_text(self.draft),
            "dt": f"发给 {title}",
            "dv": self.draft_version,
            "df": "单击发送" if self.draft else footer,
            "ft": footer,
            "fk": fk,
            "ln": _cursor_table(len(self.rows), 1),
            "lp": _cursor_table(len(self.rows), -1),
            "R": row_data(self.live_rows[-1 - self.back] if self.live_rows else [], self.font_size),
            "B": bar_data(self.live_rows[-1 - self.back] if self.live_rows else [], self.font_size),
            "fm": "/" + str(len(self.pages)),
        }
        data.update(gauges)
        return data

    def _refresh(self) -> list[dict[str, Any]]:
        data = {**PAGE_TABLES, **self._data()}
        changed = {key: value for key, value in data.items() if self.sent.get(key) != value}
        if not changed:
            return []
        self.sent.update(changed)
        return [self._show("UPDATE", changed)]

    def _full_refresh(self) -> list[dict[str, Any]]:
        data = {**PAGE_TABLES, **self._data()}
        self.sent.update(data)
        return [self._show("UPDATE", data)]

    # --------------------------------------------------------------- actions

    def _action(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        action = payload.get("action") or {}
        name = action.get("name")
        params = action.get("params") or {}
        self.last_action = time.monotonic()
        if isinstance(params.get("m"), int) and time.monotonic() - self.mode_sent_at > REPORT_SECONDS:
            self.sent["m"] = params["m"]
        self.reported_mode = params.get("m") if isinstance(params.get("m"), int) else None
        try:
            return self._act(name, params)
        finally:
            self.reported_mode = None

    def _act(self, name: Any, params: dict[str, Any]) -> list[dict[str, Any]]:
        if name == "menu":
            self.menu = MENU
            return self._refresh()
        if name == "older":
            self.back += 1
            return self._refresh()
        if name == "newer":
            if self.overview and not self.back:
                self.overview, self.page = False, 0
            self.back = max(self.back - 1, 0)
            return self._refresh()
        if name == "more":
            self.overview = bool(self.progress) and not self.live
            self.back = 0
            self._forget("rp", "rb")
            return self._refresh()
        if name == "pick":
            return self._choose(params.get("k"), params.get("s"), params)
        if name == "send":
            return self._send(params.get("dv"))
        if name == "font":
            return self._set_font(params.get("f"))
        if name == "drop":
            return self._drop(params.get("dv"))
        if name == "turn":
            return self._turn(params.get("slot"))
        if name == "exit":
            self.closing = True
            return []
        return self._refresh()

    def _turn(self, slot: Any) -> list[dict[str, Any]]:
        if not isinstance(slot, int) or not 0 <= slot < SLOTS:
            return []
        delta = (slot - self.slot) % SLOTS
        if delta > AHEAD:
            delta -= SLOTS
        if delta == 0 and time.monotonic() - self.resent_at >= RESEND_SECONDS:
            self.resent_at = time.monotonic()
            for key in ("P", "hi", "lo"):
                self.sent.pop(key, None)
        self.slot = slot
        self.page = min(max(self.page + delta, 0), len(self.pages) - 1)
        self.sent["i"] = slot
        return self._refresh()

    def _choose(self, item: Any, shown: Any = None, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if not isinstance(item, int) or not 0 <= item < CURSORS:
            return self._refresh()
        expected = next(menu for menu, (first, end) in _SEGMENT.items() if first <= item < end)
        seen = params.get("l") if isinstance(params, dict) else None
        stale = expected == PICK and item != LIST_BASE and isinstance(seen, int) and seen != self.list_version
        if self.menu != expected or (isinstance(shown, int) and shown != expected) or stale:
            self.reported_mode = None
            self._forget("m", "rp", "rb", "L", "lv")
            return self._refresh()
        if item == LIST_BASE:
            self.menu = MENU
            self._reload_rows()
        elif item == QUICK_BASE:
            self.menu = MENU
        elif LIST_BASE < item < QUICK_BASE:
            return self._pick(item - LIST_BASE - 1)
        elif QUICK_BASE < item < MENU_BASE:
            self._close_menu()
            return self._quick(item - QUICK_BASE - 1)
        elif item == MENU_SWITCH:
            self.notice = ""
            self.menu = PICK
        elif item == MENU_VIEW:
            self._close_menu()
            self._toggle_view()
            self._forget("rp", "rb")
        elif item == MENU_PLAN:
            self.menu = PLAN
        elif item == MENU_QUICK:
            self.menu = QUICK
        elif item == MENU_FONT:
            self.menu = FONT
        else:
            self._close_menu()
        return self._refresh()

    def _toggle_view(self) -> None:
        if self.live or self.overview:
            self.overview = False
        elif self.progress:
            self.overview = True
        self.back = 0

    def _pick(self, index: int) -> list[dict[str, Any]]:
        self._close_menu()
        if 0 <= index < len(self.rows):
            row = self.rows[index]
            alive = next((s for s in self.link.sessions if s["id"] == row["id"]), None)
            if alive:
                self._select(alive["id"])
        self._reload_rows()
        return self._refresh()

    def _set_font(self, size: Any) -> list[dict[str, Any]]:
        self._close_menu()
        if size in FONT_SIZES and size != self.font_size:
            held, notice = self.held, self.notice
            self.font_size = self.link.font_size = size
            self._load(self.progress or None)
            self.held, self.notice, self.back = held, notice, 0
        return self._refresh()

    def _close_menu(self) -> None:
        self.menu = 0
        self.hint = ""

    def _forget(self, *keys: str) -> None:
        for key in keys:
            self.sent.pop(key, None)

    def _send(self, version: Any) -> list[dict[str, Any]]:
        if not self.draft or version != self.draft_version:
            return self._stale_draft()
        if self._deliver(self.draft, self.target):
            self._clear_draft()
            self.notice = "发送中"
        return self._refresh()

    def _quick(self, index: int) -> list[dict[str, Any]]:
        if 0 <= index < len(QUICK_REPLIES) and self._deliver(QUICK_REPLIES[index], self.selected):
            self.notice = "发送中"
        return self._refresh()

    def _deliver(self, text: str, target: str | None) -> bool:
        self.hint_at = time.monotonic()
        if target is None:
            self.hint = "没有选中的任务"
            return False
        if target not in [s["id"] for s in self.link.sessions]:
            self.hint = "电脑离线，没发出去" if not self.link.online else "这个任务已经不在列表里了"
            return False
        command = self.link.send(target, text)
        if command is None:
            self.hint = "电脑离线，没发出去"
            return False
        self.outgoing[command] = _Outgoing(command, text, target, time.monotonic())
        self.queued.append((target, text, time.monotonic(), command))
        return True

    def _stale_draft(self) -> list[dict[str, Any]]:
        self.hint = "内容变了，再确认一次" if self.draft else ""
        for key in ("dr", "dv", "dt"):
            self.sent.pop(key, None)
        return self._refresh()

    def _drop(self, version: Any) -> list[dict[str, Any]]:
        if version != self.draft_version:
            return self._stale_draft()
        self._clear_draft()
        return self._refresh()

    def _clear_draft(self) -> None:
        self.draft = self.hint = ""
        self.draft_version += 1

    # ------------------------------------------------------------------ voice

    def _utterance(self, text: str) -> list[dict[str, Any]]:
        spoken = _squeeze(text)
        log.info("语音（会话 %s）：%d 字", self.id, len(text))
        if spoken in _LIST_WORDS:
            self.last_action = time.monotonic()
            self.notice = self.alert = ""
            self.menu, self.cursor = PICK, LIST_BASE
            self._forget("m", "c")
            return self._refresh()
        if spoken in _PLAN_WORDS:
            self.last_action = time.monotonic()
            self.notice = self.alert = ""
            self.menu = PLAN
            self._forget("m")
            return self._refresh()
        if spoken in _CANCEL_WORDS:
            if not self.draft and self.menu != PLAN:
                return []
            self.last_action = time.monotonic()
            self._clear_draft()
            self.menu = 0
            return self._refresh()
        level = brightness_command(spoken, self.brightness)
        if level is not None:
            self.last_action = time.monotonic()
            return self._set_brightness(level)
        if len(spoken) < MIN_DRAFT_CHARS:
            return []
        if self.selected is None:
            self.hint, self.hint_at = "还没有任务，这句没记下", time.monotonic()
            return self._refresh()
        appending = bool(self.draft)
        self.draft = f"{self.draft}{text.strip()}" if appending else text.strip()
        if not appending:
            self.target = self.selected
        self.draft_at = time.monotonic()
        self.draft_version += 1
        self.hint = "有新草稿，选完再看" if self.menu else ""
        if not self.menu:
            self.sent.pop("m", None)
        return self._refresh()

    def _set_brightness(self, level: int) -> list[dict[str, Any]]:
        now = time.monotonic()
        if BRIGHTNESS_TOOL not in self.device_tools:
            self.hint, self.hint_at = "这次会话平台没有开放亮度调节", now
            return self._refresh()
        self.brightness = level
        self.calls += 1
        self.hint, self.hint_at = f"亮度调到 {level}", now
        call = {"callId": f"brightness_{self.calls}", "name": BRIGHTNESS_TOOL, "arguments": {"operation": "SET", "value": level}}
        return [self.envelope("TOOL_CALL", call), *self._refresh()]

    def _tool_result(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if not str(payload.get("callId")).startswith("brightness_") or payload.get("status") in ("DISPATCHED", "SUCCESS"):
            return []
        self.hint, self.hint_at = "亮度没调成", time.monotonic()
        return self._refresh()

    # ---------------------------------------------------------- Desktop updates

    def on_sessions(self) -> list[dict[str, Any]]:
        self._follow_session()
        if self.selected is not None:
            self.link.watch(self.selected)
        if self.menu != PICK:
            self._reload_rows()
        return self._refresh()

    def on_turn(self, turn: dict[str, Any]) -> list[dict[str, Any]]:
        if turn.get("session") != self.selected:
            return []
        started_or_ended = (turn.get("prompt"), turn.get("done")) != (
            self.progress.get("prompt"),
            self.progress.get("done"),
        )
        if not started_or_ended and time.monotonic() - self.pushed_at < LIVE_SECONDS:
            self.held = turn
            return []
        return self._push(turn)

    def _push(self, turn: dict[str, Any]) -> list[dict[str, Any]]:
        self.pushed_at = time.monotonic()
        if turn.get("prompt") != self.progress.get("prompt") or turn.get("done"):
            self.notice = ""
        self._load(turn)
        return self._refresh()

    def tick(self) -> list[dict[str, Any]]:
        out = self._give_up(list(self._overdue()))
        if self.hint and not self.draft and not self.menu and time.monotonic() - self.hint_at >= HINT_SECONDS:
            self.hint = ""
            out = [*out, *self._refresh()]
        if time.monotonic() - self.pushed_at >= LIVE_SECONDS:
            if self.held is not None:
                out = [*out, *self._push(self.held)]
            elif self.link.online and self._right_shown() and self.mode in (READ, LIVE):
                self.pushed_at = time.monotonic()
                out = [*out, *self._refresh()]
        return out

    def _right_shown(self) -> bool:
        return bool(self.live or self.overview)

    def _overdue(self) -> Iterator[str]:
        now = time.monotonic()
        for item in self.outgoing.values():
            if now - item.at > (RECEIPT_WAIT_SECONDS if item.posted else SEND_WAIT_SECONDS):
                yield item.command

    def _give_up(self, commands: list[str]) -> list[dict[str, Any]]:
        if not commands:
            return []
        for command in commands:
            self.outgoing.pop(command, None)
        self.alert, self.alert_at = _UNSURE, time.monotonic()
        self.notice = ""
        return self._refresh()

    def on_sent(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        outgoing = self.outgoing.get(str(event.get("cmd")))
        if outgoing is None:
            return []
        state = event.get("state")
        if state == "posted":
            outgoing.posted, outgoing.at = True, time.monotonic()
            self.notice = "已发出"
            return self._refresh()
        self.outgoing.pop(outgoing.command, None)
        self.notice = ""
        if state == "delivered":
            self.notice = "Codex 已收到"
            self.queued = [item for item in self.queued if item[3] != outgoing.command]
        elif state == "unconfirmed":
            self.alert, self.alert_at = "没等到回执，先看电脑", time.monotonic()
        else:
            self.alert, self.alert_at = f"没发出去：{event.get('error') or '未知原因'}", time.monotonic()
            self.queued = [item for item in self.queued if item[3] != outgoing.command]
            if not self.draft:
                self.draft, self.target, self.draft_at = outgoing.text, outgoing.target, time.monotonic()
                self.draft_version += 1
        return self._refresh()

    def on_link(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self.link.online:
            self.link.watch(self.selected)
            self._follow_session()
        else:
            self.offline_at = time.monotonic()
            out = self._give_up([item.command for item in self.outgoing.values() if not item.posted])
        if self.selected is None or self.loaded is None or not self.loaded[0]:
            self._load(self.link.turn(self.selected) if self.selected else None)
        return [*out, *self._refresh()]
