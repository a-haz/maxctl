from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk

from maxctl.fan import CurvePoint

TEMP_MIN = 30
TEMP_MAX = 100


class FanCurveGraph(Gtk.DrawingArea):
    """Draggable temp → fan% curve. Double-click adds a point, right-click removes."""

    def __init__(self) -> None:
        super().__init__()
        self.set_content_width(420)
        self.set_content_height(220)
        self.set_hexpand(True)
        self.add_css_class("fan-curve")
        self.points: list[CurvePoint] = [
            CurvePoint(40, 25),
            CurvePoint(60, 50),
            CurvePoint(80, 90),
            CurvePoint(90, 100),
        ]
        self.live_temp: float | None = None
        self.drag_index: int | None = None
        self._start = (0.0, 0.0)
        self.on_change: Callable[[], None] | None = None
        self._pad_l = 40.0
        self._pad_r = 16.0
        self._pad_t = 14.0
        self._pad_b = 28.0
        self.set_draw_func(self._draw)
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)

        right = Gtk.GestureClick()
        right.set_button(3)
        right.connect("pressed", self._right_click)
        self.add_controller(right)

        dbl = Gtk.GestureClick()
        dbl.set_button(1)
        dbl.connect("pressed", self._left_click)
        self.add_controller(dbl)

    def set_points(self, points: list[CurvePoint]) -> None:
        cleaned = [
            CurvePoint(max(TEMP_MIN, min(TEMP_MAX, int(p.temp_c))), max(0, min(100, int(p.percent))))
            for p in points
        ]
        cleaned.sort(key=lambda p: p.temp_c)
        if len(cleaned) < 2:
            cleaned = [CurvePoint(40, 25), CurvePoint(90, 100)]
        self.points = cleaned
        self.queue_draw()

    def get_points(self) -> list[dict]:
        return [{"temp_c": p.temp_c, "percent": p.percent} for p in self.points]

    def set_live_temp(self, temp_c: float | None) -> None:
        self.live_temp = temp_c
        self.queue_draw()

    def _plot(self, width: float, height: float) -> tuple[float, float, float, float]:
        return (
            self._pad_l,
            self._pad_t,
            max(1.0, width - self._pad_l - self._pad_r),
            max(1.0, height - self._pad_t - self._pad_b),
        )

    def _to_xy(self, temp: float, percent: float, width: float, height: float) -> tuple[float, float]:
        x0, y0, w, h = self._plot(width, height)
        x = x0 + (temp - TEMP_MIN) / (TEMP_MAX - TEMP_MIN) * w
        y = y0 + (1.0 - percent / 100.0) * h
        return x, y

    def _from_xy(self, x: float, y: float, width: float, height: float) -> tuple[int, int]:
        x0, y0, w, h = self._plot(width, height)
        temp = TEMP_MIN + (x - x0) / w * (TEMP_MAX - TEMP_MIN)
        percent = (1.0 - (y - y0) / h) * 100.0
        return int(round(max(TEMP_MIN, min(TEMP_MAX, temp)))), int(round(max(0, min(100, percent))))

    def _hit(self, x: float, y: float, width: float, height: float) -> int | None:
        for i, point in enumerate(self.points):
            px, py = self._to_xy(point.temp_c, point.percent, width, height)
            if (px - x) ** 2 + (py - y) ** 2 <= 14 ** 2:
                return i
        return None

    def _notify(self) -> None:
        if self.on_change:
            self.on_change()

    def _drag_begin(self, _g, x: float, y: float) -> None:
        self._start = (x, y)
        self.drag_index = self._hit(x, y, self.get_width(), self.get_height())

    def _drag_update(self, _g, dx: float, dy: float) -> None:
        if self.drag_index is None:
            return
        sx, sy = self._start
        width, height = self.get_width(), self.get_height()
        temp, percent = self._from_xy(sx + dx, sy + dy, width, height)
        lo = TEMP_MIN if self.drag_index == 0 else self.points[self.drag_index - 1].temp_c + 2
        hi = TEMP_MAX if self.drag_index == len(self.points) - 1 else self.points[self.drag_index + 1].temp_c - 2
        self.points[self.drag_index] = CurvePoint(max(lo, min(hi, temp)), percent)
        self.queue_draw()

    def _drag_end(self, *_args) -> None:
        if self.drag_index is not None:
            self._notify()
        self.drag_index = None

    def _left_click(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        if n_press != 2:
            return
        width, height = self.get_width(), self.get_height()
        if self._hit(x, y, width, height) is not None:
            return
        temp, percent = self._from_xy(x, y, width, height)
        if any(abs(p.temp_c - temp) < 3 for p in self.points):
            return
        self.points.append(CurvePoint(temp, percent))
        self.points.sort(key=lambda p: p.temp_c)
        self.queue_draw()
        self._notify()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _right_click(self, _g, _n, x: float, y: float) -> None:
        if len(self.points) <= 2:
            return
        idx = self._hit(x, y, self.get_width(), self.get_height())
        if idx is None:
            return
        del self.points[idx]
        self.queue_draw()
        self._notify()

    def _draw(self, _area, cr, width: float, height: float) -> None:
        x0, y0, w, h = self._plot(width, height)
        cr.set_source_rgba(1, 1, 1, 0.04)
        cr.rectangle(x0, y0, w, h)
        cr.fill()

        cr.set_source_rgba(1, 1, 1, 0.12)
        cr.set_line_width(1)
        for temp in (40, 50, 60, 70, 80, 90, 100):
            x, _ = self._to_xy(temp, 0, width, height)
            cr.move_to(x, y0)
            cr.line_to(x, y0 + h)
            cr.stroke()
        for pct in (0, 25, 50, 75, 100):
            _, y = self._to_xy(TEMP_MIN, pct, width, height)
            cr.move_to(x0, y)
            cr.line_to(x0 + w, y)
            cr.stroke()

        cr.select_font_face("Cantarell", 0, 0)
        cr.set_font_size(11)
        cr.set_source_rgba(1, 1, 1, 0.45)
        for temp in (40, 60, 80, 100):
            x, y = self._to_xy(temp, 0, width, height)
            cr.move_to(x - 8, y0 + h + 16)
            cr.show_text(f"{temp}°")
        for pct in (0, 50, 100):
            x, y = self._to_xy(TEMP_MIN, pct, width, height)
            cr.move_to(8, y + 4)
            cr.show_text(f"{pct}%")

        if len(self.points) >= 2:
            cr.set_source_rgba(0.23, 0.62, 0.98, 0.18)
            cr.move_to(*self._to_xy(self.points[0].temp_c, 0, width, height))
            for point in self.points:
                cr.line_to(*self._to_xy(point.temp_c, point.percent, width, height))
            cr.line_to(*self._to_xy(self.points[-1].temp_c, 0, width, height))
            cr.close_path()
            cr.fill()

            cr.set_source_rgba(0.35, 0.72, 1.0, 1)
            cr.set_line_width(2.4)
            for i, point in enumerate(self.points):
                x, y = self._to_xy(point.temp_c, point.percent, width, height)
                if i == 0:
                    cr.move_to(x, y)
                else:
                    cr.line_to(x, y)
            cr.stroke()

            for i, point in enumerate(self.points):
                x, y = self._to_xy(point.temp_c, point.percent, width, height)
                cr.set_source_rgba(0.12, 0.18, 0.28, 1)
                cr.arc(x, y, 7, 0, 6.283)
                cr.fill()
                cr.set_source_rgba(0.45, 0.82, 1.0, 1)
                cr.arc(x, y, 5.2, 0, 6.283)
                cr.fill()
                if i == self.drag_index:
                    cr.set_source_rgba(1, 1, 1, 0.9)
                    cr.set_font_size(12)
                    cr.move_to(x + 10, y - 8)
                    cr.show_text(f"{point.temp_c}°  {point.percent}%")

        if self.live_temp is not None:
            x, _ = self._to_xy(self.live_temp, 0, width, height)
            cr.set_source_rgba(1.0, 0.72, 0.2, 0.85)
            cr.set_dash([3, 3])
            cr.move_to(x, y0)
            cr.line_to(x, y0 + h)
            cr.stroke()
            cr.set_dash([])
