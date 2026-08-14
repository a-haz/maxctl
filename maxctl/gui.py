from __future__ import annotations

import sys
from collections import deque

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

from maxctl import __version__, alib, ryzenadj
from maxctl.client import HelperError, connected, request
from maxctl.fan import DEFAULT_CURVES
from maxctl.fan_graph import FanCurveGraph
from maxctl.hardware import detect_device, read_telemetry
from maxctl.profiles import PROFILES, Settings

CSS = """
.hero-tdp {
  font-size: 64px;
  font-weight: 800;
  letter-spacing: -2px;
}
.hero-unit {
  font-size: 22px;
  font-weight: 600;
  opacity: 0.7;
}
.metric-value {
  font-size: 20px;
  font-weight: 700;
}
.metric-label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.6px;
  opacity: 0.65;
}
.profile-btn {
  min-width: 92px;
  min-height: 42px;
  font-weight: 600;
}
.status-chip {
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
}
"""


def _fmt(value, digits=0, suffix="", fallback="—"):
    if value is None:
        return fallback
    if digits == 0:
        return f"{int(round(value))}{suffix}"
    return f"{value:.{digits}f}{suffix}"


class Spark(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__()
        self.set_content_width(240)
        self.set_content_height(36)
        self.points: deque[float] = deque(maxlen=60)
        self.set_draw_func(self._draw)

    def push(self, value: float | None) -> None:
        self.points.append(0.0 if value is None else float(value))
        self.queue_draw()

    def _draw(self, _area, cr, width, height) -> None:
        if len(self.points) < 2:
            return
        vals = list(self.points)
        lo, hi = min(vals), max(vals)
        if hi - lo < 1:
            hi = lo + 1
        cr.set_source_rgba(0.2, 0.55, 0.95, 0.85)
        cr.set_line_width(2)
        for i, val in enumerate(vals):
            x = i / (len(vals) - 1) * width
            y = height - ((val - lo) / (hi - lo)) * (height - 6) - 3
            if i == 0:
                cr.move_to(x, y)
            else:
                cr.line_to(x, y)
        cr.stroke()


class MaxctlWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="maxctl")
        self.set_default_size(480, 820)
        self.settings = Settings()
        self.helper = False
        self._applying = False
        self._synced = False
        self.spark = Spark()
        self.fan_graph = FanCurveGraph()
        self.fan_graph.on_change = self._on_curve_edit
        self._build()
        GLib.timeout_add(1000, self.refresh)

    def _build(self) -> None:
        css = Gtk.CssProvider()
        css.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="maxctl", subtitle="Win Max 2 power"))
        about = Gtk.Button(icon_name="help-about-symbolic")
        about.connect("clicked", self._about)
        header.pack_end(about)
        toolbar.add_top_bar(header)

        self.toast = Adw.ToastOverlay()
        clamp = Adw.Clamp(maximum_size=560, tightening_threshold=400)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        page.set_margin_top(12)
        page.set_margin_bottom(24)
        page.set_margin_start(16)
        page.set_margin_end(16)

        self.banner = Adw.Banner(title="Helper is not installed — live sensors work, TDP apply needs install")
        self.banner.set_button_label("How")
        self.banner.connect("button-clicked", self._install_help)
        page.append(self.banner)

        self.device_label = Gtk.Label(xalign=0)
        self.device_label.add_css_class("dim-label")
        page.append(self.device_label)

        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        hero.set_halign(Gtk.Align.CENTER)
        row = Gtk.Box(spacing=6)
        row.set_halign(Gtk.Align.CENTER)
        self.tdp_label = Gtk.Label(label="28")
        self.tdp_label.add_css_class("hero-tdp")
        unit = Gtk.Label(label="W")
        unit.add_css_class("hero-unit")
        unit.set_valign(Gtk.Align.END)
        unit.set_margin_bottom(10)
        row.append(self.tdp_label)
        row.append(unit)
        self.hero_sub = Gtk.Label(label="Balanced · Battery")
        self.hero_sub.add_css_class("dim-label")
        hero.append(row)
        hero.append(self.hero_sub)
        hero.append(self.spark)
        page.append(hero)

        pills = Gtk.Box(spacing=8)
        pills.set_halign(Gtk.Align.CENTER)
        pills.set_homogeneous(True)
        self.profile_buttons: dict[str, Gtk.ToggleButton] = {}
        for name, title in (
            ("silent", "Silent 12W"),
            ("balanced", "Balanced 28W"),
            ("performance", "Sport 35W"),
            ("turbo", "Turbo 40W"),
        ):
            btn = Gtk.ToggleButton(label=title)
            btn.add_css_class("profile-btn")
            if name == "turbo":
                btn.add_css_class("destructive-action")
            elif name == "performance":
                btn.add_css_class("suggested-action")
            btn.connect("toggled", self._on_profile, name)
            self.profile_buttons[name] = btn
            pills.append(btn)
        page.append(pills)

        metrics = Gtk.Grid(column_spacing=10, row_spacing=10, column_homogeneous=True)
        self.m_cpu = self._metric("CPU")
        self.m_gpu = self._metric("GPU")
        self.m_fan = self._metric("FAN")
        self.m_bat = self._metric("POWER")
        metrics.attach(self.m_cpu[0], 0, 0, 1, 1)
        metrics.attach(self.m_gpu[0], 1, 0, 1, 1)
        metrics.attach(self.m_fan[0], 0, 1, 1, 1)
        metrics.attach(self.m_bat[0], 1, 1, 1, 1)
        page.append(metrics)

        prefs = Adw.PreferencesGroup(title="Limits")
        self.tdp_row = Adw.SpinRow(
            title="TDP",
            subtitle="Sustained package power (STAPM / PPT)",
            adjustment=Gtk.Adjustment(lower=8, upper=40, step_increment=1, page_increment=5, value=28),
        )
        self.tdp_row.connect("changed", self._on_tdp)
        self.tctl_row = Adw.SpinRow(
            title="Temp limit",
            subtitle="Tctl target sent to the SMU",
            adjustment=Gtk.Adjustment(lower=70, upper=100, step_increment=1, page_increment=5, value=90),
        )
        self.boost_row = Adw.SwitchRow(title="PPT boost", subtitle="Allow short Fast PPT above TDP")
        self.cpu_boost_row = Adw.SwitchRow(title="CPU boost", subtitle="amd-pstate Precision Boost")
        self.boost_row.set_active(True)
        self.cpu_boost_row.set_active(True)
        prefs.add(self.tdp_row)
        prefs.add(self.tctl_row)
        prefs.add(self.boost_row)
        prefs.add(self.cpu_boost_row)
        page.append(prefs)

        energy = Adw.PreferencesGroup(title="Energy")
        self.epp_row = Adw.ComboRow(title="CPU preference (EPP)")
        self.epp_row.set_model(Gtk.StringList.new([
            "performance",
            "balance_performance",
            "balance_power",
            "power",
        ]))
        self.epp_row.set_selected(1)
        self.ppd_row = Adw.ComboRow(title="Ubuntu power profile")
        self.ppd_row.set_model(Gtk.StringList.new(["power-saver", "balanced", "performance"]))
        self.ppd_row.set_selected(1)
        energy.add(self.epp_row)
        energy.add(self.ppd_row)
        page.append(energy)

        graphics = Adw.PreferencesGroup(title="iGPU")
        self.gpu_auto = Adw.SwitchRow(title="Automatic clocks", subtitle="Let amdgpu pick 600–2900 MHz")
        self.gpu_auto.set_active(True)
        self.gpu_min = Adw.SpinRow(
            title="Min clock",
            adjustment=Gtk.Adjustment(lower=600, upper=2900, step_increment=50, page_increment=200, value=600),
        )
        self.gpu_max = Adw.SpinRow(
            title="Max clock",
            adjustment=Gtk.Adjustment(lower=600, upper=2900, step_increment=50, page_increment=200, value=2900),
        )
        graphics.add(self.gpu_auto)
        graphics.add(self.gpu_min)
        graphics.add(self.gpu_max)
        page.append(graphics)

        cooling = Adw.PreferencesGroup(
            title="Fan curve",
            description="Drag points. Double-click to add, right-click to remove. Editing switches to software curve.",
        )
        self.fan_row = Adw.ComboRow(title="Mode")
        self.fan_row.set_model(Gtk.StringList.new(["EC auto", "Software curve", "Manual PWM"]))
        self.fan_pwm = Adw.SpinRow(
            title="Manual PWM",
            subtitle="0–255. Switching to manual briefly spikes to full, then this value.",
            adjustment=Gtk.Adjustment(lower=0, upper=255, step_increment=5, page_increment=25, value=128),
        )
        reset = Gtk.Button(label="Reset curve")
        reset.set_halign(Gtk.Align.END)
        reset.connect("clicked", self._reset_curve)
        cooling.add(self.fan_row)
        cooling.add(self.fan_pwm)
        page.append(cooling)
        page.append(self.fan_graph)
        page.append(reset)

        behavior = Adw.PreferencesGroup(title="Behavior")
        self.auto_row = Adw.SwitchRow(title="Auto profile on AC / battery")
        self.auto_row.set_active(True)
        self.unlock_row = Adw.SwitchRow(
            title="Unlock 54W",
            subtitle="Turbo is 40W. This lets Turbo and the slider go to AMD cTDP max (54W).",
        )
        self.unlock_row.connect("notify::active", self._on_unlock)
        behavior.add(self.auto_row)
        behavior.add(self.unlock_row)
        page.append(behavior)

        apply_btn = Gtk.Button(label="Apply")
        apply_btn.add_css_class("suggested-action")
        apply_btn.add_css_class("pill")
        apply_btn.set_hexpand(True)
        apply_btn.connect("clicked", self._apply)
        page.append(apply_btn)

        self.status_line = Gtk.Label(xalign=0)
        self.status_line.add_css_class("dim-label")
        self.status_line.set_wrap(True)
        page.append(self.status_line)

        clamp.set_child(page)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(clamp)
        self.toast.set_child(scrolled)
        toolbar.set_content(self.toast)
        self.set_content(toolbar)
        self.refresh()

    def _metric(self, title: str):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("card")
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class("metric-label")
        value = Gtk.Label(label="—", xalign=0)
        value.add_css_class("metric-value")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        inner.set_margin_start(12)
        inner.set_margin_end(12)
        inner.append(label)
        inner.append(value)
        box.append(inner)
        return box, value

    def _about(self, *_args) -> None:
        dialog = Adw.AboutWindow(
            transient_for=self,
            application_name="maxctl",
            version=__version__,
            developer_name="Built for this GPD Win Max 2",
            comments="TDP, power profiles, iGPU clocks, and gpdfan control on Ubuntu. Uses AMD ALIB (same path as Handheld Daemon) with RyzenAdj as fallback.",
        )
        dialog.present()

    def _install_help(self, *_args) -> None:
        self.toast.add_toast(
            Adw.Toast(title="In ~/Projects/maxctl: sudo ./scripts/install.sh")
        )

    def _on_unlock(self, *_args) -> None:
        upper = 54 if self.unlock_row.get_active() else 40
        self.tdp_row.get_adjustment().set_upper(upper)
        self._refresh_turbo_label()

    def _refresh_turbo_label(self) -> None:
        btn = self.profile_buttons.get("turbo")
        if btn:
            btn.set_label("Turbo 54W" if self.unlock_row.get_active() else "Turbo 40W")

    def _on_profile(self, button: Gtk.ToggleButton, name: str) -> None:
        if not button.get_active() or self._applying:
            return
        self._applying = True
        for key, other in self.profile_buttons.items():
            other.set_active(key == name)
        self._applying = False
        if self.helper:
            try:
                reply = request("set_profile", name=name)
                self._ingest_settings(reply.get("settings") or {})
                self.toast.add_toast(Adw.Toast(title=f"{name.title()} applied"))
            except HelperError as exc:
                self.toast.add_toast(Adw.Toast(title=str(exc)))
        else:
            self._ingest_settings(PROFILES[name].to_dict())

    def _on_tdp(self, row: Adw.SpinRow) -> None:
        self.tdp_label.set_text(str(int(row.get_value())))

    def _on_curve_edit(self) -> None:
        if self._applying:
            return
        self.fan_row.set_selected(1)
        self.settings.fan_mode = "curve"
        self.settings.fan_curve = self.fan_graph.get_points()

    def _reset_curve(self, *_args) -> None:
        name = self.settings.profile if self.settings.profile in DEFAULT_CURVES else "balanced"
        self.fan_graph.set_points(DEFAULT_CURVES[name])
        self._on_curve_edit()

    def _collect(self) -> dict:
        epp_model = self.epp_row.get_model()
        ppd_model = self.ppd_row.get_model()
        fan_map = {0: "auto", 1: "curve", 2: "manual"}
        profile = "custom"
        for name, btn in self.profile_buttons.items():
            if btn.get_active():
                profile = name
        return {
            "profile": profile,
            "tdp_w": int(self.tdp_row.get_value()),
            "tctl_c": int(self.tctl_row.get_value()),
            "boost": self.boost_row.get_active(),
            "cpu_boost": self.cpu_boost_row.get_active(),
            "epp": epp_model.get_string(self.epp_row.get_selected()),
            "ppd": ppd_model.get_string(self.ppd_row.get_selected()),
            "gpu_mode": "auto" if self.gpu_auto.get_active() else "range",
            "gpu_min": int(self.gpu_min.get_value()),
            "gpu_max": int(self.gpu_max.get_value()),
            "fan_mode": fan_map[self.fan_row.get_selected()],
            "fan_pwm": int(self.fan_pwm.get_value()),
            "fan_curve": self.fan_graph.get_points(),
            "unlock": self.unlock_row.get_active(),
        }

    def _ingest_settings(self, data: dict) -> None:
        self._applying = True
        settings = Settings.from_dict(data)
        self.settings = settings
        self.tdp_row.set_value(settings.tdp_w)
        self.tctl_row.set_value(settings.tctl_c)
        self.boost_row.set_active(settings.boost)
        self.cpu_boost_row.set_active(settings.cpu_boost)
        self.tdp_label.set_text(str(settings.tdp_w))
        epp_idx = ["performance", "balance_performance", "balance_power", "power"].index(settings.epp)
        self.epp_row.set_selected(epp_idx)
        self.ppd_row.set_selected(["power-saver", "balanced", "performance"].index(settings.ppd))
        self.gpu_auto.set_active(settings.gpu_mode == "auto")
        self.gpu_min.set_value(settings.gpu_min)
        self.gpu_max.set_value(settings.gpu_max)
        self.fan_row.set_selected({"auto": 0, "curve": 1, "manual": 2}[settings.fan_mode])
        self.fan_pwm.set_value(settings.fan_pwm)
        self.fan_graph.set_points(settings.curve_points())
        for name, btn in self.profile_buttons.items():
            btn.set_active(settings.profile == name)
        self._refresh_turbo_label()
        self._applying = False

    def _apply(self, *_args) -> None:
        payload = self._collect()
        if not self.helper:
            self.toast.add_toast(Adw.Toast(title="Install the helper to apply TDP"))
            return
        try:
            request("set_settings", settings=payload)
            request(
                "set_config",
                config={
                    "auto_switch": self.auto_row.get_active(),
                    "unlock": self.unlock_row.get_active(),
                },
            )
            self.toast.add_toast(Adw.Toast(title=f"Applied {payload['tdp_w']}W"))
        except HelperError as exc:
            self.toast.add_toast(Adw.Toast(title=str(exc)))

    def refresh(self) -> bool:
        self.helper = connected()
        self.banner.set_revealed(not self.helper)
        tel = None
        if self.helper:
            try:
                status = request("status")
                tel = status.get("telemetry") or {}
                if not self._synced and not self._applying:
                    self._ingest_settings(status.get("settings") or {})
                    cfg = status.get("config") or {}
                    if "auto_switch" in cfg:
                        self.auto_row.set_active(bool(cfg["auto_switch"]))
                    if "unlock" in cfg:
                        self.unlock_row.set_active(bool(cfg["unlock"]))
                    self._synced = True
                backend = status.get("tdp_backend") or "none"
                self.status_line.set_text(f"Helper online · TDP backend {backend}")
                dev = status.get("device") or {}
                self.device_label.set_text(
                    f"{dev.get('name', 'GPD')} · {dev.get('cpu', '')}"
                )
            except HelperError as exc:
                self.helper = False
                self.status_line.set_text(str(exc))
        if tel is None:
            raw = read_telemetry()
            tel = {
                "cpu_temp_c": raw.cpu_temp_c,
                "gpu_temp_c": raw.gpu_temp_c,
                "gpu_power_w": raw.gpu_power_w,
                "gpu_clock_mhz": raw.gpu_clock_mhz,
                "fan_rpm": raw.fan_rpm,
                "battery_pct": raw.battery_pct,
                "battery_w": raw.battery_w,
                "on_ac": raw.on_ac,
                "package_w": raw.package_w,
            }
            dev = detect_device()
            self.device_label.set_text(f"{dev.name} · {dev.cpu}")
            backends = []
            if alib.available():
                backends.append("ALIB")
            if ryzenadj.available():
                backends.append("RyzenAdj")
            self.status_line.set_text(
                "Read-only · " + (" / ".join(backends) if backends else "no TDP backend yet")
            )

        self.m_cpu[1].set_text(_fmt(tel.get("cpu_temp_c"), 1, "°"))
        gpu = _fmt(tel.get("gpu_temp_c"), 1, "°")
        clock = tel.get("gpu_clock_mhz")
        if clock:
            gpu = f"{gpu}  {clock} MHz"
        self.m_gpu[1].set_text(gpu)
        self.m_fan[1].set_text(_fmt(tel.get("fan_rpm"), 0, " rpm"))
        if tel.get("on_ac"):
            self.m_bat[1].set_text("AC")
            source = "AC"
        else:
            self.m_bat[1].set_text(
                f"{_fmt(tel.get('battery_pct'), 0, '%')}  {_fmt(tel.get('battery_w'), 1, 'W')}"
            )
            source = "Battery"
        self.hero_sub.set_text(f"{self.settings.profile.title()} · {source}")
        self.spark.push(tel.get("package_w") or tel.get("gpu_power_w") or tel.get("cpu_temp_c"))
        live = tel.get("cpu_temp_c")
        if tel.get("gpu_temp_c") is not None:
            live = max(live or 0.0, tel["gpu_temp_c"])
        self.fan_graph.set_live_temp(live)
        return True


class MaxctlApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id="dev.maxctl.Maxctl")
        self.connect("activate", self._activate)

    def _activate(self, *_args) -> None:
        win = MaxctlWindow(self)
        win.present()


def main(argv: list[str] | None = None) -> int:
    app = MaxctlApp()
    return app.run(argv or sys.argv)
