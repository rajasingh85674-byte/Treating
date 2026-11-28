# -*- coding: utf-8 -*-
"""patterns.py - bullish and bearish pattern analysis (two separate detector sets) and a PAPER preview.

Everything is read from the real candles the live feed delivered. With no fresh candles the result is None
("NO LIVE DATA"): nothing is shown and nothing is invented.

Bullish detectors   hammer, bullish engulfing, morning star, three white soldiers, breakout above the last 20 candles,
                    higher highs with higher lows.
Bearish detectors   shooting star, bearish engulfing, evening star, three black crows, breakdown below the last
                    20 candles, lower highs with lower lows.
Each detector returns a strength 0..1. A pattern is a description of what the candles did, not a promise.

paper_preview() only computes numbers for a PAPER idea (entry, stop 1.5 ATR away, target at 2R, quantity from the 1%
risk rule). It never places anything: to trade, use BUY / SELL on the HUD, where the agents must approve and the
CONFIRM popup is shown.
"""
import math
import time
from typing import Callable, Dict, List, Optional, Tuple

from agents import Candle, atr
from risk_manager import RiskManager

MAX_AGE = 300.0
MIN_CANDLES = 30
BREAK_LOOKBACK = 20


def _ok(c: Candle) -> bool:
    return all(math.isfinite(x) for x in (c.o, c.h, c.l, c.c)) and c.h >= c.l


def _body(c: Candle) -> float:
    return abs(c.c - c.o)


def _range(c: Candle) -> float:
    return max(c.h - c.l, 1e-12)


def _up(c: Candle) -> bool:
    return c.c > c.o


def _down(c: Candle) -> bool:
    return c.c < c.o


# ---------------- bullish ----------------
def hammer(cs: List[Candle]) -> float:
    c = cs[-1]
    lower = min(c.o, c.c) - c.l
    upper = c.h - max(c.o, c.c)
    if _body(c) > 0 and lower >= 2 * _body(c) and upper <= _body(c) and _down(cs[-4]) and cs[-1].c < cs[-4].c:
        return min(1.0, 0.5 + lower / _range(c) * 0.5)
    return 0.0


def bull_engulf(cs: List[Candle]) -> float:
    a, b = cs[-2], cs[-1]
    if _down(a) and _up(b) and b.o <= a.c and b.c >= a.o and _body(b) > _body(a):
        return min(1.0, 0.55 + 0.45 * _body(b) / _range(b))
    return 0.0


def morning_star(cs: List[Candle]) -> float:
    a, b, c = cs[-3], cs[-2], cs[-1]
    if _down(a) and _body(a) > 0 and _body(b) < 0.4 * _body(a) and _up(c) and c.c > (a.o + a.c) / 2:
        return 0.75
    return 0.0


def three_soldiers(cs: List[Candle]) -> float:
    a, b, c = cs[-3:]
    if _up(a) and _up(b) and _up(c) and b.c > a.c and c.c > b.c and b.o > a.o and c.o > b.o:
        return 0.7
    return 0.0


def breakout_up(cs: List[Candle]) -> float:
    prior = cs[-(BREAK_LOOKBACK + 1):-1]
    top = max(x.h for x in prior)
    if cs[-1].c > top:
        return min(1.0, 0.6 + (cs[-1].c - top) / max(atr(cs), 1e-9) * 0.4)
    return 0.0


def higher_highs(cs: List[Candle]) -> float:
    a, b, c = cs[-9:-6], cs[-6:-3], cs[-3:]
    hs = [max(x.h for x in g) for g in (a, b, c)]
    ls = [min(x.l for x in g) for g in (a, b, c)]
    return 0.65 if hs[0] < hs[1] < hs[2] and ls[0] < ls[1] < ls[2] else 0.0


# ---------------- bearish ----------------
def shooting_star(cs: List[Candle]) -> float:
    c = cs[-1]
    upper = c.h - max(c.o, c.c)
    lower = min(c.o, c.c) - c.l
    if _body(c) > 0 and upper >= 2 * _body(c) and lower <= _body(c) and _up(cs[-4]) and cs[-1].c > cs[-4].c:
        return min(1.0, 0.5 + upper / _range(c) * 0.5)
    return 0.0


def bear_engulf(cs: List[Candle]) -> float:
    a, b = cs[-2], cs[-1]
    if _up(a) and _down(b) and b.o >= a.c and b.c <= a.o and _body(b) > _body(a):
        return min(1.0, 0.55 + 0.45 * _body(b) / _range(b))
    return 0.0


def evening_star(cs: List[Candle]) -> float:
    a, b, c = cs[-3], cs[-2], cs[-1]
    if _up(a) and _body(a) > 0 and _body(b) < 0.4 * _body(a) and _down(c) and c.c < (a.o + a.c) / 2:
        return 0.75
    return 0.0


def three_crows(cs: List[Candle]) -> float:
    a, b, c = cs[-3:]
    if _down(a) and _down(b) and _down(c) and b.c < a.c and c.c < b.c and b.o < a.o and c.o < b.o:
        return 0.7
    return 0.0


def breakdown(cs: List[Candle]) -> float:
    prior = cs[-(BREAK_LOOKBACK + 1):-1]
    bottom = min(x.l for x in prior)
    if cs[-1].c < bottom:
        return min(1.0, 0.6 + (bottom - cs[-1].c) / max(atr(cs), 1e-9) * 0.4)
    return 0.0


def lower_lows(cs: List[Candle]) -> float:
    a, b, c = cs[-9:-6], cs[-6:-3], cs[-3:]
    hs = [max(x.h for x in g) for g in (a, b, c)]
    ls = [min(x.l for x in g) for g in (a, b, c)]
    return 0.65 if hs[0] > hs[1] > hs[2] and ls[0] > ls[1] > ls[2] else 0.0


BULLISH: Tuple[Tuple[str, Callable[[List[Candle]], float]], ...] = (
    ("Hammer", hammer), ("Bullish engulfing", bull_engulf), ("Morning star", morning_star),
    ("Three white soldiers", three_soldiers), ("Breakout above 20-candle high", breakout_up),
    ("Higher highs and higher lows", higher_highs))
BEARISH: Tuple[Tuple[str, Callable[[List[Candle]], float]], ...] = (
    ("Shooting star", shooting_star), ("Bearish engulfing", bear_engulf), ("Evening star", evening_star),
    ("Three black crows", three_crows), ("Breakdown below 20-candle low", breakdown),
    ("Lower highs and lower lows", lower_lows))


def _run(table, cs: List[Candle]) -> List[Dict]:
    found = []
    for name, fn in table:
        try:
            s = float(fn(cs))
        except Exception:
            s = 0.0
        if s > 0 and math.isfinite(s):
            found.append({"name": name, "strength": round(min(1.0, s), 2)})
    found.sort(key=lambda r: -r["strength"])
    return found


def analyze(candles: List[Candle], now: Optional[float] = None) -> Optional[Dict]:
    """Bullish and bearish pattern lists with scores, or None for NO LIVE DATA."""
    try:
        t = time.time() if now is None else now
        cs = [c for c in candles if _ok(c)]
        if len(cs) < MIN_CANDLES or t - cs[-1].t > MAX_AGE:
            return None
        bull, bear = _run(BULLISH, cs), _run(BEARISH, cs)
        bs = round(min(1.0, sum(p["strength"] for p in bull) / 2.0), 2)
        es = round(min(1.0, sum(p["strength"] for p in bear) / 2.0), 2)
        lean = "BULLISH" if bs - es >= 0.2 else "BEARISH" if es - bs >= 0.2 else "BALANCED"
        return {"bullish": bull, "bearish": bear, "bull_score": bs, "bear_score": es, "lean": lean}
    except Exception:
        return None


def paper_preview(candles: List[Candle], lean: str, equity: float, cash: Optional[float] = None,
                  sizer: Optional[Callable[[str, float], Tuple[float, Optional[float], int]]] = None,
                  symbol: str = "", min_rr: float = 2.0) -> Optional[Dict]:
    """PAPER idea in numbers (BUY for a bullish lean, SELL for a bearish one). Preview only, nothing is sent."""
    try:
        if lean not in ("BULLISH", "BEARISH") or len(candles) < MIN_CANDLES:
            return None
        price = candles[-1].c
        a = atr(candles)
        if not (math.isfinite(price) and price > 0 and math.isfinite(a) and a > 0 and equity > 0):
            return None
        buy = lean == "BULLISH"
        risk_dist = 1.5 * a
        sl = price - risk_dist if buy else price + risk_dist
        tp = price + min_rr * risk_dist if buy else price - min_rr * risk_dist
        vpu, notional, max_units = sizer(symbol, price) if sizer is not None else (1.0, None, 100000)
        qty = RiskManager.position_size(equity, price, sl, cash, max_units, vpu, notional)
        return {"side": "BUY" if buy else "SELL", "entry": price, "sl": sl, "tp": tp, "qty": int(qty),
                "risk_amount": round(qty * abs(price - sl) * vpu, 2), "rr": min_rr,
                "note": "PAPER preview only. To trade, use BUY / SELL on the HUD: the agents must approve and the "
                        "CONFIRM popup appears. Risk is capped at 1% and the day at 2%."}
    except Exception:
        return None


def report(res: Optional[Dict]) -> str:
    if not res:
        return "NO LIVE DATA: pattern analysis needs fresh candles (at least %d)." % MIN_CANDLES

    def block(title, rows):
        if not rows:
            return "%s: none found in the latest candles." % title
        return "%s:\n%s" % (title, "\n".join("  - %s (strength %.0f%%)" % (r["name"], r["strength"] * 100) for r in rows))
    return "\n".join([block("BULLISH patterns", res["bullish"]), block("BEARISH patterns", res["bearish"]),
                      "Bullish score %.0f%% | bearish score %.0f%% | lean %s. A pattern describes the candles, it is "
                      "not a promise." % (res["bull_score"] * 100, res["bear_score"] * 100, res["lean"])])
