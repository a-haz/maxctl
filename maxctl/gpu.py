from __future__ import annotations

from pathlib import Path

from maxctl.const import GPU_DPM_MODES, GPU_SCLK_MAX, GPU_SCLK_MIN


def _card() -> Path:
    drm = Path("/sys/class/drm")
    for card in sorted(drm.glob("card[0-9]")):
        dev = card / "device"
        if (dev / "pp_od_clk_voltage").exists():
            return dev
    raise RuntimeError("AMD GPU sysfs not found")


def set_auto() -> None:
    set_dpm("auto")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def parse_od_sclk(text: str) -> tuple[int, int] | None:
    """Return (min, max) MHz from the OD_SCLK block of pp_od_clk_voltage."""
    lo = hi = None
    in_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.endswith(":"):
            in_block = line == "OD_SCLK:"
            continue
        if not in_block or ":" not in line:
            continue
        idx, _, value = line.partition(":")
        value = value.strip().lower().rstrip("mhz").strip()
        try:
            mhz = int(value)
        except ValueError:
            continue
        if idx.strip() == "0":
            lo = mhz
        elif idx.strip() == "1":
            hi = mhz
    if lo is None or hi is None:
        return None
    return lo, hi


def set_dpm(level: str) -> None:
    if level not in GPU_DPM_MODES:
        raise ValueError(f"invalid GPU DPM level: {level}")
    write = _card() / "power_dpm_force_performance_level"
    write.write_text(level, encoding="utf-8")


def set_sclk_range(min_mhz: int, max_mhz: int) -> bool:
    """Pin the iGPU to [min, max] MHz. Returns True when sysfs was written.

    The helper re-applies settings every few seconds; each OD commit is a
    round of SMU messages, and the Strix Point SMU on this chassis is known to
    wedge. Skip the writes when the table already holds the requested range.
    """
    min_mhz = max(GPU_SCLK_MIN, min(GPU_SCLK_MAX, int(min_mhz)))
    max_mhz = max(min_mhz, min(GPU_SCLK_MAX, int(max_mhz)))
    card = _card()
    level = card / "power_dpm_force_performance_level"
    od = card / "pp_od_clk_voltage"
    if _read(level) == "manual" and parse_od_sclk(_read(od)) == (min_mhz, max_mhz):
        return False
    level.write_text("manual", encoding="utf-8")
    for cmd in (f"s 0 {min_mhz}\n", f"s 1 {max_mhz}\n", "c\n"):
        od.write_text(cmd, encoding="utf-8")
    return True
