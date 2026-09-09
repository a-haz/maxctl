"""Read-only inventory of TDP backends, kernel knobs, and sysfs."""

from __future__ import annotations

from pathlib import Path

from maxctl import alib, ryzenadj
from maxctl.sysfs import first_hwmon, read_text


def charge_limit_path() -> Path | None:
    root = Path("/sys/class/power_supply")
    if not root.exists():
        return None
    for entry in sorted(root.iterdir()):
        path = entry / "charge_control_end_threshold"
        if path.exists():
            return path
    return None


def read_charge_limit() -> int | None:
    path = charge_limit_path()
    if path is None:
        return None
    raw = read_text(path)
    if raw is None:
        return None
    try:
        return int(raw.split()[0])
    except (ValueError, IndexError):
        return None


def set_charge_limit(percent: int) -> None:
    path = charge_limit_path()
    if path is None:
        raise RuntimeError("no charge_control_end_threshold sysfs")
    path.write_text(str(int(percent)), encoding="utf-8")


def amd_pstate_status() -> str | None:
    for path in (
        "/sys/module/amd_pstate/parameters/status",
        "/sys/devices/system/cpu/amd_pstate/status",
    ):
        value = read_text(path)
        if value:
            return value
    return None


def collect() -> dict:
    cmdline = read_text("/proc/cmdline") or ""
    modules = read_text("/proc/modules") or ""
    charge = charge_limit_path()
    return {
        "alib": alib.available(),
        "acpi_call": Path("/proc/acpi/call").exists(),
        "ryzenadj": ryzenadj.find_binary(),
        "iomem_relaxed": "iomem=relaxed" in cmdline,
        "amd_pstate": amd_pstate_status(),
        "ryzen_smu": "ryzen_smu " in modules or modules.startswith("ryzen_smu"),
        "gpdfan": first_hwmon("gpdfan") is not None,
        "charge_limit": read_charge_limit(),
        "charge_limit_path": str(charge) if charge else None,
        "kernel": read_text("/proc/sys/kernel/osrelease"),
        "cmdline": cmdline,
    }


def format_report(data: dict | None = None, helper: bool | None = None) -> str:
    info = data or collect()
    rows = [
        ("Helper", "online" if helper else "offline" if helper is not None else "?"),
        ("ALIB / acpi_call", "yes" if info.get("alib") else "no"),
        ("RyzenAdj", info.get("ryzenadj") or "not found"),
        ("ryzen_smu", "yes" if info.get("ryzen_smu") else "no"),
        ("iomem=relaxed", "yes" if info.get("iomem_relaxed") else "no"),
        ("amd_pstate", info.get("amd_pstate") or "unknown"),
        ("gpdfan", "yes" if info.get("gpdfan") else "no"),
        ("Charge limit", f"{info['charge_limit']}%" if info.get("charge_limit") is not None else "not exposed"),
        ("Kernel", info.get("kernel") or "?"),
    ]
    return "\n".join(f"{name:16} {value}" for name, value in rows)
