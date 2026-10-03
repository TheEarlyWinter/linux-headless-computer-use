import http.client
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import virtual_desktop as vd  # noqa: E402


class CliParsingTests(unittest.TestCase):
    def test_display_can_be_before_or_after_supported_subcommands(self):
        cases = [
            ["start", "--display", ":114"],
            ["stop", "--display", ":114"],
            ["status", "--display", ":114"],
            ["prepare", "--no-browser", "--display", ":114"],
            ["live", "19116", "--display", ":114"],
            ["screenshot", "--display", ":114"],
            ["click", "1", "2", "--display", ":114"],
            ["type", "hello", "--display", ":114"],
            ["key", "Return", "--display", ":114"],
            ["launch", "true", "--display", ":114"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                args = vd.parse_args(argv)
                self.assertEqual(args.display, ":114")

    def test_global_display_is_not_overwritten_by_live_default(self):
        args = vd.parse_args(["--display", ":114", "live", "19116"])
        self.assertEqual(args.display, ":114")
        self.assertEqual(args.port, 19116)

    def test_live_rejects_two_ports_and_invalid_ports(self):
        with self.assertRaises(SystemExit):
            vd.parse_args(["live", "19116", "--port", "19117"])
        with self.assertRaises(SystemExit):
            vd.parse_args(["live", "0"])
        with self.assertRaises(SystemExit):
            vd.parse_args(["prepare", "--port", "65536"])


class WindowManagerTests(unittest.TestCase):
    def test_existing_display_recovers_missing_window_manager(self):
        process = mock.Mock(pid=1142)
        with mock.patch.object(vd, "is_display_active", return_value=True), \
             mock.patch.object(vd, "is_window_manager_running", side_effect=[False, True]), \
             mock.patch.object(vd, "_record_process"), \
             mock.patch.object(vd.subprocess, "Popen", return_value=process) as popen, \
             mock.patch.object(vd.time, "sleep"):
            self.assertTrue(vd.ensure_running(":114"))

        self.assertEqual(popen.call_args.args[0], ["openbox"])
        self.assertEqual(popen.call_args.kwargs["env"]["DISPLAY"], ":114")


class BrowserLaunchTests(unittest.TestCase):
    def test_get_env_forces_x11_and_removes_wayland(self):
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-9"}, clear=False):
            env = vd.get_env(":114")
        self.assertEqual(env["DISPLAY"], ":114")
        self.assertNotIn("WAYLAND_DISPLAY", env)
        self.assertEqual(env["XDG_SESSION_TYPE"], "x11")
        self.assertEqual(env["GDK_BACKEND"], "x11")
        self.assertEqual(env["QT_QPA_PLATFORM"], "xcb")

    def test_browser_injection_handles_prefixes_absolute_paths_and_combinators(self):
        command = (
            "DISPLAY=:114 google-chrome --ozone-platform=wayland "
            "&& /opt/chromium --foo | chromium-browser --ozone-platform wayland"
        )
        transformed = vd._prepare_launch_command(command)
        self.assertEqual(transformed.count("--ozone-platform=x11"), 3)
        self.assertNotIn("--ozone-platform=wayland", transformed)
        self.assertNotIn("--ozone-platform wayland", transformed)
        self.assertEqual(vd._prepare_launch_command("echo google-chrome"), "echo google-chrome")

    def test_launch_passes_injected_command_to_shell(self):
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        with mock.patch.object(vd, "ensure_running"), \
             mock.patch.object(vd.subprocess, "Popen", return_value=process) as popen:
            self.assertEqual(vd.launch("env FOO=bar /usr/bin/google-chrome --new-window"), 123)
        self.assertIn("/usr/bin/google-chrome --ozone-platform=x11", popen.call_args.args[0])
        self.assertTrue(popen.call_args.kwargs["shell"])


class ScopedStopTests(unittest.TestCase):
    def test_source_has_no_global_kill_incantation(self):
        source = (ROOT / "virtual_desktop.py").read_text(encoding="utf-8")
        self.assertNotIn("pkill", source)
        self.assertNotIn("killall", source)

    @staticmethod
    def process(pid, argv, env=None):
        return {
            "pid": pid,
            "argv": argv,
            "cmdline": argv,
            "env": env or {},
            "cwd": str(ROOT),
            "start_ticks": str(pid),
        }

    def test_stop_matches_display_and_live_port_without_killing_other_display(self):
        script = os.path.abspath(vd.__file__)
        processes = [
            self.process(1101, ["Xvfb", ":110"]),
            self.process(9901, ["Xvfb", ":99"]),
            self.process(1102, ["openbox"], {"DISPLAY": ":110"}),
            self.process(9902, ["openbox"], {"DISPLAY": ":99"}),
            self.process(1103, ["python3", script, "live", "--port", "19116", "--display", ":110"]),
            self.process(9903, ["python3", script, "live", "--port", "19117"]),
            self.process(1104, ["ffmpeg", "-f", "x11grab", "-i", ":110"]),
            self.process(9904, ["ffmpeg", "-f", "x11grab", "-i", ":99"]),
        ]
        terminated = []
        fake_state = mock.Mock()
        fake_state.unlink.side_effect = FileNotFoundError

        def terminate(_display, record):
            terminated.append(record)
            return True

        with mock.patch.object(vd, "_owned_records", return_value=[processes[index] for index in (0, 2, 4, 6)]), \
             mock.patch.object(vd, "_terminate_record", side_effect=terminate), \
             mock.patch.object(vd, "is_display_active", return_value=False), \
             mock.patch.object(vd, "_path_is_held", return_value=False), \
             mock.patch.object(vd, "_state_path", return_value=fake_state), \
             mock.patch.object(vd.Path, "exists", return_value=False):
            self.assertTrue(vd.stop_display(":110"))

        self.assertEqual(
            {record["pid"] for record in terminated},
            {1101, 1102, 1103, 1104},
        )

    def test_stop_keeps_files_when_another_process_holds_them(self):
        process = self.process(1101, ["Xvfb", ":110"])
        fake_state = mock.Mock()
        fake_state.unlink.side_effect = FileNotFoundError
        with mock.patch.object(vd, "_owned_records", return_value=[process]), \
             mock.patch.object(vd, "_terminate_record", return_value=True), \
             mock.patch.object(vd, "is_display_active", return_value=False), \
             mock.patch.object(vd, "_path_is_held", return_value=True), \
             mock.patch.object(vd, "_state_path", return_value=fake_state), \
             mock.patch.object(vd.Path, "exists", return_value=True), \
             mock.patch.object(vd.Path, "unlink") as unlink:
            self.assertFalse(vd.stop_display(":110"))
        unlink.assert_not_called()


class LiveMonitorTests(unittest.TestCase):
    def _free_port(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def test_sigint_closes_server_without_same_thread_shutdown(self):
        holder = {}
        real_server = socketserver.ThreadingTCPServer

        class InterruptingServer(real_server):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                holder["server"] = self

            def serve_forever(self, *args, **kwargs):
                raise KeyboardInterrupt

        with mock.patch("socketserver.ThreadingTCPServer", InterruptingServer), \
             mock.patch.object(vd, "ensure_running"), \
             mock.patch.object(vd, "get_display_size", return_value=(1920, 1080)), \
             mock.patch.object(vd, "_record_process"), \
             mock.patch.object(vd, "_unrecord_process"), \
             mock.patch.object(vd, "print"):
            vd.start_live_monitor(self._free_port(), ":114")

        self.assertIn("server", holder)
        self.assertEqual(holder["server"].socket.fileno(), -1)

    def test_stream_child_cleanup_escalates_after_timeout(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("ffmpeg", 1), None]
        vd._terminate_stream_process(process)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_bad_click_is_json_400_and_server_stays_alive(self):
        holder = {}
        real_server = socketserver.ThreadingTCPServer

        class RecordingServer(real_server):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                holder["server"] = self

        port = self._free_port()
        with mock.patch("socketserver.ThreadingTCPServer", RecordingServer), \
             mock.patch.object(vd, "ensure_running"), \
             mock.patch.object(vd, "get_display_size", return_value=(1920, 1080)), \
             mock.patch.object(vd, "_record_process"), \
             mock.patch.object(vd, "_unrecord_process"), \
             mock.patch.object(vd, "click") as click:
            thread = threading.Thread(target=vd.start_live_monitor, args=(port, ":114"), daemon=True)
            thread.start()
            deadline = time.monotonic() + 2
            while "server" not in holder and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn("server", holder)

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/click?x=abc&y=1")
            response = conn.getresponse()
            body = response.read()
            conn.close()
            self.assertEqual(response.status, 400)
            self.assertIn(b"invalid literal", body)

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/click?x=1&y=1")
            response = conn.getresponse()
            response.read()
            conn.close()
            self.assertEqual(response.status, 204)
            click.assert_called_once_with(1, 1, display=":114")

            holder["server"].shutdown()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())


class InputValidationTests(unittest.TestCase):
    def test_coordinates_are_strictly_inside_display_bounds(self):
        with mock.patch.object(vd, "ensure_running"), \
             mock.patch.object(vd, "get_display_size", return_value=(1920, 1080)), \
             mock.patch.object(vd, "_run_xdotool") as run_xdotool:
            with self.assertRaises(ValueError):
                vd.click(1920, 1080, display=":114")
            with self.assertRaises(ValueError):
                vd.mouse_move(-1, 0, display=":114")
            with self.assertRaises(ValueError):
                vd.click(1, 1, display=":114", button=0)
            vd.click(1919, 1079, display=":114")
        run_xdotool.assert_called_once()

    def test_empty_key_and_xdotool_failure_are_explicit(self):
        with self.assertRaises(ValueError):
            vd.press_key("", display=":114")
        failed = subprocess.CompletedProcess(["xdotool"], 1, "", "X Error: Bad Key")
        with mock.patch.object(vd, "ensure_running"), \
             mock.patch.object(vd.subprocess, "run", return_value=failed):
            with self.assertRaises(vd.XdotoolError) as raised:
                vd.press_key("NotAKey", display=":114")
        self.assertIn("X Error", str(raised.exception))

    def test_empty_text_is_a_noop(self):
        with mock.patch.object(vd, "ensure_running") as ensure, \
             mock.patch.object(vd, "_run_xdotool") as run_xdotool:
            self.assertIsNone(vd.type_text("", display=":114"))
        ensure.assert_not_called()
        run_xdotool.assert_not_called()


class DetachedLaunchTests(unittest.TestCase):
    def test_detached_nonzero_exit_is_not_reported_as_success(self):
        process = mock.Mock(pid=456)
        process.poll.return_value = 23
        with mock.patch.object(vd, "ensure_running"), \
             mock.patch.object(vd.subprocess, "Popen", return_value=process) as popen:
            with self.assertRaises(RuntimeError) as raised:
                vd.launch("exit 23", display=":114")
        self.assertIn("23", str(raised.exception))
        self.assertTrue(popen.call_args.kwargs["shell"])


if __name__ == "__main__":
    unittest.main()
