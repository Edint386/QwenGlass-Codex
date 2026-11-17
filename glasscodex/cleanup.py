"""Redact secrets and flatten Markdown before content leaves the Mac."""

from __future__ import annotations

import re

__all__ = ["MAX_CHARS", "brief", "clean", "fold"]

MAX_CHARS = 6000
MASK = "〔已隐藏〕"
_SECRETS = (
    re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?<![A-Za-z0-9_])(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{16,}"),
    re.compile(r"(?<![A-Za-z0-9_])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    re.compile(r"(?<![A-Za-z0-9_])xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?<![A-Za-z0-9_])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
    re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32,}(?![0-9a-fA-F])"),
)
_AUTH_LINE = re.compile(r"(?i)(authorization[\"'`]{0,3}[ \t]*[:=][ \t]*[\"'`]?)((?:[A-Za-z]{1,16}[ \t]+)?[!-~]{4,})")
_AUTH_WORD = re.compile(r"(?i)(?<![A-Za-z0-9_])(bearer|basic)[ \t]+([A-Za-z0-9._~+/-]{8,}={0,2})")
_PRIVATE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY[A-Z ]*-----.*?-----END [A-Z ]*PRIVATE KEY[A-Z ]*-----", re.S)
_URL_PASSWORD = re.compile(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]{0,20}://[^\s:/@]{0,64}):([!-~]{1,200})@")
_KEY_VALUE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])((?:api[ _-]?key|access[ _-]?key|client[ _-]?secret|secret(?:[ _-]?key)?|token|password|passwd|私钥|密钥|令牌|密码)[\"'`*]{0,3}[ \t]*[:=：][ \t]*[\"'`]?)([!-~]{6,})"
)
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,}).*$")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_LINK = re.compile(r"!?\[([^\]\n]{0,200})\]\(([^)\s]{0,500})(?:\s+\"[^\"\n]{0,200}\")?\)")
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.M)
_BULLET = re.compile(r"^([ \t]*)[-*+][ \t]+", re.M)
_EMPHASIS = re.compile(r"(?<![A-Za-z0-9_*])(\*\*|__)(?=\S)([^\n]{1,200}?)(?<=\S)\1(?![A-Za-z0-9_*])")
_BLANKS = re.compile(r"\n{3,}")


def fold(text: str) -> str:
    out: list[str] = []
    fence = ""
    count = 0
    for line in text.split("\n"):
        opening = _FENCE.match(line)
        if fence:
            if opening and opening.group(1)[0] == fence[0] and len(opening.group(1)) >= len(fence):
                out.append(f"〔代码 {count} 行〕")
                fence, count = "", 0
            else:
                count += 1
            continue
        if opening:
            fence = opening.group(1)
            continue
        out.append(line)
    if fence:
        out.append(f"〔代码 {count} 行〕")
    return "\n".join(out)


def _redact(text: str) -> str:
    text = _PRIVATE.sub(MASK, text)
    for pattern in _SECRETS:
        text = pattern.sub(MASK, text)
    text = _AUTH_LINE.sub(lambda m: f"{m.group(1)}{MASK}", text)
    text = _AUTH_WORD.sub(lambda m: f"{m.group(1)} {MASK}", text)
    text = _URL_PASSWORD.sub(lambda m: f"{m.group(1)}:{MASK}@", text)
    text = _KEY_VALUE.sub(lambda m: f"{m.group(1)}{MASK}", text)
    return text


def clean(text: str) -> str:
    text = _redact(fold(text or ""))
    text = _MD_LINK.sub(lambda m: m.group(1), text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub(r"· ", text)
    text = _INLINE_CODE.sub(lambda m: m.group(1), text)
    text = _EMPHASIS.sub(lambda m: m.group(2), text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _BLANKS.sub("\n\n", text).strip()
    return text[:MAX_CHARS]


def brief(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else f"{text[:limit - 1]}…"
