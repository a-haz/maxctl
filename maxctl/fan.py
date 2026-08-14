from __future__ import annotations

from dataclasses import dataclass

from maxctl.const import FAN_AUTO, FAN_MANUAL
from maxctl.sysfs import first_hwmon, write_text


@dataclass
class CurvePoint:
    temp_c: int
    percent: int


DEFAULT_CURVES: dict[str, list[CurvePoint]] = {
    "silent": [
        CurvePoint(40, 15),
        CurvePoint(50, 22),
        CurvePoint(60, 35),
        CurvePoint(70, 55),
        CurvePoint(80, 80),
        CurvePoint(90, 100),
    ],
    "balanced": [
        CurvePoint(40, 25),
        CurvePoint(50, 35),
        CurvePoint(60, 50),
        CurvePoint(70, 70),
        CurvePoint(80, 90),
        CurvePoint(90, 100),
    ],
    "performance": [
        CurvePoint(40, 40),
        CurvePoint(50, 55),
        CurvePoint(60, 70),
        CurvePoint(70, 85),
        CurvePoint(80, 100),
        CurvePoint(90, 100),
    ],
}


def _fan_dir():
    path = first_hwmon("gpdfan")
    if path is None:
        raise RuntimeError("gpdfan hwmon not found")
    return path


def set_auto() -> None:
    write_text(_fan_dir() / "pwm1_enable", str(FAN_AUTO))


def set_manual_pwm(pwm: int) -> None:
    pwm = max(0, min(255, int(pwm)))
    fan = _fan_dir()
    write_text(fan / "pwm1_enable", str(FAN_MANUAL))
    write_text(fan / "pwm1", str(pwm))


def interpolate(temp_c: float, curve: list[CurvePoint]) -> int:
    points = sorted(curve, key=lambda p: p.temp_c)
    if not points:
        return 128
    if temp_c <= points[0].temp_c:
        pct = points[0].percent
    elif temp_c >= points[-1].temp_c:
        pct = points[-1].percent
    else:
        pct = points[-1].percent
        for lo, hi in zip(points, points[1:]):
            if lo.temp_c <= temp_c <= hi.temp_c:
                span = hi.temp_c - lo.temp_c or 1
                t = (temp_c - lo.temp_c) / span
                pct = lo.percent + t * (hi.percent - lo.percent)
                break
    return max(0, min(255, int(round(pct * 2.55))))


class FanController:
    """Software curve with hysteresis and slew so the EC fan does not hunt."""

    def __init__(self) -> None:
        self.pwm = 80
        self._hold_temp = 0.0

    def tick(self, temp_c: float, curve: list[CurvePoint], slew: int = 12) -> int:
        if temp_c < self._hold_temp - 2.0 or temp_c > self._hold_temp + 0.8:
            self._hold_temp = temp_c
        target = interpolate(self._hold_temp, curve)
        if target > self.pwm:
            self.pwm = min(target, self.pwm + slew)
        elif target < self.pwm:
            self.pwm = max(target, self.pwm - max(4, slew // 2))
        set_manual_pwm(self.pwm)
        return self.pwm
