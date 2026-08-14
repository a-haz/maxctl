from __future__ import annotations

from pathlib import Path

from maxctl.const import GPU_SCLK_MAX, GPU_SCLK_MIN


def _card() -> Path:
    drm = Path("/sys/class/drm")
    for card in sorted(drm.glob("card[0-9]")):
        dev = card / "device"
        if (dev / "pp_od_clk_voltage").exists():
            return dev
    raise RuntimeError("AMD GPU sysfs not found")


def set_auto() -> None:
    write = _card() / "power_dpm_force_performance_level"
    write.write_text("auto", encoding="utf-8")


def set_sclk_range(min_mhz: int, max_mhz: int) -> None:
    min_mhz = max(GPU_SCLK_MIN, min(GPU_SCLK_MAX, int(min_mhz)))
    max_mhz = max(min_mhz, min(GPU_SCLK_MAX, int(max_mhz)))
    card = _card()
    (card / "power_dpm_force_performance_level").write_text("manual", encoding="utf-8")
    od = card / "pp_od_clk_voltage"
    for cmd in (f"s 0 {min_mhz}\n", f"s 1 {max_mhz}\n", "c\n"):
        od.write_text(cmd, encoding="utf-8")
