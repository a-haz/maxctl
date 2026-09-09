from __future__ import annotations

from dataclasses import dataclass, field

from maxctl import alib, cpu, diagnose, fan, gpu, ppd, ryzenadj
from maxctl.profiles import Settings


@dataclass
class ApplyResult:
    ok: bool
    tdp_backend: str | None = None
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "tdp_backend": self.tdp_backend,
            "errors": self.errors,
            "notes": self.notes,
        }


def apply_tdp(settings: Settings) -> tuple[str | None, str | None, str | None]:
    last_error = None
    limits = settings.limits()
    if alib.available():
        try:
            detail = alib.apply_tdp(settings.tdp_w, settings.tctl_c, settings.boost, limits=limits)
            return "alib", None, detail
        except Exception as exc:
            last_error = f"ALIB: {exc}"
    if ryzenadj.available():
        try:
            detail = ryzenadj.apply_tdp(settings.tdp_w, settings.tctl_c, settings.boost, limits=limits)
            return "ryzenadj", last_error, detail
        except Exception as exc:
            last_error = f"{last_error + '; ' if last_error else ''}RyzenAdj: {exc}"
    return None, last_error or "No TDP backend (need acpi_call or ryzenadj)", None


def apply_settings(settings: Settings, fan_tick: bool = False) -> ApplyResult:
    settings = settings.clamp()
    result = ApplyResult(ok=True)
    backend, tdp_error, detail = apply_tdp(settings)
    result.tdp_backend = backend
    if tdp_error:
        result.errors.append(tdp_error)
    if detail:
        result.notes.append(detail)
    result.notes.append(ryzenadj.command_line(settings.limits()))
    if backend is None:
        result.ok = False

    for label, fn in (
        ("cpu epp", lambda: cpu.set_epp(settings.epp)),
        ("cpu governor", lambda: cpu.set_governor(settings.governor)),
        ("cpu boost", lambda: cpu.set_boost(settings.cpu_boost)),
        (
            "cpu freq",
            lambda: cpu.set_freq_limits(
                settings.cpu_min_mhz,
                settings.cpu_max_mhz,
                nonlinear=settings.nonlinear_min,
            ),
        ),
        ("ppd", lambda: ppd.set_profile(settings.ppd)),
    ):
        try:
            fn()
        except Exception as exc:
            result.errors.append(f"{label}: {exc}")

    try:
        if settings.gpu_mode == "range":
            gpu.set_sclk_range(settings.gpu_min, settings.gpu_max)
        else:
            gpu.set_dpm(settings.gpu_dpm)
    except Exception as exc:
        result.errors.append(f"gpu: {exc}")

    if settings.power_shift:
        try:
            note = ryzenadj.power_shift(settings.power_shift)
            if note:
                result.notes.append(note)
        except Exception as exc:
            result.errors.append(f"power shift: {exc}")

    if settings.charge_limit:
        try:
            diagnose.set_charge_limit(settings.charge_limit)
        except Exception as exc:
            result.errors.append(f"charge limit: {exc}")

    if not fan_tick:
        try:
            if settings.fan_mode == "auto":
                fan.set_auto()
            elif settings.fan_mode == "manual":
                fan.set_manual_pwm(settings.fan_pwm)
        except Exception as exc:
            result.errors.append(f"fan: {exc}")

    if result.errors and backend is None:
        result.ok = False
    return result
