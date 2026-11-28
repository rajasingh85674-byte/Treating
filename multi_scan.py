# -*- coding: utf-8 -*-
"""multi_scan.py - multi-chart scan (1m, 3m, 5m, 15m, 1h, 1D) with a confluence score, from REAL candles only.

The live feeds deliver 1-minute candles (the last few hundred, at most one trading day). The other charts are built
from those candles by grouping them, so no extra data source and no invented numbers are involved:
  1m 3m 5m 15m 1h   grouped candles (a chart needs MIN_BARS bars, otherwise it says NO DATA, for example 1h on a feed
                    that holds only a few hours)
  1D                the day so far as one view: close against open and the position inside the day's range. It uses
                    candles of the last 24 hours only; it is not a daily-history trend.
Each chart gets a bias from three simple facts: fast EMA against slow EMA, RSI against 50, and the last 3 candles.
Confluence = how strongly the charts that have data agree (0 to 100). With fewer than MIN_CHARTS charts that have data,
or when the last candle is older than MAX_AGE seconds, there is NO result and the caller shows "NO LIVE DATA".
It never produces an order or a signal by itself; the consensus engine and the CONFIRM popup stay in charge.
"""
import math
import time
from typing import Dict, List, Optional, Tuple

from agents import Candle

MAX_AGE = 300.0
MIN_BARS = 15
MIN_CHARTS = 3
CHARTS: Tuple[Tuple[str, int], ...] = (("1m", 1), ("3m", 3), ("5m", 5), ("15m", 15), ("1h", 60), ("1D", 0))
WEIGHTS = {"1m": 1.0, "3m": 1.0, "5m": 1.5, "15m": 2.0, "1h": 2.5, "1D": 1.5}
BULL_AT = 0.25
BEAR_AT = -0.25


def resample(candles: List[Candle], minutes: int) -> List[Candle]:
    """Groups 1-minute candles into candles of `minutes` minutes (clock aligned). Candles must be in time order."""
    if minutes <= 1:
        return list(candles)
    out: List[Candle] = []
    bucket = -1
    size = minutes * 60
    for c in candles:
        b = int(c.t // size)
        if b != bucket or not out:
            out.append(Candle(float(b * size), c.o, c.h, c.l, c.c, c.v))
            bucket = b
        else:
            k = out[-1]
            k.h = max(k.h, c.h)
            k.l = min(k.l, c.l)
            k.c = c.c
            k.v += c.v
    return out


def ema(values: List[float], period: int) -> float:
    k = 2.0 / (period + 1.0)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1.0 - k)
    return e


def rsi(values: List[float], period: int = 14) -> float:
    gains = losses = 0.0
    window = values[-(period + 1):]
    for a, b in zip(window, window[1:]):
        d = b - a
        if d > 0:
            gains += d
        else:
            losses -= d
    if gains + losses == 0:
        return 50.0
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - 100.0 / (1.0 + rs)


def chart_view(bars: List[Candle]) -> Optional[Dict]:
    """Bias of one chart: score -1..+1 and label, or None when there are too few bars."""
    bars = [b for b in bars if all(math.isfinite(x) for x in (b.o, b.h, b.l, b.c))]
    if len(bars) < MIN_BARS:
        return None
    closes = [b.c for b in bars]
    fast, slow = ema(closes, 5), ema(closes, 13)
    base = max(abs(slow), 1e-9)
    trend = max(-1.0, min(1.0, (fast - slow) / base * 400.0))
    mom = (rsi(closes) - 50.0) / 50.0
    last3 = bars[-3:]
    body = sum(1 if b.c > b.o else -1 if b.c < b.o else 0 for b in last3) / 3.0
    score = round(0.45 * trend + 0.35 * mom + 0.20 * body, 3)
    label = "BULLISH" if score >= BULL_AT else "BEARISH" if score <= BEAR_AT else "NEUTRAL"
    return {"score": score, "label": label, "bars": len(bars), "rsi": round(rsi(closes), 1)}


def day_view(candles: List[Candle], now: float) -> Optional[Dict]:
    """The day so far from candles of the last 24 hours."""
    day = [c for c in candles if now - c.t <= 86400.0]
    if len(day) < MIN_BARS:
        return None
    o, c = day[0].o, day[-1].c
    hi, lo = max(x.h for x in day), min(x.l for x in day)
    if not all(math.isfinite(x) for x in (o, c, hi, lo)) or hi <= lo or o <= 0:
        return None
    pos = (c - lo) / (hi - lo)                       # 0 = at the day's low, 1 = at the day's high
    chg = (c - o) / o
    score = round(max(-1.0, min(1.0, 0.5 * max(-1.0, min(1.0, chg * 100.0)) + 0.5 * (pos - 0.5) * 2.0)), 3)
    label = "BULLISH" if score >= BULL_AT else "BEARISH" if score <= BEAR_AT else "NEUTRAL"
    return {"score": score, "label": label, "bars": len(day), "rsi": 0.0, "chg": round(chg * 100.0, 2)}


def scan(candles: List[Candle], now: Optional[float] = None, allowed: Optional[Tuple[str, ...]] = None) -> Optional[Dict]:
    """The multi-chart scan. allowed (budget tier) limits which charts are read. Returns None for NO LIVE DATA."""
    try:
        t = time.time() if now is None else now
        if not candles or t - candles[-1].t > MAX_AGE:
            return None
        rows: List[Dict] = []
        for name, minutes in CHARTS:
            if allowed is not None and name not in allowed:
                rows.append({"chart": name, "view": None, "why": "not in your budget tier"})
                continue
            view = day_view(candles, t) if minutes == 0 else chart_view(resample(candles, minutes))
            rows.append({"chart": name, "view": view, "why": "" if view else "NO DATA (too few bars)"})
        have = [r for r in rows if r["view"]]
        if len(have) < MIN_CHARTS:
            return None
        total = sum(WEIGHTS[r["chart"]] for r in have)
        net = sum(WEIGHTS[r["chart"]] * r["view"]["score"] for r in have) / total
        agree_bull = sum(WEIGHTS[r["chart"]] for r in have if r["view"]["label"] == "BULLISH") / total
        agree_bear = sum(WEIGHTS[r["chart"]] for r in have if r["view"]["label"] == "BEARISH") / total
        direction = "BULLISH" if net >= BULL_AT and agree_bull > agree_bear else \
            "BEARISH" if net <= BEAR_AT and agree_bear > agree_bull else "MIXED"
        agree = max(agree_bull, agree_bear)
        confluence = int(round(100.0 * min(1.0, abs(net) * 1.6) * (0.5 + 0.5 * agree)))
        return {"rows": rows, "direction": direction, "confluence": confluence, "charts_used": len(have),
                "net": round(net, 3)}
    except Exception:
        return None


def report(result: Optional[Dict]) -> str:
    if not result:
        return "NO LIVE DATA: the multi-chart scan needs fresh candles and at least %d charts with enough bars." % MIN_CHARTS
    lines = []
    for r in result["rows"]:
        v = r["view"]
        if v:
            extra = (" day %+.2f%%" % v["chg"]) if "chg" in v else (" RSI %.0f" % v["rsi"])
            lines.append("%-4s %-8s %+.2f  (%d bars,%s)" % (r["chart"], v["label"], v["score"], v["bars"], extra))
        else:
            lines.append("%-4s %s" % (r["chart"], r["why"]))
    lines.append("Confluence %d%% | %s | %d of %d charts used. Not an order: the agents and CONFIRM decide." % (
        result["confluence"], result["direction"], result["charts_used"], len(CHARTS)))
    return "\n".join(lines)


def wilson(wins: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """95 percent range of a win rate measured on n trades."""
    if n <= 0:
        return 0.0, 1.0
    p = wins / float(n)
    den = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / den
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


MIN_HISTORY = 20


def win_history(trades: List[Dict]) -> Dict:
    """Win rate of YOUR OWN closed trades kept by the Agent Brain (paper and live). This is history, not a forecast.
    With fewer than MIN_HISTORY trades there is no rate: the number would be noise."""
    pnls = []
    for t in trades or []:
        try:
            p = float(t.get("pnl"))
        except (TypeError, ValueError, AttributeError):
            continue
        if math.isfinite(p):
            pnls.append(p)
    n = len(pnls)
    if n < MIN_HISTORY:
        return {"n": n, "rate": None, "low": None, "high": None}
    wins = sum(1 for p in pnls if p > 0)
    lo, hi = wilson(wins, n)
    return {"n": n, "rate": wins / float(n), "low": lo, "high": hi}


def market_verdict(res: Optional[Dict], pat: Optional[Dict], data_problem: str, session_open: bool, decision,
                   history: Dict) -> str:
    """Says whether this market / setup looks suitable right now, and why. A suitability check and a setup score
    (0-100), NOT a probability of winning: no one can know that. The only probability-like number is the win rate of
    your own past trades, shown with its 95 percent range and the number of trades it rests on."""
    reasons: List[str] = []
    if data_problem:
        return "VERDICT: NOT SUITABLE. %s" % data_problem
    if not session_open:
        reasons.append("the market is closed")
    if res is None:
        reasons.append("the multi-chart scan has no usable data")
    elif res["direction"] == "MIXED":
        reasons.append("the charts do not agree (confluence %d%%)" % res["confluence"])
    if pat is not None and res is not None and res["direction"] in ("BULLISH", "BEARISH") and pat["lean"] not in (
            res["direction"], "BALANCED"):
        reasons.append("the patterns lean %s against the charts (%s)" % (pat["lean"], res["direction"]))
    want = {"BULLISH": "BUY", "BEARISH": "SELL"}.get(res["direction"] if res else "", "")
    score = 0
    if decision is None:
        reasons.append("the 20 voting agents have not scanned this symbol yet (tap SCAN NOW)")
    else:
        if not decision.approved:
            reasons.append("the agents do not approve: %s" % str(decision.reason)[:90])
        elif want and decision.direction != want:
            reasons.append("the agents approve %s but the charts point the other way" % decision.direction)
        total = max(1, int(getattr(decision, "total", 20)))
        score = int(round(100.0 * decision.agree / total)) if decision.direction == want else 0
    if res is not None and want:
        score = int(round(0.6 * res["confluence"] + 0.4 * score))
    else:
        score = 0
    if history["rate"] is None:
        hist = "Your own win rate: not enough closed trades yet (%d of %d needed)." % (history["n"], MIN_HISTORY)
    else:
        hist = "Your own win rate: %.0f%% of %d closed trades (95%% range %.0f%% to %.0f%%). History, not a forecast." % (
            history["rate"] * 100, history["n"], history["low"] * 100, history["high"] * 100)
    if reasons:
        return "VERDICT: NOT SUITABLE now - %s.\n%s" % ("; ".join(reasons), hist)
    return ("VERDICT: SUITABLE for a PAPER proposal. Setup score %d/100 (not a probability of winning). %s\n"
            "An order is sent only after you tap BUY / SELL and CONFIRM." % (score, hist))
