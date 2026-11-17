"""Turn the running Codex timeline into seven rows on the glasses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .text import FONT_SIZES, LINE_PITCH, NARROW_WIDTH, TEXT_SIZE, wrapped

__all__ = [
    "BARS",
    "ROWS",
    "Row",
    "bar_data",
    "blank_bars",
    "blank_rows",
    "clock",
    "live_pages",
    "row_data",
    "stream_rows",
]

TOP, BOTTOM, ROWS = 40, 248, 7
BARS = 2
OFFSCREEN = 300
WHITE, OLDER, QUOTE, DIM, LIVE, BLACK = "#FFFFFF", "#B4B4B4", "#B0B0B0", "#808080", "#D0D0D0", "#000000"
_COLOR = {"new": WHITE, "old": OLDER, "you": QUOTE, "tool": DIM, "live": LIVE, "agent": "#C8C8C8"}
_PARAGRAPH, _BLOCK = 10, 8


def _small(size: int) -> int:
    return max(size - 2, FONT_SIZES[0])


def _height(kind: str, size: int) -> int:
    return LINE_PITCH[_small(size)] + 1 if kind in ("tool", "live", "agent") else LINE_PITCH[size]


@dataclass(frozen=True)
class Row:
    kind: str
    text: str
    time: str = ""
    paragraph: bool = False


def clock(seconds: float) -> str:
    total = max(int(seconds), 0)
    minutes, rest = divmod(total, 60)
    return f"{minutes // 60}h{minutes % 60:02d}" if minutes >= 60 else f"{minutes}:{rest:02d}"


def stream_rows(steps: list[dict[str, Any]], now: float, width: int = NARROW_WIDTH, size: int = TEXT_SIZE) -> list[Row]:
    last_text = max((i for i, step in enumerate(steps) if step.get("k") == "text"), default=-1)
    rows: list[Row] = []
    for i, step in enumerate(steps):
        kind = step.get("k")
        if kind == "tools":
            running = bool(step.get("o"))
            spent = (now if running else float(step.get("e") or 0)) - float(step.get("s") or 0)
            rows.append(Row("live" if running else "tool", f"已运行 {step.get('n', 0)} 个命令", clock(spent)))
        elif kind == "file":
            rows.append(Row("tool", f"改了 {step.get('n', 0)} 个文件", ""))
        elif kind == "agent":
            rows.append(Row("agent", "子代理正在协作", clock(max(0, now - float(step.get("s") or now)))))
        elif kind == "you":
            rows.extend(Row("you", line) for line in wrapped(str(step.get("t", "")), width, size))
        elif kind == "text":
            style, spaced = ("new" if i == last_text else "old"), False
            for paragraph in str(step.get("t", "")).split("\n"):
                if not paragraph.strip():
                    spaced = True
                    continue
                for j, line in enumerate(wrapped(paragraph, width, size)):
                    rows.append(Row(style, line, paragraph=spaced and j == 0))
                spaced = False
    return rows


def _gap(above: Row, below: Row) -> int:
    if below.paragraph:
        return _PARAGRAPH
    flows = above.kind == below.kind or {above.kind, below.kind} == {"new", "old"} or below.kind in ("tool", "live", "agent")
    return 0 if flows else _BLOCK


def live_pages(rows: list[Row], size: int = TEXT_SIZE) -> list[list[Row]]:
    pages: list[list[Row]] = []
    page: list[Row] = []
    used = 0
    for row in reversed(rows):
        add = _height(row.kind, size) + (_gap(row, page[0]) if page else 0)
        if page and (used + add > BOTTOM - TOP or len(page) == ROWS):
            pages.insert(0, page)
            page, used, add = [], 0, _height(row.kind, size)
        page.insert(0, row)
        used += add
    pages.insert(0, page)
    return pages


def _placed(page: list[Row], size: int) -> list[tuple[Row, int]]:
    placed = []
    y, above = TOP, None
    for row in page:
        if above is not None:
            y += _gap(above, row)
        placed.append((row, y))
        y += _height(row.kind, size)
        above = row
    return placed


def row_data(page: list[Row], size: int = TEXT_SIZE) -> list[dict[str, Any]]:
    out = []
    for row, y in _placed(page, size):
        small = row.kind in ("tool", "live", "agent")
        out.append({
            "y": y,
            "r": row.text,
            "t": row.time,
            "c": _COLOR[row.kind],
            "z": _small(size) if small else size,
        })
    return out + blank_rows(ROWS - len(page))


def bar_data(page: list[Row], size: int = TEXT_SIZE) -> list[dict[str, int]]:
    runs: list[list[int]] = []
    previous = None
    for row, y in _placed(page, size):
        if row.kind == "you":
            if previous == "you":
                runs[-1][1] = y + _height(row.kind, size)
            else:
                runs.append([y, y + _height(row.kind, size)])
        previous = row.kind
    return [{"y": top, "h": bottom - top} for top, bottom in runs[-BARS:]] + blank_bars(BARS - min(len(runs), BARS))


def blank_rows(count: int = ROWS) -> list[dict[str, Any]]:
    return [{"y": OFFSCREEN, "r": "", "t": "", "c": BLACK, "z": TEXT_SIZE} for _ in range(count)]


def blank_bars(count: int = BARS) -> list[dict[str, int]]:
    return [{"y": OFFSCREEN, "h": 1} for _ in range(count)]
