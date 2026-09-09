from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from maxctl.const import CONFIG_PATH, STATE_PATH, USER_CONFIG_PATH, USER_PRESETS_PATH
from maxctl.profiles import Settings, builtin


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
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_config() -> AppConfig:
    data: dict = {}
    for raw in (CONFIG_PATH, USER_CONFIG_PATH):
        path = Path(raw).expanduser()
        if path.exists():
            data.update(_load_json(path))
    return AppConfig.from_dict(data)


def save_user_config(cfg: AppConfig) -> None:
    path = Path(USER_CONFIG_PATH).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg.to_dict(), indent=2) + "\n", encoding="utf-8")


def save_system_config(cfg: AppConfig) -> None:
    path = Path(CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg.to_dict(), indent=2) + "\n", encoding="utf-8")


def load_settings(cfg: AppConfig | None = None, on_ac: bool | None = None) -> Settings:
    cfg = cfg or load_config()
    state = _load_json(Path(STATE_PATH))
    if state.get("settings"):
        settings = Settings.from_dict(state["settings"])
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
    data = _load_json(_presets_path())
    return data if isinstance(data, dict) else {}


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
