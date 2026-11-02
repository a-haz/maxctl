import dataclasses
import tempfile
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


if __name__ == "__main__":
    unittest.main()
