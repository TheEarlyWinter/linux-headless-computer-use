#!/usr/bin/env python3
"""
Linux Headless Virtual Desktop (Computer Use Helper)
为 AI Agent 打造的无感后台 X11 虚拟屏幕控制套件。
纯标准库实现，依赖: Xvfb, xdotool, scrot, openbox。
"""

import sys
import os
import time
import subprocess
import argparse

DEFAULT_DISPLAY = ":99"
DEFAULT_RES = "1920x1080x24"

def get_env(display=DEFAULT_DISPLAY):
    env = os.environ.copy()
    env["DISPLAY"] = display
    if "WAYLAND_DISPLAY" in env:
        del env["WAYLAND_DISPLAY"]
    env["GDK_BACKEND"] = "x11"
    env["QT_QPA_PLATFORM"] = "xcb"
    return env

def is_display_active(display=DEFAULT_DISPLAY):
    try:
        res = subprocess.run(
            ["xdpyinfo", "-display", display],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2
        )
        return res.returncode == 0
    except Exception:
        return False

def ensure_running(display=DEFAULT_DISPLAY, resolution=DEFAULT_RES):
    """确保虚拟显示器和基础窗口管理器已启动"""
    if is_display_active(display):
        return True
    
    print(f"[*] Starting Xvfb on {display} ({resolution})...")
    xvfb_cmd = [
        "Xvfb", display,
        "-screen", "0", resolution,
        "-nolisten", "tcp",
        "-ac", "+extension", "GLX", "+render", "-noreset"
    ]
    subprocess.Popen(xvfb_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    for _ in range(30):
        time.sleep(0.1)
        if is_display_active(display):
            break
    else:
        raise RuntimeError(f"Failed to start Xvfb on {display}")

    # 尝试拉起轻量级窗口管理器 openbox，以便获得真实的窗口焦点与边框控制
    try:
        subprocess.Popen(
            ["openbox"],
            env=get_env(display),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        time.sleep(0.3)
    except FileNotFoundError:
        pass

    print(f"[+] Display {display} is ready!")
    return True

def stop_display(display=DEFAULT_DISPLAY):
    """停止指定的虚拟显示器"""
    display_num = display.lstrip(":")
    subprocess.run(["pkill", "-f", f"Xvfb {display}"], stderr=subprocess.DEVNULL)
    print(f"[*] Stopped Xvfb on {display}")

def screenshot(output_path="/tmp/screen.png", display=DEFAULT_DISPLAY):
    """在虚拟屏幕中进行毫秒级截屏"""
    ensure_running(display)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    subprocess.run(["scrot", "-z", output_path], env=get_env(display), check=True)
    print(f"[+] Screenshot saved to: {output_path}")
    return output_path

def click(x, y, display=DEFAULT_DISPLAY, button=1):
    """在指定绝对坐标点按鼠标"""
    ensure_running(display)
    cmd = ["xdotool", "mousemove", str(x), str(y), "click", str(button)]
    subprocess.run(cmd, env=get_env(display), check=True)
    print(f"[+] Clicked ({x}, {y}) [button {button}] on {display}")

def mouse_move(x, y, display=DEFAULT_DISPLAY):
    """移动鼠标到指定绝对坐标"""
    ensure_running(display)
    cmd = ["xdotool", "mousemove", str(x), str(y)]
    subprocess.run(cmd, env=get_env(display), check=True)

def type_text(text, display=DEFAULT_DISPLAY, delay_ms=12):
    """模拟真人键盘输入字符串"""
    ensure_running(display)
    cmd = ["xdotool", "type", "--delay", str(delay_ms), text]
    subprocess.run(cmd, env=get_env(display), check=True)
    print(f"[+] Typed: {text[:20]}... on {display}")

def press_key(key_name, display=DEFAULT_DISPLAY):
    """按下特殊按键（如 Return, Escape, BackSpace, ctrl+c 等）"""
    ensure_running(display)
    cmd = ["xdotool", "key", key_name]
    subprocess.run(cmd, env=get_env(display), check=True)
    print(f"[+] Pressed key: {key_name} on {display}")

def launch(command_str, display=DEFAULT_DISPLAY, detached=True):
    """在虚拟屏幕中启动图形软件"""
    ensure_running(display)
    env = get_env(display)
    if detached:
        proc = subprocess.Popen(
            command_str,
            shell=True,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        print(f"[+] Launched '{command_str}' on {display} (PID: {proc.pid})")
        return proc.pid
    else:
        return subprocess.run(command_str, shell=True, env=env)

def list_windows(display=DEFAULT_DISPLAY):
    """列出虚拟屏幕中的所有可见窗口"""
    ensure_running(display)
    try:
        res = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--name", ""],
            env=get_env(display),
            capture_output=True,
            text=True
        )
        window_ids = [w.strip() for w in res.stdout.splitlines() if w.strip()]
        details = []
        for wid in window_ids:
            name_res = subprocess.run(
                ["xdotool", "getwindowname", wid],
                env=get_env(display),
                capture_output=True,
                text=True
            )
            title = name_res.stdout.strip()
            details.append({"id": wid, "title": title})
        return details
    except Exception as e:
        print(f"[-] Error listing windows: {e}")
        return []

def start_live_monitor(port=9999, display=DEFAULT_DISPLAY):
    """启动本地网页实时直播间（零额外依赖，纯标准库 + scrot）"""
    import http.server
    import socketserver
    import urllib.parse

    ensure_running(display)

    class StreamHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>AI 隐形房间实时监视器 ({display})</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ background: #090a0f; color: #e4e4e7; font-family: system-ui, sans-serif; display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100vh; overflow: hidden; }}
        .header {{ display: flex; align-items: center; justify-content: space-between; width: 100%; max-width: 1280px; padding: 12px 16px; font-size: 13px; color: #a1a1aa; }}
        .status-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #10b981; margin-right: 6px; box-shadow: 0 0 8px #10b981; }}
        .screen-container {{ position: relative; max-width: 1280px; width: 95vw; aspect-ratio: 16/10; border: 1px solid #27272a; border-radius: 8px; overflow: hidden; background: #000; box-shadow: 0 20px 50px rgba(0,0,0,0.6); }}
        img {{ width: 100%; height: 100%; object-fit: contain; display: block; cursor: crosshair; }}
        .tips {{ margin-top: 10px; font-size: 12px; color: #71717a; }}
    </style>
</head>
<body>
    <div class="header">
        <div><span class="status-dot"></span> 实时监控: 虚拟隐形房间 ({display})</div>
        <div>点击画面可直接注入鼠标操作</div>
    </div>
    <div class="screen-container">
        <img id="stream" src="/stream" alt="Live Stream" />
    </div>
    <div class="tips">无感运行中 · 物理主屏幕与鼠标 100% 自由</div>
    <script>
        const img = document.getElementById("stream");
        img.addEventListener("click", (e) => {{
            const rect = img.getBoundingClientRect();
            const scaleX = 1920 / rect.width;
            const scaleY = 1080 / rect.height;
            const x = Math.round((e.clientX - rect.left) * scaleX);
            const y = Math.round((e.clientY - rect.top) * scaleY);
            fetch(`/click?x=${{x}}&y=${{y}}`);
        }});
    </script>
</body>
</html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
            elif parsed.path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-cache, private")
                self.end_headers()
                env = get_env(display)
                tmp_jpg = f"/tmp/live_stream_{display.replace(':', '')}.jpg"
                try:
                    while True:
                        subprocess.run(
                            ["scrot", "-p", "-z", "-o", "-q", "80", tmp_jpg],
                            env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL
                        )
                        if os.path.exists(tmp_jpg):
                            with open(tmp_jpg, "rb") as f:
                                frame = f.read()
                            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
                        time.sleep(0.05)
                except Exception:
                    pass
            elif parsed.path == "/click":
                q = urllib.parse.parse_qs(parsed.query)
                if "x" in q and "y" in q:
                    click(int(q["x"][0]), int(q["y"][0]), display=display)
                self.send_response(204)
                self.end_headers()

    server = socketserver.ThreadingTCPServer(("127.0.0.1", port), StreamHandler)
    server.allow_reuse_address = True
    print(f"[+] Live monitor streaming at: http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Stopping live monitor...")
        server.shutdown()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Linux Headless Computer Use CLI")
    subparsers = parser.add_subparsers(dest="action", required=True)

    subparsers.add_parser("start", help="Start the virtual display")
    subparsers.add_parser("stop", help="Stop the virtual display")
    subparsers.add_parser("status", help="Check status and list windows")

    p_live = subparsers.add_parser("live", help="Start web live stream monitor")
    p_live.add_argument("--port", type=int, default=9999, help="HTTP port (default: 9999)")

    p_shot = subparsers.add_parser("screenshot", help="Take a screenshot")
    p_shot.add_argument("path", nargs="?", default="/tmp/screen.png", help="Output file path")

    p_click = subparsers.add_parser("click", help="Click at coordinates")
    p_click.add_argument("x", type=int)
    p_click.add_argument("y", type=int)
    p_click.add_argument("--button", type=int, default=1)

    p_type = subparsers.add_parser("type", help="Type text")
    p_type.add_argument("text", type=str)

    p_key = subparsers.add_parser("key", help="Press a key")
    p_key.add_argument("key", type=str)

    p_launch = subparsers.add_parser("launch", help="Launch an application")
    p_launch.add_argument("cmd", type=str)

    parser.add_argument("--display", default=DEFAULT_DISPLAY, help="Display target (default: :99)")

    args = parser.parse_args()

    if args.action == "start":
        ensure_running(args.display)
    elif args.action == "stop":
        stop_display(args.display)
    elif args.action == "live":
        start_live_monitor(args.port, args.display)
    elif args.action == "status":
        active = is_display_active(args.display)
        print(f"Display {args.display} active: {active}")
        if active:
            wins = list_windows(args.display)
            print(f"Windows ({len(wins)}):")
            for w in wins:
                print(f"  - [{w['id']}] {w['title']}")
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
