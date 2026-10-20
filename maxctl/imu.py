"""Built-in IMU (Bosch BMI260) logging and still-test analysis.

Reads the kernel IIO driver through sysfs only; nothing is written to the
device. Recordings are CSV on the host clock:

  t_mono_ns, t_wall_ns : CLOCK_MONOTONIC midpoint of the reads, time.time_ns()
  gx, gy, gz           : angular rate, rad/s (sensor axes)
  ax, ay, az           : acceleration, m/s^2 (sensor axes)
  temp_c               : chip temperature in C, every tenth row (blank otherwise)

Analysis is standard library only: gyro bias with a motion check, noise,
Allan deviation, heading drift, tilt, and bias subtraction.
"""

from __future__ import annotations

import bisect
import csv
import datetime
import hashlib
import json
import math
import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

IIO_ROOT = Path("/sys/bus/iio/devices")
DEVICE_NAMES = ("bmi260", "bmi270")
COLUMNS = ("t_mono_ns", "t_wall_ns", "gx", "gy", "gz", "ax", "ay", "az", "temp_c")
AXES = ("x", "y", "z")
TAUS = (0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 40.0)
STANDARD_GRAVITY = 9.80665
TEMP_INVALID = -32768  # 0x8000, the BMI260 "no valid temperature" value


class ImuError(Exception):
    pass


# --------------------------------------------------------------------- device


def find_device(root: Path = IIO_ROOT) -> Path | None:
    if not root.is_dir():
        return None
    for entry in sorted(root.iterdir()):
        try:
            if (entry / "name").read_text(encoding="utf-8").strip() in DEVICE_NAMES:
                return entry
        except OSError:
            continue
    return None


def _read_float(path: Path) -> float:
    return float(path.read_text(encoding="utf-8").strip())


def device_meta(dev: Path) -> dict:
    meta = {
        "device": str(dev),
        "name": (dev / "name").read_text(encoding="utf-8").strip(),
        "anglvel_scale": _read_float(dev / "in_anglvel_scale"),
        "accel_scale": _read_float(dev / "in_accel_scale"),
    }
    for attr in ("in_anglvel_sampling_frequency", "in_accel_sampling_frequency"):
        path = dev / attr
        if path.exists():
            meta[attr] = path.read_text(encoding="utf-8").strip()
    return meta


def record(
    out: Path,
    seconds: float,
    rate: float = 100.0,
    dev: Path | None = None,
    progress=None,
) -> dict:
    """Poll the latest sysfs values at `rate` Hz for `seconds` into a CSV at `out`."""
    dev = dev or find_device()
    if dev is None:
        raise ImuError("no BMI260 IIO device (is the bmi270_i2c driver loaded with its firmware?)")
    meta = device_meta(dev)
    gscale, ascale = meta["anglvel_scale"], meta["accel_scale"]
    names = [f"in_anglvel_{a}_raw" for a in AXES] + [f"in_accel_{a}_raw" for a in AXES]
    fds = [os.open(dev / n, os.O_RDONLY) for n in names]
    temp_fd, toff, tscale = None, 0.0, 0.0
    if (dev / "in_temp_raw").exists():
        temp_fd = os.open(dev / "in_temp_raw", os.O_RDONLY)
        toff = _read_float(dev / "in_temp_offset")
        tscale = _read_float(dev / "in_temp_scale")
    out.parent.mkdir(parents=True, exist_ok=True)
    period = 1.0 / rate
    n_target = int(seconds * rate)
    try:
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(COLUMNS)
            t_next = time.monotonic()
            for i in range(n_target):
                t0 = time.monotonic_ns()
                raw = [int(os.pread(fd, 32, 0)) for fd in fds]
                temp = ""
                if temp_fd is not None and i % 10 == 0:
                    traw = int(os.pread(temp_fd, 32, 0))
                    if traw != TEMP_INVALID:
                        temp = f"{(traw + toff) * tscale / 1000.0:.3f}"
                t1 = time.monotonic_ns()
                w.writerow(
                    [(t0 + t1) // 2, time.time_ns()]
                    + [f"{r * gscale:.7f}" for r in raw[:3]]
                    + [f"{r * ascale:.6f}" for r in raw[3:]]
                    + [temp]
                )
                if progress and i and i % int(rate * 10) == 0:
                    progress(i / rate)
                t_next += period
                delay = t_next - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
    finally:
        for fd in fds + ([temp_fd] if temp_fd is not None else []):
            os.close(fd)
    return meta


# --------------------------------------------------------------------- files


@dataclass
class Recording:
    path: Path
    rows: list[dict]
    t: list[int]
    gyro: list[tuple[float, float, float]]
    accel: list[tuple[float, float, float]]
    temps: list[float] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return (self.t[-1] - self.t[0]) / 1e9


def load(path: str | Path) -> Recording:
    path = Path(path)
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ImuError(f"{path}: missing columns {missing}")
        rows = list(reader)
    if len(rows) < 100:
        raise ImuError(f"{path}: only {len(rows)} rows")
    t = [int(r["t_mono_ns"]) for r in rows]
    if any(b <= a for a, b in zip(t, t[1:])):
        raise ImuError(f"{path}: t_mono_ns is not strictly increasing")
    gyro = [(float(r["gx"]), float(r["gy"]), float(r["gz"])) for r in rows]
    accel = [(float(r["ax"]), float(r["ay"]), float(r["az"])) for r in rows]
    temps = [float(r["temp_c"]) for r in rows if (r.get("temp_c") or "").strip()]
    return Recording(path, rows, t, gyro, accel, temps)


def write_corrected(rec: Recording, out: Path, gyro_bias, accel_bias=(0.0, 0.0, 0.0)) -> None:
    """Copy of the recording with the biases subtracted; every row and column is kept."""
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for r, g, a in zip(rec.rows, rec.gyro, rec.accel):
            w.writerow(
                [r["t_mono_ns"], r["t_wall_ns"]]
                + [f"{g[k] - gyro_bias[k]:.7f}" for k in range(3)]
                + [f"{a[k] - accel_bias[k]:.6f}" for k in range(3)]
                + [r["temp_c"]]
            )


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------- maths


def _mean(v) -> float:
    return math.fsum(v) / len(v)


def _std(v) -> float:
    m = _mean(v)
    return math.sqrt(math.fsum((x - m) ** 2 for x in v) / max(1, len(v) - 1))


def _col(rows, k: int) -> list[float]:
    return [r[k] for r in rows]


def integrate(rates, t_ns, bias=(0.0, 0.0, 0.0)) -> list[float]:
    """Trapezoid integral of (N, 3) rates over t_ns, after subtracting bias -> per-axis total."""
    out = [0.0, 0.0, 0.0]
    for i in range(len(t_ns) - 1):
        dt = (t_ns[i + 1] - t_ns[i]) / 1e9
        a, b = rates[i], rates[i + 1]
        for k in range(3):
            out[k] += 0.5 * ((a[k] - bias[k]) + (b[k] - bias[k])) * dt
    return out


def allan_dev(x, fs: float, taus=TAUS) -> dict[float, float]:
    """Overlapping Allan deviation of a rate series x (same units as x)."""
    n = len(x)
    c = [0.0]
    for v in x:
        c.append(c[-1] + v)
    out = {}
    for tau in taus:
        m = int(round(tau * fs))
        if m < 1 or 2 * m >= n:
            continue
        s = 0.0
        k = 0
        for i in range(n - 2 * m + 1):
            a1 = (c[i + m] - c[i]) / m
            a2 = (c[i + 2 * m] - c[i + m]) / m
            s += (a2 - a1) ** 2
            k += 1
        out[tau] = math.sqrt(s / (2 * k))
    return out


def _deg(v) -> list[float]:
    return [round(math.degrees(x), 6) for x in v]


def _r6(v) -> list[float]:
    return [round(x, 6) for x in v]


def _up(accel_mean) -> tuple[int, float, list[float], float, float]:
    """(axis index, sign, unit vector, |g|, tilt in degrees) from the mean specific force."""
    norm = math.sqrt(sum(x * x for x in accel_mean))
    if norm == 0.0:
        raise ImuError(
            "accelerometer reads all zeros; the sensor is not running "
            "(usually the bmi260 init firmware did not load, see dmesg)"
        )
    idx = max(range(3), key=lambda k: abs(accel_mean[k]))
    sign = 1.0 if accel_mean[idx] > 0 else -1.0
    unit = [x / norm for x in accel_mean]
    tilt = math.degrees(math.acos(min(1.0, abs(accel_mean[idx]) / norm)))
    return idx, sign, unit, norm, tilt


# --------------------------------------------------------------------- analysis


def summary(rec: Recording, gyro_bias=(0.0, 0.0, 0.0)) -> dict:
    """Quick numbers for any recording: rate, gaps, integrated angle, up axis."""
    n = len(rec.t)
    dur = rec.duration_s
    gaps = [(b - a) / 1e6 for a, b in zip(rec.t, rec.t[1:])]
    am = [_mean(_col(rec.accel, k)) for k in range(3)]
    idx, sign, _, norm, _ = _up(am)
    return {
        "file": str(rec.path),
        "samples": n,
        "duration_s": round(dur, 3),
        "rate_hz": round((n - 1) / dur, 2) if dur > 0 else None,
        "max_gap_ms": round(max(gaps), 2) if gaps else None,
        "gyro_mean_deg_s": [round(math.degrees(_mean(_col(rec.gyro, k))), 5) for k in range(3)],
        "gyro_std_deg_s": [round(math.degrees(_std(_col(rec.gyro, k))), 5) for k in range(3)],
        "bias_subtracted_deg_s": [round(math.degrees(b), 5) for b in gyro_bias],
        "integrated_angle_deg": [round(math.degrees(x), 3) for x in integrate(rec.gyro, rec.t, gyro_bias)],
        "accel_mean_m_s2": [round(x, 4) for x in am],
        "accel_norm_m_s2": round(norm, 4),
        "up_axis": ("+" if sign > 0 else "-") + AXES[idx],
        "temp_c_first_last": [rec.temps[0], rec.temps[-1]] if rec.temps else None,
    }


def _motion_keep(t_ns, gyro, accel, gyro_dps: float, accel_dev: float, guard_s: float):
    """Flag samples off the window median; returns (keep mask, flagged count)."""
    med = [statistics.median(_col(gyro, k)) for k in range(3)]
    norms = [math.sqrt(a[0] ** 2 + a[1] ** 2 + a[2] ** 2) for a in accel]
    nmed = statistics.median(norms)
    lim = math.radians(gyro_dps)
    flagged = [
        max(abs(g[k] - med[k]) for k in range(3)) > lim or abs(nm - nmed) > accel_dev
        for g, nm in zip(gyro, norms)
    ]
    ts = [(x - t_ns[0]) / 1e9 for x in t_ns]
    bad = [s for s, f in zip(ts, flagged) if f]
    keep = []
    for s, f in zip(ts, flagged):
        if f:
            keep.append(False)
            continue
        j = bisect.bisect_left(bad, s)
        near = min((abs(s - bad[i]) for i in (j - 1, j) if 0 <= i < len(bad)), default=math.inf)
        keep.append(near > guard_s)
    return keep, sum(flagged)


def analyze(
    rec: Recording,
    skip: float = 15.0,
    tail: float = 0.0,
    window: tuple[float, float] | None = None,
    motion_gyro_dps: float = 1.0,
    motion_accel: float = 0.5,
    guard: float = 0.5,
    g: float = STANDARD_GRAVITY,
) -> dict:
    """Still-test analysis: gyro bias with a motion check, noise, Allan deviation and drift."""
    rel = [(x - rec.t[0]) / 1e9 for x in rec.t]
    dur = rel[-1]
    lo, hi = window if window else (skip, dur - tail)
    idx = [i for i, s in enumerate(rel) if lo <= s <= hi]
    if len(idx) < 100:
        raise ImuError(f"window [{lo}, {hi}] s holds only {len(idx)} samples")
    tw = [rec.t[i] for i in idx]
    gw = [rec.gyro[i] for i in idx]
    aw = [rec.accel[i] for i in idx]
    fs = (len(tw) - 1) / ((tw[-1] - tw[0]) / 1e9)

    keep, n_flagged = _motion_keep(tw, gw, aw, motion_gyro_dps, motion_accel, guard)
    flagged_frac = n_flagged / len(tw)
    gk = [x for x, k in zip(gw, keep) if k]
    ak = [x for x, k in zip(aw, keep) if k]
    tk = [x for x, k in zip(tw, keep) if k]
    n = len(gk)
    if n < 100:
        raise ImuError(f"only {n} samples left after the motion check")

    cols = [_col(gk, k) for k in range(3)]
    bias = [_mean(c) for c in cols]
    std = [_std(c) for c in cols]
    half = n // 2
    b1 = [_mean(c[:half]) for c in cols]
    b2 = [_mean(c[half:]) for c in cols]
    # holdout drift: bias from one half integrated over the other half
    drift_2nd = integrate(gk[half:], tk[half:], b1)
    drift_1st = integrate(gk[:half], tk[:half], b2)
    w2 = (tk[-1] - tk[half]) / 1e9
    w1 = (tk[half - 1] - tk[0]) / 1e9
    raw_drift = integrate(gk, tk)
    used_s = (tk[-1] - tk[0]) / 1e9
    tc = [(x - tk[0]) / 1e9 for x in tk]
    tm = _mean(tc)
    sxx = math.fsum((x - tm) ** 2 for x in tc)
    slope = [math.fsum((x - tm) * (v - bias[k]) for x, v in zip(tc, cols[k])) / sxx for k in range(3)]

    am = [_mean(_col(ak, k)) for k in range(3)]
    up_idx, up_sign, e_up, gnorm, tilt = _up(am)
    axis_unit = [0.0, 0.0, 0.0]
    axis_unit[up_idx] = up_sign
    accel_bias_level = [am[k] - g * axis_unit[k] for k in range(3)]
    b_up = sum(bias[k] * e_up[k] for k in range(3))
    up_rate = [sum(x[k] * e_up[k] for k in range(3)) for x in gk]

    allan = {
        "g" + AXES[k]: {str(tau): round(math.degrees(v), 6) for tau, v in allan_dev(cols[k], fs).items()}
        for k in range(3)
    }
    allan_up = {str(tau): round(math.degrees(v), 6) for tau, v in allan_dev(up_rate, fs).items()}

    def about_up(v):
        return round(math.degrees(sum(v[k] * e_up[k] for k in range(3))), 4)

    return {
        "file": str(rec.path),
        "window_s": [round(lo, 3), round(hi, 3)],
        "samples_in_window": len(tw),
        "samples_used": n,
        "rate_hz": round(fs, 2),
        "motion_check": {
            "gyro_dev_dps": motion_gyro_dps,
            "accel_norm_dev_m_s2": motion_accel,
            "guard_s": guard,
            "flagged_samples": n_flagged,
            "flagged_fraction": round(flagged_frac, 5),
            "status": "STILL" if flagged_frac <= 0.01 else "NOT_STILL",
        },
        "gyro_bias_rad_s": bias,
        "gyro_bias_deg_s": _deg(bias),
        "gyro_bias_median_deg_s": _deg([statistics.median(c) for c in cols]),
        "gyro_noise_std_deg_s": _deg(std),
        "gyro_bias_stderr_deg_s": _deg([s / math.sqrt(n) for s in std]),
        "gyro_bias_split_half_diff_deg_s": _deg([b2[k] - b1[k] for k in range(3)]),
        "gyro_trend_deg_s_per_min": _deg([s * 60.0 for s in slope]),
        "allan_dev_deg_s": allan,
        "allan_dev_about_up_deg_s": allan_up,
        "heading_drift": {
            "used_s": round(used_s, 2),
            "no_bias_removed_deg": _deg(raw_drift),
            "no_bias_removed_deg_per_min": _deg([x / used_s * 60.0 for x in raw_drift]),
            "no_bias_removed_about_up_deg": about_up(raw_drift),
            "holdout_first_half_bias_on_second_half_deg": _deg(drift_2nd),
            "holdout_second_half_s": round(w2, 2),
            "holdout_second_half_bias_on_first_half_deg": _deg(drift_1st),
            "holdout_first_half_s": round(w1, 2),
            "holdout_about_up_deg": [about_up(drift_2nd), about_up(drift_1st)],
        },
        "gyro_max_abs_rate_deg_s": [
            round(math.degrees(max(abs(x - bias[k]) for x in cols[k])), 3) for k in range(3)
        ],
        "up_axis": ("+" if up_sign > 0 else "-") + AXES[up_idx],
        "up_unit_vector": _r6(e_up),
        "gyro_bias_about_up_deg_s": round(math.degrees(b_up), 6),
        "accel_mean_m_s2": _r6(am),
        "accel_norm_m_s2": round(gnorm, 5),
        "tilt_from_up_axis_deg": round(tilt, 4),
        "accel_max_dev_m_s2": [round(max(abs(x - am[k]) for x in _col(ak, k)), 4) for k in range(3)],
        "g_m_s2": g,
        "accel_bias_if_level_m_s2": _r6(accel_bias_level),
        "temp_c_first_last": [rec.temps[0], rec.temps[-1]] if rec.temps else None,
    }


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")
