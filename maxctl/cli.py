from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

from maxctl import __version__, alib, diagnose, imu, ryzenadj
from maxctl.apply import apply_settings
from maxctl.client import HelperError, connected, request
from maxctl.hardware import detect_device, read_telemetry
from maxctl.profiles import PROFILES, Settings, builtin
from maxctl.state import delete_preset, load_presets, load_settings, save_preset, save_settings


def _print_telemetry() -> None:
    tel = read_telemetry()
    dev = detect_device()
    print(f"{dev.name}  ({dev.product})")
    print(f"CPU    {dev.cpu}")
    print(f"GPU    {dev.gpu}")
    print(f"Power  {'AC' if tel.on_ac else 'Battery'}  {tel.battery_pct if tel.battery_pct is not None else '?'}%")
    if tel.egpu:
        print(f"eGPU   {tel.egpu['name']}  ({tel.egpu['pci']})")
    if tel.cpu_temp_c is not None:
        print(f"Tctl   {tel.cpu_temp_c:.1f} C")
    if tel.gpu_temp_c is not None:
        print(f"GPU    {tel.gpu_temp_c:.1f} C  {tel.gpu_clock_mhz or '?'} MHz  {tel.gpu_power_w or 0:.1f} W")
    if tel.fan_rpm is not None:
        mode = {0: "full", 1: "manual", 2: "auto"}.get(tel.fan_mode or -1, str(tel.fan_mode))
        print(f"Fan    {tel.fan_rpm} RPM  pwm={tel.fan_pwm}  {mode}")
    if tel.cpu_mhz:
        print(f"CPU    {tel.cpu_mhz} MHz  {tel.cpu_min_mhz}-{tel.cpu_max_mhz}  {tel.governor}")
    if tel.package_w is not None:
        print(f"Pkg    {tel.package_w:.1f} W")
    if tel.epp:
        print(f"EPP    {tel.epp}  boost={tel.boost}")
    if tel.gpu_busy_pct is not None:
        print(f"GPU%   {tel.gpu_busy_pct}  level={tel.gpu_perf_level}")
    if tel.charge_limit is not None:
        print(f"Charge {tel.charge_limit}%")
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
    p_info = sub.add_parser("info", help="print resolved SMU limits and backends")
    p_info.add_argument("--json", action="store_true")
    p_watch = sub.add_parser("watch", help="stream telemetry")
    p_watch.add_argument("--interval", type=float, default=1.0)
    p_watch.add_argument("--csv", action="store_true")
    p_exp = sub.add_parser("export", help="write the current profile JSON")
    p_exp.add_argument("path")
    p_imp = sub.add_parser("import", help="load a profile JSON")
    p_imp.add_argument("path")
    p_diag = sub.add_parser("diagnose", help="show TDP backends and kernel knobs")
    p_diag.add_argument("--json", action="store_true")
    sub.add_parser("smu", help="print parsed RyzenAdj -i table")
    p_adv = sub.add_parser("set", help="set advanced SMU / CPU fields")
    p_adv.add_argument("--stapm", type=int)
    p_adv.add_argument("--fast", type=int)
    p_adv.add_argument("--slow", type=int)
    p_adv.add_argument("--skin", type=int)
    p_adv.add_argument("--tctl", type=int)
    p_adv.add_argument("--stapm-time", type=int)
    p_adv.add_argument("--slow-time", type=int)
    p_adv.add_argument("--epp")
    p_adv.add_argument("--governor")
    p_adv.add_argument("--cpu-min", type=int)
    p_adv.add_argument("--cpu-max", type=int)
    p_adv.add_argument("--dpm")
    p_adv.add_argument("--charge", type=int)
    p_adv.add_argument("--power-shift", choices=["off", "saving", "max"])
    p_adv.add_argument("--recipe", choices=["flat", "burst", "efficiency"])
    p_pre = sub.add_parser("preset", help="save / load named user presets")
    p_pre.add_argument("action", choices=["list", "save", "load", "delete"])
    p_pre.add_argument("name", nargs="?")
    p_egpu = sub.add_parser("egpu", help="external GPU detection and auto-switch (on / off)")
    p_egpu.add_argument("action", nargs="?", default="status", choices=["status", "on", "off"])
    p_egpu.add_argument("--profile", help="profile to apply while an eGPU is attached")
    p_imu = sub.add_parser("imu", help="log and analyse the built-in IMU (BMI260)")
    imu_sub = p_imu.add_subparsers(dest="imu_cmd", required=True)
    p_ilog = imu_sub.add_parser("log", help="record gyro + accel to CSV")
    p_ilog.add_argument("--label", default="rec")
    p_ilog.add_argument("--seconds", type=float, default=120.0)
    p_ilog.add_argument("--rate", type=float, default=100.0, help="polling rate in Hz")
    p_ilog.add_argument("--out", default=".", help="folder for imu_<label>_<time>.csv")
    p_ilog.add_argument("--progress", action="store_true")
    p_isum = imu_sub.add_parser("summary", help="rate, gaps and integrated angle of a recording")
    p_isum.add_argument("csv")
    p_isum.add_argument("--bias", help="bias report JSON whose gyro bias is subtracted")
    p_ibias = imu_sub.add_parser("bias", help="still test: gyro bias, noise, Allan deviation, drift")
    p_ibias.add_argument("csv")
    p_ibias.add_argument("--skip", type=float, default=15.0, help="seconds dropped at the start")
    p_ibias.add_argument("--tail", type=float, default=0.0, help="seconds dropped at the end")
    p_ibias.add_argument("--window", type=float, nargs=2, metavar=("A", "B"), help="use [A, B] s instead")
    p_ibias.add_argument("--motion-gyro-dps", type=float, default=1.0)
    p_ibias.add_argument("--motion-accel", type=float, default=0.5)
    p_ibias.add_argument("--guard", type=float, default=0.5, help="seconds dropped around a flagged sample")
    p_ibias.add_argument("--accel-bias", choices=["none", "level"], default="none")
    p_ibias.add_argument("--g", type=float, default=9.80665, help="local gravity for --accel-bias level")
    p_ibias.add_argument("--out", help="write <name>.bias.json and a bias-corrected CSV here")
    p_ibias.add_argument("--overwrite", action="store_true")
    p_iapp = imu_sub.add_parser("apply", help="subtract a bias report from another recording")
    p_iapp.add_argument("csv")
    p_iapp.add_argument("--bias", required=True, help="bias report JSON from maxctl imu bias --out")
    p_iapp.add_argument("--out", required=True)
    p_iapp.add_argument("--overwrite", action="store_true")

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
            lim = settings.limits()
            igpu = f"  iGPU<={settings.gpu_max}MHz" if settings.gpu_mode == "range" else ""
            print(
                f"{name:12} {settings.tdp_w:>3}W  "
                f"STAPM {lim.stapm_w} Fast {lim.fast_w} Slow {lim.slow_w}  "
                f"tctl {settings.tctl_c}  {settings.epp}{igpu}"
            )
        return 0
    if args.cmd == "gui":
        from maxctl.gui import main as gui_main

        return gui_main()
    if args.cmd == "daemon":
        from maxctl.daemon import main as daemon_main

        return daemon_main()
    if args.cmd == "info":
        return _print_info(args.json)
    if args.cmd == "watch":
        return _watch(args.interval, args.csv)
    if args.cmd == "diagnose":
        return _print_diagnose(args.json)
    if args.cmd == "smu":
        return _print_smu()
    if args.cmd == "preset":
        return _preset(args.action, args.name)
    if args.cmd == "egpu":
        return _egpu(args.action, args.profile)
    if args.cmd == "imu":
        return _imu(args)
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
        elif args.cmd == "export":
            bundle = _rpc_or_root("export")
            Path(args.path).write_text(json.dumps(bundle.get("bundle") or bundle, indent=2) + "\n")
            print(args.path)
        elif args.cmd == "import":
            bundle = json.loads(Path(args.path).read_text())
            print(json.dumps(_rpc_or_root("import", bundle=bundle.get("bundle", bundle)), indent=2))
        elif args.cmd == "set":
            payload: dict = {"profile": "custom"}
            if args.recipe or any(
                getattr(args, key, None) is not None
                for key in ("stapm", "fast", "slow", "skin")
            ):
                payload["advanced"] = True
            mapping = {
                "stapm": "stapm_w",
                "fast": "fast_w",
                "slow": "slow_w",
                "skin": "skin_w",
                "tctl": "tctl_c",
                "stapm_time": "stapm_time",
                "slow_time": "slow_time",
                "epp": "epp",
                "governor": "governor",
                "cpu_min": "cpu_min_mhz",
                "cpu_max": "cpu_max_mhz",
                "dpm": "gpu_dpm",
                "charge": "charge_limit",
            }
            for arg, key in mapping.items():
                value = getattr(args, arg, None)
                if value is not None:
                    payload[key] = value
            if args.power_shift is not None:
                payload["power_shift"] = "" if args.power_shift == "off" else args.power_shift
            if args.recipe:
                current = Settings.from_dict({**_current_settings(), **payload})
                current.apply_recipe(args.recipe)
                payload = current.to_dict()
            print(json.dumps(_rpc_or_root("set_settings", settings=payload), indent=2))
    except HelperError as exc:
        print(f"maxctl: {exc}", file=sys.stderr)
        return 1
    return 0


def _current_settings() -> dict:
    if connected():
        try:
            return request("status").get("settings") or {}
        except HelperError:
            pass
    return load_settings().to_dict()


def _rpc_or_root(cmd: str, **fields) -> dict:
    try:
        return request(cmd, **fields)
    except HelperError:
        if os.geteuid() != 0:
            raise
        if cmd == "export":
            from maxctl.state import load_config

            settings = load_settings()
            return {"ok": True, "bundle": {"settings": settings.to_dict(), "config": load_config().to_dict()}}
        if cmd == "import":
            bundle = fields.get("bundle") or {}
            settings = Settings.from_dict(bundle.get("settings") or {})
            result = apply_settings(settings)
            save_settings(settings)
            return {"ok": result.ok, "apply": result.to_dict(), "settings": settings.to_dict()}
        if cmd == "set_profile":
            settings = builtin(str(fields["name"]))
        elif cmd == "set_settings":
            settings = Settings.from_dict({**load_settings().to_dict(), **(fields.get("settings") or {})})
        else:
            raise
        result = apply_settings(settings)
        save_settings(settings)
        return {"ok": result.ok, "apply": result.to_dict(), "settings": settings.to_dict()}


def _print_info(as_json: bool) -> int:
    if connected():
        try:
            status = request("status")
            payload = {
                "limits": status.get("limits"),
                "settings": status.get("settings"),
                "tdp_backend": status.get("tdp_backend"),
                "device": status.get("device"),
                "last_apply": status.get("last_apply"),
            }
            if as_json:
                print(json.dumps(payload, indent=2))
                return 0
            lim = payload.get("limits") or {}
            print(
                f"STAPM {lim.get('stapm_w')}W  Fast {lim.get('fast_w')}W  "
                f"Slow {lim.get('slow_w')}W  Skin {lim.get('skin_w')}W  "
                f"Tctl {lim.get('tctl_c')}  times {lim.get('stapm_time')}/{lim.get('slow_time')}s"
            )
            print(f"Backend {payload.get('tdp_backend') or 'none'}")
            if lim:
                print(ryzenadj.command_line(Settings.from_dict(status.get("settings") or {}).limits()))
            return 0
        except HelperError:
            pass
    settings = load_settings()
    lim = settings.limits()
    if as_json:
        print(json.dumps({"limits": lim.__dict__, "settings": settings.to_dict()}, indent=2))
        return 0
    print(
        f"STAPM {lim.stapm_w}W  Fast {lim.fast_w}W  Slow {lim.slow_w}W  "
        f"Skin {lim.skin_w}W  Tctl {lim.tctl_c}  times {lim.stapm_time}/{lim.slow_time}s"
    )
    print(ryzenadj.command_line(lim))
    return 0


def _print_diagnose(as_json: bool) -> int:
    helper = connected()
    data = diagnose.collect()
    if helper:
        try:
            reply = request("diagnose")
            data = reply.get("diagnose") or data
        except HelperError:
            pass
    if as_json:
        print(json.dumps({"helper": helper, **data}, indent=2))
        return 0
    print(diagnose.format_report(data, helper=helper))
    return 0


def _print_smu() -> int:
    if connected():
        try:
            print(json.dumps(request("smu"), indent=2))
            return 0
        except HelperError:
            pass
    try:
        print(json.dumps(ryzenadj.read_table(), indent=2))
        return 0
    except Exception as exc:
        print(f"maxctl: {exc}", file=sys.stderr)
        return 1


def _preset(action: str, name: str | None) -> int:
    if action == "list":
        for key in sorted(load_presets()):
            print(key)
        return 0
    if not name:
        print("maxctl: preset name required", file=sys.stderr)
        return 1
    if action == "save":
        if connected():
            try:
                status = request("status")
                bundle = {"settings": status.get("settings") or {}, "config": status.get("config") or {}}
            except HelperError:
                bundle = {"settings": load_settings().to_dict()}
        else:
            bundle = {"settings": load_settings().to_dict()}
        save_preset(name, bundle)
        print(name)
        return 0
    if action == "delete":
        delete_preset(name)
        print(name)
        return 0
    presets = load_presets()
    if name not in presets:
        print(f"maxctl: unknown preset {name}", file=sys.stderr)
        return 1
    print(json.dumps(_rpc_or_root("import", bundle=presets[name]), indent=2))
    return 0


def _egpu(action: str, profile: str | None) -> int:
    from maxctl.hardware import detect_egpu
    from maxctl.state import load_config

    egpu = detect_egpu()
    if action == "status" and profile is None:
        cfg = load_config()
        if connected():
            try:
                cfg = AppConfigView(request("status").get("config") or {})
            except HelperError:
                pass
        print(f"eGPU        {egpu.name + '  (' + egpu.pci + ')' if egpu else 'not detected'}")
        print(f"Auto-switch {'on' if getattr(cfg, 'egpu_switch', True) else 'off'}  -> {getattr(cfg, 'egpu_profile', 'egpu')}")
        return 0
    config: dict = {}
    if action in ("on", "off"):
        config["egpu_switch"] = action == "on"
    if profile is not None:
        if profile not in PROFILES and profile != "custom":
            print(f"maxctl: unknown profile {profile}", file=sys.stderr)
            return 1
        config["egpu_profile"] = profile
    try:
        reply = request("set_config", config=config)
    except HelperError as exc:
        print(f"maxctl: {exc}", file=sys.stderr)
        return 1
    cfg = reply.get("config") or {}
    print(f"Auto-switch {'on' if cfg.get('egpu_switch', True) else 'off'}  -> {cfg.get('egpu_profile', 'egpu')}")
    return 0


def _imu_outputs(src: Path, out_dir: Path, tag: str, overwrite: bool) -> tuple[Path, Path]:
    csv_out = out_dir / f"{src.stem}.{tag}.csv"
    json_out = out_dir / f"{src.stem}.{tag}.json"
    for path in (csv_out, json_out):
        if path.resolve() == src.resolve():
            raise imu.ImuError("refusing to write over the input")
        if path.exists() and not overwrite:
            raise imu.ImuError(f"{path} exists (pass --overwrite)")
    out_dir.mkdir(parents=True, exist_ok=True)
    return csv_out, json_out


def _imu(args) -> int:
    try:
        if args.imu_cmd == "log":
            out = Path(args.out).expanduser() / f"imu_{args.label}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            print(f"Recording {args.seconds:.0f} s at {args.rate:.0f} Hz -> {out}", flush=True)
            progress = (lambda s: print(f"  {s:.0f} s", flush=True)) if args.progress else None
            meta = imu.record(out, args.seconds, args.rate, progress=progress)
            result = imu.summary(imu.load(out))
            result["meta"] = meta
            Path(str(out) + ".summary.json").write_text(json.dumps(result, indent=1) + "\n")
        elif args.imu_cmd == "summary":
            bias = (0.0, 0.0, 0.0)
            if args.bias:
                bias = json.loads(Path(args.bias).read_text())["gyro_bias_rad_s"]
            result = imu.summary(imu.load(args.csv), bias)
        elif args.imu_cmd == "bias":
            rec = imu.load(args.csv)
            result = imu.analyze(
                rec, skip=args.skip, tail=args.tail, window=tuple(args.window) if args.window else None,
                motion_gyro_dps=args.motion_gyro_dps, motion_accel=args.motion_accel, guard=args.guard, g=args.g,
            )
            abias = result["accel_bias_if_level_m_s2"] if args.accel_bias == "level" else [0.0, 0.0, 0.0]
            result["accel_bias_mode"] = args.accel_bias
            result["accel_bias_subtracted_m_s2"] = abias
            if args.out:
                src = Path(args.csv)
                csv_out, json_out = _imu_outputs(src, Path(args.out).expanduser(), "bias", args.overwrite)
                imu.write_corrected(rec, csv_out, result["gyro_bias_rad_s"], abias)
                result["input_sha256"] = imu.sha256(src)
                result["written"] = imu.now_iso()
                result["output_csv"] = str(csv_out)
                json_out.write_text(json.dumps(result, indent=1) + "\n")
        else:
            report = json.loads(Path(args.bias).read_text())
            src = Path(args.csv)
            rec = imu.load(src)
            csv_out, json_out = _imu_outputs(src, Path(args.out).expanduser(), "biasapplied", args.overwrite)
            gbias = report["gyro_bias_rad_s"]
            abias = report.get("accel_bias_subtracted_m_s2", [0.0, 0.0, 0.0])
            imu.write_corrected(rec, csv_out, gbias, abias)
            result = {
                "file": str(src),
                "input_sha256": imu.sha256(src),
                "bias_report": str(args.bias),
                "gyro_bias_subtracted_deg_s": [round(math.degrees(b), 6) for b in gbias],
                "accel_bias_subtracted_m_s2": abias,
                "integrated_deg_raw": [round(math.degrees(x), 6) for x in imu.integrate(rec.gyro, rec.t)],
                "integrated_deg_corrected": [
                    round(math.degrees(x), 6) for x in imu.integrate(rec.gyro, rec.t, gbias)
                ],
                "written": imu.now_iso(),
                "output_csv": str(csv_out),
            }
            json_out.write_text(json.dumps(result, indent=1) + "\n")
    except (imu.ImuError, OSError, KeyError, ValueError) as exc:
        print(f"maxctl: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


class AppConfigView:
    """Attribute view over a config dict returned by the helper."""

    def __init__(self, data: dict) -> None:
        self.__dict__.update(data)


def _watch(interval: float, csv: bool) -> int:
    if csv:
        print("package_w,cpu_c,cpu_mhz,gpu_c,gpu_busy,fan_rpm,fan_pwm,on_ac,battery_pct,battery_w,epp")
    try:
        while True:
            tel = read_telemetry()
            if csv:
                print(
                    ",".join(
                        [
                            "" if tel.package_w is None else f"{tel.package_w:.2f}",
                            "" if tel.cpu_temp_c is None else f"{tel.cpu_temp_c:.1f}",
                            "" if tel.cpu_mhz is None else str(tel.cpu_mhz),
                            "" if tel.gpu_temp_c is None else f"{tel.gpu_temp_c:.1f}",
                            "" if tel.gpu_busy_pct is None else str(tel.gpu_busy_pct),
                            "" if tel.fan_rpm is None else str(tel.fan_rpm),
                            "" if tel.fan_pwm is None else str(tel.fan_pwm),
                            "1" if tel.on_ac else "0",
                            "" if tel.battery_pct is None else str(tel.battery_pct),
                            "" if tel.battery_w is None else f"{tel.battery_w:.2f}",
                            tel.epp or "",
                        ]
                    ),
                    flush=True,
                )
            else:
                pkg = f"{tel.package_w:5.1f}W" if tel.package_w is not None else "  n/a"
                cpu = f"{tel.cpu_temp_c:5.1f}C" if tel.cpu_temp_c is not None else "  n/a"
                gpu = f"{tel.gpu_temp_c:5.1f}C" if tel.gpu_temp_c is not None else "  n/a"
                mhz = tel.cpu_mhz or 0
                fan = tel.fan_rpm or 0
                pwm = tel.fan_pwm if tel.fan_pwm is not None else -1
                print(
                    f"\r{pkg}  CPU {cpu} {mhz:4d}MHz  GPU {gpu}  fan {fan:4d} pwm={pwm:3d}  "
                    f"{'AC' if tel.on_ac else 'BAT'}  {tel.epp or '-'}",
                    end="",
                    flush=True,
                )
            time.sleep(max(0.2, interval))
    except KeyboardInterrupt:
        if not csv:
            print()
        return 0
