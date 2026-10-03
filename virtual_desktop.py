#!/usr/bin/env python3
"""
Linux Headless Virtual Desktop (Computer Use Helper)
为 AI Agent 打造的无感后台 X11 虚拟屏幕控制套件。
支持 1080P 60FPS 串流、X11 GUI 自动化与按显示器隔离的生命周期管理。
依赖: Xvfb, xdotool, scrot, openbox, ffmpeg (可选用于串流)。
"""

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_DISPLAY = ":99"
DEFAULT_RES = "1920x1080x24"
STATE_ROOT = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "linux-headless-computer-use"
_DISPLAY_RE = re.compile(r"^:(\d+)$")
_BROWSER_NAMES = {
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
}
_SHELL_BOUNDARIES = ("&&", "||", ";;", ";", "|", "&", "(", ")", "{", "}", "\n")


def _normalize_display(display):
    if not isinstance(display, str) or not _DISPLAY_RE.fullmatch(display):
        raise ValueError(f"Invalid X11 display {display!r}; expected :<number>")
    return display


def _display_number(display):
    return _DISPLAY_RE.fullmatch(_normalize_display(display)).group(1)


def _state_path(display):
    return STATE_ROOT / f"display-{_display_number(display)}.json"


def _read_state_unlocked(handle):
    handle.seek(0)
    try:
        value = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return []
    return value if isinstance(value, list) else []


def _update_state(display, transform):
    """Atomically update the per-display ownership registry."""
    path = _state_path(display)
    STATE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        records = _read_state_unlocked(handle)
        records = transform(records)
        handle.seek(0)
        handle.truncate()
        json.dump(records, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    if not records:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return records


def _proc_snapshot(pid):
    """Return a small identity snapshot, or None if the process is gone."""
    try:
        raw_cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        cmdline = [part.decode(errors="replace") for part in raw_cmdline.split(b"\0") if part]
        env = {}
        for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
            if b"=" in item:
                key, value = item.split(b"=", 1)
                env[key.decode(errors="replace")] = value.decode(errors="replace")
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        rest = stat_text.rsplit(")", 1)[1].split()
        start_ticks = rest[19]  # /proc stat field 22, after pid/comm/state.
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = None
        return {
            "pid": int(pid),
            "uid": os.stat(f"/proc/{pid}").st_uid,
            "start_ticks": start_ticks,
            "cmdline": cmdline,
            "env": env,
            "cwd": cwd,
        }
    except (FileNotFoundError, PermissionError, OSError, ValueError, IndexError):
        return None


def _record_process(display, role, pid, port=None):
    snapshot = _proc_snapshot(pid)
    if snapshot is None:
        return
    record = {
        "display": display,
        "role": role,
        "pid": int(pid),
        "uid": snapshot["uid"],
        "start_ticks": snapshot["start_ticks"],
        "cmdline": snapshot["cmdline"],
    }
    if port is not None:
        record["port"] = int(port)

    def add(records):
        records = [item for item in records if not (
            item.get("pid") == record["pid"] and item.get("start_ticks") == record["start_ticks"]
        )]
        records.append(record)
        return records

    _update_state(display, add)


def _unrecord_process(display, pid, start_ticks=None):
    def remove(records):
        return [item for item in records if not (
            item.get("pid") == int(pid)
            and (start_ticks is None or item.get("start_ticks") == start_ticks)
        )]

    _update_state(display, remove)


def _owned_records(display):
    display = _normalize_display(display)
    path = _state_path(display)
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as handle:
            records = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    result = []
    stale = []
    for record in records if isinstance(records, list) else []:
        if record.get("display") != display:
            continue
        snapshot = _proc_snapshot(record.get("pid"))
        if snapshot is None or snapshot["uid"] != os.getuid() or snapshot["start_ticks"] != record.get("start_ticks"):
            stale.append(record)
            continue
        result.append(record)
    if stale:
        stale_keys = {(item.get("pid"), item.get("start_ticks")) for item in stale}
        _update_state(display, lambda items: [
            item for item in items
            if (item.get("pid"), item.get("start_ticks")) not in stale_keys
        ])
    return result


def _wait_dead(pid, start_ticks, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = _proc_snapshot(pid)
        if snapshot is None or snapshot["start_ticks"] != start_ticks:
            return True
        time.sleep(0.05)
    return False


def _terminate_record(display, record, timeout=2.0):
    pid = int(record.get("pid", -1))
    start_ticks = record.get("start_ticks")
    snapshot = _proc_snapshot(pid)
    if snapshot is None or snapshot["start_ticks"] != start_ticks:
        _unrecord_process(display, pid, start_ticks)
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _unrecord_process(display, pid, start_ticks)
        return True
    if not _wait_dead(pid, start_ticks, timeout):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _wait_dead(pid, start_ticks, 1.0)
    _unrecord_process(display, pid, start_ticks)
    return _proc_snapshot(pid) is None


def _children_of(pid):
    children = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        snapshot = _proc_snapshot(entry.name)
        if snapshot is None:
            continue
        try:
            stat = Path(f"/proc/{entry.name}/stat").read_text(encoding="utf-8")
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if ppid == int(pid):
            children.append(snapshot)
    return children


def _iter_processes():
    """Yield process snapshots from /proc without invoking global kill tools."""
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.name.isdigit():
            continue
        snapshot = _proc_snapshot(entry.name)
        if snapshot is not None:
            yield snapshot


def _canonical_display(display):
    """Return a display key suitable for exact process ownership checks."""
    return _normalize_display(display)


def _display_matches(left, right):
    try:
        return _canonical_display(left) == _canonical_display(right)
    except ValueError:
        return False


def _process_argv(process):
    return process.get("cmdline") or process.get("argv") or []


def _process_env(process):
    return process.get("env") or process.get("environ") or {}


def _find_x11_wm_pid(display):
    """Resolve the WM PID advertised by the target X server, if available."""
    try:
        env = get_env(display)
        root = subprocess.run(
            ["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        root_stdout = root.stdout if isinstance(root.stdout, str) else ""
        match = re.search(r"window id # (0x[0-9a-fA-F]+)", root_stdout)
        if root.returncode != 0 or not match:
            return None
        wm = subprocess.run(
            ["xprop", "-id", match.group(1), "_NET_WM_PID"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        wm_stdout = wm.stdout if isinstance(wm.stdout, str) else ""
        pid_match = re.search(r"_NET_WM_PID[^=]*=\s*(\d+)", wm_stdout)
        return int(pid_match.group(1)) if wm.returncode == 0 and pid_match else None
    except (FileNotFoundError, OSError, TypeError, ValueError, subprocess.SubprocessError):
        return None


def _resolved_process_argument(argument, process):
    if os.path.isabs(argument):
        return os.path.realpath(argument)
    cwd = process.get("cwd")
    if cwd:
        return os.path.realpath(os.path.join(cwd, argument))
    return None


def _is_our_script_process(process):
    expected = os.path.realpath(__file__)
    for argument in _process_argv(process)[1:]:
        if os.path.basename(argument) != os.path.basename(__file__):
            continue
        resolved = _resolved_process_argument(argument, process)
        if resolved == expected:
            return True
    return False


def _live_process_details(process):
    """Parse a live daemon command, including its default display and port."""
    argv = _process_argv(process)
    if not argv or not _is_our_script_process(process):
        return None
    try:
        action_index = next(index for index, value in enumerate(argv) if value == "live")
    except StopIteration:
        return None

    display = DEFAULT_DISPLAY
    port = 9999
    positional_port = None
    index = 1
    while index < len(argv):
        value = argv[index]
        if value == "--display" and index + 1 < len(argv):
            display = argv[index + 1]
            index += 2
            continue
        if value.startswith("--display="):
            display = value.split("=", 1)[1]
            index += 1
            continue
        if value == "--port" and index + 1 < len(argv):
            try:
                port = int(argv[index + 1])
            except ValueError:
                return None
            index += 2
            continue
        if value.startswith("--port="):
            try:
                port = int(value.split("=", 1)[1])
            except ValueError:
                return None
            index += 1
            continue
        if index > action_index and not value.startswith("-") and positional_port is None:
            try:
                positional_port = int(value)
            except ValueError:
                return None
        index += 1
    if positional_port is not None and port == 9999:
        port = positional_port
    try:
        display = _normalize_display(display)
        _validate_port(port)
    except (ValueError, UnboundLocalError):
        return None
    return {"display": display, "port": port}


def _process_role_for_display(process, display, wm_pid=None):
    """Return a role only when argv/env prove ownership of *display*."""
    argv = _process_argv(process)
    if not argv:
        return None
    executable = os.path.basename(argv[0])
    if executable == "Xvfb" and any(_display_matches(value, display) for value in argv[1:]):
        return "xvfb"
    if executable == "openbox":
        env_display = _process_env(process).get("DISPLAY")
        option_display = None
        for index, value in enumerate(argv):
            if value == "--display" and index + 1 < len(argv):
                option_display = argv[index + 1]
            elif value.startswith("--display="):
                option_display = value.split("=", 1)[1]
        if process.get("pid") == wm_pid or _display_matches(env_display, display) or _display_matches(option_display, display):
            return "openbox"
        return None
    if executable in {"ffmpeg", "ffmpeg_g"} and "x11grab" in argv:
        for index, value in enumerate(argv):
            if value == "-i" and index + 1 < len(argv) and _display_matches(argv[index + 1], display):
                return "ffmpeg"
        return None
    details = _live_process_details(process)
    if details and _display_matches(details["display"], display):
        # Parsing the port is intentional: a malformed or non-live command is
        # not considered owned merely because its argv contains this filename.
        return "live"
    return None


def _path_is_held(path):
    """Check /proc/*/fd before unlinking a lock or X11 socket."""
    wanted = os.path.realpath(os.fspath(path))
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return True
    for entry in entries:
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.path.realpath(os.readlink(fd))
            except OSError:
                continue
            if target == wanted:
                return True
    return False


def get_env(display=DEFAULT_DISPLAY):
    display = _normalize_display(display)
    env = os.environ.copy()
    env["DISPLAY"] = display
    env.pop("WAYLAND_DISPLAY", None)
    env["XDG_SESSION_TYPE"] = "x11"
    env["GDK_BACKEND"] = "x11"
    env["QT_QPA_PLATFORM"] = "xcb"
    # Kept for clients that understand the environment hint; launch() also
    # injects the real Chromium command-line switch below.
    env["CHROME_OZONE_PLATFORM"] = "x11"
    env["OZONE_PLATFORM"] = "x11"
    return env


def is_display_active(display=DEFAULT_DISPLAY):
    try:
        display = _normalize_display(display)
        res = subprocess.run(
            ["xdpyinfo", "-display", display],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return res.returncode == 0
    except (Exception, ValueError):
        return False


def is_port_open(port):
    try:
        port = _validate_port(port)
    except ValueError:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect(("127.0.0.1", port))
        return True
    except (OSError, ValueError):
        return False
    finally:
        sock.close()


def is_window_manager_active(display=DEFAULT_DISPLAY):
    """Check the WM advertised by this X server, not a global process list."""
    try:
        env = get_env(display)
        result = subprocess.run(
            ["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"],
            env=env,
            capture_output=True,
            text=True,
            timeout=2,
        )
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        match = re.search(r"window id # (0x[0-9a-fA-F]+)", stdout)
        if result.returncode == 0 and match:
            wm = subprocess.run(
                ["xprop", "-id", match.group(1)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            return wm.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError, OSError, TypeError, ValueError):
        pass
    try:
        fallback = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--name", "^Openbox$"],
            env=get_env(display),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        if fallback.returncode == 0:
            return True
    except (FileNotFoundError, subprocess.SubprocessError, OSError, TypeError, ValueError):
        pass
    for process in _iter_processes():
        if os.path.basename(_process_argv(process)[0] if _process_argv(process) else "") != "openbox":
            continue
        if _display_matches(_process_env(process).get("DISPLAY"), display):
            return True
    return False


def is_window_manager_running(display=DEFAULT_DISPLAY):
    """Compatibility seam for checking the WM on exactly one display."""
    return is_window_manager_active(display)


def _start_window_manager(display):
    try:
        proc = subprocess.Popen(
            ["openbox"],
            env=get_env(display),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Openbox is required to provide a usable virtual desktop") from exc
    for _ in range(30):
        time.sleep(0.1)
        if is_window_manager_running(display):
            _record_process(display, "openbox", proc.pid)
            return proc.pid
    try:
        proc.terminate()
    except ProcessLookupError:
        pass
    raise RuntimeError(f"Window manager did not become ready on {display}")


def ensure_running(display=DEFAULT_DISPLAY, resolution=DEFAULT_RES):
    """Ensure Xvfb and a WM are ready; recover a display whose WM died."""
    display = _normalize_display(display)
    if is_display_active(display):
        if not is_window_manager_running(display):
            _start_window_manager(display)
        return True

    print(f"[*] Starting Xvfb on {display} ({resolution})...")
    xvfb_cmd = [
        "Xvfb", display,
        "-screen", "0", resolution,
        "-nolisten", "tcp", "-ac", "+extension", "GLX", "+render", "-noreset",
    ]
    xvfb = subprocess.Popen(
        xvfb_cmd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        time.sleep(0.1)
        if is_display_active(display):
            _record_process(display, "xvfb", xvfb.pid)
            break
    else:
        try:
            xvfb.terminate()
        except ProcessLookupError:
            pass
        raise RuntimeError(f"Failed to start Xvfb on {display}")

    _start_window_manager(display)
    print(f"[+] Display {display} is ready!")
    return True


def get_display_size(display=DEFAULT_DISPLAY):
    """Read the target X11 screen dimensions from xdpyinfo."""
    display = _normalize_display(display)
    try:
        result = subprocess.run(
            ["xdpyinfo", "-display", display],
            env=get_env(display),
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, OSError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise RuntimeError(f"Unable to determine screen dimensions for {display}: {detail}") from exc
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    match = re.search(r"dimensions:\s+(\d+)x(\d+) pixels", stdout)
    if not match:
        raise RuntimeError(f"Unable to determine screen dimensions for {display}")
    return int(match.group(1)), int(match.group(2))


def _screen_size(display):
    """Backward-compatible private alias for the display-size helper."""
    return get_display_size(display)


class XdotoolError(RuntimeError):
    """Raised when xdotool rejects an otherwise well-formed input request."""



def _validate_coordinate(value, name, limit):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < 0 or value >= limit:
        raise ValueError(f"{name}={value} is outside [0, {limit})")


def _validate_coordinate_type(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")


def _run_xdotool(command, display):
    try:
        result = subprocess.run(
            command,
            env=get_env(display),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise XdotoolError("xdotool is not installed") from exc
    stderr = (getattr(result, "stderr", None) or "").strip()
    if result.returncode != 0 or stderr:
        detail = stderr or f"xdotool exited with status {result.returncode}"
        raise XdotoolError(f"{' '.join(command)} failed: {detail}")
    return result


def _validate_port(port):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"port must be an integer in [1, 65535], got {port!r}")
    return port


def stop_display(display=DEFAULT_DISPLAY):
    """Stop only processes recorded as owned by *display*.

    The registry is authoritative: matching a display in an arbitrary
    process's argv or environment is not proof that this helper owns it.
    Unknown Xvfb/Openbox/FFmpeg processes are left running rather than
    risking another user's desktop session.
    """
    display = _normalize_display(display)
    candidates = {}

    def add_candidate(process, role):
        pid = int(process.get("pid", -1))
        uid = process.get("uid")
        if pid <= 0 or pid == os.getpid() or (uid is not None and uid != os.getuid()):
            return
        key = (pid, process.get("start_ticks"))
        candidates[key] = {
            "pid": pid,
            "start_ticks": process.get("start_ticks"),
            "role": role,
        }

    owned = _owned_records(display)
    for record in owned:
        if record.get("pid") != os.getpid():
            add_candidate(record, record.get("role", "unknown"))

    # A stream encoder is a child of an owned live daemon. Capture it even if
    # the request thread raced the state-file write; never scan unrelated
    # x11grab processes by display alone.
    for record in owned:
        if record.get("role") != "live":
            continue
        for child in _children_of(record["pid"]):
            argv = child.get("cmdline", [])
            if not argv or os.path.basename(argv[0]) not in {"ffmpeg", "ffmpeg_g"} or "x11grab" not in argv:
                continue
            try:
                input_display = argv[argv.index("-i") + 1]
            except (ValueError, IndexError):
                continue
            if _display_matches(input_display, display):
                add_candidate(child, "ffmpeg")

    order = {"ffmpeg": 0, "live": 1, "openbox": 2, "xvfb": 3, "unknown": 4}
    all_stopped = True
    for record in sorted(candidates.values(), key=lambda item: order.get(item["role"], 4)):
        try:
            all_stopped = _terminate_record(display, record) and all_stopped
        except (OSError, ValueError) as exc:
            print(f"[!] Could not terminate PID {record['pid']}: {exc}")
            all_stopped = False

    if is_display_active(display) or not all_stopped:
        print(f"[!] Display {display} is still active; lock/socket retained.")
        return False

    display_num = _display_number(display)
    cleanup_complete = True
    for path in (Path(f"/tmp/.X{display_num}-lock"), Path(f"/tmp/.X11-unix/X{display_num}")):
        try:
            if not (path.exists() or path.is_symlink()):
                continue
            if _path_is_held(path):
                print(f"[!] {path} is still held; retained.")
                cleanup_complete = False
                continue
            path.unlink()
        except OSError as exc:
            print(f"[!] Could not remove {path}: {exc}")
            cleanup_complete = False
    try:
        _state_path(display).unlink()
    except FileNotFoundError:
        pass
    print(f"[*] Cleanly stopped owned services for display {display}.")
    return cleanup_complete


def screenshot(output_path="/tmp/screen.png", display=DEFAULT_DISPLAY):
    ensure_running(display)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    subprocess.run(["scrot", "-z", output_path], env=get_env(display), check=True)
    print(f"[+] Screenshot saved to: {output_path}")
    return output_path


def click(x, y, display=DEFAULT_DISPLAY, button=1):
    _validate_coordinate_type(x, "x")
    _validate_coordinate_type(y, "y")
    if isinstance(button, bool) or not isinstance(button, int) or not 1 <= button <= 9:
        raise ValueError("button must be an integer in [1, 9]")
    ensure_running(display)
    width, height = get_display_size(display)
    _validate_coordinate(x, "x", width)
    _validate_coordinate(y, "y", height)
    cmd = ["xdotool", "mousemove", str(x), str(y), "click", str(button)]
    _run_xdotool(cmd, display)
    print(f"[+] Clicked ({x}, {y}) [button {button}] on {display}")


def mouse_move(x, y, display=DEFAULT_DISPLAY):
    _validate_coordinate_type(x, "x")
    _validate_coordinate_type(y, "y")
    ensure_running(display)
    width, height = get_display_size(display)
    _validate_coordinate(x, "x", width)
    _validate_coordinate(y, "y", height)
    _run_xdotool(["xdotool", "mousemove", str(x), str(y)], display)


def type_text(text, display=DEFAULT_DISPLAY, delay_ms=12):
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if isinstance(delay_ms, bool) or not isinstance(delay_ms, int) or delay_ms < 0:
        raise ValueError("delay_ms must be a non-negative integer")
    if not text:
        return
    ensure_running(display)
    _run_xdotool(["xdotool", "type", "--delay", str(delay_ms), text], display)
    print(f"[+] Typed: {text[:20]}... on {display}")


def press_key(key_name, display=DEFAULT_DISPLAY):
    if not isinstance(key_name, str) or not key_name.strip() or "\x00" in key_name:
        raise ValueError("key must be a non-empty string without NUL bytes")
    ensure_running(display)
    _run_xdotool(["xdotool", "key", key_name], display)
    print(f"[+] Pressed key: {key_name} on {display}")


def _shell_tokens(command):
    """Tokenize enough shell syntax to edit command words without re-quoting."""
    tokens = []
    index = 0
    length = len(command)
    boundaries = sorted(_SHELL_BOUNDARIES, key=len, reverse=True)
    while index < length:
        if command[index] == "\n":
            tokens.append({"kind": "boundary", "raw": "\n", "start": index, "end": index + 1})
            index += 1
            continue
        if command[index].isspace():
            index += 1
            continue
        boundary = next((item for item in boundaries if command.startswith(item, index)), None)
        if boundary is not None:
            tokens.append({"kind": "boundary", "raw": boundary, "start": index, "end": index + len(boundary)})
            index += len(boundary)
            continue

        start = index
        quote = None
        while index < length:
            char = command[index]
            if quote == "'":
                index += 1
                if char == "'":
                    quote = None
                continue
            if quote == '\"':
                if char == "\\" and index + 1 < length:
                    index += 2
                    continue
                index += 1
                if char == '\"':
                    quote = None
                continue
            if char in "'\"":
                quote = char
                index += 1
                continue
            if char == "\\" and index + 1 < length:
                index += 2
                continue
            if char.isspace() or any(command.startswith(item, index) for item in boundaries):
                break
            index += 1
        tokens.append({"kind": "word", "raw": command[start:index], "start": start, "end": index})
    return tokens


def _shell_word_value(raw):
    try:
        values = shlex.split(raw, posix=True)
    except ValueError:
        return raw
    return values[0] if len(values) == 1 else raw


def _is_shell_assignment(value):
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", value))


def _browser_command_index(segment):
    index = 0
    while index < len(segment):
        value = _shell_word_value(segment[index]["raw"])
        if _is_shell_assignment(value):
            index += 1
            continue
        if value in {"exec", "command", "nohup"}:
            index += 1
            continue
        if value == "env":
            index += 1
            while index < len(segment):
                option = _shell_word_value(segment[index]["raw"])
                if _is_shell_assignment(option):
                    index += 1
                    continue
                if option == "--":
                    index += 1
                    break
                if option in {"-u", "--unset"} and index + 1 < len(segment):
                    index += 2
                    continue
                if option.startswith("-"):
                    index += 1
                    continue
                break
            continue
        break
    if index >= len(segment):
        return None
    executable = os.path.basename(_shell_word_value(segment[index]["raw"]))
    return index if executable in _BROWSER_NAMES else None


def _inject_ozone_platform(command_str):
    """Force X11 Ozone for every supported browser command in a shell string."""
    tokens = _shell_tokens(command_str)
    edits = []
    segment = []
    segments = []
    for token in tokens:
        if token["kind"] == "boundary":
            if segment:
                segments.append(segment)
                segment = []
        else:
            segment.append(token)
    if segment:
        segments.append(segment)

    for words in segments:
        browser_index = _browser_command_index(words)
        if browser_index is None:
            continue
        found_ozone = False
        index = browser_index + 1
        while index < len(words):
            value = _shell_word_value(words[index]["raw"])
            if value.startswith("--ozone-platform=") or value == "--ozone-platform":
                end = words[index]["end"]
                if value == "--ozone-platform" and index + 1 < len(words):
                    end = words[index + 1]["end"]
                    index += 1
                replacement = "--ozone-platform=x11" if not found_ozone else ""
                edits.append((words[index - (1 if value == "--ozone-platform" else 0)]["start"], end, replacement))
                found_ozone = True
            index += 1
        if not found_ozone:
            browser = words[browser_index]
            edits.append((browser["end"], browser["end"], " --ozone-platform=x11"))

    for start, end, replacement in sorted(edits, key=lambda item: (item[0], item[1]), reverse=True):
        command_str = command_str[:start] + replacement + command_str[end:]
    return command_str


def _prepare_launch_command(command_str):
    if not isinstance(command_str, str) or not command_str.strip():
        raise ValueError("command must be a non-empty string")
    return _inject_ozone_platform(command_str)


def launch(command_str, display=DEFAULT_DISPLAY, detached=True):
    """Launch a trusted shell command in the virtual display.

    ``shell=True`` is retained for compatibility with existing commands that
    contain environment assignments or ``&``; callers must not pass untrusted
    input. Detached commands are checked for immediate non-zero failure.
    """
    ensure_running(display)
    command = _prepare_launch_command(command_str)
    env = get_env(display)
    if detached:
        proc = subprocess.Popen(
            command,
            shell=True,
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.08)
        returncode = proc.poll()
        if returncode is not None and returncode != 0:
            raise RuntimeError(f"Launch failed with exit code {returncode}: {command_str}")
        print(f"[+] Launched '{command_str}' on {display} (PID: {proc.pid})")
        return proc.pid
    return subprocess.run(command, shell=True, env=env, check=True)


def list_windows(display=DEFAULT_DISPLAY):
    ensure_running(display)
    try:
        res = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--name", ""],
            env=get_env(display),
            capture_output=True,
            text=True,
            check=True,
        )
        details = []
        for wid in (item.strip() for item in res.stdout.splitlines()):
            if not wid:
                continue
            name_res = subprocess.run(
                ["xdotool", "getwindowname", wid],
                env=get_env(display),
                capture_output=True,
                text=True,
                check=False,
            )
            title = name_res.stdout.strip()
            if name_res.returncode == 0 and title:
                details.append({"id": wid, "title": title})
        return details
    except Exception as exc:
        print(f"[-] Error listing windows: {exc}")
        return []


def _json_error(handler, status, message):
    body = json.dumps({"ok": False, "error": message}, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _terminate_stream_process(proc, timeout=1.0):
    """Terminate a stream child and escalate only after a bounded wait."""
    try:
        running = proc.poll() is None
    except (AttributeError, OSError):
        running = True
    if running:
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            pass
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
        pass


def start_live_monitor(port=9999, display=DEFAULT_DISPLAY):
    """Start the loopback live monitor and cleanly close it on Ctrl-C."""
    import http.server
    import socketserver
    import urllib.parse

    display = _normalize_display(display)
    port = _validate_port(port)
    ensure_running(display)
    width, height = _screen_size(display)

    class StreamHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>AI 虚拟桌面 1080P 60FPS 实时监视器 ({display})</title>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:#090a0f; color:#e4e4e7; font-family:system-ui,sans-serif; display:flex; flex-direction:column; height:100vh; overflow:hidden; }}
.header {{ display:flex; align-items:center; justify-content:space-between; height:42px; padding:0 16px; font-size:13px; color:#a1a1aa; background:#12131a; border-bottom:1px solid #27272a; flex-shrink:0; }}
.left-meta {{ display:flex; align-items:center; gap:10px; }}
.status-dot {{ width:8px; height:8px; border-radius:50%; background:#10b981; box-shadow:0 0 8px #10b981; }}
.badge {{ background:#27272a; color:#10b981; padding:2px 8px; border-radius:4px; font:700 11px monospace; border:1px solid #059669; }}
.btn {{ background:#1f2029; border:1px solid #3f3f46; color:#e4e4e7; padding:4px 10px; border-radius:4px; cursor:pointer; font-size:12px; }}
.viewport {{ flex:1; display:flex; align-items:center; justify-content:center; background:#000; overflow:auto; position:relative; }}
.screen-container {{ position:relative; width:100%; height:100%; display:flex; align-items:center; justify-content:center; }}
img {{ max-width:100%; max-height:100%; aspect-ratio:{width}/{height}; object-fit:contain; display:block; cursor:crosshair; }}
.native-mode img {{ max-width:none; max-height:none; width:{width}px; height:{height}px; }}
</style></head>
<body><div class="header"><div class="left-meta"><span class="status-dot"></span><span>AI 实时监视器 ({display})</span><span class="badge">{width} x {height}</span></div>
<div style="display:flex;gap:8px"><button class="btn" id="toggle-size">切换 1:1 点对点 / 自适应</button><button class="btn" id="btn-fullscreen">网页全屏</button></div></div>
<div class="viewport" id="viewport"><div class="screen-container"><img id="stream" src="/stream" alt="Live Stream" /></div></div>
<script>
const img=document.getElementById("stream"), viewport=document.getElementById("viewport");
document.getElementById("toggle-size").addEventListener("click",()=>viewport.classList.toggle("native-mode"));
document.getElementById("btn-fullscreen").addEventListener("click",()=>document.fullscreenElement?document.exitFullscreen():document.documentElement.requestFullscreen());
img.addEventListener("click",e=>{{const r=img.getBoundingClientRect(); const x=Math.round((e.clientX-r.left)*{width}/r.width); const y=Math.round((e.clientY-r.top)*{height}/r.height); if(x>=0&&x<{width}&&y>=0&&y<{height}) fetch(`/click?x=${{x}}&y=${{y}});}});
</script></body></html>""".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return
            if parsed.path == "/stream":
                self._stream()
                return
            if parsed.path == "/click":
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                try:
                    if len(query.get("x", [])) != 1 or len(query.get("y", [])) != 1:
                        raise ValueError("x and y are required")
                    click(int(query["x"][0]), int(query["y"][0]), display=display)
                except (TypeError, ValueError) as exc:
                    _json_error(self, 400, str(exc))
                except (XdotoolError, RuntimeError, subprocess.CalledProcessError) as exc:
                    _json_error(self, 500, f"input injection failed: {exc}")
                else:
                    self.send_response(204)
                    self.end_headers()
                return
            self.send_error(404)

        def _stream(self):
            has_ffmpeg = shutil.which("ffmpeg") is not None
            if has_ffmpeg:
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=ffmpeg")
                self.send_header("Cache-Control", "no-cache, private")
                self.end_headers()
                env = get_env(display)
                cmd = [
                    "ffmpeg", "-f", "x11grab", "-draw_mouse", "1", "-framerate", "60",
                    "-video_size", f"{width}x{height}", "-i", display,
                    "-c:v", "mjpeg", "-q:v", "3", "-f", "mpjpeg", "-",
                ]
                proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                _record_process(display, "ffmpeg", proc.pid, port)
                try:
                    while True:
                        chunk = proc.stdout.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    _terminate_stream_process(proc)
                    _unrecord_process(display, proc.pid)
                return

            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache, private")
            self.end_headers()
            fd, tmp_jpg = tempfile.mkstemp(prefix=f"live_stream_{_display_number(display)}_", suffix=".jpg")
            os.close(fd)
            try:
                while True:
                    subprocess.run(
                        ["scrot", "-p", "-z", "-o", "-q", "90", tmp_jpg],
                        env=get_env(display), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    if os.path.exists(tmp_jpg):
                        frame = Path(tmp_jpg).read_bytes()
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
                        self.wfile.flush()
                    time.sleep(0.04)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                try:
                    os.unlink(tmp_jpg)
                except FileNotFoundError:
                    pass

    class ReusableServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
        block_on_close = False

    try:
        server = ReusableServer(("127.0.0.1", port), StreamHandler)
    except OSError as exc:
        raise OSError(f"Could not bind live monitor to 127.0.0.1:{port}: {exc}") from exc
    _record_process(display, "live", os.getpid(), port)
    print(f"[+] Live monitor streaming at: http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Stopping live monitor...", flush=True)
    finally:
        server.server_close()
        _unrecord_process(display, os.getpid())


def prepare_session(port=9999, display=DEFAULT_DISPLAY, open_browser=True):
    """Prepare the virtual display and a checked live monitor child."""
    display = _normalize_display(display)
    port = _validate_port(port)
    ensure_running(display, resolution=DEFAULT_RES)
    if not is_port_open(port):
        script_path = os.path.abspath(__file__)
        child = subprocess.Popen(
            [sys.executable, script_path, "live", "--port", str(port), "--display", display],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(30):
            if is_port_open(port):
                break
            time.sleep(0.1)
        else:
            try:
                child.terminate()
            except ProcessLookupError:
                pass
            raise RuntimeError(f"Live monitor failed to start on port {port}")
        print(f"[+] Live monitor service running on port {port}")
    else:
        print(f"[+] Live monitor service is already running on port {port}")

    if open_browser:
        url = f"http://127.0.0.1:{port}"
        try:
            host_env = os.environ.copy()
            host_env["DISPLAY"] = ":0"
            host_env["WAYLAND_DISPLAY"] = "wayland-0"
            host_env["XDG_RUNTIME_DIR"] = "/run/user/1000"
            host_env.pop("GDK_BACKEND", None)
            subprocess.Popen(
                ["google-chrome", f"--app={url}", "--start-maximized"],
                env=host_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            print(f"[+] Opened desktop surveillance window (1080P 60FPS) on host display: {url}")
        except Exception as exc:
            print(f"[-] Could not auto-launch browser window: {exc}")
    return True


def _port_arg(value):
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be in [1, 65535]")
    return port


def _add_display_option(parser):
    # SUPPRESS is essential: a subparser without an explicit option must not
    # overwrite a value supplied before the subcommand.
    parser.add_argument("--display", default=argparse.SUPPRESS, help="Display target (default: :99)")


def build_parser():
    parser = argparse.ArgumentParser(description="Linux Headless Computer Use CLI")
    parser.add_argument("--display", default=DEFAULT_DISPLAY, help="Display target (default: :99)")
    subparsers = parser.add_subparsers(dest="action", required=True)

    p_start = subparsers.add_parser("start", help="Start the virtual display"); _add_display_option(p_start)
    p_stop = subparsers.add_parser("stop", help="Stop the virtual display"); _add_display_option(p_stop)
    p_status = subparsers.add_parser("status", help="Check status and list windows"); _add_display_option(p_status)

    p_sess = subparsers.add_parser("prepare", help="Prepare session: start display, live service, and pop up viewer")
    p_sess.add_argument("--port", type=_port_arg, default=9999, help="Live monitor port (default: 9999)")
    p_sess.add_argument("--no-browser", action="store_true", help="Do not auto-open browser window")
    _add_display_option(p_sess)

    p_live = subparsers.add_parser("live", help="Start web live stream monitor")
    p_live.add_argument("port_pos", nargs="?", type=_port_arg, default=None, help="HTTP port positional")
    p_live.add_argument("--port", type=_port_arg, default=None, help="HTTP port")
    _add_display_option(p_live)

    p_shot = subparsers.add_parser("screenshot", help="Take a screenshot")
    p_shot.add_argument("path", nargs="?", default="/tmp/screen.png", help="Output file path")
    _add_display_option(p_shot)
    p_click = subparsers.add_parser("click", help="Click at coordinates")
    p_click.add_argument("x", type=int); p_click.add_argument("y", type=int); p_click.add_argument("--button", type=int, default=1); _add_display_option(p_click)
    p_type = subparsers.add_parser("type", help="Type text"); p_type.add_argument("text", type=str); _add_display_option(p_type)
    p_key = subparsers.add_parser("key", help="Press a key"); p_key.add_argument("key", type=str); _add_display_option(p_key)
    p_launch = subparsers.add_parser("launch", help="Launch an application"); p_launch.add_argument("cmd", type=str); _add_display_option(p_launch)
    return parser


def parse_args(argv=None):
    """Parse CLI arguments and resolve the mutually exclusive live port forms."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.action == "live":
        if args.port_pos is not None and args.port is not None:
            parser.error("live accepts either a positional port or --port, not both")
        args.port = args.port_pos if args.port_pos is not None else (args.port or 9999)
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.action == "start":
        ensure_running(args.display)
    elif args.action == "stop":
        stop_display(args.display)
    elif args.action == "prepare":
        prepare_session(args.port, args.display, open_browser=not args.no_browser)
    elif args.action == "live":
        start_live_monitor(args.port, args.display)
    elif args.action == "status":
        active = is_display_active(args.display)
        print(f"Display {args.display} active: {active}")
        if active:
            wins = list_windows(args.display)
            print(f"Windows ({len(wins)}):")
            for win in wins:
                print(f"  - [{win['id']}] {win['title']}")
    elif args.action == "screenshot":
        screenshot(args.path, args.display)
    elif args.action == "click":
        click(args.x, args.y, args.display, args.button)
    elif args.action == "type":
        type_text(args.text, args.display)
    elif args.action == "key":
        press_key(args.key, args.display)
    elif args.action == "launch":
        launch(args.cmd, args.display)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
