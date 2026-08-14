from __future__ import annotations

import argparse
import json
import os
import sys

from maxctl import __version__, alib, ryzenadj
from maxctl.apply import apply_settings
from maxctl.client import HelperError, connected, request
from maxctl.hardware import detect_device, read_telemetry
from maxctl.profiles import PROFILES, Settings, builtin
from maxctl.state import save_settings


def _print_telemetry() -> None:
    tel = read_telemetry()
    dev = detect_device()
    print(f"{dev.name}  ({dev.product})")
    print(f"CPU    {dev.cpu}")
    print(f"GPU    {dev.gpu}")
    print(f"Power  {'AC' if tel.on_ac else 'Battery'}  {tel.battery_pct if tel.battery_pct is not None else '?'}%")
    if tel.cpu_temp_c is not None:
        print(f"Tctl   {tel.cpu_temp_c:.1f} C")
    if tel.gpu_temp_c is not None:
        print(f"GPU    {tel.gpu_temp_c:.1f} C  {tel.gpu_clock_mhz or '?'} MHz  {tel.gpu_power_w or 0:.1f} W")
    if tel.fan_rpm is not None:
        mode = {0: "full", 1: "manual", 2: "auto"}.get(tel.fan_mode or -1, str(tel.fan_mode))
        print(f"Fan    {tel.fan_rpm} RPM  pwm={tel.fan_pwm}  {mode}")
    if tel.epp:
        print(f"EPP    {tel.epp}  boost={tel.boost}")
    print(f"Helper {'online' if connected() else 'offline'}")
    print(f"ALIB   {'yes' if alib.available() else 'no'}   RyzenAdj {'yes' if ryzenadj.available() else 'no'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="maxctl",
        description="TDP and power control for GPD Win Max 2 on Ubuntu",
    )
    parser.add_argument("--version", action="version", version=f"maxctl {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("status", help="show live hardware status")
    sub.add_parser("profiles", help="list built-in profiles")
    p_set = sub.add_parser("profile", help="apply a built-in profile")
    p_set.add_argument("name", choices=list(PROFILES))
    p_tdp = sub.add_parser("tdp", help="set TDP in watts")
    p_tdp.add_argument("watts", type=int)
    p_tdp.add_argument("--no-boost", action="store_true")
    p_fan = sub.add_parser("fan", help="fan mode: auto, or a PWM 0-255")
    p_fan.add_argument("value")
    sub.add_parser("gui", help="open the GTK window")
    sub.add_parser("daemon", help="run maxctld in the foreground")
    p_raw = sub.add_parser("rpc", help="send a JSON command to the helper")
    p_raw.add_argument("command")
    p_raw.add_argument("--json", default="{}")

    args = parser.parse_args(argv)
    if args.cmd in (None, "status"):
        if connected():
            try:
                print(json.dumps(request("status"), indent=2))
                return 0
            except HelperError:
                pass
        _print_telemetry()
        return 0
    if args.cmd == "profiles":
        for name, settings in PROFILES.items():
            print(f"{name:12} {settings.tdp_w:>3}W  tctl {settings.tctl_c}  {settings.epp}")
        return 0
    if args.cmd == "gui":
        from maxctl.gui import main as gui_main

        return gui_main()
    if args.cmd == "daemon":
        from maxctl.daemon import main as daemon_main

        return daemon_main()
    try:
        if args.cmd == "profile":
            print(json.dumps(_rpc_or_root("set_profile", name=args.name), indent=2))
        elif args.cmd == "tdp":
            print(
                json.dumps(
                    _rpc_or_root(
                        "set_settings",
                        settings={"tdp_w": args.watts, "boost": not args.no_boost, "profile": "custom"},
                    ),
                    indent=2,
                )
            )
        elif args.cmd == "fan":
            if args.value == "auto":
                payload = {"fan_mode": "auto"}
            elif args.value == "curve":
                payload = {"fan_mode": "curve"}
            else:
                payload = {"fan_mode": "manual", "fan_pwm": int(args.value)}
            print(json.dumps(_rpc_or_root("set_settings", settings=payload), indent=2))
        elif args.cmd == "rpc":
            extra = json.loads(args.json)
            print(json.dumps(request(args.command, **extra), indent=2))
    except HelperError as exc:
        print(f"maxctl: {exc}", file=sys.stderr)
        return 1
    return 0


def _rpc_or_root(cmd: str, **fields) -> dict:
    try:
        return request(cmd, **fields)
    except HelperError:
        if os.geteuid() != 0:
            raise
        if cmd == "set_profile":
            settings = builtin(str(fields["name"]))
        elif cmd == "set_settings":
            settings = Settings.from_dict(fields.get("settings") or {})
        else:
            raise
        result = apply_settings(settings)
        save_settings(settings)
        return {"ok": result.ok, "apply": result.to_dict(), "settings": settings.to_dict()}
