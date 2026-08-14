from __future__ import annotations

from dataclasses import asdict, dataclass, field

from maxctl.const import (
    EPP_MODES,
    GPU_SCLK_MAX,
    GPU_SCLK_MIN,
    TCTL_DEFAULT,
    TDP_DEFAULT,
    TDP_MIN,
    TDP_SAFE_MAX,
    TDP_UNLOCK_MAX,
)
from maxctl.fan import CurvePoint, DEFAULT_CURVES


@dataclass
class Settings:
    profile: str = "balanced"
    tdp_w: int = TDP_DEFAULT
    tctl_c: int = TCTL_DEFAULT
    boost: bool = True
    cpu_boost: bool = True
    epp: str = "balance_performance"
    ppd: str = "balanced"
    gpu_mode: str = "auto"
    gpu_min: int = GPU_SCLK_MIN
    gpu_max: int = GPU_SCLK_MAX
    fan_mode: str = "auto"
    fan_pwm: int = 128
    fan_curve: list[dict] = field(default_factory=list)
    unlock: bool = False

    def clamp(self) -> Settings:
        tmax = TDP_UNLOCK_MAX if self.unlock else TDP_SAFE_MAX
        self.tdp_w = max(TDP_MIN, min(tmax, int(self.tdp_w)))
        self.tctl_c = max(70, min(100, int(self.tctl_c)))
        self.epp = self.epp if self.epp in EPP_MODES else "balance_performance"
        self.gpu_min = max(GPU_SCLK_MIN, min(GPU_SCLK_MAX, int(self.gpu_min)))
        self.gpu_max = max(self.gpu_min, min(GPU_SCLK_MAX, int(self.gpu_max)))
        self.fan_pwm = max(0, min(255, int(self.fan_pwm)))
        if self.gpu_mode not in ("auto", "range"):
            self.gpu_mode = "auto"
        if self.fan_mode not in ("auto", "curve", "manual"):
            self.fan_mode = "auto"
        return self

    def curve_points(self) -> list[CurvePoint]:
        if self.fan_curve:
            return [CurvePoint(int(p["temp_c"]), int(p["percent"])) for p in self.fan_curve]
        return list(DEFAULT_CURVES.get(self.profile, DEFAULT_CURVES["balanced"]))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Settings:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known).clamp()


PROFILES: dict[str, Settings] = {
    "silent": Settings(
        profile="silent",
        tdp_w=12,
        tctl_c=80,
        boost=False,
        cpu_boost=False,
        epp="power",
        ppd="power-saver",
        fan_mode="auto",
        fan_curve=[p.__dict__ for p in DEFAULT_CURVES["silent"]],
    ),
    "balanced": Settings(
        profile="balanced",
        tdp_w=28,
        tctl_c=90,
        boost=True,
        cpu_boost=True,
        epp="balance_performance",
        ppd="balanced",
        fan_mode="auto",
        fan_curve=[p.__dict__ for p in DEFAULT_CURVES["balanced"]],
    ),
    "performance": Settings(
        profile="performance",
        tdp_w=35,
        tctl_c=95,
        boost=True,
        cpu_boost=True,
        epp="performance",
        ppd="performance",
        fan_mode="auto",
        fan_curve=[p.__dict__ for p in DEFAULT_CURVES["performance"]],
    ),
    "turbo": Settings(
        profile="turbo",
        tdp_w=40,
        tctl_c=98,
        boost=True,
        cpu_boost=True,
        epp="performance",
        ppd="performance",
        fan_mode="auto",
        fan_curve=[p.__dict__ for p in DEFAULT_CURVES["performance"]],
    ),
}


def builtin(name: str, unlock: bool = False) -> Settings:
    if name not in PROFILES:
        raise KeyError(name)
    settings = Settings.from_dict(PROFILES[name].to_dict())
    settings.unlock = unlock
    if name == "turbo" and unlock:
        settings.tdp_w = TDP_UNLOCK_MAX
    return settings.clamp()
