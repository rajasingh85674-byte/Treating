# -*- coding: utf-8 -*-
"""ui_hud.py - sci-fi camera-viewfinder HUD widgets and the theme engine for Tactical Neural Matrix.

Everything is drawn with Kivy canvas instructions (no image files):
  * HudFrame     - glowing panel with corner brackets, a faint status grid and an optional scan line
  * HudButton    - terminal button with bracket corners; HoldButton needs a 0.9 s press (panic actions)
  * MicButton    - large push-to-talk button with crosshairs, tick ring and pulse / radar animation
  * HudChart     - live 1-minute candles with crosshair, reticle ring, edge ticks and level lines
  * AgentTile    - tappable agent card
  * StatusGrid   - small key/value cells for the header
  * HudNav       - bottom navigation bar
  * PnlPanel     - real-money ledger: live balance, winnings, losses, net P&L (numbers glide to new values)
All animation runs from ONE shared 15 frames-per-second timer (ANIM) which can be switched off to save battery.
"""
import math
import time
import weakref

from kivy.clock import Clock
from kivy.core.text import Label as CoreLabel
from kivy.graphics import Color, Line, Rectangle, RoundedRectangle
from kivy.metrics import dp, sp
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.widget import Widget

# --------------------------------------------------------------------------- #
# Theme engine
# --------------------------------------------------------------------------- #
THEMES = {
    "TIGER HUD": {"bg": "#010103", "panel": "#070A10", "a1": "#00E5FF", "a2": "#FF8800", "up": "#00E5FF",
                  "down": "#FF8800", "text": "#EAF6FF", "muted": "#7F94A8", "danger": "#FF3B47", "ok": "#2BFF88"},
    "AMBER RECON": {"bg": "#030201", "panel": "#0E0903", "a1": "#FF8800", "a2": "#FFC24A", "up": "#FFC24A",
                    "down": "#FF5A1F", "text": "#FFF1DC", "muted": "#9A8266", "danger": "#FF3B47", "ok": "#FFC24A"},
    "ICE VISOR": {"bg": "#010305", "panel": "#06101A", "a1": "#00E5FF", "a2": "#7AB8FF", "up": "#00E5FF",
                  "down": "#7A8CFF", "text": "#E8F8FF", "muted": "#6E8EA3", "danger": "#FF3B47", "ok": "#00E5FF"},
    "NIGHT GREEN": {"bg": "#010301", "panel": "#06110A", "a1": "#2BFF88", "a2": "#FF8800", "up": "#2BFF88",
                    "down": "#FF3B47", "text": "#E6FFF0", "muted": "#6D9A7F", "danger": "#FF3B47", "ok": "#2BFF88"},
}
THEME_NAMES = list(THEMES.keys())
P = {}


def _rgba(h, a=1.0):
    h = h.lstrip("#")
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0, a)


def apply_theme(name):
    """Switch the whole palette. Widgets read P when they are built or redrawn."""
    theme = THEMES.get(name, THEMES["TIGER HUD"])
    for key, value in theme.items():
        P[key] = _rgba(value)
        P["hx_" + key] = value.lstrip("#").lower()


apply_theme("TIGER HUD")


def rgba(key, alpha=1.0):
    c = P[key]
    return (c[0], c[1], c[2], alpha)


def hex_of(color):
    return "%02x%02x%02x" % tuple(int(c * 255) for c in color[:3])


def inr(x):
    """Indian digit grouping, e.g. Rs 1,00,000.00 (plain 'Rs' so every phone font can show it)."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "-"
    sign = "-" if x < 0 else ""
    whole, frac = ("%.2f" % abs(x)).split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return "%sRs %s.%s" % (sign, whole, frac)


def esc(text):
    return str(text).replace("&", "&amp;").replace("[", "&bl;").replace("]", "&br;")


# --------------------------------------------------------------------------- #
# One shared animation clock
# --------------------------------------------------------------------------- #
class HudAnimator:
    def __init__(self):
        self.phase = 0.0
        self.enabled = True
        self._widgets = weakref.WeakSet()
        self._event = None

    def register(self, widget):
        self._widgets.add(widget)

    def start(self):
        if self._event is None:
            self._event = Clock.schedule_interval(self._tick, 1.0 / 15.0)

    def stop(self):
        if self._event is not None:
            self._event.cancel()
            self._event = None

    def _tick(self, dt):
        if not self.enabled:
            return
        self.phase = (self.phase + dt) % 100000.0
        for w in list(self._widgets):
            try:
                if w.parent is not None:
                    w.on_phase(self.phase)
            except Exception:
                pass


ANIM = HudAnimator()


def draw_brackets(x, y, w, h, size, width=1.6):
    """Four L-shaped viewfinder corners inside the box (x, y, w, h). Colour must be set by the caller."""
    s = min(size, w / 2.0, h / 2.0)
    Line(points=[x, y + s, x, y, x + s, y], width=width)
    Line(points=[x + w - s, y, x + w, y, x + w, y + s], width=width)
    Line(points=[x, y + h - s, x, y + h, x + s, y + h], width=width)
    Line(points=[x + w - s, y + h, x + w, y + h, x + w, y + h - s], width=width)


# --------------------------------------------------------------------------- #
# Text and frames
# --------------------------------------------------------------------------- #
class AutoLabel(Label):
    """Wraps to the available width and grows in height, so text never overlaps or gets clipped."""

    def __init__(self, **kw):
        kw.setdefault("size_hint_y", None)
        kw.setdefault("markup", True)
        kw.setdefault("halign", "left")
        kw.setdefault("valign", "top")
        kw.setdefault("color", P["text"])
        kw.setdefault("font_size", sp(14))
        super().__init__(**kw)
        self.height = dp(24)
        self.bind(width=self._wrap, texture_size=self._fit)

    def _wrap(self, *_):
        self.text_size = (max(self.width, 1), None)

    def _fit(self, *_):
        self.height = self.texture_size[1] + dp(4)


def heading(text, accent=None):
    return AutoLabel(text="[b][color=%s]%s[/color][/b]" % (hex_of(accent or P["a1"]), text), font_size=sp(12))


class HudFrame(BoxLayout):
    """Dark panel with viewfinder corner brackets, a faint status grid and (optionally) a sweeping scan line.
    Its height follows its content unless fixed=True."""

    def __init__(self, accent=None, scan=False, fixed=False, **kw):
        kw.setdefault("orientation", "vertical")
        kw.setdefault("padding", dp(12))
        kw.setdefault("spacing", dp(4))
        if not fixed:
            kw.setdefault("size_hint_y", None)
        super().__init__(**kw)
        self.accent = accent or P["a1"]
        self.scan = scan
        if not fixed:
            self.bind(minimum_height=self.setter("height"))
        self.bind(pos=self._draw, size=self._draw)
        ANIM.register(self)

    def _draw(self, *_):
        self.canvas.before.clear()
        x, y, w, h = self.x, self.y, self.width, self.height
        with self.canvas.before:
            Color(*P["panel"])
            Rectangle(pos=self.pos, size=self.size)
            Color(self.accent[0], self.accent[1], self.accent[2], 0.045)
            step = dp(22)
            gx = x + step
            while gx < x + w:
                Line(points=[gx, y, gx, y + h], width=1)
                gx += step
            gy = y + step
            while gy < y + h:
                Line(points=[x, gy, x + w, gy], width=1)
                gy += step
            for grow, alpha in ((dp(3), 0.05), (dp(1.5), 0.10)):
                Color(self.accent[0], self.accent[1], self.accent[2], alpha)
                Line(rectangle=(x - grow, y - grow, w + 2 * grow, h + 2 * grow), width=grow)
            Color(self.accent[0], self.accent[1], self.accent[2], 0.35)
            Line(rectangle=(x, y, w, h), width=1)
        self._draw_dynamic(ANIM.phase)

    def _draw_dynamic(self, phase):
        self.canvas.after.clear()
        x, y, w, h = self.x, self.y, self.width, self.height
        pulse = 0.75 + 0.25 * math.sin(phase * 3.0)
        with self.canvas.after:
            Color(self.accent[0], self.accent[1], self.accent[2], pulse)
            draw_brackets(x, y, w, h, dp(16), dp(1.8))
            alt = P["a2"] if self.accent == P["a1"] else P["a1"]
            Color(alt[0], alt[1], alt[2], 0.9)
            Line(points=[x + w - dp(34), y + h, x + w - dp(10), y + h], width=dp(1.8))
            if self.scan and ANIM.enabled:
                sy = y + ((phase * 0.18) % 1.0) * h
                Color(self.accent[0], self.accent[1], self.accent[2], 0.16)
                Line(points=[x + dp(2), sy, x + w - dp(2), sy], width=dp(1.4))

    def on_phase(self, phase):
        self._draw_dynamic(phase)


# --------------------------------------------------------------------------- #
# Buttons
# --------------------------------------------------------------------------- #
class HudButton(Button):
    """Terminal button: dark body, bracket corners, glowing border. filled=True gives a solid accent body."""

    wrap = True                  # False: one line only, see NavButton

    def __init__(self, text="", accent=None, filled=False, height=48, **kw):
        kw.setdefault("size_hint_y", None)
        super().__init__(text=text, background_normal="", background_down="", background_color=(0, 0, 0, 0),
                         font_size=sp(13), bold=True, **kw)
        self.height = dp(height)
        self.accent = accent or P["a1"]
        self.filled = filled
        self.halign = "center"
        self.valign = "middle"
        self.bind(pos=self._draw, size=self._draw, state=self._draw, text_size=self._noop)
        if self.wrap:
            self.bind(width=lambda inst, w: setattr(inst, "text_size", (max(w - dp(10), 1), None)))
        self._apply_text_color()
        self._draw()

    def _noop(self, *_):
        pass

    def set_accent(self, accent, filled=None):
        self.accent = accent
        if filled is not None:
            self.filled = filled
        self._apply_text_color()
        self._draw()

    def _apply_text_color(self):
        self.color = (0.01, 0.01, 0.02, 1) if self.filled else self.accent

    def _draw(self, *_):
        self.canvas.before.clear()
        x, y, w, h = self.x, self.y, self.width, self.height
        down = self.state == "down"
        a = self.accent
        with self.canvas.before:
            if self.filled:
                Color(a[0], a[1], a[2], 0.95 if not down else 0.70)
            else:
                Color(a[0], a[1], a[2], 0.07 if not down else 0.28)
            Rectangle(pos=(x, y), size=(w, h))
            for grow, alpha in ((dp(2.5), 0.07), (dp(1.2), 0.15)):
                Color(a[0], a[1], a[2], alpha)
                Line(rectangle=(x - grow, y - grow, w + 2 * grow, h + 2 * grow), width=grow)
            Color(a[0], a[1], a[2], 0.85)
            Line(rectangle=(x, y, w, h), width=1)
            Color(1, 1, 1, 0.9 if self.filled else 0.0)
            draw_brackets(x + dp(2), y + dp(2), w - dp(4), h - dp(4), dp(8), 1.4)
            if not self.filled:
                Color(a[0], a[1], a[2], 1)
                draw_brackets(x, y, w, h, dp(10), dp(1.8))


class HoldButton(HudButton):
    """Must be held for hold_time seconds. A bar fills along the bottom edge; release early to cancel."""

    def __init__(self, text="", on_hold=None, hold_time=0.9, **kw):
        super().__init__(text=text, **kw)
        self.on_hold = on_hold
        self.hold_time = hold_time
        self.progress = 0.0
        self._t0 = 0.0
        self._ev = None

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos) and not self.disabled:
            touch.grab(self)
            self.state = "down"
            self._t0 = time.time()
            self.progress = 0.0
            if self._ev is None:
                self._ev = Clock.schedule_interval(self._hold_tick, 1.0 / 30.0)
            return True
        return False

    def on_touch_up(self, touch):
        if touch.grab_current is self:
            touch.ungrab(self)
            self._cancel()
            return True
        return False

    def _cancel(self):
        if self._ev is not None:
            self._ev.cancel()
            self._ev = None
        self.progress = 0.0
        self.state = "normal"
        self._draw_progress()

    def _hold_tick(self, dt):
        self.progress = min(1.0, (time.time() - self._t0) / self.hold_time)
        self._draw_progress()
        if self.progress >= 1.0:
            self._cancel()
            if self.on_hold:
                self.on_hold()

    def _draw_progress(self):
        self.canvas.after.clear()
        if self.progress <= 0:
            return
        with self.canvas.after:
            Color(1, 1, 1, 0.95)
            Rectangle(pos=(self.x, self.y), size=(self.width * self.progress, dp(5)))


class NavButton(HudButton):
    """Bottom menu tab. The label always stays on ONE line: it is never wrapped, and the font shrinks a little
    when the phone is narrow, so AGENTS never turns into AGENT / S."""
    wrap = False
    BASE = 12.5
    SMALLEST = 7.0

    def __init__(self, **kw):
        super().__init__(**kw)
        self.text_size = (None, None)
        self.halign = "center"
        self.bind(width=self._fit, text=self._fit)
        Clock.schedule_once(self._fit, 0)

    def _fit(self, *_):
        avail = max(self.width - dp(8), 1)
        size = sp(self.BASE)
        try:
            lab = CoreLabel(text=self.text, font_size=size, bold=True)
            need = float(lab.get_extents(self.text)[0])
            if need > avail:
                size = max(sp(self.SMALLEST), size * avail / need)
        except Exception:
            size = sp(10)
        if abs(size - self.font_size) > 0.2:
            self.font_size = size


class HudNav(BoxLayout):
    """Bottom navigation: one HudButton per screen. The active one is filled."""

    def __init__(self, items, on_select, current, **kw):
        kw.setdefault("orientation", "horizontal")
        kw.setdefault("size_hint_y", None)
        kw.setdefault("spacing", dp(4))
        super().__init__(**kw)
        self.height = dp(44)
        self.buttons = {}
        for key, label in items:
            b = NavButton(text=label, accent=P["a1"], filled=(key == current), height=44)
            b.bind(on_release=lambda inst, k=key: on_select(k))
            self.buttons[key] = b
            self.add_widget(b)

    def select(self, current):
        for key, b in self.buttons.items():
            b.set_accent(P["a2"] if key == current else P["a1"], filled=(key == current))


# --------------------------------------------------------------------------- #
# Push-to-talk microphone
# --------------------------------------------------------------------------- #
class MicButton(Widget):
    """Crosshair microphone. Press and hold to talk. States: idle, listening, processing."""

    def __init__(self, on_press=None, on_release=None, **kw):
        kw.setdefault("size_hint", (None, None))
        super().__init__(**kw)
        self.size = (dp(150), dp(150))
        self.cb_press = on_press
        self.cb_release = on_release
        self.state_name = "idle"
        self.level = 0.0                 # 0..1 loudness while listening
        self.bind(pos=self._redraw, size=self._redraw)
        ANIM.register(self)

    def set_state(self, name):
        if name != self.state_name:
            self.state_name = name
            self._redraw()

    def set_level(self, value):
        self.level = max(0.0, min(1.0, float(value)))

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            touch.grab(self)
            if self.cb_press:
                self.cb_press()
            return True
        return False

    def on_touch_up(self, touch):
        if touch.grab_current is self:
            touch.ungrab(self)
            if self.cb_release:
                self.cb_release()
            return True
        return False

    def on_phase(self, phase):
        self._redraw()

    def _redraw(self, *_):
        self.canvas.clear()
        cx, cy = self.center_x, self.center_y
        r = min(self.width, self.height) / 2.0 * 0.62
        phase = ANIM.phase
        st = self.state_name
        a1, a2 = P["a1"], P["a2"]
        main = a2 if st == "listening" else a1
        with self.canvas:
            # soft glow discs
            for k, alpha in ((1.55, 0.04), (1.3, 0.07), (1.12, 0.10)):
                Color(main[0], main[1], main[2], alpha)
                Line(circle=(cx, cy, r * k), width=dp(3.5))
            # body
            Color(P["panel"][0], P["panel"][1], P["panel"][2], 1)
            RoundedRectangle(pos=(cx - r, cy - r), size=(2 * r, 2 * r), radius=[r])
            Color(main[0], main[1], main[2], 0.9)
            Line(circle=(cx, cy, r), width=dp(1.8))
            # tick ring
            tick_r1, tick_r2 = r * 1.12, r * 1.22
            spin = (phase * 18.0) % 360.0 if st != "idle" else 0.0
            for i in range(36):
                ang = math.radians(i * 10.0 + spin)
                long_tick = (i % 3 == 0)
                r2 = tick_r2 + (r * 0.06 if long_tick else 0.0)
                Color(a1[0], a1[1], a1[2], 0.85 if long_tick else 0.4)
                Line(points=[cx + tick_r1 * math.sin(ang), cy + tick_r1 * math.cos(ang),
                             cx + r2 * math.sin(ang), cy + r2 * math.cos(ang)], width=1.2)
            # crosshair with a gap around the centre
            gap, far = r * 0.78, r * 1.62
            Color(a2[0], a2[1], a2[2], 0.9)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                Line(points=[cx + dx * gap, cy + dy * gap, cx + dx * far, cy + dy * far], width=dp(1.3))
            # viewfinder brackets around the whole control
            half = min(self.width, self.height) / 2.0 * 0.98
            Color(a1[0], a1[1], a1[2], 0.9)
            draw_brackets(cx - half, cy - half, 2 * half, 2 * half, dp(14), dp(2))
            # state animation
            if st == "idle":
                pulse = 0.5 + 0.5 * math.sin(phase * 2.4)
                Color(a1[0], a1[1], a1[2], 0.25 + 0.35 * pulse)
                Line(circle=(cx, cy, r * (1.04 + 0.05 * pulse)), width=dp(1.2))
            elif st == "listening":
                for k in range(3):
                    f = (phase * 0.9 + k / 3.0) % 1.0
                    Color(a2[0], a2[1], a2[2], 0.55 * (1.0 - f))
                    Line(circle=(cx, cy, r * (1.0 + 0.55 * f + 0.25 * self.level)), width=dp(1.6 + 2.0 * self.level))
            else:
                Color(a1[0], a1[1], a1[2], 0.95)
                start = (phase * 260.0) % 360.0
                Line(circle=(cx, cy, r * 1.06, start, start + 110), width=dp(2.4))
            # microphone glyph
            gw, gh = r * 0.34, r * 0.66
            Color(main[0], main[1], main[2], 1)
            RoundedRectangle(pos=(cx - gw / 2.0, cy - gh * 0.18), size=(gw, gh), radius=[gw / 2.0])
            Line(circle=(cx, cy + gh * 0.08, r * 0.34, 90, 270), width=dp(1.8))
            Line(points=[cx, cy - r * 0.26, cx, cy - r * 0.40], width=dp(1.8))
            Line(points=[cx - r * 0.18, cy - r * 0.40, cx + r * 0.18, cy - r * 0.40], width=dp(1.8))


# --------------------------------------------------------------------------- #
# Chart
# --------------------------------------------------------------------------- #
class HudChart(Widget):
    """Live 1-minute candles with a camera-style overlay: crosshair on the last price, pulsing reticle,
    edge ticks, corner brackets and a slow vertical scan line."""
    SHOWN = 60

    def __init__(self, height=260, **kw):
        kw.setdefault("size_hint_y", None)
        super().__init__(**kw)
        self.height = dp(height)
        self.candles = []
        self.levels = []
        self._geo = None
        self.bind(pos=self._draw, size=self._draw)
        ANIM.register(self)

    def update(self, candles, levels=None):
        self.candles = list(candles[-self.SHOWN:])
        self.levels = list(levels or [])
        self._draw()

    def _scale(self):
        data = self.candles
        hi = max(c.h for c in data)
        lo = min(c.l for c in data)
        for price, _c in self.levels:
            if lo - (hi - lo) <= price <= hi + (hi - lo):
                hi, lo = max(hi, price), min(lo, price)
        span = (hi - lo) or 1.0
        return lo - span * 0.06, hi + span * 0.06

    def _draw(self, *_):
        self.canvas.clear()
        self._geo = None
        with self.canvas:
            Color(P["bg"][0] + 0.008, P["bg"][1] + 0.012, P["bg"][2] + 0.02, 1)
            Rectangle(pos=self.pos, size=self.size)
            data = self.candles
            if len(data) < 2 or self.width < 10 or self.height < 10:
                return
            lo, hi = self._scale()
            span = hi - lo

            def ypx(p):
                return self.y + (p - lo) / span * self.height

            Color(P["a1"][0], P["a1"][1], P["a1"][2], 0.10)
            for k in range(1, 5):
                gy = self.y + self.height * k / 5.0
                Line(points=[self.x, gy, self.x + self.width, gy], width=1)
            pad = dp(6)
            step = (self.width - 2 * pad - dp(26)) / float(len(data))
            bw = max(1.0, step * 0.62)
            for i, c in enumerate(data):
                cx = self.x + pad + step * (i + 0.5)
                Color(*(P["up"] if c.c >= c.o else P["down"]))
                Line(points=[cx, ypx(c.l), cx, ypx(c.h)], width=1)
                y1, y2 = ypx(min(c.o, c.c)), ypx(max(c.o, c.c))
                Rectangle(pos=(cx - bw / 2.0, y1), size=(bw, max(1.0, y2 - y1)))
            for price, col in self.levels:
                if lo <= price <= hi:
                    Color(col[0], col[1], col[2], 0.95)
                    Line(points=[self.x, ypx(price), self.x + self.width, ypx(price)], width=dp(1.2), dash_length=dp(5),
                         dash_offset=dp(4))
            last = data[-1]
            self._geo = (self.x + pad + step * (len(data) - 0.5), ypx(last.c))
        self._draw_overlay(ANIM.phase)

    def _draw_overlay(self, phase):
        self.canvas.after.clear()
        x, y, w, h = self.x, self.y, self.width, self.height
        with self.canvas.after:
            Color(P["a1"][0], P["a1"][1], P["a1"][2], 0.95)
            draw_brackets(x + dp(2), y + dp(2), w - dp(4), h - dp(4), dp(18), dp(2))
            Color(P["a2"][0], P["a2"][1], P["a2"][2], 0.9)
            Line(points=[x + dp(2), y + h - dp(2), x + dp(46), y + h - dp(2)], width=dp(2))
            for k in range(1, 20):
                ty = y + h * k / 20.0
                ln = dp(7) if k % 5 == 0 else dp(3.5)
                Color(P["a1"][0], P["a1"][1], P["a1"][2], 0.55)
                Line(points=[x + w - ln - dp(2), ty, x + w - dp(2), ty], width=1)
            if ANIM.enabled:
                sx = x + ((phase * 0.12) % 1.0) * w
                Color(P["a1"][0], P["a1"][1], P["a1"][2], 0.10)
                Line(points=[sx, y + dp(3), sx, y + h - dp(3)], width=dp(1.4))
            if self._geo is not None:
                gx, gy = self._geo
                pulse = 0.5 + 0.5 * math.sin(phase * 4.0)
                Color(P["a2"][0], P["a2"][1], P["a2"][2], 0.7)
                Line(points=[x + dp(4), gy, gx - dp(14), gy], width=1, dash_length=dp(4), dash_offset=dp(4))
                Line(points=[gx + dp(14), gy, x + w - dp(30), gy], width=1, dash_length=dp(4), dash_offset=dp(4))
                Line(points=[gx, y + dp(4), gx, gy - dp(14)], width=1, dash_length=dp(4), dash_offset=dp(4))
                Color(P["a2"][0], P["a2"][1], P["a2"][2], 0.5 + 0.5 * pulse)
                Line(circle=(gx, gy, dp(8) + dp(4) * pulse), width=dp(1.5))
                Color(1, 1, 1, 0.9)
                Line(circle=(gx, gy, dp(2.2)), width=dp(1.5))

    def on_phase(self, phase):
        self._draw_overlay(phase)


# --------------------------------------------------------------------------- #
# Agent tile and status grid
# --------------------------------------------------------------------------- #
class AgentTile(ButtonBehavior, BoxLayout):
    """One tappable agent. The border colour follows the agent's signal; the selected tile gets bright brackets."""

    def __init__(self, name, **kw):
        kw.setdefault("orientation", "vertical")
        kw.setdefault("size_hint_y", None)
        kw.setdefault("padding", (dp(10), dp(6)))
        super().__init__(**kw)
        self.agent = name
        self.edge = P["muted"]
        self.selected = False
        self.label = AutoLabel(text="[b]%s[/b]" % esc(name), font_size=sp(12))
        self.add_widget(self.label)
        self.bind(minimum_height=self.setter("height"), pos=self._draw, size=self._draw, state=self._draw)
        self._draw()

    def set_content(self, text, edge, selected):
        self.label.text = text
        self.edge = edge
        self.selected = selected
        self._draw()

    def _draw(self, *_):
        self.canvas.before.clear()
        x, y, w, h = self.x, self.y, self.width, self.height
        e = self.edge
        with self.canvas.before:
            Color(e[0], e[1], e[2], 0.22 if self.state == "down" else (0.12 if self.selected else 0.05))
            Rectangle(pos=(x, y), size=(w, h))
            Color(e[0], e[1], e[2], 0.55)
            Line(rectangle=(x, y, w, h), width=1)
            Color(e[0], e[1], e[2], 1.0)
            Line(points=[x, y, x, y + h], width=dp(3))
            if self.selected:
                Color(P["a2"][0], P["a2"][1], P["a2"][2], 1)
                draw_brackets(x, y, w, h, dp(12), dp(2))


class StatusGrid(GridLayout):
    """Small key / value cells, for example FEED, PRICE, NSE, RISK."""

    def __init__(self, keys, cols=3, **kw):
        kw.setdefault("size_hint_y", None)
        kw.setdefault("spacing", dp(4))
        super().__init__(cols=cols, **kw)
        self.bind(minimum_height=self.setter("height"))
        self.cells = {}
        for key in keys:
            lab = AutoLabel(text="", font_size=sp(12))
            self.cells[key] = lab
            self.add_widget(lab)

    def set(self, key, value, color=None):
        lab = self.cells.get(key)
        if lab is not None:
            lab.text = "[size=%d][color=%s]%s[/color][/size]\n[b][color=%s]%s[/color][/b]" % (
                int(sp(9)), P["hx_muted"], key, hex_of(color or P["text"]), esc(value))


# --------------------------------------------------------------------------- #
# Real-money ledger
# --------------------------------------------------------------------------- #
class PnlPanel(HudFrame):
    """Live balance and realized results. The big numbers glide to their new value instead of jumping and
    the balance flashes green or red when a trade closes in profit or loss."""
    GLIDE = 0.22                    # share of the remaining distance covered per animation frame

    def __init__(self, **kw):
        kw.setdefault("accent", P["a2"])
        super().__init__(**kw)
        self.add_widget(heading("REAL-MONEY LEDGER", P["a2"]))
        self.lbl_balance = AutoLabel(text="", font_size=sp(26), halign="center")
        self.add_widget(self.lbl_balance)
        self.lbl_sub = AutoLabel(text="", font_size=sp(11), color=P["muted"], halign="center")
        self.add_widget(self.lbl_sub)
        self.grid = StatusGrid(["WINNINGS", "LOSSES", "NET P&L", "TODAY", "OPEN P&L", "TRADES"], cols=3)
        self.add_widget(self.grid)
        self.target = {"balance": 0.0, "profit": 0.0, "loss": 0.0, "net": 0.0}
        self.shown = dict(self.target)
        self.extra = {"day_net": 0.0, "open_pnl": 0.0, "trades": 0, "wins": 0, "losses": 0, "live": False, "note": ""}
        self._seen_trades = None
        self._flash_until = 0.0
        self._flash_col = P["text"]
        self._first = True

    def set_values(self, balance, profit, loss, net, day_net, open_pnl, trades, wins, losses, live, note=""):
        new = {"balance": float(balance), "profit": float(profit), "loss": float(loss), "net": float(net)}
        if self._seen_trades is not None and trades != self._seen_trades:
            # a trade just closed: flash by the sign of the change in net result
            self._flash_col = P["ok"] if new["net"] >= self.target["net"] else P["danger"]
            self._flash_until = time.time() + 1.4
        self._seen_trades = trades
        self.target = new
        self.extra = {"day_net": day_net, "open_pnl": open_pnl, "trades": trades, "wins": wins, "losses": losses,
                      "live": live, "note": note}
        if self._first or not ANIM.enabled:
            self.shown = dict(new)
            self._first = False
        self._render()

    def on_phase(self, phase):
        super().on_phase(phase)
        moving = False
        for key, goal in self.target.items():
            cur = self.shown[key]
            if abs(goal - cur) < 0.005:
                self.shown[key] = goal
                continue
            self.shown[key] = cur + (goal - cur) * self.GLIDE
            moving = True
        if moving or time.time() < self._flash_until + 0.1:
            self._render()

    def _tone(self, value):
        return P["ok"] if value > 0.004 else P["danger"] if value < -0.004 else P["text"]

    def _render(self):
        s, e = self.shown, self.extra
        col = self._flash_col if time.time() < self._flash_until else P["text"]
        self.lbl_balance.text = "[b][color=%s]%s[/color][/b]" % (hex_of(col), inr(s["balance"]))
        self.lbl_sub.text = esc(("LIVE BALANCE (broker equity + open P&L)" if e["live"] else
                                 "NOT CONNECTED - preview only") + ((" | " + e["note"]) if e["note"] else ""))
        self.grid.set("WINNINGS", inr(s["profit"]), P["ok"])
        self.grid.set("LOSSES", inr(-s["loss"]) if s["loss"] else inr(0), P["danger"] if s["loss"] else P["text"])
        self.grid.set("NET P&L", inr(s["net"]), self._tone(s["net"]))
        self.grid.set("TODAY", inr(e["day_net"]), self._tone(e["day_net"]))
        self.grid.set("OPEN P&L", inr(e["open_pnl"]), self._tone(e["open_pnl"]))
        self.grid.set("TRADES", "%d  (W %d / L %d)" % (e["trades"], e["wins"], e["losses"]), P["a1"])
