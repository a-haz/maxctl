from __future__ import annotations

import subprocess

from maxctl.const import PPD_MODES


def set_profile(name: str) -> None:
    if name not in PPD_MODES:
        raise ValueError(f"invalid power profile: {name}")
    proc = subprocess.run(
        ["powerprofilesctl", "set", name],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "powerprofilesctl failed")


def get_profile() -> str | None:
    proc = subprocess.run(
        ["powerprofilesctl", "get"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None
