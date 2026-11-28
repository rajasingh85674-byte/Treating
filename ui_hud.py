# -*- coding: utf-8 -*-
"""ui_hud.py - widgets and theme of Tactical Neural Matrix, drawn to the NAVY TERMINAL design (dark navy / black,
soft gradients, thin blue-grey borders, rounded cards, green / red / amber / blue badges).

Everything is drawn with Kivy canvas instructions (no image files):
  * HudFrame     - card ("panel"): vertical gradient, thin border, rounded corners
  * Heading      - card title: small capitals with a thin rule underneath
  * HudButton    - primary (blue gradient), secondary (dark) and danger (dark red) buttons; HoldButton needs a 0.9 s press
  * NavButton / HudNav - bottom bar with six tabs, the active tab is highlighted
  * Badge / BadgeRow   - small outlined status badges (DISARMED, PAPER MODE, LIVE LOCKED, NO LIVE DATA ...)
  * TickerCard   - label, price and change of one instrument, or NO LIVE DATA
  * MetricTile   - big number with a small caption (1% risk, 2% loss limit)
  * MicButton    - push-to-talk button
  * HudChart     - live candles on a grid with a LIVE / NO LIVE DATA chip
  * AgentTile    - one row of the agent list (status dot, name, job, state)
  * StatusGrid   - small key/value cells
  * PnlPanel     - ledger (PAPER virtual money or LIVE real money)
  * hud_popup    - dialog card with the same look
Only real values are drawn here: when there is nothing to show the widgets say NO LIVE DATA.
One shared 15 frames-per-second timer (ANIM) drives the few small animations and can be switched off to save battery.
"""
import math
import time
import weakref

from kivy.clock import Clock
from kivy.core.text import Label as CoreLabel
from kivy.graphics import Color, Line, Ellipse, Rectangle, RoundedRectangle
from kivy.graphics.texture import Texture
from kivy.uix.popup import Popup
from kivy.uix.stacklayout import StackLayout
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
    "NAVY TERMINAL": {"bg": "#050a12", "panel": "#0b1421", "a1": "#39a7ff", "a2": "#39d9e8", "up": "#00d68f",
                      "down": "#ff5368", "text": "#e7f0fb", "muted": "#8fa4bb", "danger": "#ff5368", "ok": "#00d68f",
                      "surface2": "#101e30", "line": "#243950", "amber": "#ffc45c", "blue": "#39a7ff",
                      "cyan": "#39d9e8", "purple": "#a879ff"},
}
DEFAULT_THEME = "NAVY TERMINAL"
THEME_NAMES = list(THEMES.keys())
P = {}


def _rgba(h, a=1.0):
    h = h.lstrip("#")
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0, a)


def apply_theme(name):
    """Switch the whole palette. Widgets read P when they are built or redrawn."""
    theme = THEMES.get(name, THEMES[DEFAULT_THEME])
    for key, value in theme.items():
        P[key] = _rgba(value)
        P["hx_" + key] = value.lstrip("#").lower()


apply_theme(DEFAULT_THEME)


def rgba(key, alpha=1.0):
    return rgba_of(P.get(key), alpha)


SAFE_HEX = "ffffff"
SAFE_RGBA = (1.0, 1.0, 1.0, 1.0)


def _hex6(text):
    """A 6-digit lowercase hex string from '#abc', 'aabbcc', '#aabbccdd' (alpha dropped) or None when it is not a color."""
    t = str(text).strip().lstrip("#").lower()
    if len(t) in (3, 4) and all(ch in "0123456789abcdef" for ch in t):
        t = "".join(ch * 2 for ch in t[:3])
    if len(t) in (6, 8) and all(ch in "0123456789abcdef" for ch in t):
        return t[:6]
    return None


def hex_of(color):
    """Hex text for Kivy markup from an (r, g, b[, a]) tuple of 0..1 numbers OR from a hex string. Anything else
    (None, '', masked text such as '***', short tuples, non-numbers, NaN) gives SAFE_HEX instead of an exception."""
    try:
        if isinstance(color, str):
            return _hex6(color) or SAFE_HEX
        parts = []
        for c in list(color)[:3]:
            v = float(c)
            if v != v:
                return SAFE_HEX
            parts.append(max(0, min(255, int(v * 255))))
        if len(parts) < 3:
            return SAFE_HEX
        return "%02x%02x%02x" % tuple(parts)
    except (TypeError, ValueError, OverflowError):
        return SAFE_HEX


def rgba_of(color, alpha=1.0):
    """An (r, g, b, a) tuple from a tuple or hex string; SAFE_RGBA for anything unusable."""
    try:
        if isinstance(color, str):
            h = _hex6(color)
            if h is None:
                return SAFE_RGBA
            return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0, float(alpha))
        vals = [float(c) for c in list(color)[:3]]
        if len(vals) < 3 or any(v != v for v in vals):
            return SAFE_RGBA
        return (max(0.0, min(1.0, vals[0])), max(0.0, min(1.0, vals[1])), max(0.0, min(1.0, vals[2])), float(alpha))
    except (TypeError, ValueError, OverflowError):
        return SAFE_RGBA


_CURRENCY = "INR"
_PRICE_DP = 2


def set_currency(code):
    """INR for the NSE book, USD for the forex book. Every money amount on screen follows it."""
    global _CURRENCY
    _CURRENCY = "USD" if str(code).upper() == "USD" else "INR"


def get_currency():
    return _CURRENCY


def set_price_format(dp):
    """Decimals used for prices (2 for shares, 5 for most forex pairs, 3 for yen pairs)."""
    global _PRICE_DP
    try:
        _PRICE_DP = max(2, min(6, int(dp)))
    except (TypeError, ValueError, OverflowError):
        _PRICE_DP = 2


def px(x):
    """A price (not money) with the decimals of the active instrument."""
    try:
        v = float(x)
        if v != v or v in (float("inf"), float("-inf")):
            return "-"
        return "%.*f" % (_PRICE_DP, v)
    except (TypeError, ValueError, OverflowError):
        return "-"


def inr(x):
    """Money in the active currency. INR: Indian digit grouping, e.g. Rs 1,00,000.00 (plain 'Rs' so every phone font
    can show it). USD: $1,234.56. The name is kept from the NSE-only versions."""
    try:
        x = float(x)
    except (TypeError, ValueError, OverflowError):
        return "-"
    if x != x or x in (float("inf"), float("-inf")):      # NaN or infinity (a masked or broken value): never crash
        return "-"
    sign = "-" if x < 0 else ""
    whole, frac = ("%.2f" % abs(x)).split(".")
    if _CURRENCY == "USD":
        return "%s$%s.%s" % (sign, "{:,}".format(int(whole)), frac)
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


RISK_NOTE = ("Discipline: trend, reward to risk and the invalidation level come first. These are probabilities, not "
             "promises. Any trade can lose money.")


_GRAD_CACHE = {}


def vgrad(top, bottom):
    """A 1 x 2 texture with a smooth vertical gradient (top and bottom are (r, g, b) in 0..1). Cached."""
    key = (tuple(round(v, 3) for v in top[:3]), tuple(round(v, 3) for v in bottom[:3]))
    tex = _GRAD_CACHE.get(key)
    if tex is None:
        tex = Texture.create(size=(1, 2), colorfmt="rgba")
        row = lambda c: bytes(int(max(0.0, min(1.0, v)) * 255) for v in (c[0], c[1], c[2])) + b"\xff"
        tex.blit_buffer(row(bottom) + row(top), colorfmt="rgba", bufferfmt="ubyte")
        tex.mag_filter = "linear"
        tex.min_filter = "linear"
        _GRAD_CACHE[key] = tex
    return tex


def _mix(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


class Heading(AutoLabel):
    """Card title: small bold capitals and a thin rule underneath."""

    def __init__(self, text="", **kw):
        kw.setdefault("font_size", sp(12))
        kw.setdefault("color", rgba_of("#dce9f8"))
        super().__init__(text=text, **kw)
        self.bind(pos=self._rule, size=self._rule)

    def _fit(self, *_):
        self.height = self.texture_size[1] + dp(16)

    def _rule(self, *_):
        self.canvas.after.clear()
        with self.canvas.after:
            Color(*rgba_of("#263b52"))
            Line(points=[self.x, self.y + dp(5), self.right, self.y + dp(5)], width=1)


def heading(text, accent=None):
    return Heading(text="[b]%s[/b]" % str(text).upper())


class HudFrame(BoxLayout):
    """Card: vertical gradient, thin blue-grey border, rounded corners. Its height follows its content unless
    fixed=True. accent and scan are accepted for older callers; the NAVY TERMINAL design has no accent edge."""

    def __init__(self, accent=None, scan=False, fixed=False, **kw):
        kw.setdefault("orientation", "vertical")
        kw.setdefault("padding", dp(14))
        kw.setdefault("spacing", dp(5))
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
            Color(1, 1, 1, 1)
            RoundedRectangle(pos=(x, y), size=(w, h), radius=[dp(7)],
                             texture=vgrad(rgba_of("#0d1928"), rgba_of("#09121e")))
            Color(*rgba_of("#2a4058"))
            Line(rounded_rectangle=(x, y, w, h, dp(7)), width=1)
            Color(1, 1, 1, 0.035)
            Line(points=[x + dp(8), y + h - 1, x + w - dp(8), y + h - 1], width=1)

    def on_phase(self, phase):
        pass


# --------------------------------------------------------------------------- #
# Buttons
# --------------------------------------------------------------------------- #
class HudButton(Button):
    """Button in three looks. A filled button in the blue accent is the PRIMARY button (blue gradient). A danger-colour
    button is the DANGER button (dark red). Every other button is SECONDARY (dark, thin border); its text takes the
    accent colour when the accent carries meaning (green armed, muted locked)."""

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

    def _kind(self):
        a = tuple(self.accent)[:3]
        if a == tuple(P["danger"])[:3] or a == tuple(P["down"])[:3]:
            return "danger"
        if self.filled and a in (tuple(P["a1"])[:3], tuple(P["a2"])[:3], tuple(P["amber"])[:3]):
            return "primary"
        if self.filled:
            return "solid"
        return "secondary"

    def _apply_text_color(self):
        kind = self._kind()
        if kind == "primary" or kind == "solid":
            self.color = (1, 1, 1, 1)
        elif kind == "danger":
            self.color = rgba_of("#ffdce0")
        else:
            a = tuple(self.accent)[:3]
            neutral = a in (tuple(P["a1"])[:3], tuple(P["a2"])[:3])
            self.color = P["text"] if neutral else self.accent

    def _draw(self, *_):
        self.canvas.before.clear()
        x, y, w, h = self.x, self.y, self.width, self.height
        down = self.state == "down"
        kind = self._kind()
        r = dp(4)
        with self.canvas.before:
            Color(1, 1, 1, 1)
            if kind == "primary":
                top, bot, edge = ("#167dc1", "#10558b", "#3b9ee2") if not down else ("#10558b", "#0c4670", "#3b9ee2")
                RoundedRectangle(pos=(x, y), size=(w, h), radius=[r], texture=vgrad(rgba_of(top), rgba_of(bot)))
                Color(*rgba_of(edge))
            elif kind == "solid":
                a = tuple(self.accent)[:3]
                top, bot = _mix(a, (0, 0, 0), 0.20 if not down else 0.45), _mix(a, (0, 0, 0), 0.50 if not down else 0.62)
                RoundedRectangle(pos=(x, y), size=(w, h), radius=[r], texture=vgrad(top, bot))
                Color(*_mix(a, (1, 1, 1), 0.12), 1)
            elif kind == "danger":
                Color(*rgba_of("#51232c" if not down else "#6a2d38"))
                RoundedRectangle(pos=(x, y), size=(w, h), radius=[r])
                Color(*rgba_of("#85424e"))
            else:
                Color(*rgba_of("#101f31" if not down else "#173654"))
                RoundedRectangle(pos=(x, y), size=(w, h), radius=[r])
                Color(*rgba_of("#38516d"))
            Line(rounded_rectangle=(x, y, w, h, r), width=1)


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
    when the phone is narrow, so AGENTS never turns into AGENT / S. The active tab is highlighted."""
    wrap = False
    BASE = 11.5
    SMALLEST = 7.0

    def __init__(self, **kw):
        self.active = bool(kw.get("filled", False))
        super().__init__(**kw)
        self.text_size = (None, None)
        self.halign = "center"
        self.bind(width=self._fit, text=self._fit)
        Clock.schedule_once(self._fit, 0)

    def set_active(self, on):
        self.active = bool(on)
        self._apply_text_color()
        self._draw()

    def _apply_text_color(self):
        self.color = (1, 1, 1, 1) if getattr(self, "active", False) else rgba_of("#94a6bc")

    def _draw(self, *_):
        self.canvas.before.clear()
        x, y, w, h = self.x, self.y, self.width, self.height
        with self.canvas.before:
            if getattr(self, "active", False):
                Color(*rgba_of("#112b43"))
                RoundedRectangle(pos=(x, y), size=(w, h), radius=[dp(4)])
                Color(*rgba_of("#2f80b7"))
                Line(rounded_rectangle=(x, y, w, h, dp(4)), width=1)
            elif self.state == "down":
                Color(*rgba_of("#0c1d2e"))
                RoundedRectangle(pos=(x, y), size=(w, h), radius=[dp(4)])

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
    """Bottom navigation bar: dark strip with a thin top border and one tab per screen."""

    def __init__(self, items, on_select, current, **kw):
        kw.setdefault("orientation", "horizontal")
        kw.setdefault("size_hint_y", None)
        kw.setdefault("spacing", dp(4))
        kw.setdefault("padding", (dp(6), dp(6)))
        super().__init__(**kw)
        self.height = dp(54)
        self.buttons = {}
        for key, label in items:
            b = NavButton(text=label, accent=P["a1"], filled=(key == current), height=42)
            b.bind(on_release=lambda inst, k=key: on_select(k))
            self.buttons[key] = b
            self.add_widget(b)
        self.bind(pos=self._bar, size=self._bar)

    def _bar(self, *_):
        self.canvas.before.clear()
        with self.canvas.before:
            Color(*rgba_of("#060d17", 0.96))
            Rectangle(pos=self.pos, size=self.size)
            Color(*rgba_of("#29415c"))
            Line(points=[self.x, self.top, self.right, self.top], width=1)

    def select(self, current):
        for key, b in self.buttons.items():
            b.set_active(key == current)


# --------------------------------------------------------------------------- #
# Badges, tickers, metric tiles, dialogs
# --------------------------------------------------------------------------- #
BADGE_TONES = {"green": ("#00d68f", "#28654f"), "red": ("#ff5368", "#70404a"), "amber": ("#ffc45c", "#6a5835"),
               "blue": ("#39a7ff", "#365c80"), "muted": ("#8fa4bb", "#243950")}


class Badge(Label):
    """Small outlined status badge, for example DISARMED (green), PAPER MODE (blue), LIVE LOCKED (red)."""

    def __init__(self, text="", tone="muted", dot=False, **kw):
        kw.setdefault("size_hint", (None, None))
        super().__init__(text="", markup=True, bold=True, font_size=sp(10), **kw)
        self.dot = dot
        self.tone = "muted"
        self.set(text, tone)
        self.bind(pos=self._draw, size=self._draw)

    def set(self, text, tone=None):
        if tone in BADGE_TONES:
            self.tone = tone
        self.color = rgba_of(BADGE_TONES[self.tone][0])
        self.text = esc(str(text).upper())
        self.texture_update()
        tw, th = self.texture_size
        self.size = (tw + dp(18) + (dp(12) if self.dot else 0), max(th, dp(12)) + dp(10))
        self.padding_x = 0
        self._draw()

    def _draw(self, *_):
        self.canvas.before.clear()
        text_col, edge = BADGE_TONES[self.tone]
        with self.canvas.before:
            Color(*rgba_of("#0a1320"))
            RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(4)])
            Color(*rgba_of(edge))
            Line(rounded_rectangle=(self.x, self.y, self.width, self.height, dp(4)), width=1)
            if self.dot:
                Color(*rgba_of(text_col))
                d = dp(6)
                Ellipse(pos=(self.x + dp(8), self.center_y - d / 2.0), size=(d, d))
        self.canvas.after.clear()

    def on_texture_size(self, *_):
        pass


class BadgeRow(StackLayout):
    """Badges that wrap onto the next line when the phone is narrow."""

    def __init__(self, **kw):
        kw.setdefault("orientation", "lr-tb")
        kw.setdefault("size_hint_y", None)
        kw.setdefault("spacing", (dp(6), dp(6)))
        super().__init__(**kw)
        self.bind(minimum_height=self.setter("height"))


class TickerCard(HudFrame):
    """Label, price and change of one instrument, with a LIVE or NO DATA badge. Never shows a made-up number."""

    def __init__(self, **kw):
        kw.setdefault("padding", (dp(10), dp(8)))
        kw.setdefault("spacing", dp(1))
        super().__init__(**kw)
        self.lbl_name = AutoLabel(text="", font_size=sp(10), color=P["muted"])
        self.lbl_price = AutoLabel(text="", font_size=sp(16))
        self.lbl_chg = AutoLabel(text="", font_size=sp(10))
        for w in (self.lbl_name, self.lbl_price, self.lbl_chg):
            self.add_widget(w)

    def show(self, name, price_text, change_text, tone, badge):
        """tone: 'up', 'down' or 'muted'. badge: short text such as LIVE or NO DATA."""
        col = {"up": P["hx_up"], "down": P["hx_down"], "danger": P["hx_danger"], "amber": P["hx_amber"]}.get(
            tone, P["hx_muted"])
        self.lbl_name.text = esc(name)
        self.lbl_price.text = "[b][color=%s]%s[/color][/b]" % (P["hx_text"] if tone in ("up", "down", "muted") else col,
                                                              esc(price_text))
        self.lbl_chg.text = "[color=%s]%s[/color]" % (col, esc(change_text if change_text else badge))


class MetricTile(HudFrame):
    """A big number with a small caption, for example RISK PER TRADE 1%."""

    def __init__(self, caption, value, color, **kw):
        kw.setdefault("padding", (dp(12), dp(8)))
        kw.setdefault("spacing", dp(0))
        super().__init__(**kw)
        self.add_widget(AutoLabel(text=esc(caption), font_size=sp(10), color=P["muted"]))
        self.lbl_value = AutoLabel(text="[b][color=%s]%s[/color][/b]" % (hex_of(color), esc(value)), font_size=sp(24))
        self.add_widget(self.lbl_value)


def notice(text, tone="amber"):
    """The small coloured note box (HTML .notice): amber by default, red or green for states."""
    col = {"amber": ("#f0ce8c", "#241d11", "#66502a"), "red": ("#ffb3bc", "#2a1519", "#70404a"),
           "green": ("#9be8cc", "#0f2a22", "#28654f")}.get(tone, ("#f0ce8c", "#241d11", "#66502a"))
    box = BoxLayout(orientation="vertical", size_hint_y=None, padding=dp(9))
    lab = AutoLabel(text=esc(text), font_size=sp(11), color=rgba_of(col[0]))
    box.add_widget(lab)
    box.bind(minimum_height=box.setter("height"))

    def draw(*_):
        box.canvas.before.clear()
        with box.canvas.before:
            Color(*rgba_of(col[1]))
            RoundedRectangle(pos=box.pos, size=box.size, radius=[dp(4)])
            Color(*rgba_of(col[2]))
            Line(rounded_rectangle=(box.x, box.y, box.width, box.height, dp(4)), width=1)
    box.bind(pos=draw, size=draw)
    box.label = lab
    return box


def hud_popup(**kw):
    """A Popup with the dialog look: dark navy card, thin border rule, title in small capitals."""
    kw.setdefault("background", "")
    kw.setdefault("background_color", rgba_of("#0d1a2b"))
    kw.setdefault("separator_color", rgba_of("#55728f"))
    kw.setdefault("title_color", rgba_of("#e7f0fb"))
    kw.setdefault("overlay_color", (0, 0, 0, 0.6))
    if "title" in kw:
        kw["title"] = str(kw["title"]).upper()
    return Popup(**kw)


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
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = 0.0
        self.level = max(0.0, min(1.0, v)) if v == v else 0.0

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
        r = min(self.width, self.height) / 2.0 * 0.58
        phase = ANIM.phase
        st = self.state_name
        a1, a2 = P["a1"], P["a2"]
        main = a2 if st == "listening" else a1
        with self.canvas:
            for k, alpha in ((1.5, 0.04), (1.3, 0.07), (1.14, 0.10)):
                Color(main[0], main[1], main[2], alpha)
                Line(circle=(cx, cy, r * k), width=dp(3.5))
            Color(1, 1, 1, 1)
            Ellipse(pos=(cx - r, cy - r), size=(2 * r, 2 * r), texture=vgrad(rgba_of("#132a42"), rgba_of("#0a1626")))
            Color(*rgba_of("#2f80b7"))
            Line(circle=(cx, cy, r), width=dp(1.4))
            if st == "idle":
                pulse = 0.5 + 0.5 * math.sin(phase * 2.4)
                Color(main[0], main[1], main[2], 0.20 + 0.30 * pulse)
                Line(circle=(cx, cy, r * (1.05 + 0.05 * pulse)), width=dp(1.2))
            elif st == "listening":
                for k in range(3):
                    f = (phase * 0.9 + k / 3.0) % 1.0
                    Color(a2[0], a2[1], a2[2], 0.55 * (1.0 - f))
                    Line(circle=(cx, cy, r * (1.0 + 0.55 * f + 0.25 * self.level)), width=dp(1.6 + 2.0 * self.level))
            else:
                Color(a1[0], a1[1], a1[2], 0.95)
                start = (phase * 260.0) % 360.0
                Line(circle=(cx, cy, r * 1.08, start, start + 110), width=dp(2.4))
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
    """Live candles on a navy grid. A chip in the corner says LIVE or NO LIVE DATA. Only real candles are drawn:
    with fewer than two candles the grid stays empty."""
    SHOWN = 60

    def __init__(self, height=260, **kw):
        kw.setdefault("size_hint_y", None)
        super().__init__(**kw)
        self.height = dp(height)
        self.candles = []
        self.levels = []
        self.chip = ""                      # text of the corner chip; "" = chosen from the data
        self._geo = None
        self.bind(pos=self._draw, size=self._draw)
        ANIM.register(self)

    def update(self, candles, levels=None, chip=""):
        self.candles = list(candles[-self.SHOWN:])
        self.levels = list(levels or [])
        self.chip = str(chip or "")
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

    def _draw_chip(self, text, live):
        tcol, edge = ("#00d68f", "#28654f") if live else ("#ffce70", "#6a552b")
        bg = "#0f2a22" if live else "#1d1a10"
        try:
            lab = CoreLabel(text=text, font_size=sp(9.5), bold=True)
            lab.refresh()
            tex = lab.texture
            tw, th = tex.size
        except Exception:
            return
        x, y = self.x + dp(10), self.top - dp(10) - th - dp(8)
        w, h = tw + dp(14), th + dp(8)
        Color(*rgba_of(bg))
        RoundedRectangle(pos=(x, y), size=(w, h), radius=[dp(3)])
        Color(*rgba_of(edge))
        Line(rounded_rectangle=(x, y, w, h, dp(3)), width=1)
        Color(*rgba_of(tcol))
        Rectangle(texture=tex, pos=(x + dp(7), y + dp(4)), size=(tw, th))

    def _draw(self, *_):
        self.canvas.clear()
        self._geo = None
        with self.canvas:
            Color(1, 1, 1, 1)
            RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(5)],
                             texture=vgrad(rgba_of("#071321"), rgba_of("#08101b")))
            Color(*rgba_of("#23384f"))
            gy = self.y + dp(24)
            while gy < self.top:
                Line(points=[self.x, gy, self.right, gy], width=1)
                gy += dp(42)
            gx = self.x + dp(50)
            while gx < self.right:
                Line(points=[gx, self.y, gx, self.top], width=1)
                gx += dp(65)
            data = self.candles
            if len(data) >= 2 and self.width >= 10 and self.height >= 10:
                lo, hi = self._scale()
                span = hi - lo
                top_pad = dp(30)

                def ypx(p):
                    return self.y + dp(8) + (p - lo) / span * (self.height - top_pad - dp(8))

                pad = dp(10)
                step = (self.width - 2 * pad - dp(8)) / float(len(data))
                bw = max(1.0, step * 0.62)
                for i, c in enumerate(data):
                    cx = self.x + pad + step * (i + 0.5)
                    Color(*(rgba_of("#28c58b") if c.c >= c.o else rgba_of("#ef6675")))
                    Line(points=[cx, ypx(c.l), cx, ypx(c.h)], width=1)
                    y1, y2 = ypx(min(c.o, c.c)), ypx(max(c.o, c.c))
                    Rectangle(pos=(cx - bw / 2.0, y1), size=(bw, max(1.0, y2 - y1)))
                for price, col in self.levels:
                    if lo <= price <= hi:
                        Color(col[0], col[1], col[2], 0.95)
                        Line(points=[self.x, ypx(price), self.right, ypx(price)], width=dp(1.1), dash_length=dp(5),
                             dash_offset=dp(4))
                last = data[-1]
                self._geo = (self.x + pad + step * (len(data) - 0.5), ypx(last.c))
                Color(*rgba_of("#e7f0fb", 0.9))
                Ellipse(pos=(self._geo[0] - dp(2.5), self._geo[1] - dp(2.5)), size=(dp(5), dp(5)))
            live = len(data) >= 2
            self._draw_chip(self.chip or ("LIVE" if live else "NO LIVE DATA"), live and self.chip.upper() != "NO LIVE DATA")
            Color(*rgba_of("#29425e"))
            Line(rounded_rectangle=(self.x, self.y, self.width, self.height, dp(5)), width=1)

    def on_phase(self, phase):
        pass


# --------------------------------------------------------------------------- #
# Agent tile and status grid
# --------------------------------------------------------------------------- #
class AgentTile(ButtonBehavior, BoxLayout):
    """One row of the agent list: status dot, name and job, state. The dot and the left edge follow the agent's
    signal; the selected row is highlighted."""

    def __init__(self, name, **kw):
        kw.setdefault("orientation", "vertical")
        kw.setdefault("size_hint_y", None)
        kw.setdefault("padding", (dp(26), dp(8), dp(8), dp(8)))
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
            Color(*rgba_of("#11263b" if (self.state == "down" or self.selected) else "#091421"))
            Rectangle(pos=(x, y), size=(w, h))
            Color(*rgba_of("#20334a"))
            Line(points=[x, y, x + w, y], width=1)
            Color(e[0], e[1], e[2], 1)
            d = dp(8)
            Ellipse(pos=(x + dp(10), y + h / 2.0 - d / 2.0), size=(d, d))
            if self.selected:
                Color(*rgba_of("#2f80b7"))
                Line(points=[x, y, x, y + h], width=dp(2))


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
# Ledger (virtual money in PAPER mode, real money in LIVE mode)
# --------------------------------------------------------------------------- #
class PnlPanel(HudFrame):
    """Live balance and realized results. The big numbers glide to their new value instead of jumping and
    the balance flashes green or red when a trade closes in profit or loss."""
    GLIDE = 0.22                    # share of the remaining distance covered per animation frame

    def __init__(self, **kw):
        kw.setdefault("accent", P["a2"])
        super().__init__(**kw)
        self.head = heading("LEDGER", P["a2"])
        self.add_widget(self.head)
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

    def set_title(self, text):
        text = str(text)
        if getattr(self, "_title", None) != text:
            self._title = text
            self.head.text = "[b][color=%s]%s[/color][/b]" % (hex_of(P["a2"]), esc(text))

    def set_values(self, balance, profit, loss, net, day_net, open_pnl, trades, wins, losses, live, note=""):
        def num(x):
            try:
                v = float(x)
            except (TypeError, ValueError, OverflowError):
                return 0.0
            return v if v == v and v not in (float("inf"), float("-inf")) else 0.0
        new = {"balance": num(balance), "profit": num(profit), "loss": num(loss), "net": num(net)}
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
