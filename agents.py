# -*- coding: utf-8 -*-
"""agents.py - interactive 20-agent consensus engine, live inspection and trailing stop for Tactical Neural Matrix (NSE).

Pure Python (standard library only). Every agent returns a live status:
signal (BUY / SELL / NEUTRAL), confidence 0..1 and a text with the numbers it used.
Four agents can veto a trade: Volatility Guard, Risk Manager 1%, Fakeout Shield and Volume Flow (weak volume).
"""
import math
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import List, Optional

from risk_manager import DAILY_LOSS_LIMIT_PCT, MAX_CONSECUTIVE_LOSSES, MAX_RISK_PCT, RiskManager

BUY = "BUY"
SELL = "SELL"
NEUTRAL = "NEUTRAL"

MIN_RR = 2.0              # minimum reward : risk
MIN_AGREE = 9             # agents (of 20) that must point the same way
MIN_CANDLES = 60
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class Candle:
    t: float
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


@dataclass
class Vote:
    agent: str
    signal: str = NEUTRAL
    confidence: float = 0.0
    detail: str = ""
    veto: bool = False


@dataclass
class Decision:
    symbol: str
    direction: str = NEUTRAL
    approved: bool = False
    agree: int = 0
    against: int = 0
    total: int = 20
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    rr: float = 0.0
    qty: int = 0
    risk_amount: float = 0.0
    reason: str = ""
    votes: List[Vote] = field(default_factory=list)
    ts: float = field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# Indicators (pure Python)
# --------------------------------------------------------------------------- #
def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out, e = [], values[0]
    for v in values:
        e = v * k + e * (1.0 - k)
        out.append(e)
    return out


def sma(values: List[float], period: int) -> float:
    part = values[-period:]
    return sum(part) / len(part) if part else 0.0


def rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 50.0
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def true_ranges(candles: List[Candle]) -> List[float]:
    out = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c.h - c.l)
        else:
            pc = candles[i - 1].c
            out.append(max(c.h - c.l, abs(c.h - pc), abs(c.l - pc)))
    return out


def atr(candles: List[Candle], period: int = 14) -> float:
    trs = true_ranges(candles)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def adx(candles: List[Candle], period: int = 14):
    """Returns (adx, +DI, -DI)."""
    if len(candles) < period * 2 + 2:
        return 0.0, 0.0, 0.0
    pdm, mdm, trs = [], [], []
    for i in range(1, len(candles)):
        up = candles[i].h - candles[i - 1].h
        dn = candles[i - 1].l - candles[i].l
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
        pc = candles[i - 1].c
        trs.append(max(candles[i].h - candles[i].l, abs(candles[i].h - pc), abs(candles[i].l - pc)))
    sp_, sm_, st_ = sum(pdm[:period]), sum(mdm[:period]), sum(trs[:period])
    dxs, pdi, mdi = [], 0.0, 0.0
    for i in range(period, len(trs) + 1):
        if i > period:
            sp_ = sp_ - sp_ / period + pdm[i - 1]
            sm_ = sm_ - sm_ / period + mdm[i - 1]
            st_ = st_ - st_ / period + trs[i - 1]
        if st_ <= 0:
            continue
        pdi, mdi = 100.0 * sp_ / st_, 100.0 * sm_ / st_
        dxs.append(100.0 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0.0)
    if not dxs:
        return 0.0, 0.0, 0.0
    val = sum(dxs[:period]) / min(period, len(dxs))
    for d in dxs[period:]:
        val = (val * (period - 1) + d) / period
    return val, pdi, mdi


def swings(candles: List[Candle], k: int = 2):
    """Swing points as (index, 'H'|'L', price)."""
    out = []
    for i in range(k, len(candles) - k):
        win = candles[i - k:i + k + 1]
        if candles[i].h == max(c.h for c in win):
            out.append((i, "H", candles[i].h))
        if candles[i].l == min(c.l for c in win):
            out.append((i, "L", candles[i].l))
    return out


def slope(values: List[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mx, my = (n - 1) / 2.0, sum(values) / n
    num = sum((i - mx) * (v - my) for i, v in enumerate(values))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


# --------------------------------------------------------------------------- #
# Context shared by all agents
# --------------------------------------------------------------------------- #
class Ctx:
    pass


def build_ctx(candles: List[Candle], equity: float, risk: Optional[RiskManager], depth: Optional[dict] = None) -> Ctx:
    x = Ctx()
    x.candles = candles
    x.closes = [c.c for c in candles]
    x.last = candles[-1]
    x.prev = candles[-2]
    x.price = x.last.c
    x.e9, x.e21, x.e50 = ema(x.closes, 9), ema(x.closes, 21), ema(x.closes, 50)
    x.atr = max(atr(candles), 1e-9)
    x.rsi = rsi(x.closes)
    x.sw = swings(candles[:-1])
    x.equity = equity
    x.risk = risk
    x.depth = depth
    return x


def vote(name, signal, conf, detail, veto=False) -> Vote:
    return Vote(name, signal, max(0.0, min(1.0, conf)), detail, veto)


# --------------------------------------------------------------------------- #
# The 20 agents
# --------------------------------------------------------------------------- #
def a_structure(x):
    hs = [p for _, k, p in x.sw if k == "H"][-2:]
    ls = [p for _, k, p in x.sw if k == "L"][-2:]
    if len(hs) < 2 or len(ls) < 2:
        return vote("Market Structure", NEUTRAL, 0.1, "not enough swings")
    hh, hl = hs[-1] > hs[-2], ls[-1] > ls[-2]
    txt = "%s highs, %s lows" % ("higher" if hh else "lower", "higher" if hl else "lower")
    if hh and hl:
        return vote("Market Structure", BUY, 0.8, txt)
    if not hh and not hl:
        return vote("Market Structure", SELL, 0.8, txt)
    return vote("Market Structure", NEUTRAL, 0.2, txt + " (mixed)")


def a_sweep(x):
    hs = [p for i, k, p in x.sw if k == "H" and i < len(x.candles) - 4]
    ls = [p for i, k, p in x.sw if k == "L" and i < len(x.candles) - 4]
    for c in (x.last, x.prev):
        if hs and c.h > hs[-1] and c.c < hs[-1]:
            return vote("Liquidity Sweep", SELL, 0.7, "swept high %.2f, closed back below" % hs[-1])
        if ls and c.l < ls[-1] and c.c > ls[-1]:
            return vote("Liquidity Sweep", BUY, 0.7, "swept low %.2f, closed back above" % ls[-1])
    return vote("Liquidity Sweep", NEUTRAL, 0.1, "no sweep")


def a_choch(x):
    hs = [p for _, k, p in x.sw if k == "H"][-2:]
    ls = [p for _, k, p in x.sw if k == "L"][-2:]
    if len(hs) == 2 and hs[1] < hs[0] and x.price > hs[1]:
        return vote("CHoCH", BUY, 0.65, "close %.2f broke lower-high %.2f" % (x.price, hs[1]))
    if len(ls) == 2 and ls[1] > ls[0] and x.price < ls[1]:
        return vote("CHoCH", SELL, 0.65, "close %.2f broke higher-low %.2f" % (x.price, ls[1]))
    return vote("CHoCH", NEUTRAL, 0.1, "no character change")


def a_trend(x):
    e9, e21, e50 = x.e9[-1], x.e21[-1], x.e50[-1]
    spread = abs(e9 - e50) / x.atr
    txt = "EMA9 %.2f / 21 %.2f / 50 %.2f" % (e9, e21, e50)
    if e9 > e21 > e50 and x.price > e21:
        return vote("Trend EMA", BUY, 0.5 + min(0.4, spread * 0.1), txt)
    if e9 < e21 < e50 and x.price < e21:
        return vote("Trend EMA", SELL, 0.5 + min(0.4, spread * 0.1), txt)
    return vote("Trend EMA", NEUTRAL, 0.2, txt + " (tangled)")


def a_rsi(x):
    r = x.rsi
    txt = "RSI %.1f" % r
    if 52 <= r <= 72:
        return vote("Momentum RSI", BUY, 0.35 + (r - 50) / 40.0, txt)
    if 28 <= r <= 48:
        return vote("Momentum RSI", SELL, 0.35 + (50 - r) / 40.0, txt)
    if r > 72:
        return vote("Momentum RSI", NEUTRAL, 0.2, txt + " overbought")
    if r < 28:
        return vote("Momentum RSI", NEUTRAL, 0.2, txt + " oversold")
    return vote("Momentum RSI", NEUTRAL, 0.1, txt + " flat")


def a_volume(x):
    vols = [c.v for c in x.candles]
    base = sum(vols[-55:-5]) / 50.0 if len(vols) >= 60 else 0.0
    recent = sum(vols[-5:]) / 5.0
    if base > 0 and recent < 0.4 * base:
        return vote("Volume Flow", NEUTRAL, 1.0, "WEAK VOLUME: last 5 bars %.0f%% of normal" % (100.0 * recent / base),
                    veto=True)
    part = x.candles[-12:]
    up = sum(c.v for c in part if c.c >= c.o)
    dn = sum(c.v for c in part if c.c < c.o)
    if up + dn <= 0:
        return vote("Volume Flow", NEUTRAL, 0.0, "no volume data")
    ratio = (up + 1e-9) / (dn + 1e-9)
    txt = "up/down volume %.2f" % ratio
    if ratio > 1.25:
        return vote("Volume Flow", BUY, min(0.85, 0.4 + (ratio - 1) * 0.3), txt)
    if ratio < 0.8:
        return vote("Volume Flow", SELL, min(0.85, 0.4 + (1 / ratio - 1) * 0.3), txt)
    return vote("Volume Flow", NEUTRAL, 0.1, txt)


def a_sr(x):
    sup = [p for _, k, p in x.sw[-30:] if k == "L" and p < x.price]
    res = [p for _, k, p in x.sw[-30:] if k == "H" and p > x.price]
    ns = max(sup) if sup else None
    nr = min(res) if res else None
    d_s = (x.price - ns) / x.atr if ns is not None else 99.0
    d_r = (nr - x.price) / x.atr if nr is not None else 99.0
    txt = "support %s (%.1f ATR) / resistance %s (%.1f ATR)" % (
        "%.2f" % ns if ns else "-", d_s if d_s < 99 else 0, "%.2f" % nr if nr else "-", d_r if d_r < 99 else 0)
    if d_s < 0.8 and d_r >= 1.5:
        return vote("Support/Resistance", BUY, 0.6, txt)
    if d_r < 0.8 and d_s >= 1.5:
        return vote("Support/Resistance", SELL, 0.6, txt)
    return vote("Support/Resistance", NEUTRAL, 0.1, txt)


def a_vol_guard(x):
    trs = true_ranges(x.candles)
    recent, base = sum(trs[-3:]) / 3.0, sum(trs[-50:]) / len(trs[-50:])
    ratio = recent / base if base > 0 else 1.0
    txt = "range ratio %.2f" % ratio
    if ratio > 2.5:
        return vote("Volatility Guard", NEUTRAL, 1.0, txt + " SPIKE", veto=True)
    if ratio < 0.4:
        return vote("Volatility Guard", NEUTRAL, 1.0, txt + " dead market", veto=True)
    return vote("Volatility Guard", NEUTRAL, 0.0, txt + " calm")


def a_risk(x):
    cap = x.equity * MAX_RISK_PCT / 100.0
    st = x.risk.status() if x.risk else {"killed": False, "locked": False, "consecutive_losses": 0,
                                         "daily_pnl": 0.0, "trades_today": 0}
    txt = "1%% cap = %.2f | losses in row %d | trades today %d" % (cap, st["consecutive_losses"], st["trades_today"])
    if st["killed"]:
        return vote("Risk Manager 1%", NEUTRAL, 1.0, "KILL SWITCH is ON", veto=True)
    if st["locked"]:
        return vote("Risk Manager 1%", NEUTRAL, 1.0, "LOCKED today after %d losses" % MAX_CONSECUTIVE_LOSSES, veto=True)
    if x.equity > 0 and st["daily_pnl"] <= -x.equity * DAILY_LOSS_LIMIT_PCT / 100.0:
        return vote("Risk Manager 1%", NEUTRAL, 1.0, "daily loss limit hit (%.2f)" % st["daily_pnl"], veto=True)
    return vote("Risk Manager 1%", NEUTRAL, 0.0, txt)


def a_macd(x):
    line = [a - b for a, b in zip(ema(x.closes, 12), ema(x.closes, 26))]
    sig = ema(line, 9)
    h, hp = line[-1] - sig[-1], line[-2] - sig[-2]
    txt = "hist %.3f (prev %.3f)" % (h, hp)
    if h > 0 and h >= hp:
        return vote("MACD", BUY, 0.55, txt)
    if h < 0 and h <= hp:
        return vote("MACD", SELL, 0.55, txt)
    return vote("MACD", NEUTRAL, 0.15, txt + " fading")


def a_bollinger(x):
    part = x.closes[-20:]
    m = sum(part) / len(part)
    sd = math.sqrt(sum((v - m) ** 2 for v in part) / len(part)) or 1e-9
    pb = (x.price - (m - 2 * sd)) / (4 * sd)
    txt = "%%B %.2f" % pb
    if 0.6 <= pb <= 1.0:
        return vote("Bollinger", BUY, 0.5, txt + " riding upper band")
    if 0.0 <= pb <= 0.4:
        return vote("Bollinger", SELL, 0.5, txt + " riding lower band")
    return vote("Bollinger", NEUTRAL, 0.1, txt)


def a_vwap(x):
    part = x.candles[-60:]
    pv = sum(((c.h + c.l + c.c) / 3.0) * c.v for c in part)
    vv = sum(c.v for c in part)
    vwap = pv / vv if vv > 0 else sum(c.c for c in part) / len(part)
    txt = "VWAP %.2f, price %.2f" % (vwap, x.price)
    if x.price > vwap + 0.1 * x.atr:
        return vote("VWAP", BUY, 0.5, txt)
    if x.price < vwap - 0.1 * x.atr:
        return vote("VWAP", SELL, 0.5, txt)
    return vote("VWAP", NEUTRAL, 0.1, txt)


def a_stoch(x):
    ks = []
    for i in range(len(x.candles) - 5, len(x.candles)):
        win = x.candles[i - 13:i + 1]
        hi, lo = max(c.h for c in win), min(c.l for c in win)
        ks.append(100.0 * (x.candles[i].c - lo) / (hi - lo) if hi > lo else 50.0)
    k, d = ks[-1], sum(ks[-3:]) / 3.0
    txt = "%%K %.0f / %%D %.0f" % (k, d)
    if k > d and 20 < k < 80:
        return vote("Stochastic", BUY, 0.45, txt)
    if k < d and 20 < k < 80:
        return vote("Stochastic", SELL, 0.45, txt)
    return vote("Stochastic", NEUTRAL, 0.1, txt)


def a_adx(x):
    a, p, m = adx(x.candles)
    txt = "ADX %.1f (+DI %.1f / -DI %.1f)" % (a, p, m)
    if a >= 20 and p > m:
        return vote("ADX Strength", BUY, min(0.9, a / 50.0 + 0.2), txt)
    if a >= 20 and m > p:
        return vote("ADX Strength", SELL, min(0.9, a / 50.0 + 0.2), txt)
    return vote("ADX Strength", NEUTRAL, 0.1, txt + " weak trend")


def a_pattern(x):
    c, p = x.last, x.prev
    body, rng = abs(c.c - c.o), max(c.h - c.l, 1e-9)
    lower, upper = min(c.o, c.c) - c.l, c.h - max(c.o, c.c)
    if c.c > c.o and p.c < p.o and c.c >= p.o and c.o <= p.c:
        return vote("Candle Pattern", BUY, 0.6, "bullish engulfing")
    if c.c < c.o and p.c > p.o and c.c <= p.o and c.o >= p.c:
        return vote("Candle Pattern", SELL, 0.6, "bearish engulfing")
    if lower > 2 * body and upper < body + 0.1 * rng and x.e21[-1] >= x.e50[-1]:
        return vote("Candle Pattern", BUY, 0.5, "hammer")
    if upper > 2 * body and lower < body + 0.1 * rng and x.e21[-1] <= x.e50[-1]:
        return vote("Candle Pattern", SELL, 0.5, "shooting star")
    return vote("Candle Pattern", NEUTRAL, 0.1, "no pattern")


def a_breakout(x):
    prior = x.candles[-21:-1]
    hi, lo = max(c.h for c in prior), min(c.l for c in prior)
    txt = "20-bar range %.2f - %.2f" % (lo, hi)
    if x.price > hi:
        return vote("Breakout", BUY, 0.65, txt + " broke up")
    if x.price < lo:
        return vote("Breakout", SELL, 0.65, txt + " broke down")
    return vote("Breakout", NEUTRAL, 0.1, txt + " inside")


def a_pullback(x):
    e21, e50 = x.e21[-1], x.e50[-1]
    c = x.last
    if e21 > e50 and abs(c.l - e21) <= 0.3 * x.atr and c.c > e21:
        return vote("Pullback", BUY, 0.6, "tagged EMA21 %.2f and held" % e21)
    if e21 < e50 and abs(c.h - e21) <= 0.3 * x.atr and c.c < e21:
        return vote("Pullback", SELL, 0.6, "rejected at EMA21 %.2f" % e21)
    return vote("Pullback", NEUTRAL, 0.1, "no pullback to EMA21")


def a_range(x):
    part = x.candles[-20:]
    hi, lo = max(c.h for c in part), min(c.l for c in part)
    rng = hi - lo
    txt = "20-bar range %.1f ATR" % (rng / x.atr)
    if rng < 3.0 * x.atr:
        return vote("Range Filter", NEUTRAL, 0.1, txt + " choppy")
    pos = (x.price - lo) / rng
    if pos > 0.66:
        return vote("Range Filter", BUY, 0.45, txt + ", price in top third")
    if pos < 0.34:
        return vote("Range Filter", SELL, 0.45, txt + ", price in bottom third")
    return vote("Range Filter", NEUTRAL, 0.1, txt + ", mid-range")


def a_slope(x):
    s = slope(x.closes[-20:]) * 20.0 / x.atr
    txt = "slope %.2f ATR / 20 bars" % s
    if s > 1.0:
        return vote("Momentum Slope", BUY, min(0.85, 0.3 + s * 0.15), txt)
    if s < -1.0:
        return vote("Momentum Slope", SELL, min(0.85, 0.3 + -s * 0.15), txt)
    return vote("Momentum Slope", NEUTRAL, 0.1, txt)


def a_trap(x):
    prior = x.candles[-21:-1]
    hi, lo = max(c.h for c in prior), min(c.l for c in prior)
    avg_v = sum(c.v for c in prior) / len(prior)
    c = x.last
    rng = max(c.h - c.l, 1e-9)
    broke = c.c > hi or c.c < lo
    if broke and avg_v > 0 and c.v < 0.8 * avg_v:
        return vote("Fakeout Shield", NEUTRAL, 1.0, "breakout on weak volume (%.0f%% of avg)" % (100 * c.v / avg_v), veto=True)
    if broke and (c.h - max(c.o, c.c) > 0.6 * rng or min(c.o, c.c) - c.l > 0.6 * rng):
        return vote("Fakeout Shield", NEUTRAL, 1.0, "breakout candle has a huge wick", veto=True)
    return vote("Fakeout Shield", NEUTRAL, 0.0, "no trap detected")


AGENTS = [a_structure, a_sweep, a_choch, a_trend, a_rsi, a_volume, a_sr, a_vol_guard, a_risk, a_macd,
          a_bollinger, a_vwap, a_stoch, a_adx, a_pattern, a_breakout, a_pullback, a_range, a_slope, a_trap]

AGENT_NAMES = ["Market Structure", "Liquidity Sweep", "CHoCH", "Trend EMA", "Momentum RSI", "Volume Flow",
               "Support/Resistance", "Volatility Guard", "Risk Manager 1%", "MACD", "Bollinger", "VWAP",
               "Stochastic", "ADX Strength", "Candle Pattern", "Breakout", "Pullback", "Range Filter",
               "Momentum Slope", "Fakeout Shield"]


# --------------------------------------------------------------------------- #
# Live inspection: what exactly is each agent looking at right now?
# Every number below is recomputed from the live candles / depth the engine just analysed.
# --------------------------------------------------------------------------- #
def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M")


def _ago(x, idx: int) -> int:
    return len(x.candles) - 1 - idx


def _pts(x, kind: str, n: int = 2):
    return [(i, p) for i, k, p in x.sw if k == kind][-n:]


def _fmt_pts(x, pts) -> str:
    return ", ".join("%.2f (%d bars ago, %s)" % (p, _ago(x, i), _hhmm(x.candles[i].t)) for i, p in pts) or "none found yet"


def _depth_imbalance(x):
    d = getattr(x, "depth", None)
    if not d or not d.get("bids") or not d.get("asks"):
        return None
    bq = sum(q for _p, q, _o in d["bids"])
    aq = sum(q for _p, q, _o in d["asks"])
    if bq + aq <= 0:
        return None
    return bq, aq, bq / float(bq + aq)


def i_structure(x, v, m):
    hs, ls = _pts(x, "H"), _pts(x, "L")
    lines = ["Last swing highs: " + _fmt_pts(x, hs), "Last swing lows: " + _fmt_pts(x, ls), "Reading: " + v.detail]
    if hs and ls:
        sentence = "Last swing high %.2f, last swing low %.2f. %s." % (hs[-1][1], ls[-1][1], v.detail)
    else:
        sentence = "Not enough swing points yet."
    return sentence, lines


def i_sweep(x, v, m):
    oh = [p for i, k, p in x.sw if k == "H" and i < len(x.candles) - 4]
    ol = [p for i, k, p in x.sw if k == "L" and i < len(x.candles) - 4]
    c, p = x.last, x.prev
    lines = ["Reference swing high: %s | swing low: %s" % ("%.2f" % oh[-1] if oh else "-", "%.2f" % ol[-1] if ol else "-"),
             "Last candle: high %.2f low %.2f close %.2f" % (c.h, c.l, c.c),
             "Previous candle: high %.2f low %.2f close %.2f" % (p.h, p.l, p.c), "Reading: " + v.detail]
    sentence = "Last candle high %.2f, low %.2f, close %.2f. %s." % (c.h, c.l, c.c, v.detail)
    return sentence, lines


def i_choch(x, v, m):
    hs, ls = _pts(x, "H"), _pts(x, "L")
    lines = ["Last two swing highs: " + _fmt_pts(x, hs), "Last two swing lows: " + _fmt_pts(x, ls),
             "Current close: %.2f" % x.price, "Reading: " + v.detail]
    sentence = "Close is %.2f. %s." % (x.price, v.detail)
    return sentence, lines


def i_trend(x, v, m):
    e9, e21, e50 = x.e9[-1], x.e21[-1], x.e50[-1]
    sl21 = e21 - x.e21[-6]
    lines = ["EMA 9: %.2f | EMA 21: %.2f | EMA 50: %.2f" % (e9, e21, e50),
             "EMA 21 moved %+.2f over the last 5 bars" % sl21,
             "Price %.2f is %+.2f ATR from EMA 21 (ATR %.2f)" % (x.price, (x.price - e21) / x.atr, x.atr)]
    sentence = "EMA nine %.2f, twenty-one %.2f, fifty %.2f. Price %.2f." % (e9, e21, e50, x.price)
    return sentence, lines


def i_rsi(x, v, m):
    prev = rsi(x.closes[:-5])
    lines = ["RSI(14) now: %.1f | five bars ago: %.1f" % (x.rsi, prev), "Zone: %s" % v.detail]
    return "RSI is %.1f, five bars ago it was %.1f. %s." % (x.rsi, prev, v.detail), lines


def i_volume(x, v, m):
    vols = [c.v for c in x.candles]
    part = x.candles[-12:]
    up = sum(c.v for c in part if c.c >= c.o)
    dn = sum(c.v for c in part if c.c < c.o)
    base = sum(vols[-55:-5]) / 50.0 if len(vols) >= 60 else 0.0
    recent = sum(vols[-5:]) / 5.0
    lines = ["Last 12 bars: up-volume %.0f, down-volume %.0f" % (up, dn),
             "Last bar volume %.0f | last 5 bars average %.0f | normal (50 bars) %.0f" % (x.last.v, recent, base)]
    sentence = "Up volume %.0f against down volume %.0f over twelve bars." % (up, dn)
    if base > 0:
        sentence += " Recent volume is %.0f percent of normal." % (100.0 * recent / base)
    imb = _depth_imbalance(x)
    if imb:
        lines.append("Order book (top 5): bid quantity %d, ask quantity %d, buyers hold %.0f%%" % (imb[0], imb[1], imb[2] * 100))
        sentence += " Order book buyers hold %.0f percent of the top five levels." % (imb[2] * 100)
    else:
        lines.append("Order book: no market depth data right now")
    return sentence, lines


def i_sr(x, v, m):
    sup = [p for _, k, p in x.sw[-30:] if k == "L" and p < x.price]
    res = [p for _, k, p in x.sw[-30:] if k == "H" and p > x.price]
    ns, nr = (max(sup) if sup else None), (min(res) if res else None)
    lines = ["Nearest support: %s" % ("%.2f (%.2f below, %.1f ATR)" % (ns, x.price - ns, (x.price - ns) / x.atr) if ns else "none"),
             "Nearest resistance: %s" % ("%.2f (%.2f above, %.1f ATR)" % (nr, nr - x.price, (nr - x.price) / x.atr) if nr else "none")]
    sentence = "Support %s, resistance %s, price %.2f." % ("%.2f" % ns if ns else "none", "%.2f" % nr if nr else "none", x.price)
    return sentence, lines


def i_volguard(x, v, m):
    trs = true_ranges(x.candles)
    recent, base = sum(trs[-3:]) / 3.0, sum(trs[-50:]) / len(trs[-50:])
    ratio = recent / base if base > 0 else 1.0
    lines = ["Average range of last 3 bars: %.2f | normal (50 bars): %.2f | ratio %.2f" % (recent, base, ratio),
             "ATR(14): %.2f = %.2f%% of price" % (x.atr, 100.0 * x.atr / x.price), "Limits: veto above 2.5 or below 0.4"]
    return "Volatility ratio is %.2f. ATR is %.2f rupees." % (ratio, x.atr), lines


def i_risk(x, v, m):
    cap = x.equity * MAX_RISK_PCT / 100.0
    st = x.risk.status() if x.risk else {"killed": False, "locked": False, "consecutive_losses": 0, "daily_pnl": 0.0, "trades_today": 0}
    limit = x.equity * DAILY_LOSS_LIMIT_PCT / 100.0
    lines = ["Equity %.2f | 1%% cap per trade = %.2f" % (x.equity, cap),
             "Today's result %.2f | daily loss limit %.2f | room left %.2f" % (st["daily_pnl"], limit, limit + st["daily_pnl"]),
             "Losses in a row %d of %d | trades today %d" % (st["consecutive_losses"], MAX_CONSECUTIVE_LOSSES, st["trades_today"]),
             "Kill switch: %s | day lock: %s" % ("ON" if st["killed"] else "off", "ON" if st["locked"] else "off")]
    return ("One percent risk is %.2f rupees. Today's result %.2f. Kill switch %s." % (cap, st["daily_pnl"], "on" if st["killed"] else "off")), lines


def i_macd(x, v, m):
    line = [a - b for a, b in zip(ema(x.closes, 12), ema(x.closes, 26))]
    sig = ema(line, 9)
    lines = ["MACD line %.3f | signal %.3f" % (line[-1], sig[-1]),
             "Histogram now %.3f | previous bar %.3f" % (line[-1] - sig[-1], line[-2] - sig[-2])]
    return "MACD histogram is %.3f, previous bar %.3f." % (line[-1] - sig[-1], line[-2] - sig[-2]), lines


def i_boll(x, v, m):
    part = x.closes[-20:]
    mid = sum(part) / len(part)
    sd = math.sqrt(sum((q - mid) ** 2 for q in part) / len(part)) or 1e-9
    pb = (x.price - (mid - 2 * sd)) / (4 * sd)
    lines = ["Middle %.2f | upper %.2f | lower %.2f" % (mid, mid + 2 * sd, mid - 2 * sd),
             "Price %.2f sits at %.0f%% of the band | band width %.2f%% of price" % (x.price, pb * 100, 400.0 * sd / mid)]
    return "Price %.2f is at %.0f percent of the Bollinger band." % (x.price, pb * 100), lines


def i_vwap(x, v, m):
    part = x.candles[-60:]
    pv = sum(((c.h + c.l + c.c) / 3.0) * c.v for c in part)
    vv = sum(c.v for c in part)
    vwap = pv / vv if vv > 0 else sum(c.c for c in part) / len(part)
    lines = ["VWAP (last %d bars): %.2f%s" % (len(part), vwap, "" if vv > 0 else " (no volume, simple average)"),
             "Price %.2f is %+.2f (%+.2f ATR) from VWAP" % (x.price, x.price - vwap, (x.price - vwap) / x.atr)]
    return "VWAP is %.2f and price is %.2f." % (vwap, x.price), lines


def i_stoch(x, v, m):
    ks = []
    for i in range(len(x.candles) - 5, len(x.candles)):
        win = x.candles[i - 13:i + 1]
        hi, lo = max(c.h for c in win), min(c.l for c in win)
        ks.append(100.0 * (x.candles[i].c - lo) / (hi - lo) if hi > lo else 50.0)
    k, d = ks[-1], sum(ks[-3:]) / 3.0
    lines = ["%%K %.1f | %%D %.1f" % (k, d), "Last five %%K values: " + ", ".join("%.0f" % q for q in ks)]
    return "Stochastic K is %.0f and D is %.0f." % (k, d), lines


def i_adx(x, v, m):
    a, p, mi = adx(x.candles)
    lines = ["ADX %.1f (trend strength; below 20 is weak)" % a, "+DI %.1f | -DI %.1f" % (p, mi)]
    return "ADX is %.1f. Plus DI %.1f, minus DI %.1f." % (a, p, mi), lines


def i_pattern(x, v, m):
    c, p = x.last, x.prev
    body, up, lo = abs(c.c - c.o), c.h - max(c.o, c.c), min(c.o, c.c) - c.l
    lines = ["Last candle: O %.2f H %.2f L %.2f C %.2f" % (c.o, c.h, c.l, c.c),
             "Previous: O %.2f H %.2f L %.2f C %.2f" % (p.o, p.h, p.l, p.c),
             "Body %.2f | upper wick %.2f | lower wick %.2f" % (body, up, lo), "Reading: " + v.detail]
    return "Last candle body %.2f, wicks %.2f above and %.2f below. %s." % (body, up, lo, v.detail), lines


def i_breakout(x, v, m):
    prior = x.candles[-21:-1]
    hi, lo = max(c.h for c in prior), min(c.l for c in prior)
    lines = ["Previous 20-bar high %.2f | low %.2f" % (hi, lo),
             "Close %.2f is %+.2f from the high and %+.2f from the low" % (x.price, x.price - hi, x.price - lo)]
    return "Twenty bar range is %.2f to %.2f. Close is %.2f." % (lo, hi, x.price), lines


def i_pullback(x, v, m):
    e21, e50 = x.e21[-1], x.e50[-1]
    c = x.last
    lines = ["EMA 21 %.2f vs EMA 50 %.2f (%s)" % (e21, e50, "uptrend" if e21 > e50 else "downtrend"),
             "Last low is %.2f ATR and last high is %.2f ATR from EMA 21" % ((c.l - e21) / x.atr, (c.h - e21) / x.atr),
             "Needs: within 0.3 ATR of EMA 21 and closing back on the trend side"]
    return "EMA twenty-one is %.2f. %s." % (e21, v.detail), lines


def i_range(x, v, m):
    part = x.candles[-20:]
    hi, lo = max(c.h for c in part), min(c.l for c in part)
    rng = hi - lo
    pos = (x.price - lo) / rng if rng > 0 else 0.5
    lines = ["20-bar high %.2f | low %.2f | range %.2f = %.1f ATR" % (hi, lo, rng, rng / x.atr),
             "Price is at %.0f%% of the range (choppy below 3 ATR)" % (pos * 100)]
    return "Range is %.2f rupees, %.1f ATR. Price sits at %.0f percent of it." % (rng, rng / x.atr, pos * 100), lines


def i_slope(x, v, m):
    s = slope(x.closes[-20:])
    lines = ["Regression slope over 20 bars: %+.3f per bar" % s, "Normalised: %+.2f ATR over 20 bars (signal beyond +-1.0)" % (s * 20.0 / x.atr)]
    return "Price is moving %.3f rupees per bar over twenty bars." % s, lines


def i_trap(x, v, m):
    prior = x.candles[-21:-1]
    hi, lo = max(c.h for c in prior), min(c.l for c in prior)
    avg_v = sum(c.v for c in prior) / len(prior)
    c = x.last
    rng = max(c.h - c.l, 1e-9)
    broke = c.c > hi or c.c < lo
    lines = ["Breakout right now: %s" % ("YES" if broke else "no"),
             "Last bar volume %.0f vs 20-bar average %.0f" % (c.v, avg_v),
             "Wicks: upper %.0f%% / lower %.0f%% of the bar range" % (100 * (c.h - max(c.o, c.c)) / rng, 100 * (min(c.o, c.c) - c.l) / rng),
             "Result: " + v.detail]
    return "%s." % v.detail, lines


INSPECTORS = {"Market Structure": i_structure, "Liquidity Sweep": i_sweep, "CHoCH": i_choch, "Trend EMA": i_trend,
              "Momentum RSI": i_rsi, "Volume Flow": i_volume, "Support/Resistance": i_sr, "Volatility Guard": i_volguard,
              "Risk Manager 1%": i_risk, "MACD": i_macd, "Bollinger": i_boll, "VWAP": i_vwap, "Stochastic": i_stoch,
              "ADX Strength": i_adx, "Candle Pattern": i_pattern, "Breakout": i_breakout, "Pullback": i_pullback,
              "Range Filter": i_range, "Momentum Slope": i_slope, "Fakeout Shield": i_trap}


# --------------------------------------------------------------------------- #
# Trailing stop-loss (TSL). It only ever tightens the stop, so the 1% risk can never grow.
# --------------------------------------------------------------------------- #
class TrailingStop:
    BREAKEVEN_AT_R = 1.0      # at +1R profit move the stop to entry plus 0.1R
    TRAIL_START_R = 1.5       # from +1.5R the stop follows price at a distance of 1R
    TRAIL_GAP_R = 1.0
    MIN_STEP_R = 0.1          # do not bother the broker for tiny moves
    SAFE_GAP_R = 0.2          # the stop must stay at least 0.2R away from price
    TICK = 0.05

    @staticmethod
    def stage(side: str, entry: float, sl0: float, current_sl: float) -> str:
        sign = 1 if side == BUY else -1
        if sign * (current_sl - entry) >= 0.5 * abs(entry - sl0):
            return "TRAILING"
        if sign * (current_sl - entry) >= 0:
            return "BREAK-EVEN"
        return "INITIAL"

    @classmethod
    def new_stop(cls, side: str, entry: float, sl0: float, current_sl: float, price: float) -> Optional[float]:
        """Returns the better stop price, or None if the stop should stay where it is."""
        r = abs(entry - sl0)
        if r <= 0 or price <= 0:
            return None
        sign = 1 if side == BUY else -1
        profit_r = (price - entry) * sign / r
        if profit_r >= cls.TRAIL_START_R:
            target = price - sign * cls.TRAIL_GAP_R * r
        elif profit_r >= cls.BREAKEVEN_AT_R:
            target = entry + sign * 0.1 * r
        else:
            return None
        if sign * (target - current_sl) < cls.MIN_STEP_R * r:
            return None
        if sign * (price - target) < cls.SAFE_GAP_R * r:
            return None
        return round(round(target / cls.TICK) * cls.TICK, 2)


# --------------------------------------------------------------------------- #
# AI co-pilot: a plain-language read-out built only from live engine and account data
# --------------------------------------------------------------------------- #
def copilot_report(decision: Optional["Decision"], depth: Optional[dict], positions: list, risk_status: dict,
                   price: float, source: str):
    """Returns (lines, spoken)."""
    lines: List[str] = []
    if decision is None or not decision.votes:
        return ["Waiting for live data."], "Waiting for live data."
    buys = [v for v in decision.votes if v.signal == BUY]
    sells = [v for v in decision.votes if v.signal == SELL]
    vetoes = [v for v in decision.votes if v.veto]
    lines.append("Live %s price %.2f from %s." % (decision.symbol, price, source))
    lines.append("Agents: %d buy, %d sell, %d neutral, %d veto." % (len(buys), len(sells), len(decision.votes) - len(buys) - len(sells), len(vetoes)))
    lead = sorted(buys if decision.direction == BUY else sells, key=lambda v: -v.confidence)[:3]
    if lead:
        lines.append("Strongest: " + "; ".join("%s %d%% (%s)" % (v.agent, int(v.confidence * 100), v.detail) for v in lead))
    for v in vetoes:
        lines.append("VETO %s: %s" % (v.agent, v.detail))
    if depth and depth.get("bids") and depth.get("asks"):
        bq = sum(q for _p, q, _o in depth["bids"])
        aq = sum(q for _p, q, _o in depth["asks"])
        if bq + aq > 0:
            lines.append("Order book top 5: bids %d vs asks %d (%.0f%% buyers)." % (bq, aq, 100.0 * bq / (bq + aq)))
    if positions:
        p = positions[0]
        sign = 1 if p["side"] == BUY else -1
        lines.append("Open %s %d %s entry %.2f, stop %.2f (%s), target %.2f, open P&L %.2f." % (
            p["side"], p["qty"], p["symbol"], p["entry"], p["sl"],
            TrailingStop.stage(p["side"], p["entry"], p.get("sl0", p["sl"]), p["sl"]), p["tp"], (price - p["entry"]) * p["qty"] * sign))
    if risk_status.get("killed"):
        advice = "System is disarmed. Tap ARM to allow trading again."
    elif risk_status.get("locked"):
        advice = "Trading is locked for today. Review your trades."
    elif decision.approved:
        advice = "Setup approved: %s with entry %.2f, stop %.2f, target %.2f. Review and confirm." % (
            decision.direction, decision.entry, decision.sl, decision.tp)
    elif vetoes:
        advice = "Blocked by %s." % vetoes[0].agent
    else:
        advice = "Standing by. %s" % decision.reason
    lines.append("Advice: " + advice)
    spoken = "%s. %d agents buy, %d sell, %d veto. %s" % (decision.symbol, len(buys), len(sells), len(vetoes), advice)
    return lines, spoken


class ConsensusEngine:
    def __init__(self, risk: Optional[RiskManager] = None, min_agree: int = MIN_AGREE, min_rr: float = MIN_RR):
        self.risk = risk
        self.min_agree = max(MIN_AGREE, int(min_agree))
        self.min_rr = max(MIN_RR, float(min_rr))
        self.agent_names = list(AGENT_NAMES)
        self.last_ctx = None
        self.last_decision: Optional[Decision] = None

    def evaluate(self, symbol: str, candles: List[Candle], equity: float, cash: Optional[float] = None,
                 depth: Optional[dict] = None) -> Decision:
        d = Decision(symbol=symbol, total=len(AGENTS))
        if len(candles) < MIN_CANDLES:
            d.reason = "Need %d candles, have %d" % (MIN_CANDLES, len(candles))
            return d
        x = build_ctx(candles, equity, self.risk, depth)
        for i, fn in enumerate(AGENTS):
            try:
                d.votes.append(fn(x))
            except Exception as exc:  # one broken agent must never stop the others
                d.votes.append(Vote(AGENT_NAMES[i], NEUTRAL, 0.0, "error: %s" % exc))
        buys = sum(1 for v in d.votes if v.signal == BUY)
        sells = sum(1 for v in d.votes if v.signal == SELL)
        d.direction = BUY if buys > sells else SELL if sells > buys else NEUTRAL
        d.agree, d.against = (buys, sells) if d.direction == BUY else (sells, buys)
        vetoes = [v for v in d.votes if v.veto]
        d.entry = x.price
        self.last_ctx, self.last_decision = x, d
        if d.direction == NEUTRAL:
            d.reason = "Agents are split %d-%d. No edge." % (buys, sells)
            return d
        risk_dist = 1.5 * x.atr
        reward_dist = max(self.min_rr, 2.0) * risk_dist
        d.sl = d.entry - risk_dist if d.direction == BUY else d.entry + risk_dist
        d.tp = d.entry + reward_dist if d.direction == BUY else d.entry - reward_dist
        d.rr = reward_dist / risk_dist
        d.qty = RiskManager.position_size(equity, d.entry, d.sl, cash)
        d.risk_amount = round(d.qty * abs(d.entry - d.sl), 2)
        if vetoes:
            d.reason = "VETO by %s: %s" % (vetoes[0].agent, vetoes[0].detail)
        elif d.agree < self.min_agree:
            d.reason = "Only %d of %d agents agree (need %d)." % (d.agree, d.total, self.min_agree)
        elif d.agree < 2 * d.against:
            d.reason = "Opposition too strong (%d for, %d against)." % (d.agree, d.against)
        elif d.qty < 1:
            d.reason = "Capital too small for a 1 percent risk on this setup."
        elif d.rr < self.min_rr:
            d.reason = "Reward:risk %.1f is below %.1f." % (d.rr, self.min_rr)
        else:
            d.approved = True
            d.reason = "%d of %d agents agree %s. Risk %.2f (<= 1%% of equity)." % (
                d.agree, d.total, d.direction, d.risk_amount)
        return d

    def explain(self, name: str, meta: Optional[dict] = None) -> dict:
        """Live status of one agent, for the screen log and the voice. Uses only the data analysed a moment ago."""
        meta = meta or {}
        x, d = self.last_ctx, self.last_decision
        if x is None or d is None or name not in INSPECTORS:
            return {"agent": name, "signal": NEUTRAL, "confidence": 0.0, "veto": False,
                    "spoken": "%s has no live data yet." % name, "lines": ["No live candles analysed yet."]}
        vote = next((v for v in d.votes if v.agent == name), None)
        if vote is None:
            vote = Vote(name)
        try:
            sentence, lines = INSPECTORS[name](x, vote, meta)
        except Exception as exc:
            sentence, lines = "Could not read this agent: %s." % exc, ["error: %s" % exc]
        age = max(0, int(meta.get("now", time.time()) - x.last.t))
        head = ["%s | %s | price %.2f | last candle %s IST | %d candles | feed %s%s" % (
            name, d.symbol, x.price, _hhmm(x.last.t), len(x.candles), meta.get("source", "?"),
            (" | ticks " + meta["tick_mode"]) if meta.get("tick_mode") else ""),
            "Verdict: %s at %d%% confidence%s" % (vote.signal, int(vote.confidence * 100), " | VETO" if vote.veto else "")]
        spoken = "%s says %s at %d percent confidence. %s%s" % (
            name, vote.signal.lower(), int(vote.confidence * 100), sentence, " It is vetoing the trade." if vote.veto else "")
        return {"agent": name, "signal": vote.signal, "confidence": vote.confidence, "veto": vote.veto,
                "spoken": spoken, "lines": head + lines, "age": age}
