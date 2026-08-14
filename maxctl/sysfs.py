from __future__ import annotations

from pathlib import Path


def read_text(path: str | Path, default: str | None = None) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return default


def read_int(path: str | Path, default: int | None = None) -> int | None:
    raw = read_text(path)
    if raw is None:
        return default
    try:
        return int(raw.split()[0])
    except (ValueError, IndexError):
        return default


def write_text(path: str | Path, value: str) -> None:
    Path(path).write_text(value, encoding="utf-8")


def first_hwmon(name: str) -> Path | None:
    root = Path("/sys/class/hwmon")
    if not root.exists():
        return None
    for entry in sorted(root.iterdir()):
        if read_text(entry / "name") == name:
            return entry
    return None


def iter_cpus() -> list[Path]:
    cpu_root = Path("/sys/devices/system/cpu")
    cpus: list[Path] = []
    for entry in sorted(cpu_root.iterdir()):
        if not entry.name.startswith("cpu"):
            continue
        suffix = entry.name[3:]
        if suffix.isdigit():
            cpus.append(entry)
    return cpus
