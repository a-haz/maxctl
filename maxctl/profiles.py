from __future__ import annotations

from dataclasses import asdict, dataclass, field

from maxctl.const import (
    EGPU_IGPU_MAX_MHZ,
    EPP_MODES,
    GOVERNORS,
    GPU_DPM_MODES,
    GPU_SCLK_MAX,
    GPU_SCLK_MIN,
    POWER_SHIFT_MODES,
    TCTL_DEFAULT,
    TDP_DEFAULT,
    TDP_MIN,
    TDP_SAFE_MAX,
    TDP_UNLOCK_MAX,
)
from maxctl.fan import CurvePoint, DEFAULT_CURVES


@dataclass
class Limits:
    stapm_w: int
    fast_w: int
    slow_w: int
    skin_w: int
    tctl_c: int
    stapm_time: int
    slow_time: int


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
    advanced: bool = False
    stapm_w: int = TDP_DEFAULT
    fast_w: int = 32
    slow_w: int = 30
    skin_w: int = TDP_DEFAULT
    stapm_time: int = 100
    slow_time: int = 10
    fast_boost_w: int = 0
    cpu_min_mhz: int = 0
    cpu_max_mhz: int = 0
    nonlinear_min: bool = False
    fan_slew: int = 12
    governor: str = "powersave"
    gpu_dpm: str = "auto"
    charge_limit: int = 0
    power_shift: str = ""

    def clamp(self) -> Settings:
        tmax = TDP_UNLOCK_MAX if self.unlock else TDP_SAFE_MAX
        self.tdp_w = max(TDP_MIN, min(tmax, int(self.tdp_w)))
        self.tctl_c = max(70, min(100, int(self.tctl_c)))
        self.epp = self.epp if self.epp in EPP_MODES else "balance_performance"
        self.gpu_min = max(GPU_SCLK_MIN, min(GPU_SCLK_MAX, int(self.gpu_min)))
        self.gpu_max = max(self.gpu_min, min(GPU_SCLK_MAX, int(self.gpu_max)))
        self.fan_pwm = max(0, min(255, int(self.fan_pwm)))
        self.stapm_w = max(TDP_MIN, min(tmax, int(self.stapm_w)))
        self.fast_w = max(TDP_MIN, min(tmax + 8, int(self.fast_w)))
        self.slow_w = max(TDP_MIN, min(tmax + 4, int(self.slow_w)))
        self.skin_w = max(TDP_MIN, min(tmax, int(self.skin_w)))
        self.stapm_time = max(1, min(300, int(self.stapm_time)))
        self.slow_time = max(1, min(30, int(self.slow_time)))
        self.fast_boost_w = max(0, min(15, int(self.fast_boost_w)))
        self.cpu_min_mhz = max(0, min(5500, int(self.cpu_min_mhz)))
        self.cpu_max_mhz = max(0, min(5500, int(self.cpu_max_mhz)))
        self.fan_slew = max(2, min(64, int(self.fan_slew)))
        self.charge_limit = int(self.charge_limit)
        if self.charge_limit != 0:
            self.charge_limit = max(50, min(100, self.charge_limit))
        if self.gpu_mode not in ("auto", "range"):
            self.gpu_mode = "auto"
        if self.fan_mode not in ("auto", "curve", "manual"):
            self.fan_mode = "auto"
        if self.governor not in GOVERNORS:
            self.governor = "powersave"
        if self.gpu_dpm not in GPU_DPM_MODES:
            self.gpu_dpm = "auto"
        if self.power_shift not in POWER_SHIFT_MODES:
            self.power_shift = ""
        return self

    def limits(self) -> Limits:
        tmax = TDP_UNLOCK_MAX if self.unlock else TDP_SAFE_MAX
        if self.advanced:
            stapm = min(tmax, self.stapm_w)
            fast = min(tmax + 8, max(stapm, self.fast_w))
            slow = min(fast, max(stapm, self.slow_w))
            skin = min(tmax, self.skin_w)
        elif self.boost:
            stapm = self.tdp_w
            fast = min(tmax + 8, int(self.tdp_w * 32 / 28) + self.fast_boost_w)
            slow = min(fast, self.tdp_w + 2)
            skin = self.tdp_w
        else:
            stapm = fast = slow = skin = self.tdp_w
        return Limits(
            stapm_w=stapm,
            fast_w=fast,
            slow_w=slow,
            skin_w=skin,
            tctl_c=self.tctl_c,
            stapm_time=self.stapm_time,
            slow_time=self.slow_time,
        )

    def apply_recipe(self, name: str) -> Settings:
        tdp = self.tdp_w
        self.advanced = True
        self.profile = "custom"
        if name == "flat":
            self.stapm_w = self.fast_w = self.slow_w = self.skin_w = tdp
        elif name == "burst":
            self.stapm_w = tdp
            self.fast_w = tdp + 8
            self.slow_w = tdp + 2
            self.skin_w = tdp
        elif name == "efficiency":
            self.stapm_w = max(TDP_MIN, tdp - 6)
            self.fast_w = tdp + 2
            self.slow_w = tdp
            self.skin_w = max(TDP_MIN, tdp - 4)
        else:
            raise KeyError(name)
        return self.clamp()

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
    # External GPU attached: Balanced-tier CPU budget, iGPU pinned low so the
    # shared STAPM/PPT budget is spent on cores instead of the 890M. Kept at
    # the 28W tier on purpose: the 35W / EPP-performance tier is where this
    # chassis's SMU hangs (black screen), the 12W tier never does.
    "egpu": Settings(
        profile="egpu",
        tdp_w=28,
        tctl_c=90,
        boost=True,
        cpu_boost=True,
        epp="balance_performance",
        ppd="balanced",
        gpu_mode="range",
        gpu_min=GPU_SCLK_MIN,
        gpu_max=EGPU_IGPU_MAX_MHZ,
        fan_mode="auto",
        fan_curve=[p.__dict__ for p in DEFAULT_CURVES["balanced"]],
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
