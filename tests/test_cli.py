import dataclasses
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from maxctl import cli
from maxctl.hardware import DeviceInfo, Telemetry


class StatusOutputTests(unittest.TestCase):
    def _fan_line(self, fan_mode) -> str:
        values = {f.name: None for f in dataclasses.fields(Telemetry)}
        values.update(on_ac=True, fan_rpm=4100, fan_pwm=255, fan_mode=fan_mode)
        device = DeviceInfo("G1619-05", "GPD", "GPD Win Max 2", "HX 370", "Radeon 890M", True, "6.18")
        buf = io.StringIO()
        with (
            mock.patch.object(cli, "read_telemetry", return_value=Telemetry(**values)),
            mock.patch.object(cli, "detect_device", return_value=device),
            mock.patch.object(cli, "connected", return_value=False),
            mock.patch.object(cli.alib, "available", return_value=False),
            mock.patch.object(cli.ryzenadj, "available", return_value=False),
            redirect_stdout(buf),
        ):
            cli._print_telemetry()
        return next(line for line in buf.getvalue().splitlines() if line.startswith("Fan"))

    def test_fan_modes_are_named(self):
        self.assertTrue(self._fan_line(0).endswith("full"))
        self.assertTrue(self._fan_line(1).endswith("manual"))
        self.assertTrue(self._fan_line(2).endswith("auto"))


class FanCommandTests(unittest.TestCase):
    def _run(self, value: str):
        err = io.StringIO()
        with (
            mock.patch.object(cli, "_rpc_or_root", return_value={"ok": True}) as rpc,
            redirect_stdout(io.StringIO()),
            mock.patch("sys.stderr", err),
        ):
            code = cli.main(["fan", value])
        return code, rpc, err.getvalue()

    def test_pwm_and_modes(self):
        code, rpc, _ = self._run("90")
        self.assertEqual(code, 0)
        rpc.assert_called_once_with("set_settings", settings={"fan_mode": "manual", "fan_pwm": 90})
        _, rpc, _ = self._run("curve")
        rpc.assert_called_once_with("set_settings", settings={"fan_mode": "curve"})

    def test_bad_value_is_an_error_not_a_traceback(self):
        # A negative PWM used to clamp to 0, i.e. fan off.
        for value in ("max", "-5", "256", "12.5", "²"):
            code, rpc, err = self._run(value)
            self.assertEqual(code, 1)
            rpc.assert_not_called()
            self.assertIn("fan takes auto, curve or a PWM", err)


if __name__ == "__main__":
    unittest.main()
