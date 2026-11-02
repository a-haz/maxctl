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


if __name__ == "__main__":
    unittest.main()
