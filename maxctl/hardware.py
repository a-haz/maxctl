from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path

from maxctl.const import CPU_NAME, DEVICE_NAME, DMI_PRODUCT, GPU_NAME
from maxctl.sysfs import first_hwmon, read_int, read_text


@dataclass
class DeviceInfo:
    product: str
    vendor: str
    name: str
    cpu: str
    gpu: str
    known: bool
    kernel: str


@dataclass
class Telemetry:
    cpu_temp_c: float | None
    gpu_temp_c: float | None
    gpu_power_w: float | None
    gpu_clock_mhz: int | None
    gpu_busy_pct: int | None
    fan_rpm: int | None
    fan_pwm: int | None
    fan_mode: int | None
    battery_pct: int | None
    battery_w: float | None
    on_ac: bool
    epp: str | None
    boost: bool | None
    gpu_perf_level: str | None
    package_w: float | None


class PowerMeter:
    """Estimate package power from RAPL energy counters when present."""

    def __init__(self) -> None:
        self._path = Path("/sys/class/powercap/intel-rapl:0/energy_uj")
        self._last_uj: int | None = None
        self._last_t: float | None = None

    def sample(self) -> float | None:
        uj = read_int(self._path)
        now = time.monotonic()
        if uj is None:
            return None
        watts = None
        if self._last_uj is not None and self._last_t is not None:
            dt = now - self._last_t
            if dt > 0.05:
                du = uj - self._last_uj
                if du < 0:
                    du += 1 << 32
                watts = (du / 1_000_000.0) / dt
        self._last_uj = uj
        self._last_t = now
        return watts


_POWER = PowerMeter()


def detect_device() -> DeviceInfo:
    product = read_text("/sys/class/dmi/id/product_name", "unknown") or "unknown"
    vendor = read_text("/sys/class/dmi/id/sys_vendor", "unknown") or "unknown"
    cpu = read_text("/proc/cpuinfo", "") or ""
    cpu_name = CPU_NAME
    for line in cpu.splitlines():
        if line.startswith("model name"):
            cpu_name = line.split(":", 1)[1].strip()
            break
    kernel = read_text("/proc/version", "") or ""
    return DeviceInfo(
        product=product,
        vendor=vendor,
        name=DEVICE_NAME if product == DMI_PRODUCT else f"{vendor} {product}",
        cpu=cpu_name,
        gpu=GPU_NAME if "150e" in (read_text("/sys/class/drm/card1/device/device") or "") else "AMD iGPU",
        known=product.startswith("G1619") or "HX 370" in cpu_name,
        kernel=kernel.split()[2] if kernel else "",
    )


def _amdgpu_card() -> Path | None:
    drm = Path("/sys/class/drm")
    if not drm.exists():
        return None
    for card in sorted(drm.glob("card[0-9]")):
        dev = card / "device"
        if (dev / "pp_od_clk_voltage").exists():
            return dev
    return None


def _battery_watts() -> float | None:
    bat = Path("/sys/class/power_supply/BAT0")
    current = read_int(bat / "current_now")
    voltage = read_int(bat / "voltage_now")
    if current is None or voltage is None:
        power = read_int(bat / "power_now")
        return power / 1_000_000.0 if power is not None else None
    return (current * voltage) / 1e12


def read_telemetry() -> Telemetry:
    k10 = first_hwmon("k10temp")
    gpu_hw = first_hwmon("amdgpu")
    fan = first_hwmon("gpdfan")
    gpu_dev = _amdgpu_card()
    on_ac = read_int("/sys/class/power_supply/ADP1/online", 0) == 1
    boost = read_int("/sys/devices/system/cpu/cpufreq/boost")
    return Telemetry(
        cpu_temp_c=_milli_c(k10 / "temp1_input" if k10 else None),
        gpu_temp_c=_milli_c(gpu_hw / "temp1_input" if gpu_hw else None),
        gpu_power_w=_micro_w(gpu_hw / "power1_input" if gpu_hw else None),
        gpu_clock_mhz=_mhz(gpu_hw / "freq1_input" if gpu_hw else None),
        gpu_busy_pct=read_int(gpu_dev / "gpu_busy_percent") if gpu_dev else None,
        fan_rpm=read_int(fan / "fan1_input") if fan else None,
        fan_pwm=read_int(fan / "pwm1") if fan else None,
        fan_mode=read_int(fan / "pwm1_enable") if fan else None,
        battery_pct=read_int("/sys/class/power_supply/BAT0/capacity"),
        battery_w=_battery_watts(),
        on_ac=on_ac,
        epp=read_text("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference"),
        boost=None if boost is None else bool(boost),
        gpu_perf_level=read_text(gpu_dev / "power_dpm_force_performance_level") if gpu_dev else None,
        package_w=_POWER.sample(),
    )


def telemetry_dict() -> dict:
    return asdict(read_telemetry())


def _milli_c(path: Path | None) -> float | None:
    if path is None:
        return None
    raw = read_int(path)
    return None if raw is None else raw / 1000.0


def _micro_w(path: Path | None) -> float | None:
    if path is None:
        return None
    raw = read_int(path)
    return None if raw is None else raw / 1_000_000.0


def _mhz(path: Path | None) -> int | None:
    if path is None:
        return None
    raw = read_int(path)
    return None if raw is None else int(raw / 1_000_000)
