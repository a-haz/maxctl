import dataclasses
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from maxctl import daemon as daemon_mod
from maxctl import fan
from maxctl.apply import ApplyResult
from maxctl.const import FAN_AUTO, FAN_MANUAL
from maxctl.hardware import Telemetry
from maxctl.profiles import Settings
from maxctl.state import AppConfig


def _telemetry(cpu=None, gpu=None, on_ac=True) -> Telemetry:
    values = {f.name: None for f in dataclasses.fields(Telemetry)}
    values.update(cpu_temp_c=cpu, gpu_temp_c=gpu, on_ac=on_ac)
    return Telemetry(**values)


class _DaemonCase(unittest.TestCase):
    """A Daemon wired to a fake gpdfan directory; nothing touches sysfs, /etc or /var."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hwmon = Path(tmp.name)
        self.write_fan(FAN_AUTO, 0)
        self.applied: list[Settings] = []
        for target, attr, kwargs in (
            (fan, "_fan_dir", {"return_value": self.hwmon}),
            (daemon_mod, "load_config", {"return_value": AppConfig(auto_switch=False)}),
            (daemon_mod, "load_settings", {"return_value": Settings(fan_mode="curve")}),
            (daemon_mod, "apply_settings", {"side_effect": self._apply}),
            (daemon_mod, "save_settings", {}),
            (daemon_mod, "save_system_config", {}),
            (daemon_mod, "detect_egpu", {"return_value": None}),
            (daemon_mod, "read_telemetry", {"return_value": _telemetry(cpu=50.0)}),
        ):
            patcher = mock.patch.object(target, attr, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _apply(self, settings, fan_tick=False):
        self.applied.append(settings)
        return ApplyResult(ok=True, tdp_backend="test")

    def write_fan(self, enable: int, pwm: int) -> None:
        (self.hwmon / "pwm1_enable").write_text(str(enable))
        (self.hwmon / "pwm1").write_text(str(pwm))

    def fan_state(self) -> tuple[int, int]:
        return int((self.hwmon / "pwm1_enable").read_text()), int((self.hwmon / "pwm1").read_text())

    @staticmethod
    def run_background_once(daemon) -> None:
        daemon.stop = mock.Mock()
        daemon.stop.wait.side_effect = [False, True]
        daemon.loop_background()


class FanFailSafeTests(_DaemonCase):
    def test_curve_drives_the_fan_from_temperature(self):
        daemon = daemon_mod.Daemon()
        with mock.patch.object(daemon_mod, "read_telemetry", return_value=_telemetry(cpu=85.0, gpu=60.0)):
            self.run_background_once(daemon)
        enable, pwm = self.fan_state()
        self.assertEqual(enable, FAN_MANUAL)
        self.assertGreater(pwm, 80)  # ramping up from the controller's start value

    def test_curve_without_a_temperature_hands_the_fan_to_the_ec(self):
        # Both sensors missing used to read as 0 C, i.e. the curve's minimum.
        self.write_fan(FAN_MANUAL, 200)
        daemon = daemon_mod.Daemon()
        with mock.patch.object(daemon_mod, "read_telemetry", return_value=_telemetry()):
            self.run_background_once(daemon)
        self.assertEqual(self.fan_state()[0], FAN_AUTO)

    def test_telemetry_error_hands_the_fan_to_the_ec(self):
        self.write_fan(FAN_MANUAL, 200)
        daemon = daemon_mod.Daemon()
        with mock.patch.object(daemon_mod, "read_telemetry", side_effect=OSError("sysfs gone")):
            self.run_background_once(daemon)
        self.assertEqual(self.fan_state()[0], FAN_AUTO)

    def test_auto_switch_error_does_not_freeze_the_curve(self):
        daemon = daemon_mod.Daemon()
        with (
            mock.patch.object(daemon_mod, "read_telemetry", return_value=_telemetry(cpu=85.0)),
            mock.patch.object(daemon, "maybe_auto_switch", side_effect=RuntimeError("apply failed")),
        ):
            self.run_background_once(daemon)
        enable, pwm = self.fan_state()
        self.assertEqual(enable, FAN_MANUAL)
        self.assertGreater(pwm, 80)


class DaemonExitTests(_DaemonCase):
    def _main(self, serve) -> int:
        with (
            mock.patch.object(daemon_mod, "_serve", side_effect=serve),
            mock.patch.object(daemon_mod.signal, "signal"),
        ):
            return daemon_mod.main([])

    def test_stop_hands_the_fan_back_to_the_ec(self):
        def serve(daemon, _path):
            self.write_fan(FAN_MANUAL, 40)  # the software curve owns the fan
            daemon.stop.set()  # what SIGTERM does

        self.assertEqual(self._main(serve), 0)
        self.assertEqual(self.fan_state()[0], FAN_AUTO)

    def test_crash_hands_the_fan_back_to_the_ec(self):
        def serve(_daemon, _path):
            self.write_fan(FAN_MANUAL, 40)
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self._main(serve)
        self.assertEqual(self.fan_state()[0], FAN_AUTO)

    def test_serve_loop_ending_on_its_own_is_a_failure(self):
        # A non-zero exit is what makes Restart=on-failure bring the helper back.
        self.assertEqual(self._main(lambda _daemon, _path: None), 1)

    def test_release_fan_flag_only_releases_the_fan(self):
        self.write_fan(FAN_MANUAL, 40)
        with mock.patch.object(daemon_mod, "Daemon") as daemon_cls:
            self.assertEqual(daemon_mod.main(["--release-fan"]), 0)
        daemon_cls.assert_not_called()
        self.assertEqual(self.fan_state()[0], FAN_AUTO)


class _StubDaemon:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.seen: list[dict] = []

    def handle(self, msg: dict) -> dict:
        self.seen.append(msg)
        return {"ok": True, "cmd": msg.get("cmd")}


class SocketTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "maxctl.sock")
        self.daemon = _StubDaemon()
        self.thread = threading.Thread(target=daemon_mod._serve, args=(self.daemon, self.path), daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        deadline = time.monotonic() + 5
        while not os.path.exists(self.path) and time.monotonic() < deadline:
            time.sleep(0.01)

    def _stop(self):
        self.daemon.stop.set()
        self.thread.join(timeout=5)

    def ask(self, payload: bytes) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(self.path)
            sock.sendall(payload)
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data)

    def test_peer_credentials_name_the_connecting_process(self):
        a, b = socket.socketpair()
        with a, b:
            self.assertEqual(daemon_mod.peer_credentials(a), (os.getpid(), os.getuid(), os.getgid()))

    def test_non_admin_can_read_but_not_change_anything(self):
        with mock.patch.object(daemon_mod, "is_admin", return_value=False):
            status = self.ask(b'{"cmd": "status"}\n')
            denied = [
                self.ask(json.dumps({"cmd": cmd}).encode() + b"\n")
                for cmd in ("set_settings", "set_profile", "set_config", "import", "reapply", "smu")
            ]
        self.assertTrue(status["ok"])
        for reply in denied:
            self.assertFalse(reply["ok"])
            self.assertIn("permission denied", reply["error"])
        self.assertEqual([msg["cmd"] for msg in self.daemon.seen], ["status"])

    def test_admin_can_change_settings(self):
        with mock.patch.object(daemon_mod, "is_admin", return_value=True):
            reply = self.ask(b'{"cmd": "set_settings", "settings": {"tdp_w": 20}}\n')
        self.assertTrue(reply["ok"])
        self.assertEqual(self.daemon.seen[-1]["settings"], {"tdp_w": 20})

    def test_client_hanging_up_early_does_not_stop_the_server(self):
        for _ in range(5):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(self.path)
                sock.sendall(b'{"cmd": "ping"}\n')
        self.assertTrue(self.ask(b'{"cmd": "ping"}\n')["ok"])
        self.assertTrue(self.thread.is_alive())

    def test_stalled_client_does_not_block_others(self):
        with (
            mock.patch.object(daemon_mod, "CLIENT_TIMEOUT", 0.2),
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as idle,
        ):
            idle.connect(self.path)  # connects and never sends a request
            self.assertTrue(self.ask(b'{"cmd": "ping"}\n')["ok"])

    def test_oversized_request_is_refused(self):
        with mock.patch.object(daemon_mod, "MAX_REQUEST_BYTES", 1024):
            reply = self.ask(b'{"cmd": "ping", "pad": "' + b"x" * 4096 + b'"}\n')
        self.assertFalse(reply["ok"])
        self.assertEqual(self.daemon.seen, [])


class IsAdminTests(unittest.TestCase):
    GROUPS = {"sudo": 27, "admin": 117}

    def _getgrnam(self, name):
        if name not in self.GROUPS:
            raise KeyError(name)
        return mock.Mock(gr_gid=self.GROUPS[name])

    def _is_admin(self, member_of: list[int]) -> bool:
        with (
            mock.patch.object(daemon_mod.pwd, "getpwuid", return_value=mock.Mock(pw_name="aiman")),
            mock.patch.object(daemon_mod.grp, "getgrnam", side_effect=self._getgrnam),
            mock.patch.object(daemon_mod.os, "getgrouplist", return_value=member_of),
        ):
            return daemon_mod.is_admin(1000, 1000)

    def test_root_is_admin(self):
        self.assertTrue(daemon_mod.is_admin(0, 0))

    def test_admin_group_member(self):
        self.assertTrue(self._is_admin([1000, 27]))
        self.assertTrue(self._is_admin([1000, 117]))

    def test_plain_user(self):
        self.assertFalse(self._is_admin([1000, 100]))

    def test_unknown_uid(self):
        with mock.patch.object(daemon_mod.pwd, "getpwuid", side_effect=KeyError(4242)):
            self.assertFalse(daemon_mod.is_admin(4242, 4242))


if __name__ == "__main__":
    unittest.main()
