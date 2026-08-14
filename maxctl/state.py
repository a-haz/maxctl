from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from maxctl.const import CONFIG_PATH, STATE_PATH, USER_CONFIG_PATH
from maxctl.profiles import Settings, builtin


@dataclass
class AppConfig:
    ac_profile: str = "balanced"
    battery_profile: str = "silent"
    auto_switch: bool = True
    reapply_sec: float = 4.0
    unlock: bool = False
    custom: dict = field(default_factory=dict)

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
