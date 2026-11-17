from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from glasscodex.desktop import DesktopReader


def _state(path: Path, rows: list[dict]) -> None:
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, title TEXT, name TEXT, preview TEXT, cwd TEXT,
                model TEXT, reasoning_effort TEXT, updated_at_ms INTEGER,
                rollout_path TEXT, thread_source TEXT, archived INTEGER,
                recency_at_ms INTEGER
            )
            """
        )
        for row in rows:
            db.execute(
                "INSERT INTO threads VALUES (:id,:title,:name,:preview,:cwd,:model,:effort,:at,:rollout,:source,0,:at)",
                row,
            )


def _history(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE thread_turns (
                thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER, status TEXT,
                started_at INTEGER, completed_at INTEGER, duration_ms INTEGER
            );
            CREATE TABLE thread_items (
                thread_id TEXT, turn_id TEXT, item_id TEXT, rollout_ordinal INTEGER,
                created_at_ms INTEGER, item_json TEXT
            );
            """
        )


def test_reads_desktop_task_and_live_progress(tmp_path: Path):
    codex = tmp_path / ".codex"
    codex.mkdir()
    now = int(time.time())
    stamp = datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z")
    rollout = codex / "rollout.jsonl"
    plan = {"plan": [
        {"step": "检查仓库", "status": "completed"},
        {"step": "实现连接器", "status": "in_progress"},
        {"step": "运行测试", "status": "pending"},
    ]}
    lines = [
        {"timestamp": stamp, "type": "session_meta", "payload": {"id": "t1"}},
        {"timestamp": stamp, "type": "response_item", "payload": {
            "type": "function_call", "call_id": "plan1", "name": "update_plan",
            "arguments": json.dumps(plan, ensure_ascii=False)}},
        {"timestamp": stamp, "type": "event_msg", "payload": {
            "type": "token_count",
            "info": {"last_token_usage": {"input_tokens": 12000}, "model_context_window": 100000},
            "rate_limits": {"primary": {"used_percent": 23}}}},
        {"timestamp": stamp, "type": "response_item", "payload": {
            "type": "function_call", "call_id": "run1", "name": "exec_command",
            "arguments": json.dumps({"cmd": "pytest -q"})}},
    ]
    rollout.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n")
    _state(codex / "state_5.sqlite", [{
        "id": "t1", "title": "实现连接器", "name": None, "preview": "实现连接器",
        "cwd": "/repo/acme-web", "model": "gpt-5.3-codex", "effort": "high",
        "at": now, "rollout": str(rollout), "source": "user",
    }])
    _history(codex / "thread_history_1.sqlite")
    with sqlite3.connect(codex / "thread_history_1.sqlite") as db:
        db.execute("INSERT INTO thread_turns VALUES ('t1','turn1',0,'inProgress',?,NULL,NULL)", (now - 5,))
        items = [
            ("u", now - 5000, {"type": "userMessage", "content": [{"type": "text", "text": "跑一下测试"}]}),
            ("c", now - 4000, {"type": "commandExecution", "id": "c1", "command": "ls -la", "status": "completed"}),
            ("a", now - 3000, {"type": "agentMessage", "text": "我先检查项目结构。"}),
            ("f", now - 2000, {"type": "fileChange", "changes": [{"path": "/repo/a.py", "diff": "@@ -1 +1,2 @@\n-x\n+y\n+z"}]}),
        ]
        for i, (item_id, at, item) in enumerate(items):
            db.execute(
                "INSERT INTO thread_items VALUES ('t1','turn1',?,?,?,?)",
                (item_id, i, at, json.dumps(item, ensure_ascii=False)),
            )

    reader = DesktopReader(tmp_path)
    threads = reader.list_threads()
    assert len(threads) == 1
    thread = threads[0]
    assert thread.project == "acme-web"
    assert thread.state_label == "运行中"

    turn = reader.read_turn(thread)
    assert turn.prompt == "跑一下测试"
    assert turn.text == "我先检查项目结构。"
    assert turn.tools == 1
    assert turn.files == 1 and turn.added == 2 and turn.removed == 1
    assert turn.plan[1]["s"] == "in_progress"
    assert turn.context == 12000 and turn.window == 100000
    assert turn.five_hour == 23
    assert turn.current is not None
    assert turn.current.label == "运行测试"
    assert turn.status == "running"


def test_missing_databases_do_not_crash(tmp_path: Path):
    reader = DesktopReader(tmp_path)
    assert not reader.available()
    assert reader.list_threads() == []
