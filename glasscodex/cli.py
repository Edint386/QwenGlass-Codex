"""Command line entry points."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys

import uvicorn

from .bridge import Bridge
from .desktop import DesktopReader
from .ipc import DesktopIPC, DesktopIPCError
from .server import create_app


def _require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        sys.exit(f"缺少环境变量 {name}")
    return value


def _serve(args: argparse.Namespace) -> None:
    devices = frozenset(d.strip() for d in os.environ.get("GLASSCODEX_ALLOWED_DEVICES", "").split(",") if d.strip())
    if not devices:
        logging.warning("没有设置 GLASSCODEX_ALLOWED_DEVICES，任何连到这个服务的眼镜都能操作桌面版 Codex")
    app = create_app(_require("GLASSCODEX_AGENT_TOKEN"), _require("GLASSCODEX_BRIDGE_TOKEN"), devices)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        ws_max_size=2**21,
        log_level="info",
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )


def _bridge(args: argparse.Namespace) -> None:
    if sys.platform == "win32":
        sys.exit("Windows 还不支持：桌面版 IPC 和会话数据库只在 macOS 上验证过")
    url = args.cloud or _require("GLASSCODEX_CLOUD")
    if args.keep_awake:
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    asyncio.run(Bridge(url, _require("GLASSCODEX_BRIDGE_TOKEN")).run())


def _threads(_: argparse.Namespace) -> None:
    reader = DesktopReader()
    if not reader.available():
        sys.exit("找不到 Codex Desktop 的本地数据库")
    for thread in reader.list_threads(50):
        print(f"{thread.state_label:6}  {thread.project:20}  {thread.title}  [{thread.id}]")


def _turn(args: argparse.Namespace) -> None:
    reader = DesktopReader()
    thread = reader.thread(args.thread)
    if thread is None:
        sys.exit("找不到这个桌面任务")
    state = reader.read_turn(thread)
    print(f"{thread.title} · {thread.project} · {state.status}")
    print(f"model={state.model} effort={state.effort} context={state.context}/{state.window}")
    print(f"plan={state.plan}")
    print(f"diff={state.diff_label}")
    if state.current:
        print(f"doing={state.current.label}")
    print(f"prompt={state.prompt[:200]!r}")
    print(f"text={state.text[:1000]}")


def _doctor(_: argparse.Namespace) -> None:
    reader = DesktopReader()
    print(f"Codex Desktop 数据：{'找到' if reader.available() else '缺失'}")
    threads = reader.list_threads(10)
    print(f"最近任务：{len(threads)} 个")
    for thread in threads[:5]:
        print(f"  {thread.state_label:6} {thread.project:18} {thread.title[:40]} [{thread.id}]")
    ipc = DesktopIPC()
    try:
        with ipc:
            owners = 0
            for thread in threads[:5]:
                try:
                    ipc.owner(thread.id)
                    owners += 1
                except DesktopIPCError:
                    pass
            print(f"桌面版 IPC：可用（最近 5 个任务里有 {owners} 个在桌面界面打开）")
    except DesktopIPCError as exc:
        print(f"桌面版 IPC：不可用（{exc}）")


def main() -> None:
    parser = argparse.ArgumentParser(prog="glasscodex")
    commands = parser.add_subparsers(required=True)
    serve = commands.add_parser("serve", help="云端服务")
    serve.add_argument("--host", default=os.environ.get("GLASSCODEX_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("GLASSCODEX_PORT", "8792")))
    serve.set_defaults(run=_serve)
    bridge = commands.add_parser("bridge", help="Mac 上的桥接程序")
    bridge.add_argument("--cloud", help="云端地址，不写就读 GLASSCODEX_CLOUD")
    bridge.add_argument("--keep-awake", action="store_true", help="运行时不让电脑空闲休眠")
    bridge.set_defaults(run=_bridge)
    commands.add_parser("threads", help="列出 Codex Desktop 的最近任务").set_defaults(run=_threads)
    turn = commands.add_parser("turn", help="查看一个桌面任务的当前状态")
    turn.add_argument("thread")
    turn.set_defaults(run=_turn)
    commands.add_parser("doctor", help="检查桌面版数据库和 IPC").set_defaults(run=_doctor)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args.run(args)
