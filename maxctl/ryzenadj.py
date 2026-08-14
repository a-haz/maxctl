from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

CANDIDATES = (
    "ryzenadj",
    "/usr/local/bin/ryzenadj",
    "/opt/maxctl/bin/ryzenadj",
    str(Path(__file__).resolve().parent.parent / "vendor" / "RyzenAdj" / "build" / "ryzenadj"),
)


def find_binary() -> str | None:
    for candidate in CANDIDATES:
        path = shutil.which(candidate) if "/" not in candidate else candidate
        if path and Path(path).is_file():
            return path
    return None


def available() -> bool:
    return find_binary() is not None


def apply_tdp(tdp_w: int, tctl_c: int, boost: bool) -> str:
    binary = find_binary()
    if not binary:
        raise RuntimeError("ryzenadj is not installed")
    stapm = tdp_w * 1000
    if boost:
        fast = int(tdp_w * (32 / 28) * 1000)
        slow = min((tdp_w + 2) * 1000, fast)
    else:
        fast = stapm
        slow = stapm
    cmd = [
        binary,
        f"--stapm-limit={stapm}",
        f"--fast-limit={fast}",
        f"--slow-limit={slow}",
        f"--tctl-temp={tctl_c}",
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        raise RuntimeError(output or f"ryzenadj exited {proc.returncode}")
    return output or " ".join(cmd)


def info() -> str:
    binary = find_binary()
    if not binary:
        raise RuntimeError("ryzenadj is not installed")
    proc = subprocess.run([binary, "-i"], check=False, capture_output=True, text=True)
    return (proc.stdout + proc.stderr).strip()
