from __future__ import annotations

import json
import os
import signal
import socket
import stat
import threading
import time
from pathlib import Path

from maxctl import diagnose, ryzenadj
from maxctl.apply import apply_settings
from maxctl.const import SOCKET_PATH
from maxctl.fan import FanController
from maxctl.hardware import detect_device, detect_egpu, read_telemetry
from maxctl.profiles import PROFILES, Settings, builtin
from maxctl.state import AppConfig, load_config, load_settings, save_settings, save_system_config


class Daemon:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.settings = load_settings(self.cfg)
        self.settings.unlock = self.cfg.unlock
        self.fan = FanController()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.last_ac: bool | None = None
        self.last_egpu: bool | None = None
        self.last_apply: dict = {}
        self.tdp_backend: str | None = None
        self._smu: dict = {}
        self._smu_t = 0.0

    def apply(self, settings: Settings, persist: bool = True) -> dict:
        settings = settings.clamp()
        result = apply_settings(settings)
        with self.lock:
            self.settings = settings
            self.last_apply = result.to_dict()
            self.tdp_backend = result.tdp_backend
        if persist:
            save_settings(settings)
        return result.to_dict()

    def profile_settings(self, name: str) -> Settings | None:
        if name == "custom" and self.cfg.custom:
            return Settings.from_dict({**self.cfg.custom, "unlock": self.cfg.unlock})
        try:
            return builtin(name, unlock=self.cfg.unlock)
        except KeyError:
            return None

    def boot_settings(self) -> Settings:
        """Settings to apply at daemon start.

        OCuLink eGPUs are attached before power-on, so there is never a
        hot-plug edge to catch: decide at boot instead. Persisted settings win
        when no eGPU is present or when the eGPU profile is already active
        (keeps per-profile tweaks such as a different iGPU cap).
        """
        egpu = detect_egpu() is not None
        self.last_egpu = egpu
        if not (self.cfg.auto_switch and self.cfg.egpu_switch and egpu):
            return self.settings
        settings = self.profile_settings(self.cfg.egpu_profile)
        if settings is None or self.settings.profile == settings.profile:
            return self.settings
        return settings

    def maybe_auto_switch(self, on_ac: bool, egpu: bool = False) -> None:
        if not self.cfg.auto_switch:
            return
        first = self.last_ac is None
        changed = on_ac != self.last_ac or egpu != self.last_egpu
        self.last_ac = on_ac
        self.last_egpu = egpu
        if first or not changed:
            return
        settings = self.profile_settings(self.cfg.auto_profile(on_ac, egpu))
        if settings is not None:
            self.apply(settings)

    def loop_background(self) -> None:
        while not self.stop.wait(0.25):
            try:
                tel = read_telemetry()
                self.maybe_auto_switch(tel.on_ac, tel.egpu is not None)
                with self.lock:
                    settings = self.settings
                if settings.fan_mode == "curve":
                    temp = max(
                        tel.cpu_temp_c or 0.0,
                        tel.gpu_temp_c or 0.0,
                    )
                    self.fan.tick(temp, settings.curve_points(), slew=settings.fan_slew)
            except Exception:
                pass

    def reapply_loop(self) -> None:
        while not self.stop.wait(max(1.0, self.cfg.reapply_sec)):
            try:
                with self.lock:
                    settings = self.settings
                apply_settings(settings, fan_tick=settings.fan_mode == "curve")
            except Exception:
                pass

    def handle(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        if cmd == "ping":
            return {"ok": True}
        if cmd == "status":
            tel = read_telemetry()
            dev = detect_device()
            with self.lock:
                settings = self.settings.to_dict()
                last = self.last_apply
                backend = self.tdp_backend
            return {
                "ok": True,
                "device": {
                    "name": dev.name,
                    "product": dev.product,
                    "cpu": dev.cpu,
                    "gpu": dev.gpu,
                    "known": dev.known,
                },
                "settings": settings,
                "config": self.cfg.to_dict(),
                "telemetry": {
                    "cpu_temp_c": tel.cpu_temp_c,
                    "gpu_temp_c": tel.gpu_temp_c,
                    "gpu_power_w": tel.gpu_power_w,
                    "gpu_clock_mhz": tel.gpu_clock_mhz,
                    "gpu_busy_pct": tel.gpu_busy_pct,
                    "fan_rpm": tel.fan_rpm,
                    "fan_pwm": tel.fan_pwm,
                    "fan_mode": tel.fan_mode,
                    "battery_pct": tel.battery_pct,
                    "battery_w": tel.battery_w,
                    "on_ac": tel.on_ac,
                    "epp": tel.epp,
                    "boost": tel.boost,
                    "gpu_perf_level": tel.gpu_perf_level,
                    "package_w": tel.package_w,
                    "cpu_mhz": tel.cpu_mhz,
                    "cpu_min_mhz": tel.cpu_min_mhz,
                    "cpu_max_mhz": tel.cpu_max_mhz,
                    "governor": tel.governor,
                    "battery_v": tel.battery_v,
                    "kernel": tel.kernel,
                    "charge_limit": tel.charge_limit,
                    "egpu_present": tel.egpu is not None,
                },
                "egpu": tel.egpu,
                "limits": self.settings.limits().__dict__,
                "smu": self.smu_table(),
                "diagnose": diagnose.collect(),
                "tdp_backend": backend,
                "last_apply": last,
            }
        if cmd == "set_profile":
            name = str(msg.get("name", "balanced"))
            settings = builtin(name, unlock=self.cfg.unlock)
            return {"ok": True, "apply": self.apply(settings), "settings": settings.to_dict()}
        if cmd == "set_settings":
            data = msg.get("settings") or {}
            settings = Settings.from_dict({**self.settings.to_dict(), **data})
            settings.unlock = self.cfg.unlock or bool(data.get("unlock", False))
            if any(k in data for k in ("tdp_w", "tctl_c", "epp", "gpu_mode", "fan_mode")):
                if settings.profile in PROFILES:
                    builtin_match = builtin(settings.profile, unlock=settings.unlock)
                    if settings.tdp_w != builtin_match.tdp_w:
                        settings.profile = "custom"
            return {"ok": True, "apply": self.apply(settings), "settings": settings.to_dict()}
        if cmd == "set_config":
            data = msg.get("config") or {}
            for key, value in data.items():
                if hasattr(self.cfg, key):
                    setattr(self.cfg, key, value)
            self.cfg = AppConfig.from_dict(self.cfg.to_dict())
            try:
                save_system_config(self.cfg)
            except OSError:
                pass
            return {"ok": True, "config": self.cfg.to_dict()}
        if cmd == "reapply":
            return {"ok": True, "apply": self.apply(self.settings)}
        if cmd == "export":
            return {
                "ok": True,
                "bundle": {
                    "settings": self.settings.to_dict(),
                    "config": self.cfg.to_dict(),
                },
            }
        if cmd == "import":
            bundle = msg.get("bundle") or {}
            if bundle.get("settings"):
                settings = Settings.from_dict(bundle["settings"])
                settings.unlock = self.cfg.unlock or settings.unlock
                self.apply(settings)
            if bundle.get("config"):
                self.cfg = AppConfig.from_dict({**self.cfg.to_dict(), **bundle["config"]})
                try:
                    save_system_config(self.cfg)
                except OSError:
                    pass
            return {"ok": True, "settings": self.settings.to_dict(), "config": self.cfg.to_dict()}
        if cmd == "diagnose":
            return {"ok": True, "diagnose": diagnose.collect(), "smu": self.smu_table()}
        if cmd == "smu":
            return {"ok": True, "smu": self.smu_table(force=True)}
        return {"ok": False, "error": f"unknown command: {cmd}"}

    def smu_table(self, force: bool = False) -> dict:
        now = time.monotonic()
        if not force and self._smu and now - self._smu_t < 2.0:
            return self._smu
        try:
            self._smu = ryzenadj.read_table()
        except Exception as exc:
            self._smu = {"error": str(exc), "meta": {}, "rows": {}}
        self._smu_t = now
        return self._smu


def _serve(daemon: Daemon, path: str) -> None:
    sock_path = Path(path)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH)
    server.listen(8)
    server.settimeout(0.5)
    while not daemon.stop.is_set():
        try:
            conn, _ = server.accept()
        except TimeoutError:
            continue
        except OSError:
            break
        with conn:
            try:
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                msg = json.loads(data.decode().strip() or "{}")
                reply = daemon.handle(msg)
            except Exception as exc:
                reply = {"ok": False, "error": str(exc)}
            conn.sendall((json.dumps(reply) + "\n").encode())
    server.close()
    try:
        sock_path.unlink()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    del argv
    path = os.environ.get("MAXCTL_SOCKET", SOCKET_PATH)
    daemon = Daemon()

    def stop(*_args) -> None:
        daemon.stop.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    threading.Thread(target=daemon.loop_background, daemon=True).start()
    threading.Thread(target=daemon.reapply_loop, daemon=True).start()
    try:
        daemon.apply(daemon.boot_settings())
    except Exception:
        pass
    _serve(daemon, path)
    return 0
