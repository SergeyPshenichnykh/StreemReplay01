#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.responses import StreamingResponse
import uvicorn

_ROOT = Path(__file__).resolve().parents[1]
UI_FILE = _ROOT / "ui" / "dev_ui.html"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dev GUI for betfair_bot (streams JSON frames into browser).")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8008)
    p.add_argument("--python", default=sys.executable)
    p.add_argument(
        "--replay-cmd",
        nargs=argparse.REMAINDER,
        help="Command args after '--' to run replay script. If omitted, uses a safe default.",
    )
    return p.parse_args()


async def stream_replay_sse(request: Request, python: str, replay_args: list[str]):
    proc = await asyncio.create_subprocess_exec(
        python,
        *replay_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None

    async def gen():
        async def pump_stdout():
            while True:
                if await request.is_disconnected():
                    break
                line = await proc.stdout.readline()
                if not line:
                    break
                s = line.decode("utf-8", errors="replace").strip()
                if not s:
                    continue
                if s.startswith("{") and s.endswith("}"):
                    yield f"data: {s}\n\n"

        async def pump_stderr():
            while True:
                if await request.is_disconnected():
                    break
                line = await proc.stderr.readline()
                if not line:
                    break
                s = line.decode("utf-8", errors="replace").rstrip()
                if not s:
                    continue
                evt = json.dumps({"type": "stderr", "line": s})
                yield f"data: {evt}\n\n"

        # Interleave stdout/stderr by polling both streams.
        stdout_iter = pump_stdout()
        stderr_iter = pump_stderr()
        pending = {asyncio.create_task(stdout_iter.__anext__()): "out", asyncio.create_task(stderr_iter.__anext__()): "err"}
        try:
            while pending:
                done, _ = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    label = pending.pop(task)
                    try:
                        chunk = task.result()
                        yield chunk
                        # re-arm the same iterator
                        it = stdout_iter if label == "out" else stderr_iter
                        pending[asyncio.create_task(it.__anext__())] = label
                    except StopAsyncIteration:
                        continue
        finally:
            for task in pending:
                task.cancel()
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    proc.kill()
        rc = await proc.wait()
        yield f"data: {json.dumps({'type':'eof','rc':rc})}\n\n"

    return gen()


def main() -> int:
    args = parse_args()
    if not UI_FILE.exists():
        print(f"UI file not found: {UI_FILE}", file=sys.stderr)
        return 2

    replay_args = args.replay_cmd
    if replay_args and replay_args[0] == "--":
        replay_args = replay_args[1:]
    if not replay_args:
        replay_args = [
            str(_ROOT / "scripts" / "replay_stream_selected_markets_dashboard.py"),
            "--discover-targets",
            "--start-minutes-before",
            "10",
            "--emit-json",
            "--emit-json-mode",
            "totals+cs",
            "--max-frames",
            "0",
        ]
    app = FastAPI()

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse(UI_FILE.read_text(encoding="utf-8"))

    @app.get("/events")
    async def sse_events(request: Request) -> StreamingResponse:
        gen = await stream_replay_sse(request, args.python, replay_args)
        return StreamingResponse(gen, media_type="text/event-stream")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
