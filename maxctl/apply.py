from __future__ import annotations

from dataclasses import dataclass, field

from maxctl import alib, cpu, fan, gpu, ppd, ryzenadj
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


def apply_tdp(settings: Settings) -> tuple[str | None, str | None]:
    last_error = None
    if alib.available():
        try:
            alib.apply_tdp(settings.tdp_w, settings.tctl_c, settings.boost)
            return "alib", None
        except Exception as exc:
            last_error = f"ALIB: {exc}"
    if ryzenadj.available():
        try:
            ryzenadj.apply_tdp(settings.tdp_w, settings.tctl_c, settings.boost)
            return "ryzenadj", last_error
        except Exception as exc:
            last_error = f"{last_error + '; ' if last_error else ''}RyzenAdj: {exc}"
    return None, last_error or "No TDP backend (need acpi_call or ryzenadj)"


def apply_settings(settings: Settings, fan_tick: bool = False) -> ApplyResult:
    settings = settings.clamp()
    result = ApplyResult(ok=True)
    backend, tdp_error = apply_tdp(settings)
    result.tdp_backend = backend
    if tdp_error:
        result.errors.append(tdp_error)
    if backend is None:
        result.ok = False

    for label, fn in (
        ("cpu epp", lambda: cpu.set_epp(settings.epp)),
        ("cpu governor", cpu.set_governor),
        ("cpu boost", lambda: cpu.set_boost(settings.cpu_boost)),
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
            gpu.set_auto()
    except Exception as exc:
        result.errors.append(f"gpu: {exc}")

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
