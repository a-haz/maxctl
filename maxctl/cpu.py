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
    sample = Path("/sys/devices/system/cpu/cpu0/cpufreq")
    if enabled:
        src = sample / "amd_pstate_lowest_nonlinear_freq"
    else:
        src = sample / "cpuinfo_min_freq"
    if not src.exists():
        return
    value = src.read_text(encoding="utf-8").strip()
    for cpu in iter_cpus():
        path = cpu / "cpufreq" / "scaling_min_freq"
        if path.exists():
            write_text(path, value)
