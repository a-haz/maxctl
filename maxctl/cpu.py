from __future__ import annotations

from pathlib import Path

from maxctl.const import EPP_MODES
from maxctl.sysfs import iter_cpus, write_text


def set_epp(mode: str) -> None:
    if mode not in EPP_MODES:
        raise ValueError(f"invalid EPP: {mode}")
    for cpu in iter_cpus():
        path = cpu / "cpufreq" / "energy_performance_preference"
        if path.exists():
            write_text(path, mode)


def set_governor(governor: str = "powersave") -> None:
    for cpu in iter_cpus():
        path = cpu / "cpufreq" / "scaling_governor"
        if path.exists():
            write_text(path, governor)


def set_boost(enabled: bool) -> None:
    boost = Path("/sys/devices/system/cpu/cpufreq/boost")
    cpb = Path("/sys/devices/system/cpu/amd_pstate/cpb_boost")
    if boost.exists():
        write_text(boost, "1" if enabled else "0")
    elif cpb.exists():
        write_text(cpb, "1" if enabled else "0")


def set_min_freq_nonlinear(enabled: bool) -> None:
    set_freq_limits(0, 0, nonlinear=enabled)


def set_freq_limits(min_mhz: int, max_mhz: int, nonlinear: bool = False) -> None:
    sample = Path("/sys/devices/system/cpu/cpu0/cpufreq")
    if min_mhz > 0:
        min_khz = str(int(min_mhz) * 1000)
    elif nonlinear and (sample / "amd_pstate_lowest_nonlinear_freq").exists():
        min_khz = (sample / "amd_pstate_lowest_nonlinear_freq").read_text(encoding="utf-8").strip()
    else:
        min_khz = (sample / "cpuinfo_min_freq").read_text(encoding="utf-8").strip()
    if max_mhz > 0:
        max_khz = str(int(max_mhz) * 1000)
    else:
        max_khz = (sample / "cpuinfo_max_freq").read_text(encoding="utf-8").strip()
    if int(min_khz) > int(max_khz):
        min_khz = max_khz
    for cpu in iter_cpus():
        lo = cpu / "cpufreq" / "scaling_min_freq"
        hi = cpu / "cpufreq" / "scaling_max_freq"
        if hi.exists():
            write_text(hi, max_khz)
        if lo.exists():
            write_text(lo, min_khz)
