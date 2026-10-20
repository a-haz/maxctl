from __future__ import annotations

import json
import sys
from collections import deque

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from maxctl import __version__, alib, diagnose, ryzenadj
from maxctl.client import HelperError, connected, request
from maxctl.const import GOVERNORS, GPU_DPM_MODES
from maxctl.fan import DEFAULT_CURVES
from maxctl.fan_graph import FanCurveGraph
from maxctl.hardware import detect_device, fill_missing_telemetry, read_telemetry
from maxctl.profiles import PROFILES, Settings
from maxctl.state import delete_preset, load_presets, save_preset

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

EPP_CHOICES = ["performance", "balance_performance", "balance_power", "power"]
PPD_CHOICES = ["power-saver", "balanced", "performance"]
PROFILE_CHOICES = ["silent", "balanced", "performance", "turbo", "egpu", "custom"]
SHIFT_CHOICES = ["off", "saving", "max"]


def _fmt(value, digits=0, suffix="", fallback="-"):
    if value is None:
        return fallback
    if digits == 0:
        return f"{int(round(value))}{suffix}"
    return f"{value:.{digits}f}{suffix}"


def _smu_row(smu: dict, name: str):
    rows = (smu or {}).get("rows") or {}
    item = rows.get(name) or {}
    return item.get("value")


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
        self.set_default_size(520, 880)
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

        self.banner = Adw.Banner(title="Helper is not installed; live sensors work, TDP apply needs install")
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
            ("egpu", "eGPU 28W"),
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

        details = Adw.PreferencesGroup(title="Live")
        self.d_pkg = Adw.ActionRow(title="Package power")
        self.d_clk = Adw.ActionRow(title="CPU clock")
        self.d_busy = Adw.ActionRow(title="GPU load")
        self.d_smu = Adw.ActionRow(title="Resolved SMU")
        self.d_epp = Adw.ActionRow(title="EPP / boost")
        self.d_apply = Adw.ActionRow(title="Last apply")
        for live in (self.d_pkg, self.d_clk, self.d_busy, self.d_smu, self.d_epp, self.d_apply):
            live.set_subtitle("-")
            details.add(live)
        page.append(details)

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
        self.epp_row.set_model(Gtk.StringList.new(EPP_CHOICES))
        self.epp_row.set_selected(1)
        self.ppd_row = Adw.ComboRow(title="Ubuntu power profile")
        self.ppd_row.set_model(Gtk.StringList.new(PPD_CHOICES))
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
        overview = Gtk.ScrolledWindow()
        overview.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        overview.set_child(clamp)

        power = Gtk.ScrolledWindow()
        power.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        power.set_child(self._build_power())

        self.stack = Adw.ViewStack()
        self.stack.add_titled(overview, "overview", "Overview")
        self.stack.add_titled(power, "power", "Power user")
        header.set_title_widget(Adw.ViewSwitcher(stack=self.stack))
        toolbar.add_bottom_bar(Adw.ViewSwitcherBar(stack=self.stack))

        self.toast.set_child(self.stack)
        toolbar.set_content(self.toast)
        self.set_content(toolbar)
        self._bind_shortcuts()
        self.refresh()

    def _build_power(self) -> Adw.Clamp:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(12)
        box.set_margin_bottom(24)
        box.set_margin_start(16)
        box.set_margin_end(16)

        smu = Adw.PreferencesGroup(
            title="SMU limits",
            description="Manual table writes STAPM / Fast / Slow / Skin separately. Times are seconds.",
        )
        self.advanced_row = Adw.SwitchRow(
            title="Manual SMU table",
            subtitle="Ignore the simple TDP slider mapping and set each PPT rail.",
        )
        self.advanced_row.connect("notify::active", self._on_advanced)
        self.stapm_row = self._spin("STAPM", "Sustained limit", 8, 54, 28)
        self.fast_row = self._spin("Fast PPT", "Short burst", 8, 62, 32)
        self.slow_row = self._spin("Slow PPT", "Average after burst", 8, 58, 30)
        self.skin_row = self._spin("Skin", "Skin / STT limit", 8, 54, 28)
        self.stapm_time_row = self._spin("STAPM time", "Seconds", 1, 300, 100)
        self.slow_time_row = self._spin("Slow time", "Seconds", 1, 30, 10)
        self.fast_boost_row = self._spin(
            "Fast boost extra",
            "Added to Fast PPT when boost is on and the manual table is off",
            0,
            15,
            0,
        )
        for row in (
            self.advanced_row,
            self.stapm_row,
            self.fast_row,
            self.slow_row,
            self.skin_row,
            self.stapm_time_row,
            self.slow_time_row,
            self.fast_boost_row,
        ):
            smu.add(row)
        box.append(smu)

        recipes = Gtk.Box(spacing=8)
        recipes.set_homogeneous(True)
        for name, label in (("flat", "Flat"), ("burst", "Burst"), ("efficiency", "Efficiency")):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", self._recipe, name)
            recipes.append(btn)
        box.append(recipes)

        live = Adw.PreferencesGroup(
            title="Live SMU",
            description="From RyzenAdj -i. VALUE is current draw; LIMIT is what the SMU accepted. NaN is common on Strix Point.",
        )
        self.s_stapm = Adw.ActionRow(title="STAPM")
        self.s_fast = Adw.ActionRow(title="Fast PPT")
        self.s_slow = Adw.ActionRow(title="Slow PPT")
        self.s_meta = Adw.ActionRow(title="PM table")
        for row in (self.s_stapm, self.s_fast, self.s_slow, self.s_meta):
            row.set_subtitle("-")
            live.add(row)
        box.append(live)

        cpu = Adw.PreferencesGroup(title="CPU clocks")
        self.gov_row = Adw.ComboRow(title="Governor")
        self.gov_row.set_model(Gtk.StringList.new(list(GOVERNORS)))
        self.gov_row.set_selected(0)
        self.cpu_min_row = self._spin("Min", "0 = driver default", 0, 5500, 0)
        self.cpu_max_row = self._spin("Max", "0 = full boost range", 0, 5500, 0)
        self.nonlinear_row = Adw.SwitchRow(
            title="Nonlinear min",
            subtitle="Raise the floor to amd_pstate_lowest_nonlinear_freq (less idle drain, less low-clock stutter).",
        )
        cpu.add(self.gov_row)
        cpu.add(self.cpu_min_row)
        cpu.add(self.cpu_max_row)
        cpu.add(self.nonlinear_row)
        box.append(cpu)

        gfx = Adw.PreferencesGroup(title="iGPU DPM")
        self.dpm_row = Adw.ComboRow(title="Performance level")
        self.dpm_row.set_model(Gtk.StringList.new(list(GPU_DPM_MODES)))
        self.dpm_row.set_selected(0)
        self.dpm_row.set_subtitle("Used when automatic clocks are on. Manual range forces amdgpu manual.")
        gfx.add(self.dpm_row)
        box.append(gfx)

        extra = Adw.PreferencesGroup(title="Power extras")
        self.shift_row = Adw.ComboRow(title="RyzenAdj power shift")
        self.shift_row.set_model(Gtk.StringList.new(SHIFT_CHOICES))
        self.shift_row.set_selected(0)
        self.shift_row.set_subtitle("off leaves the SMU flag alone. saving / max call RyzenAdj.")
        self.charge_row = self._spin("Charge limit", "0 = do not change. Needs charge_control_end_threshold.", 0, 100, 0)
        extra.add(self.shift_row)
        extra.add(self.charge_row)
        box.append(extra)

        fan = Adw.PreferencesGroup(title="Fan / helper")
        self.slew_row = self._spin("Curve slew", "PWM steps per tick", 2, 64, 12)
        self.reapply_row = self._spin("Re-apply", "Seconds between SMU writes", 1, 30, 4)
        fan.add(self.slew_row)
        fan.add(self.reapply_row)
        box.append(fan)

        plug = Adw.PreferencesGroup(title="AC / battery profiles")
        self.ac_row = Adw.ComboRow(title="On AC")
        self.ac_row.set_model(Gtk.StringList.new(PROFILE_CHOICES))
        self.ac_row.set_selected(1)
        self.dc_row = Adw.ComboRow(title="On battery")
        self.dc_row.set_model(Gtk.StringList.new(PROFILE_CHOICES))
        self.dc_row.set_selected(0)
        plug.add(self.ac_row)
        plug.add(self.dc_row)
        self.egpu_switch_row = Adw.SwitchRow(
            title="eGPU profile when an external GPU is attached",
            subtitle="Caps the 890M so the shared power budget goes to the CPU. Overrides AC / battery.",
        )
        self.egpu_switch_row.set_active(True)
        self.egpu_row = Adw.ComboRow(title="With eGPU")
        self.egpu_row.set_model(Gtk.StringList.new(PROFILE_CHOICES))
        self.egpu_row.set_selected(PROFILE_CHOICES.index("egpu"))
        plug.add(self.egpu_switch_row)
        plug.add(self.egpu_row)
        box.append(plug)

        stored = Adw.PreferencesGroup(title="Named presets")
        self.preset_entry = Adw.EntryRow(title="Name")
        self.preset_combo = Adw.ComboRow(title="Saved")
        stored.add(self.preset_entry)
        stored.add(self.preset_combo)
        box.append(stored)
        preset_btns = Gtk.Box(spacing=8)
        preset_btns.set_homogeneous(True)
        save_p = Gtk.Button(label="Save")
        load_p = Gtk.Button(label="Load")
        del_p = Gtk.Button(label="Delete")
        save_p.connect("clicked", self._preset_save)
        load_p.connect("clicked", self._preset_load)
        del_p.connect("clicked", self._preset_delete)
        preset_btns.append(save_p)
        preset_btns.append(load_p)
        preset_btns.append(del_p)
        box.append(preset_btns)
        self._refresh_presets()

        io = Gtk.Box(spacing=8)
        io.set_homogeneous(True)
        for label, handler in (
            ("Export JSON", self._export),
            ("Import JSON", self._import),
            ("Copy JSON", self._copy_json),
        ):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", handler)
            io.append(btn)
        box.append(io)

        tools = Gtk.Box(spacing=8)
        tools.set_homogeneous(True)
        for label, handler in (
            ("RyzenAdj -i", self._ryzenadj_info),
            ("Diagnose", self._show_diagnose),
            ("Copy ryzenadj", self._copy_ryzenadj),
        ):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", handler)
            tools.append(btn)
        box.append(tools)

        self.preview = Gtk.Label(xalign=0)
        self.preview.add_css_class("dim-label")
        self.preview.set_wrap(True)
        box.append(self.preview)

        self.diag_line = Gtk.Label(xalign=0)
        self.diag_line.add_css_class("dim-label")
        self.diag_line.set_wrap(True)
        box.append(self.diag_line)

        hint = Gtk.Label(
            label="Keys: 1 Silent  2 Balanced  3 Sport  4 Turbo  Ctrl+Enter Apply  Ctrl+E Export  Ctrl+I SMU  Ctrl+D Diagnose",
            xalign=0,
        )
        hint.add_css_class("dim-label")
        hint.set_wrap(True)
        box.append(hint)

        apply_btn = Gtk.Button(label="Apply power-user settings")
        apply_btn.add_css_class("suggested-action")
        apply_btn.add_css_class("pill")
        apply_btn.connect("clicked", self._apply)
        box.append(apply_btn)

        clamp = Adw.Clamp(maximum_size=560, tightening_threshold=400)
        clamp.set_child(box)
        self._on_advanced()
        return clamp

    def _spin(self, title: str, subtitle: str, lo: int, hi: int, value: int) -> Adw.SpinRow:
        row = Adw.SpinRow(
            title=title,
            subtitle=subtitle,
            adjustment=Gtk.Adjustment(lower=lo, upper=hi, step_increment=1, page_increment=5, value=value),
        )
        row.connect("changed", lambda *_: self._update_preview())
        return row

    def _metric(self, title: str):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("card")
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class("metric-label")
        value = Gtk.Label(label="-", xalign=0)
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
            Adw.Toast(title="In ~/Workspace/Projects/maxctl: sudo ./scripts/install.sh")
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
                self._toast_apply(reply.get("apply") or {}, f"{name.title()} applied")
            except HelperError as exc:
                self.toast.add_toast(Adw.Toast(title=str(exc)))
        else:
            self._ingest_settings(PROFILES[name].to_dict())

    def _on_tdp(self, row: Adw.SpinRow) -> None:
        self.tdp_label.set_text(str(int(row.get_value())))
        self._update_preview()

    def _on_advanced(self, *_args) -> None:
        on = self.advanced_row.get_active()
        for row in (self.stapm_row, self.fast_row, self.slow_row, self.skin_row):
            row.set_sensitive(on)
        self.fast_boost_row.set_sensitive(not on)
        self._update_preview()

    def _recipe(self, _btn, name: str) -> None:
        settings = Settings.from_dict(self._collect())
        settings.apply_recipe(name)
        self._ingest_settings(settings.to_dict())
        self.toast.add_toast(Adw.Toast(title=f"{name.title()} SMU split"))

    def _update_preview(self) -> None:
        if not hasattr(self, "preview") or not hasattr(self, "shift_row"):
            return
        settings = Settings.from_dict(self._collect())
        lim = settings.limits()
        self.preview.set_text(
            f"Will write  STAPM {lim.stapm_w}W  Fast {lim.fast_w}W  "
            f"Slow {lim.slow_w}W  Skin {lim.skin_w}W  Tctl {lim.tctl_c}°  "
            f"times {lim.stapm_time}/{lim.slow_time}s"
        )
        if hasattr(self, "d_smu"):
            self.d_smu.set_subtitle(
                f"{lim.stapm_w}/{lim.fast_w}/{lim.slow_w}W  Tctl {lim.tctl_c}°"
            )

    def _bind_shortcuts(self) -> None:
        controller = Gtk.ShortcutController()
        for key, name in (
            ("1", "silent"),
            ("2", "balanced"),
            ("3", "performance"),
            ("4", "turbo"),
            ("5", "egpu"),
        ):
            controller.add_shortcut(
                Gtk.Shortcut.new(
                    Gtk.ShortcutTrigger.parse_string(key),
                    Gtk.CallbackAction.new(lambda *_a, n=name: self._hot_profile(n) or True),
                )
            )
        for accel, fn in (
            ("<Ctrl>Return", self._apply),
            ("<Ctrl>e", self._export),
            ("<Ctrl>i", self._ryzenadj_info),
            ("<Ctrl>d", self._show_diagnose),
        ):
            controller.add_shortcut(
                Gtk.Shortcut.new(
                    Gtk.ShortcutTrigger.parse_string(accel),
                    Gtk.CallbackAction.new(lambda *_a, cb=fn: cb() or True),
                )
            )
        self.add_controller(controller)

    def _hot_profile(self, name: str) -> None:
        btn = self.profile_buttons.get(name)
        if btn:
            btn.set_active(True)

    def _bundle(self) -> dict:
        return {
            "settings": self._collect(),
            "config": {
                "auto_switch": self.auto_row.get_active(),
                "unlock": self.unlock_row.get_active(),
                "ac_profile": self.ac_row.get_model().get_string(self.ac_row.get_selected()),
                "battery_profile": self.dc_row.get_model().get_string(self.dc_row.get_selected()),
                "egpu_switch": self.egpu_switch_row.get_active(),
                "egpu_profile": self._combo_value(self.egpu_row),
                "reapply_sec": int(self.reapply_row.get_value()),
            },
        }

    def _export(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Export maxctl profile", initial_name="maxctl-profile.json")
        dialog.save(self, None, self._export_finish)

    def _export_finish(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return
        bundle = self._bundle()
        if self.helper:
            try:
                bundle = request("export").get("bundle") or bundle
            except HelperError:
                pass
        file.replace_contents(
            json.dumps(bundle, indent=2).encode(),
            None,
            False,
            Gio.FileCreateFlags.REPLACE_DESTINATION,
            None,
        )
        self.toast.add_toast(Adw.Toast(title="Profile exported"))

    def _import(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Import maxctl profile")
        dialog.open(self, None, self._import_finish)

    def _import_finish(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        ok, data, _etag = file.load_contents()
        if not ok:
            return
        bundle = json.loads(data.decode())
        self._load_bundle(bundle)

    def _load_bundle(self, bundle: dict) -> None:
        payload = bundle.get("bundle", bundle)
        if self.helper:
            try:
                reply = request("import", bundle=payload)
                self._ingest_settings(reply.get("settings") or payload.get("settings") or {})
                self._ingest_config(reply.get("config") or payload.get("config") or {})
            except HelperError as exc:
                self.toast.add_toast(Adw.Toast(title=str(exc)))
                return
        else:
            self._ingest_settings(payload.get("settings") or payload)
            self._ingest_config(payload.get("config") or {})
        self.toast.add_toast(Adw.Toast(title="Profile imported"))

    def _copy_json(self, *_args) -> None:
        self._copy_text(json.dumps(self._bundle(), indent=2), "JSON copied")

    def _copy_ryzenadj(self, *_args) -> None:
        lim = Settings.from_dict(self._collect()).limits()
        self._copy_text(ryzenadj.command_line(lim), "ryzenadj command copied")

    def _copy_text(self, text: str, title: str) -> None:
        display = Gdk.Display.get_default()
        if display:
            display.get_clipboard().set(text)
        self.toast.add_toast(Adw.Toast(title=title))

    def _refresh_presets(self) -> None:
        names = sorted(load_presets())
        self.preset_combo.set_model(Gtk.StringList.new(names or ["(none)"]))
        self.preset_combo.set_selected(0)

    def _preset_save(self, *_args) -> None:
        name = self.preset_entry.get_text().strip()
        if not name:
            self.toast.add_toast(Adw.Toast(title="Name the preset first"))
            return
        save_preset(name, self._bundle())
        self._refresh_presets()
        self.toast.add_toast(Adw.Toast(title=f"Saved {name}"))

    def _preset_load(self, *_args) -> None:
        model = self.preset_combo.get_model()
        name = model.get_string(self.preset_combo.get_selected())
        bundle = load_presets().get(name)
        if not bundle:
            self.toast.add_toast(Adw.Toast(title="No preset selected"))
            return
        self.preset_entry.set_text(name)
        self._load_bundle(bundle)

    def _preset_delete(self, *_args) -> None:
        model = self.preset_combo.get_model()
        name = model.get_string(self.preset_combo.get_selected())
        if name == "(none)":
            return
        delete_preset(name)
        self._refresh_presets()
        self.toast.add_toast(Adw.Toast(title=f"Deleted {name}"))

    def _text_dialog(self, heading: str, text: str) -> None:
        dialog = Adw.MessageDialog(transient_for=self, heading=heading)
        label = Gtk.Label(label=text or "no output", xalign=0)
        label.set_selectable(True)
        label.set_wrap(True)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(280)
        scroll.set_min_content_width(420)
        scroll.set_child(label)
        dialog.set_extra_child(scroll)
        dialog.add_response("ok", "Close")
        dialog.present()

    def _ryzenadj_info(self, *_args) -> None:
        text = ""
        if self.helper:
            try:
                smu = request("smu").get("smu") or {}
                if smu.get("error"):
                    text = str(smu["error"])
                else:
                    text = json.dumps(smu, indent=2)
            except HelperError as exc:
                text = str(exc)
        if not text:
            try:
                text = ryzenadj.info()
            except Exception as exc:
                text = str(exc)
        self._text_dialog("RyzenAdj -i", text[:8000])

    def _show_diagnose(self, *_args) -> None:
        data = diagnose.collect()
        if self.helper:
            try:
                data = request("diagnose").get("diagnose") or data
            except HelperError:
                pass
        self._text_dialog("Diagnose", diagnose.format_report(data, helper=self.helper))

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

    def _combo_value(self, row: Adw.ComboRow) -> str:
        return row.get_model().get_string(row.get_selected())

    def _set_combo(self, row: Adw.ComboRow, choices: list[str], value: str) -> None:
        if value in choices:
            row.set_selected(choices.index(value))

    def _collect(self) -> dict:
        fan_map = {0: "auto", 1: "curve", 2: "manual"}
        profile = "custom"
        for name, btn in self.profile_buttons.items():
            if btn.get_active():
                profile = name
        shift = self._combo_value(self.shift_row)
        return {
            "profile": profile,
            "tdp_w": int(self.tdp_row.get_value()),
            "tctl_c": int(self.tctl_row.get_value()),
            "boost": self.boost_row.get_active(),
            "cpu_boost": self.cpu_boost_row.get_active(),
            "epp": self._combo_value(self.epp_row),
            "ppd": self._combo_value(self.ppd_row),
            "gpu_mode": "auto" if self.gpu_auto.get_active() else "range",
            "gpu_min": int(self.gpu_min.get_value()),
            "gpu_max": int(self.gpu_max.get_value()),
            "fan_mode": fan_map[self.fan_row.get_selected()],
            "fan_pwm": int(self.fan_pwm.get_value()),
            "fan_curve": self.fan_graph.get_points(),
            "unlock": self.unlock_row.get_active(),
            "advanced": self.advanced_row.get_active(),
            "stapm_w": int(self.stapm_row.get_value()),
            "fast_w": int(self.fast_row.get_value()),
            "slow_w": int(self.slow_row.get_value()),
            "skin_w": int(self.skin_row.get_value()),
            "stapm_time": int(self.stapm_time_row.get_value()),
            "slow_time": int(self.slow_time_row.get_value()),
            "fast_boost_w": int(self.fast_boost_row.get_value()),
            "cpu_min_mhz": int(self.cpu_min_row.get_value()),
            "cpu_max_mhz": int(self.cpu_max_row.get_value()),
            "nonlinear_min": self.nonlinear_row.get_active(),
            "fan_slew": int(self.slew_row.get_value()),
            "governor": self._combo_value(self.gov_row),
            "gpu_dpm": self._combo_value(self.dpm_row),
            "charge_limit": int(self.charge_row.get_value()),
            "power_shift": "" if shift == "off" else shift,
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
        self._set_combo(self.epp_row, EPP_CHOICES, settings.epp)
        self._set_combo(self.ppd_row, PPD_CHOICES, settings.ppd)
        self.gpu_auto.set_active(settings.gpu_mode == "auto")
        self.gpu_min.set_value(settings.gpu_min)
        self.gpu_max.set_value(settings.gpu_max)
        self.fan_row.set_selected({"auto": 0, "curve": 1, "manual": 2}[settings.fan_mode])
        self.fan_pwm.set_value(settings.fan_pwm)
        self.fan_graph.set_points(settings.curve_points())
        self.advanced_row.set_active(settings.advanced)
        self.stapm_row.set_value(settings.stapm_w)
        self.fast_row.set_value(settings.fast_w)
        self.slow_row.set_value(settings.slow_w)
        self.skin_row.set_value(settings.skin_w)
        self.stapm_time_row.set_value(settings.stapm_time)
        self.slow_time_row.set_value(settings.slow_time)
        self.fast_boost_row.set_value(settings.fast_boost_w)
        self.cpu_min_row.set_value(settings.cpu_min_mhz)
        self.cpu_max_row.set_value(settings.cpu_max_mhz)
        self.nonlinear_row.set_active(settings.nonlinear_min)
        self.slew_row.set_value(settings.fan_slew)
        self._set_combo(self.gov_row, list(GOVERNORS), settings.governor)
        self._set_combo(self.dpm_row, list(GPU_DPM_MODES), settings.gpu_dpm)
        self.charge_row.set_value(settings.charge_limit)
        self._set_combo(self.shift_row, SHIFT_CHOICES, settings.power_shift or "off")
        for name, btn in self.profile_buttons.items():
            btn.set_active(settings.profile == name)
        self._refresh_turbo_label()
        self._on_advanced()
        self._applying = False

    def _ingest_config(self, cfg: dict) -> None:
        if "auto_switch" in cfg:
            self.auto_row.set_active(bool(cfg["auto_switch"]))
        if "unlock" in cfg:
            self.unlock_row.set_active(bool(cfg["unlock"]))
        if cfg.get("ac_profile") in PROFILE_CHOICES:
            self.ac_row.set_selected(PROFILE_CHOICES.index(cfg["ac_profile"]))
        if cfg.get("battery_profile") in PROFILE_CHOICES:
            self.dc_row.set_selected(PROFILE_CHOICES.index(cfg["battery_profile"]))
        if "egpu_switch" in cfg:
            self.egpu_switch_row.set_active(bool(cfg["egpu_switch"]))
        if cfg.get("egpu_profile") in PROFILE_CHOICES:
            self.egpu_row.set_selected(PROFILE_CHOICES.index(cfg["egpu_profile"]))
        if cfg.get("reapply_sec"):
            self.reapply_row.set_value(float(cfg["reapply_sec"]))

    def _toast_apply(self, apply: dict, ok_title: str) -> None:
        errors = apply.get("errors") or []
        if errors:
            self.toast.add_toast(Adw.Toast(title=errors[0][:80]))
            return
        self.toast.add_toast(Adw.Toast(title=ok_title))

    def _apply(self, *_args) -> None:
        payload = self._collect()
        if not self.helper:
            self.toast.add_toast(Adw.Toast(title="Install the helper to apply TDP"))
            return
        try:
            reply = request("set_settings", settings=payload)
            request(
                "set_config",
                config={
                    "auto_switch": self.auto_row.get_active(),
                    "unlock": self.unlock_row.get_active(),
                    "ac_profile": self._combo_value(self.ac_row),
                    "battery_profile": self._combo_value(self.dc_row),
                    "egpu_switch": self.egpu_switch_row.get_active(),
                    "egpu_profile": self._combo_value(self.egpu_row),
                    "reapply_sec": float(self.reapply_row.get_value()),
                },
            )
            self._toast_apply(reply.get("apply") or {}, f"Applied {payload['tdp_w']}W")
        except HelperError as exc:
            self.toast.add_toast(Adw.Toast(title=str(exc)))

    def _update_smu_live(self, smu: dict) -> None:
        def pair(limit_name: str, value_name: str) -> str:
            limit = _smu_row(smu, limit_name)
            value = _smu_row(smu, value_name)
            if limit is None and value is None:
                return "n/a"
            return f"{_fmt(limit, 1, ' W')} limit · {_fmt(value, 1, ' W')} now"

        self.s_stapm.set_subtitle(pair("STAPM LIMIT", "STAPM VALUE"))
        self.s_fast.set_subtitle(pair("PPT LIMIT FAST", "PPT VALUE FAST"))
        self.s_slow.set_subtitle(pair("PPT LIMIT SLOW", "PPT VALUE SLOW"))
        meta = (smu or {}).get("meta") or {}
        err = (smu or {}).get("error")
        if err:
            self.s_meta.set_subtitle(str(err)[:80])
        else:
            ver = meta.get("PM Table Version") or meta.get("Version") or "-"
            family = meta.get("CPU Family") or ""
            self.s_meta.set_subtitle(f"{family}  {ver}".strip())

    def refresh(self) -> bool:
        self.helper = connected()
        self.banner.set_revealed(not self.helper)
        tel = None
        smu = {}
        last_apply = {}
        if self.helper:
            try:
                status = request("status")
                tel = status.get("telemetry") or {}
                smu = status.get("smu") or {}
                last_apply = status.get("last_apply") or {}
                if not self._synced and not self._applying:
                    self._ingest_settings(status.get("settings") or {})
                    self._ingest_config(status.get("config") or {})
                    self._synced = True
                backend = status.get("tdp_backend") or "none"
                self.status_line.set_text(f"Helper online · TDP backend {backend}")
                dev = status.get("device") or {}
                self.device_label.set_text(
                    f"{dev.get('name', 'GPD')} · {dev.get('cpu', '')}"
                )
                diag = status.get("diagnose") or {}
                self.diag_line.set_text(
                    f"ALIB {'yes' if diag.get('alib') else 'no'}  "
                    f"RyzenAdj {diag.get('ryzenadj') or 'no'}  "
                    f"pstate {diag.get('amd_pstate') or '?'}  "
                    f"iomem={'relaxed' if diag.get('iomem_relaxed') else 'strict'}"
                )
            except HelperError as exc:
                self.helper = False
                self.status_line.set_text(str(exc))
        if not self.helper:
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
            self.diag_line.set_text(diagnose.format_report(helper=False).replace("\n", "  "))
        tel = fill_missing_telemetry(tel or {})

        self.m_cpu[1].set_text(_fmt(tel.get("cpu_temp_c"), 1, "°"))
        gpu = _fmt(tel.get("gpu_temp_c"), 1, "°")
        clock = tel.get("gpu_clock_mhz")
        if clock:
            gpu = f"{gpu}  {clock} MHz"
        self.m_gpu[1].set_text(gpu)
        fan = _fmt(tel.get("fan_rpm"), 0, " rpm")
        if tel.get("fan_pwm") is not None:
            fan = f"{fan}  pwm {tel['fan_pwm']}"
        self.m_fan[1].set_text(fan)
        if tel.get("on_ac"):
            self.m_bat[1].set_text("AC")
            source = "AC"
        else:
            self.m_bat[1].set_text(
                f"{_fmt(tel.get('battery_pct'), 0, '%')}  {_fmt(tel.get('battery_w'), 1, 'W')}"
            )
            source = "Battery"
        if tel.get("egpu") or tel.get("egpu_present"):
            source = f"{source} · eGPU"
        self.hero_sub.set_text(f"{self.settings.profile.title()} · {source}")
        self.spark.push(tel.get("package_w") or tel.get("gpu_power_w") or tel.get("cpu_temp_c"))
        live = tel.get("cpu_temp_c")
        if tel.get("gpu_temp_c") is not None:
            live = max(live or 0.0, tel["gpu_temp_c"])
        self.fan_graph.set_live_temp(live)
        pkg = tel.get("package_w")
        gpu_w = tel.get("gpu_power_w")
        self.d_pkg.set_subtitle(
            f"{_fmt(pkg, 1, ' W')} package · {_fmt(gpu_w, 1, ' W')} GPU"
            + (f" · {_fmt(tel.get('battery_v'), 2, ' V')}" if tel.get("battery_v") else "")
        )
        self.d_clk.set_subtitle(
            f"{_fmt(tel.get('cpu_mhz'), 0, ' MHz')}  "
            f"({_fmt(tel.get('cpu_min_mhz'))}–{_fmt(tel.get('cpu_max_mhz'))})  "
            f"{tel.get('governor') or '-'}"
        )
        busy = tel.get("gpu_busy_pct")
        self.d_busy.set_subtitle(
            f"{_fmt(busy, 0, '%')}  {tel.get('gpu_clock_mhz') or '-'} MHz  "
            f"{tel.get('gpu_perf_level') or 'auto'}"
        )
        boost = tel.get("boost")
        self.d_epp.set_subtitle(
            f"{tel.get('epp') or '-'}  boost={'on' if boost else 'off' if boost is not None else '-'}"
        )
        if last_apply:
            backend = last_apply.get("tdp_backend") or "none"
            errors = last_apply.get("errors") or []
            self.d_apply.set_subtitle(errors[0][:70] if errors else f"ok · {backend}")
        self._update_smu_live(smu)
        self._update_preview()
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
