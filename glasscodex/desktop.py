"""Read Codex Desktop state without starting another Codex process.

The Desktop app persists its task list in ``state_5.sqlite``, its projected
turn history in ``thread_history_1.sqlite`` and detailed events in rollout
JSONL files. These are local implementation surfaces, so every column and item
is optional here; unknown data is skipped instead of breaking the bridge.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["Call", "DesktopReader", "ThreadInfo", "TurnState"]


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _seconds(timestamp: Any) -> float:
    try:
        return datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _epoch_ms(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") in ("text", "input_text")
        )
    return ""


def _short_name(name: str) -> str:
    return {
        "exec_command": "运行命令",
        "write_stdin": "查看命令输出",
        "js": "运行页面脚本",
        "view_image": "查看图片",
        "update_plan": "更新计划",
        "spawn_agent": "派出代理",
        "wait_agent": "等待代理",
        "send_message": "联系代理",
        "apply_patch": "修改文件",
    }.get(name, name.replace("_", " "))


def _command_label(command: Any) -> str:
    text = command if isinstance(command, str) else " ".join(str(part) for part in command or [])
    lowered = text.lower()
    if "pytest" in lowered or " vitest" in lowered or " jest" in lowered:
        return "运行测试"
    if lowered.startswith("git ") or " git " in lowered:
        return "检查 Git"
    if "rg " in lowered or "grep " in lowered or "find " in lowered:
        return "搜索项目"
    if "apply_patch" in lowered:
        return "修改文件"
    if "uv run" in lowered or "npm " in lowered or "pnpm " in lowered or "cargo " in lowered:
        return "运行项目工具"
    if lowered.startswith("curl ") or " curl " in lowered:
        return "请求接口"
    return "运行命令"


def _diff_stats(diff: str) -> tuple[int, int]:
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


@dataclass(frozen=True)
class Call:
    id: str
    name: str
    label: str = ""
    at: float = 0.0


@dataclass(frozen=True)
class ThreadInfo:
    id: str
    title: str
    cwd: str
    model: str
    effort: str
    updated_at: int
    status: str
    active: bool
    rollout_path: str = ""

    @property
    def project(self) -> str:
        path = Path(self.cwd)
        return path.name or self.cwd

    @property
    def state_label(self) -> str:
        return {
            "running": "运行中",
            "failed": "失败",
            "interrupted": "已中断",
            "done": "已完成",
        }.get(self.status, "已完成")


@dataclass
class TurnState:
    thread_id: str
    prompt: str = ""
    text: str = ""
    narration: str = ""
    status: str = "done"
    steps: list[dict[str, Any]] = field(default_factory=list)
    tools: int = 0
    current: Call | None = None
    running: tuple[Call, ...] = ()
    plan: list[dict[str, str]] = field(default_factory=list)
    plan_at: float = 0.0
    context: int = 0
    window: int = 0
    five_hour: int | None = None
    seven_day: int | None = None
    files: int = 0
    added: int = 0
    removed: int = 0
    began: float = 0.0
    last: float = 0.0
    model: str = ""
    effort: str = ""
    error: str = ""

    @property
    def done(self) -> bool:
        return self.status not in ("running", "starting")

    @property
    def context_percent(self) -> int | None:
        return round(self.context * 100 / self.window) if self.window and self.context else None

    @property
    def plan_percent(self) -> int | None:
        if not self.plan:
            return None
        completed = sum(1 for step in self.plan if step.get("s") == "completed")
        return round(completed * 100 / len(self.plan))

    @property
    def diff_label(self) -> str:
        if not self.files:
            return "暂无文件改动"
        return f"{self.files} 个文件 +{self.added} -{self.removed}"


class DesktopReader:
    """Read-only view of the running Codex Desktop installation."""

    def __init__(self, home: Path | None = None):
        self.home = home or Path.home()
        self.codex = self.home / ".codex"
        self.state_path = self._database("state_*.sqlite", self.codex / "state_5.sqlite")
        self.history_path = self._database("thread_history_*.sqlite", self.codex / "thread_history_1.sqlite")

    def _database(self, pattern: str, preferred: Path) -> Path:
        if preferred.exists():
            return preferred
        candidates = [path for path in self.codex.glob(pattern) if path.is_file()]
        return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else preferred

    def available(self) -> bool:
        return self.state_path.exists() and self.history_path.exists()

    def _turns(self, thread_ids: list[str]) -> dict[str, sqlite3.Row]:
        if not thread_ids:
            return {}
        marks = ",".join("?" for _ in thread_ids)
        try:
            with _connect(self.history_path) as db:
                rows = db.execute(
                    f"""
                    WITH ranked AS (
                      SELECT thread_id, turn_id, status, started_at, completed_at, duration_ms,
                             ROW_NUMBER() OVER (PARTITION BY thread_id ORDER BY rollout_ordinal DESC) AS n
                      FROM thread_turns WHERE thread_id IN ({marks})
                    )
                    SELECT * FROM ranked WHERE n = 1
                    """,
                    thread_ids,
                ).fetchall()
                return {str(row["thread_id"]): row for row in rows}
        except sqlite3.Error:
            return {}

    def list_threads(self, limit: int = 20) -> list[ThreadInfo]:
        if not self.available():
            return []
        try:
            with _connect(self.state_path) as db:
                rows = db.execute(
                    """
                    SELECT id, title, name, preview, cwd, model, reasoning_effort,
                           updated_at_ms, rollout_path, thread_source
                    FROM threads
                    WHERE archived = 0
                      AND (thread_source IS NULL OR thread_source != 'subagent')
                    ORDER BY recency_at_ms DESC, updated_at_ms DESC
                    LIMIT ?
                    """,
                    (max(limit, 1),),
                ).fetchall()
        except sqlite3.Error:
            return []
        turns = self._turns([str(row["id"]) for row in rows])
        result: list[ThreadInfo] = []
        for row in rows:
            turn = turns.get(str(row["id"]))
            raw_status = str(turn["status"]) if turn else "completed"
            status = {
                "inProgress": "running",
                "failed": "failed",
                "interrupted": "interrupted",
            }.get(raw_status, "done")
            title = str(row["title"] or row["name"] or row["preview"] or Path(str(row["cwd"])).name)
            title = " ".join(title.replace("\\n", " ").split())
            result.append(
                ThreadInfo(
                    id=str(row["id"]),
                    title=title,
                    cwd=str(row["cwd"] or ""),
                    model=str(row["model"] or ""),
                    effort=str(row["reasoning_effort"] or ""),
                    updated_at=_epoch_ms(row["updated_at_ms"]),
                    status=status,
                    active=status == "running",
                    rollout_path=str(row["rollout_path"] or ""),
                )
            )
        return result

    def thread(self, thread_id: str) -> ThreadInfo | None:
        return next((thread for thread in self.list_threads(100) if thread.id == thread_id), None)

    def _latest_turn(self, thread_id: str) -> sqlite3.Row | None:
        try:
            with _connect(self.history_path) as db:
                return db.execute(
                    """
                    SELECT turn_id, status, started_at, completed_at, duration_ms
                    FROM thread_turns WHERE thread_id = ?
                    ORDER BY rollout_ordinal DESC LIMIT 1
                    """,
                    (thread_id,),
                ).fetchone()
        except sqlite3.Error:
            return None

    def _items(self, thread_id: str, turn_id: str) -> list[dict[str, Any]]:
        try:
            with _connect(self.history_path) as db:
                rows = db.execute(
                    """
                    SELECT created_at_ms, item_json FROM thread_items
                    WHERE thread_id = ? AND turn_id = ?
                    ORDER BY rollout_ordinal
                    """,
                    (thread_id, turn_id),
                ).fetchall()
        except sqlite3.Error:
            return []
        items = []
        for row in rows:
            try:
                item = json.loads(row["item_json"])
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict):
                item["_created_at_ms"] = _epoch_ms(row["created_at_ms"])
                items.append(item)
        return items

    def _extras(self, path: str, began: float) -> dict[str, Any]:
        """Plan, usage and still-running calls from the rollout tail."""
        extras: dict[str, Any] = {"plan": [], "plan_at": 0.0, "context": 0, "window": 0, "usage_at": 0.0,
                                  "five_hour": None, "seven_day": None, "running": []}
        if not path:
            return extras
        try:
            file = Path(path)
            size = file.stat().st_size
            with file.open("rb") as handle:
                handle.seek(max(0, size - 8 * 1024 * 1024))
                data = handle.read()
        except OSError:
            return extras
        lines = data.splitlines()
        if data and not data.endswith(b"\n"):
            lines = lines[:-1]
        open_calls: dict[str, tuple[str, dict[str, Any], float]] = {}
        for raw in lines:
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = row.get("type")
            at = _seconds(row.get("timestamp"))
            if kind == "response_item" and payload.get("type") == "function_call":
                call_id = str(payload.get("call_id") or payload.get("id") or "")
                name = str(payload.get("name") or "")
                arguments: dict[str, Any] = {}
                try:
                    parsed = json.loads(payload.get("arguments") or "{}")
                    if isinstance(parsed, dict):
                        arguments = parsed
                except ValueError:
                    pass
                if call_id:
                    open_calls[call_id] = (name, arguments, at)
                if name == "update_plan" and isinstance(arguments.get("plan"), list):
                    plan = []
                    for step in arguments["plan"]:
                        if not isinstance(step, dict):
                            continue
                        title = str(step.get("step") or step.get("title") or "").strip()
                        if title:
                            plan.append({"t": title, "s": str(step.get("status") or "pending")})
                    if plan:
                        extras["plan"], extras["plan_at"] = plan, at
            elif kind == "response_item" and payload.get("type") == "function_call_output":
                open_calls.pop(str(payload.get("call_id") or ""), None)
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                last = info.get("last_token_usage") or {}
                context = int(last.get("input_tokens") or 0)
                window = int(info.get("model_context_window") or 0)
                if context:
                    extras["context"] = context
                if window:
                    extras["window"] = window
                extras["usage_at"] = at
                limits = payload.get("rate_limits") or {}
                for key, target in (("primary", "five_hour"), ("secondary", "seven_day")):
                    window_data = limits.get(key)
                    if isinstance(window_data, dict) and window_data.get("used_percent") is not None:
                        extras[target] = round(float(window_data["used_percent"]))
        extras["running"] = [
            Call(id=call_id, name=name, label=_call_label(name, arguments), at=at)
            for call_id, (name, arguments, at) in open_calls.items()
            if at >= began - 1 and name not in ("update_plan",)
        ]
        return extras

    def read_turn(self, thread: ThreadInfo) -> TurnState:
        turn = self._latest_turn(thread.id)
        state = TurnState(
            thread_id=thread.id,
            model=thread.model,
            effort=thread.effort,
            status=thread.status,
            began=float(turn["started_at"] or 0) if turn else 0.0,
            last=float((turn["completed_at"] or turn["started_at"]) or 0) if turn else 0.0,
        )
        if turn is None:
            return state
        items = self._items(thread.id, str(turn["turn_id"]))
        text_steps: list[str] = []
        calls: list[Call] = []
        prompt_parts: list[str] = []
        last_text = ""
        for item in items:
            kind = str(item.get("type") or "")
            at = _epoch_ms(item.get("_created_at_ms")) / 1000 or state.began
            if kind == "userMessage":
                text = _text_of(item.get("content")).strip()
                if text:
                    prompt_parts.append(text)
                    state.steps.append({"k": "you", "t": text})
            elif kind == "agentMessage":
                text = str(item.get("text") or "").strip()
                if text:
                    if state.steps and state.steps[-1].get("k") == "text" and state.steps[-1].get("t") == text:
                        continue
                    state.steps.append({"k": "text", "t": text})
                    text_steps.append(text)
                    last_text = text
            elif kind == "commandExecution":
                name = "exec_command"
                label = _command_label(item.get("command"))
                call = Call(str(item.get("id") or ""), name, label, at)
                calls.append(call)
                state.tools += 1
                if state.steps and state.steps[-1].get("k") == "tools":
                    state.steps[-1]["n"] = int(state.steps[-1].get("n") or 0) + 1
                    state.steps[-1]["e"] = at
                else:
                    state.steps.append({"k": "tools", "n": 1, "s": at, "e": at})
            elif kind == "mcpToolCall":
                server = str(item.get("server") or "")
                tool = str(item.get("tool") or "")
                label = f"{server} · {tool}" if server else tool
                calls.append(Call(str(item.get("id") or ""), "mcp", label, at))
                state.tools += 1
                if state.steps and state.steps[-1].get("k") == "tools":
                    state.steps[-1]["n"] = int(state.steps[-1].get("n") or 0) + 1
                    state.steps[-1]["e"] = at
                else:
                    state.steps.append({"k": "tools", "n": 1, "s": at, "e": at})
            elif kind == "webSearch":
                calls.append(Call(str(item.get("id") or ""), "web_search", "搜索网页", at))
                state.tools += 1
                if state.steps and state.steps[-1].get("k") == "tools":
                    state.steps[-1]["n"] = int(state.steps[-1].get("n") or 0) + 1
                    state.steps[-1]["e"] = at
                else:
                    state.steps.append({"k": "tools", "n": 1, "s": at, "e": at})
            elif kind == "fileChange":
                changes = item.get("changes") if isinstance(item.get("changes"), list) else []
                state.files += len(changes)
                for change in changes:
                    if not isinstance(change, dict):
                        continue
                    added, removed = _diff_stats(str(change.get("diff") or ""))
                    state.added += added
                    state.removed += removed
                state.steps.append({"k": "file", "n": len(changes), "s": at, "e": at})
            elif kind == "subAgentActivity":
                state.steps.append({"k": "agent", "t": str(item.get("kind") or "interacted"), "s": at})
            if at:
                state.last = max(state.last, at)
        state.prompt = "\n".join(prompt_parts)[-1200:]
        final = next(
            (str(item.get("text") or "") for item in reversed(items)
             if item.get("type") == "agentMessage" and item.get("phase") != "commentary"),
            "",
        )
        state.text = final or last_text
        state.narration = "\n\n".join(text_steps)
        extras = self._extras(thread.rollout_path, state.began)
        if extras["plan_at"] >= state.began - 1:
            state.plan = extras["plan"]
            state.plan_at = extras["plan_at"]
        if extras["usage_at"] >= state.began - 1:
            state.context = extras["context"]
            state.window = extras["window"]
            state.five_hour = extras["five_hour"]
            state.seven_day = extras["seven_day"]
        state.running = tuple(extras["running"])
        state.current = (
            max(state.running, key=lambda call: call.at, default=None)
            if state.status == "running"
            else None
        )
        if state.current is None and state.status == "running":
            state.current = Call("reason", "reasoning", "正在思考", state.last or state.began)
        if turn["status"] == "inProgress":
            state.status = "running"
        elif turn["status"] == "failed":
            state.status = "failed"
            state.error = "这一轮失败了"
        elif turn["status"] == "interrupted":
            state.status = "interrupted"
        return state


def _call_label(name: str, arguments: dict[str, Any]) -> str:
    if name == "exec_command":
        return _command_label(arguments.get("cmd") or arguments.get("command"))
    if name in ("write_stdin",):
        return "查看命令输出"
    if name == "js":
        return "运行页面脚本"
    if name == "view_image":
        path = str(arguments.get("path") or "")
        return Path(path).name or "查看图片"
    if name == "spawn_agent":
        return str(arguments.get("task_name") or "派出代理")
    if name == "wait_agent":
        return "等待代理"
    if name == "send_message":
        return "联系代理"
    return _short_name(name)
