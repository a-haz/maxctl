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


def apply_tdp(tdp_w: int, tctl_c: int, boost: bool, limits=None) -> str:
    binary = find_binary()
    if not binary:
        raise RuntimeError("ryzenadj is not installed")
    if limits is not None:
        stapm = limits.stapm_w * 1000
        fast = limits.fast_w * 1000
        slow = limits.slow_w * 1000
        tctl_c = limits.tctl_c
        extra = [
            f"--stapm-time={limits.stapm_time}",
            f"--slow-time={limits.slow_time}",
            f"--skin-temp-limit={limits.skin_w * 1000}",
        ]
    else:
        stapm = tdp_w * 1000
        if boost:
            fast = int(tdp_w * (32 / 28) * 1000)
            slow = min((tdp_w + 2) * 1000, fast)
        else:
            fast = stapm
            slow = stapm
        extra = []
    cmd = [
        binary,
        f"--stapm-limit={stapm}",
        f"--fast-limit={fast}",
        f"--slow-limit={slow}",
        f"--tctl-temp={tctl_c}",
        *extra,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        raise RuntimeError(output or f"ryzenadj exited {proc.returncode}")
    return output or " ".join(cmd)


def command_line(limits) -> str:
    return (
        "ryzenadj "
        f"--stapm-limit={limits.stapm_w * 1000} "
        f"--fast-limit={limits.fast_w * 1000} "
        f"--slow-limit={limits.slow_w * 1000} "
        f"--skin-temp-limit={limits.skin_w * 1000} "
        f"--tctl-temp={limits.tctl_c} "
        f"--stapm-time={limits.stapm_time} "
        f"--slow-time={limits.slow_time}"
    )


def power_shift(mode: str) -> str:
    flag = {"saving": "--power-saving", "max": "--max-performance"}.get(mode)
    if not flag:
        return ""
    binary = find_binary()
    if not binary:
        raise RuntimeError("ryzenadj is not installed")
    proc = subprocess.run([binary, flag], check=False, capture_output=True, text=True)
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        raise RuntimeError(output or f"ryzenadj {flag} failed")
    return output or flag


def info() -> str:
    binary = find_binary()
    if not binary:
        raise RuntimeError("ryzenadj is not installed")
    proc = subprocess.run([binary, "-i"], check=False, capture_output=True, text=True)
    return (proc.stdout + proc.stderr).strip()


def parse_info(text: str) -> dict:
    """Parse `ryzenadj -i` into meta + named rows (limit/value pairs)."""
    meta: dict[str, str] = {}
    rows: dict[str, dict] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or set(line) <= set("+-|"):
            continue
        if "|" not in line and ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
            continue
        if "|" not in line:
            continue
        parts = [part.strip() for part in line.strip("|").split("|")]
        if len(parts) < 2 or parts[0].lower() in ("name", ""):
            continue
        name = parts[0]
        raw_value = parts[1].strip().lower()
        if raw_value in ("nan", "n/a", ""):
            value = None
        else:
            try:
                value = float(parts[1])
            except ValueError:
                value = parts[1]
        rows[name] = {
            "value": value,
            "param": parts[2] if len(parts) > 2 else "",
        }
    return {"meta": meta, "rows": rows}


def read_table() -> dict:
    return parse_info(info())
