#!/usr/bin/env python3
"""Pretend to be the Qwen platform and glasses page for local tests."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid

import websockets

from glasscodex.panel import (
    FONT,
    LIST_BASE,
    MENU,
    MENU_BASE,
    MENU_FONT,
    MENU_ITEMS,
    MENU_PLAN,
    MENU_QUICK,
    MENU_SWITCH,
    MENU_VIEW,
    PICK,
    PLAN,
    QUICK,
    QUICK_BASE,
    QUICK_REPLIES,
)


class Glasses:
    def __init__(self, ws, session: str):
        self.ws = ws
        self.session = session
        self.data: dict = {}
        self.page_uuid = ""

    async def platform(self, kind: str, payload: dict) -> None:
        await self.ws.send(json.dumps({
            "specVersion": "1.0",
            "sessionId": self.session,
            "type": kind,
            "payload": payload,
        }, ensure_ascii=False))

    def apply(self, message: dict) -> None:
        if message.get("type") != "OUTPUT":
            print("<-", message.get("type"), json.dumps(message.get("payload"), ensure_ascii=False)[:200])
            return
        for action in message["payload"]["actions"]:
            if action["type"] == "SPEAK":
                print("<- 播报", action["payload"]["text"])
                continue
            payload = action["payload"]
            self.page_uuid = payload["pageUuid"]
            data = payload["view"]["props"]["data"]
            self.data.update(data)
            print("<-", payload["display"]["mode"], [key for key in data if key in ("m", "i", "c", "ft", "dr")])

    async def report(self, name: str) -> None:
        data = self.data
        params = {
            "slot": data.get("i", 0),
            "c": data.get("c", 0),
            "dv": data.get("dv", 0),
            "m": data.get("m", 0),
            "k": data.get("k", 0),
            "s": data.get("s", 0),
            "l": data.get("lv", 0),
            "f": data.get("fv", data.get("fz", 18)),
        }
        await self.platform("UI_ACTION", {
            "pageUuid": self.page_uuid,
            "action": {"name": name, "params": params},
        })

    async def run(self, command: str, arg: str) -> None:
        data = self.data
        if command == "wait":
            await asyncio.sleep(float(arg))
        elif command == "say":
            await self.platform("UTTERANCE", {"text": arg})
        elif command in ("send", "drop"):
            data["m"] = 0
            await self.report(command)
        elif command == "menu":
            data["c"], data["m"] = MENU_BASE, MENU
            await self.report("menu")
        elif command == "tap":
            await self.tap()
        elif command == "list":
            await self.run("menu", "")
            data["c"] = MENU_SWITCH
            await self.tap()
        elif command == "quick":
            await self.run("menu", "")
            data["c"] = MENU_QUICK
            await self.tap()
            data["c"] = QUICK_BASE + 1 + int(arg)
            await self.tap()
        elif command == "plan":
            await self.run("menu", "")
            data["c"] = MENU_PLAN
            await self.tap()
        elif command == "pick":
            if data.get("m") != PICK:
                raise SystemExit("先 list 打开任务列表")
            data["c"] = LIST_BASE + 1 + int(arg)
            await self.tap()
        elif command in ("next", "prev") and data.get("m") == FONT:
            data["fv"] = min(max(data.get("fv", 18) + (2 if command == "next" else -2), 16), 24)
        elif command in ("next", "prev") and data.get("m") == LIVE_MODE:
            await self.report("newer" if command == "next" else "older")
        elif command in ("next", "prev") and data.get("m", 0) > 3:
            table = data.get("ln" if command == "next" else "lp", [])
            data["c"] = table[data["c"]]["n"]
        elif command in ("next", "prev"):
            table = data.get("nx" if command == "next" else "pv", [])
            guard = data.get("hi" if command == "next" else "lo", 0)
            if data.get("i") != guard:
                data["i"] = table[data["i"]]["n"]
            await self.report("turn")
        elif command == "show":
            self.show()
        elif command == "exit":
            await self.report("exit")
        else:
            raise SystemExit(f"不认识的命令 {command}")

    async def tap(self) -> None:
        data = self.data
        item = data["k"] = data["c"]
        data["s"], data["fv"] = data["m"], data.get("fz", 18)
        if item == MENU_FONT:
            data["m"] = FONT
        elif item == MENU_PLAN:
            data["m"] = PLAN
        elif item == MENU_QUICK:
            data["c"], data["m"] = QUICK_BASE, QUICK
        elif item == MENU_VIEW:
            data["m"], data["rp"] = data["mv"]["m"], data["mv"]["p"]
        elif item == MENU_SWITCH:
            data["c"], data["m"] = LIST_BASE, PICK
        elif item == QUICK_BASE:
            data["c"], data["m"] = MENU_QUICK, MENU
        elif item == LIST_BASE:
            data["c"], data["m"] = MENU_SWITCH, MENU
        else:
            data["m"] = 0
        await self.report("pick")

    def show(self) -> None:
        data = self.data
        page = data.get("P", [{"t": "", "s": 1}])[data.get("i", 0)]
        print(f"┌ {data.get('pr')} | {data.get('h')} | {data.get('st')} | {data.get('ft')}")
        if data.get("m") == PLAN:
            for row in data.get("PL", []):
                print(f"│ {row.get('s')} {row.get('t')}")
            print(f"└ {data.get('dfx')}")
        elif data.get("m") == 2:
            print(f"│ {data.get('dt')}\n│ {data.get('dr')}\n│ {data.get('df')}  滑动取消")
        elif data.get("m") == 3:
            for row in data.get("R", []):
                if row.get("r"):
                    print(f"│ {row['r']}  {row.get('t', '')}")
            print(f"└ CTX {data.get('m1')} PLAN {data.get('m2')} 5H {data.get('m3')}")
        elif data.get("m") in (PICK, MENU, QUICK):
            base, rows = {
                PICK: (LIST_BASE, ["返回", *[
                    f"{row['a']}  {row['e']}" for row in data.get("L", []) if row.get("a")
                ]]),
                QUICK: (QUICK_BASE, ["返回", *QUICK_REPLIES]),
                MENU: (MENU_BASE, [
                    data["mv"]["t"] if index == MENU_VIEW - MENU_BASE else item
                    for index, item in enumerate(MENU_ITEMS)
                ]),
            }[data["m"]]
            print("\n".join(f"│ {'>' if base + i == data.get('c', 0) else ' '} {row}" for i, row in enumerate(rows)))
        else:
            print("\n".join(f"│ {line}" for line in page.get("t", "").split("\n")))


LIVE_MODE = 3


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8792/agent")
    parser.add_argument("--token", default=os.environ.get("GLASSCODEX_AGENT_TOKEN", ""))
    parser.add_argument("--script", default="wait 2; show")
    args = parser.parse_args()
    session = f"fake_{uuid.uuid4().hex[:8]}"
    headers = {"Authorization": f"Bearer {args.token}", "X-Request-Id": uuid.uuid4().hex}
    async with websockets.connect(args.url, additional_headers=headers, proxy=None) as ws:
        glasses = Glasses(ws, session)
        await glasses.platform("SESSION_INIT", {
            "connectionEpoch": 1,
            "pages": [{"templateId": "100002", "description": "codex-panel"}],
            "tools": [],
            "identity": {"deviceOpenId": "dev_fake"},
            "entryContext": {"source": "VOICE_SKILL", "rawUtterance": "打开 Codex"},
        })

        async def listen() -> None:
            async for raw in ws:
                glasses.apply(json.loads(raw))

        listener = asyncio.create_task(listen())
        for step in filter(None, (part.strip() for part in args.script.split(";"))):
            command, _, arg = step.partition(" ")
            await glasses.run(command, arg.strip())
        listener.cancel()


if __name__ == "__main__":
    asyncio.run(main())
