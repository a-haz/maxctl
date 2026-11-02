from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from maxctl.const import CONFIG_PATH, STATE_PATH, USER_CONFIG_PATH, USER_PRESETS_PATH
from maxctl.profiles import Settings, builtin

REAPPLY_MIN_SEC = 1.0
REAPPLY_MAX_SEC = 3600.0


def _number(value) -> float | None:
    """A JSON number as a float (huge values become +-inf); None for NaN or a non-number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        number = math.inf if value > 0 else -math.inf
    return None if math.isnan(number) else number


@dataclass
class AppConfig:
    ac_profile: str = "balanced"
    battery_profile: str = "silent"
    auto_switch: bool = True
    reapply_sec: float = 4.0
    unlock: bool = False
    custom: dict = field(default_factory=dict)
    # Applied while an external GPU is attached (overrides AC / battery choice).
    egpu_profile: str = "egpu"
    egpu_switch: bool = True

    def auto_profile(self, on_ac: bool, egpu: bool = False) -> str:
        """Profile name the daemon should apply for this power / eGPU state."""
        if egpu and self.egpu_switch and self.egpu_profile:
            return self.egpu_profile
        return self.ac_profile if on_ac else self.battery_profile

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> AppConfig:
        """Config from parsed JSON (a file, set_config or an import bundle).

        A value of the wrong type keeps the default: the root helper reads
        these fields in its loops, and a string reapply_sec used to kill the
        re-apply thread.
        """
        cfg = cls()
        if not isinstance(data, dict):
            return cfg
        for key, default in asdict(cfg).items():
            if key not in data:
                continue
            value = data[key]
            if isinstance(default, bool):
                if isinstance(value, bool) or (type(value) is int and value in (0, 1)):
                    setattr(cfg, key, bool(value))
            elif isinstance(default, float):
                number = _number(value)
                if number is not None:
                    setattr(cfg, key, number)
            elif isinstance(value, type(default)):
                setattr(cfg, key, value)
        cfg.reapply_sec = max(REAPPLY_MIN_SEC, min(REAPPLY_MAX_SEC, cfg.reapply_sec))
        return cfg


def _load_json(path: Path) -> dict:
    """The JSON object in ``path``, or {} if it is missing, unreadable or not an object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config() -> AppConfig:
    data: dict = {}
    for raw in (CONFIG_PATH, USER_CONFIG_PATH):
        path = Path(raw).expanduser()
        if path.exists():
            data.update(_load_json(path))
    return AppConfig.from_dict(data)


def save_system_config(cfg: AppConfig) -> None:
    path = Path(CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg.to_dict(), indent=2) + "\n", encoding="utf-8")


def load_settings(cfg: AppConfig | None = None, on_ac: bool | None = None) -> Settings:
    cfg = cfg or load_config()
    state = _load_json(Path(STATE_PATH))
    if state.get("settings"):
        try:
            settings = Settings.from_dict(state["settings"])
        except (AttributeError, TypeError, ValueError, ArithmeticError):
            settings = None  # corrupt state file: fall back to the configured profile
        if settings is not None:
            settings.unlock = cfg.unlock
            return settings.clamp()
    name = cfg.ac_profile if on_ac else cfg.battery_profile
    if name == "custom" and cfg.custom:
        return Settings.from_dict({**cfg.custom, "unlock": cfg.unlock})
    try:
        return builtin(name, unlock=cfg.unlock)
    except KeyError:
        return builtin("balanced", unlock=cfg.unlock)


def save_settings(settings: Settings) -> None:
    path = Path(STATE_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"settings": settings.to_dict()}, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        user = Path.home() / ".config" / "maxctl" / "state.json"
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text(
            json.dumps({"settings": settings.to_dict()}, indent=2) + "\n",
            encoding="utf-8",
        )


def _presets_path() -> Path:
    return Path(USER_PRESETS_PATH).expanduser()


def load_presets() -> dict:
    return _load_json(_presets_path())


def save_preset(name: str, bundle: dict) -> None:
    name = name.strip()
    if not name:
        raise ValueError("preset name required")
    presets = load_presets()
    presets[name] = bundle
    path = _presets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(presets, indent=2) + "\n", encoding="utf-8")


def delete_preset(name: str) -> None:
    presets = load_presets()
    presets.pop(name, None)
    path = _presets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(presets, indent=2) + "\n", encoding="utf-8")
