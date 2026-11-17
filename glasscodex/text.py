"""Text layout for the glasses page.

The device only accepts a small set of font sizes and its canvas is 640x290.
The browser simulator is slightly more forgiving than the real device, so we
wrap text before sending it instead of relying on the label's clipping mode.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "BODY_HEIGHT",
    "BODY_WIDTH",
    "FONT_SIZES",
    "LINE_PITCH",
    "NARROW_WIDTH",
    "TEXT_SIZE",
    "lines_per_page",
    "paginate",
    "wrapped",
]

TEXT_SIZE = 18
FONT_SIZES = (16, 18, 20, 22, 24)
LINE_PITCH = {16: 23, 18: 26, 20: 29, 22: 32, 24: 35}
BODY_WIDTH, NARROW_WIDTH, BODY_HEIGHT = 606, 386, 206
HANG = 12
_NO_LINE_START = frozenset("，。、；：？！）》」』】〉…—～·,.;:?!)]}%”’")
_UNIT = re.compile(r"[A-Za-z0-9_./:@#%+=~\\-]+[,;!?)\]]*|.", re.S)


def _char_width(ch: str, size: int) -> int:
    if ch == "\t":
        return size * 2
    if unicodedata.east_asian_width(ch) in ("W", "F", "A") and ord(ch) > 127:
        return size
    return max(1, round(size * 0.56))


def _wrap(paragraph: str, width: int, size: int) -> list[str]:
    lines: list[str] = []
    line = ""
    used = 0
    for unit in _UNIT.findall(paragraph):
        advance = sum(_char_width(ch, size) for ch in unit)
        hanging = len(unit) == 1 and unit in _NO_LINE_START
        if line and not hanging and used + advance > width:
            lines.append(line.rstrip())
            line, used = "", 0
        if advance > width:
            for ch in unit:
                step = _char_width(ch, size)
                if line and used + step > width:
                    lines.append(line)
                    line, used = "", 0
                line += ch
                used += step
            continue
        line += unit
        used += advance
    if line or not lines:
        lines.append(line.rstrip())
    return lines


def lines_per_page(size: int = TEXT_SIZE) -> int:
    return max(4, (BODY_HEIGHT - size) // LINE_PITCH[size] + 1)


def paginate(text: str, width: int = BODY_WIDTH, size: int = TEXT_SIZE) -> list[str]:
    per_page = lines_per_page(size)
    lines: list[str] = []
    for paragraph in text.split("\n"):
        lines.extend(_wrap(paragraph, width, size) if paragraph.strip() else [""])
    pages: list[str] = []
    current: list[str] = []
    for line in lines:
        if not line and not current:
            continue
        current.append(line)
        if len(current) >= per_page:
            pages.append("\n".join(current).rstrip())
            current = []
    if any(current):
        pages.append("\n".join(current).rstrip())
    return pages or [""]


def wrapped(text: str, width: int, size: int) -> list[str]:
    return _wrap(" ".join(text.split()), width, size)
