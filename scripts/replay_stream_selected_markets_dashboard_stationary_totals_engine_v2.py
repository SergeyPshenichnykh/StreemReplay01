#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import selectors
import signal
import shutil
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _truncate_ansi(s: str, width: int) -> str:
    if width <= 0:
        return ""

    out: list[str] = []
    visible = 0
    i = 0

    while i < len(s) and visible < width:
        if s[i] == "\x1b":
            m = _ANSI_RE.match(s, i)
            if m:
                out.append(m.group(0))
                i = m.end()
                continue

        out.append(s[i])
        visible += 1
        i += 1

    out.append("\033[0m")
    return "".join(out)


def _fit_line(line: str, cols: int) -> str:
    width = max(20, int(cols)) - 1
    plain_len = len(_strip_ansi(line))

    if plain_len > width:
        return _truncate_ansi(line, width)

    return line + (" " * max(0, width - plain_len))


def _safe_write(data: str) -> None:
    fd = sys.stdout.fileno()
    buf = data.encode(errors="replace")
    pos = 0

    while pos < len(buf):
        try:
            written = os.write(fd, buf[pos : pos + 32768])
            if written <= 0:
                time.sleep(0.001)
            else:
                pos += written
        except BlockingIOError:
            time.sleep(0.001)
        except BrokenPipeError:
            return


def _frame_header(frame: list[str]) -> list[str]:
    header: list[str] = []

    for line in frame:
        header.append(line)
        if line.startswith("-" * 20):
            break

    return header


def _find_cs_index(frame: list[str]) -> int:
    for i, line in enumerate(frame):
        if "CORRECT_SCORE" in line:
            return i
    return -1


def _filter_frame(frame: list[str], page: str) -> list[str]:
    page = (page or "totals").lower().strip()

    if page == "all":
        return frame

    cs_idx = _find_cs_index(frame)

    if page == "totals":
        if cs_idx > 0:
            cut = cs_idx
            while cut > 0 and frame[cut - 1].startswith("-" * 20):
                cut -= 1
            return frame[:cut]
        return frame

    if page in {"cs", "correct_score", "correct-score"}:
        header = _frame_header(frame)

        if cs_idx < 0:
            return header + ["", "CORRECT_SCORE block not found in this frame."]

        start = cs_idx
        while start > 0 and frame[start - 1].startswith("-" * 20):
            start -= 1

        return header + frame[start:]

    if page == "orders":
        header = _frame_header(frame)
        order_lines = [
            line
            for line in frame
            if "ENGINE_V2:" in line
            or "ORDER" in line.upper()
            or "ACTIVE" in line.upper()
            or "FILL" in line.upper()
            or "PNL" in line.upper()
            or "LOCKED" in line.upper()
            or "FREE=" in line.upper()
        ]

        return header + ["", "ORDERS / FILLS / PNL"] + order_lines

    return frame


def _inject_status(lines: list[str], page: str, paused: bool) -> list[str]:
    state = "PAUSED" if paused else "RUN"
    status = (
        f"STABLE_PAGE={page:<6}  STATE={state:<6}  "
        "KEYS: 1=totals  2=correct_score  3=orders  a=all  space=pause/resume  q=quit"
    )

    out = list(lines)

    insert_at = 2
    for i, line in enumerate(out):
        if line.startswith("ENGINE_V2:"):
            insert_at = i + 1
            break

    out.insert(insert_at, status)
    out.insert(insert_at + 1, "-" * 110)
    return out


def _paint_frame(frame: list[str], page: str, paused: bool) -> None:
    size = shutil.get_terminal_size(fallback=(240, 70))
    cols = max(20, int(size.columns))
    rows = max(5, int(size.lines) - 1)

    lines = _filter_frame(frame, page)
    lines = _inject_status(lines, page, paused)
    visible = lines[:rows]

    out: list[str] = ["\033[?25l", "\033[H"]

    for idx in range(rows):
        line = visible[idx] if idx < len(visible) else ""
        out.append(f"\033[{idx + 1};1H")
        out.append(_fit_line(line.rstrip("\n"), cols))
        out.append("\033[K")

    out.append(f"\033[{rows};1H")
    _safe_write("".join(out))


def _open_tty_raw() -> tuple[int | None, list[int] | None]:
    try:
        fd = os.open("/dev/tty", os.O_RDONLY | os.O_NONBLOCK)
        old = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        return fd, old
    except Exception:
        return None, None


def _restore_tty(fd: int | None, old: list[int] | None) -> None:
    if fd is None:
        return

    try:
        if old is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        pass

    try:
        os.close(fd)
    except Exception:
        pass


def _send_signal_safe(proc: subprocess.Popen[bytes], sig: int) -> None:
    try:
        if proc.poll() is None:
            proc.send_signal(sig)
    except Exception:
        pass


def _stable_stream(cmd: list[str], page: str) -> int:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    _term_size = shutil.get_terminal_size(fallback=(240, 70))
    env["COLUMNS"] = str(_term_size.columns)
    env["LINES"] = str(_term_size.lines)

    proc = subprocess.Popen(
        [cmd[0], "-u", *cmd[1:]],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=None,
        bufsize=0,
        env=env,
    )

    assert proc.stdout is not None

    tty_fd, tty_old = _open_tty_raw()

    sel = selectors.DefaultSelector()
    sel.register(proc.stdout.fileno(), selectors.EVENT_READ, "stdout")
    if tty_fd is not None:
        sel.register(tty_fd, selectors.EVENT_READ, "tty")

    frame: list[str] = []
    last_frame: list[str] = []
    buf = ""
    current_page = page or "totals"
    paused = False
    running = True

    try:
        _safe_write("\033[?1049h\033[?25l\033[H\033[J")

        while running:
            if proc.poll() is not None:
                break

            events = sel.select(timeout=0.05)

            for key, _mask in events:
                if key.data == "tty":
                    try:
                        raw_key = os.read(int(key.fileobj), 64).decode(errors="ignore")
                    except BlockingIOError:
                        raw_key = ""

                    for ch in raw_key:
                        if ch == "1":
                            current_page = "totals"
                            _paint_frame(last_frame or frame, current_page, paused)
                        elif ch == "2":
                            current_page = "cs"
                            _paint_frame(last_frame or frame, current_page, paused)
                        elif ch == "3":
                            current_page = "orders"
                            _paint_frame(last_frame or frame, current_page, paused)
                        elif ch.lower() == "a":
                            current_page = "all"
                            _paint_frame(last_frame or frame, current_page, paused)
                        elif ch == " ":
                            paused = not paused
                            if paused:
                                _send_signal_safe(proc, signal.SIGSTOP)
                            else:
                                _send_signal_safe(proc, signal.SIGCONT)
                            _paint_frame(last_frame or frame, current_page, paused)
                        elif ch.lower() == "q":
                            running = False
                            _send_signal_safe(proc, signal.SIGINT)
                            break

                elif key.data == "stdout":
                    try:
                        chunk = os.read(proc.stdout.fileno(), 65536)
                    except BlockingIOError:
                        chunk = b""

                    if not chunk:
                        continue

                    buf += chunk.decode(errors="replace")

                    while "\n" in buf:
                        raw_line, buf = buf.split("\n", 1)
                        line = raw_line.rstrip("\r")

                        if line.startswith("BALANCE:") and frame:
                            last_frame = frame
                            if not paused:
                                _paint_frame(last_frame, current_page, paused)
                            frame = [line]
                        else:
                            frame.append(line)

        if frame:
            last_frame = frame
            _paint_frame(last_frame, current_page, paused)

        try:
            return int(proc.wait(timeout=1.0))
        except Exception:
            return 0

    except KeyboardInterrupt:
        _send_signal_safe(proc, signal.SIGINT)
        return 130

    finally:
        try:
            if paused:
                _send_signal_safe(proc, signal.SIGCONT)
            if proc.poll() is None:
                _send_signal_safe(proc, signal.SIGINT)
                proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

        try:
            sel.close()
        except Exception:
            pass

        _restore_tty(tty_fd, tty_old)
        _safe_write("\033[?25h\033[0m\033[?1049l\n")


def _pop_arg(args: list[str], name: str, default: str | None = None) -> tuple[list[str], str | None]:
    out: list[str] = []
    value = default
    i = 0

    while i < len(args):
        a = args[i]

        if a == name:
            if i + 1 < len(args):
                value = args[i + 1]
                i += 2
            else:
                i += 1
            continue

        if a.startswith(name + "="):
            value = a.split("=", 1)[1]
            i += 1
            continue

        out.append(a)
        i += 1

    return out, value


def _remove_flag(args: list[str], flag: str) -> list[str]:
    return [a for a in args if a != flag]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    target = root / "scripts" / "replay_stream_selected_markets_dashboard_engine_v2.py"

    args = sys.argv[1:]

    stable_stream = "--stable-stream" in args
    args = _remove_flag(args, "--stable-stream")

    args, stable_page = _pop_arg(args, "--stable-page", "totals")
    stable_page = stable_page or "totals"

    if "--totals-all" not in args:
        args = ["--totals-all", *args]

    if "--totals-center-threshold" not in args:
        args = ["--totals-center-threshold", "1.30", *args]

    if "--totals-rows" not in args:
        args = ["--totals-rows", "3", *args]

    if stable_stream:
        args = _remove_flag(args, "--smooth-ui")

        if "--no-clear" not in args:
            args = [*args, "--no-clear"]

        return _stable_stream([sys.executable, str(target), *args], page=stable_page)

    if "--smooth-ui" not in args and "--no-clear" not in args:
        args = ["--smooth-ui", *args]

    try:
        return subprocess.call([sys.executable, str(target), *args])
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
