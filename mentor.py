# -*- coding: utf-8 -*-
"""mentor.py - scanner, momentum filter, stock biodata, chart-drawing guide and live trade tracker.

Pure Python (standard library plus agents.py). It only reads candles and decisions and returns text or numbers;
it never places orders.
"""
import math
from typing import Dict, List, Optional

from agents import BUY, SELL, atr, ema, rsi, swings

MIN_SCAN_CANDLES = 40

RULES = [
    "Rule 1: Protect capital first. Risk at most 1 percent of your money on one trade.",
    "Rule 2: Always have a stop-loss before you enter. No stop-loss, no trade.",
    "Rule 3: Aim for at least 2 rupees of reward for every 1 rupee you risk.",
    "Rule 4: No clear setup means NO TRADE. Waiting is also a position.",
    "Rule 5: After 2 losses in a row, stop for the day. Do not chase losses.",
    "Rule 6: Trade only with the trend and strong volume. Avoid sideways, choppy markets.",
    "Rule 7: Never add to a losing trade and never move the stop-loss further away.",
]


def _ok(candles) -> bool:
    return bool(candles) and len(candles) >= MIN_SCAN_CANDLES


def momentum(candles) -> Optional[Dict]:
    """Momentum snapshot of one stock, or None when there is not enough data."""
    if not _ok(candles):
        return None
    closes = [c.c for c in candles]
    last = closes[-1]
    first = candles[0].o
    if not (last > 0 and first > 0) or any(not math.isfinite(x) for x in (last, first)):
        return None
    day_pct = (last - first) / first * 100.0
    n = min(15, len(closes) - 1)
    recent_pct = (last - closes[-1 - n]) / closes[-1 - n] * 100.0 if closes[-1 - n] > 0 else 0.0
    r = rsi(closes)
    e9, e21 = ema(closes, 9)[-1], ema(closes, 21)[-1]
    a = atr(candles)
    vols = [c.v for c in candles]
    base = sum(vols[-40:-5]) / max(1, len(vols[-40:-5]))
    vol_ratio = (sum(vols[-5:]) / 5.0) / base if base > 0 else 1.0
    score = 50.0
    score += max(-20.0, min(20.0, recent_pct * 10.0))
    score += 10.0 if e9 > e21 else -10.0
    score += max(-10.0, min(10.0, (r - 50.0) * 0.5))
    score = max(0.0, min(100.0, score))
    direction = "UP" if (e9 > e21 and recent_pct > 0) else "DOWN" if (e9 < e21 and recent_pct < 0) else "FLAT"
    strength = abs(score - 50.0) * 2.0
    qualified = direction != "FLAT" and strength >= 40.0 and vol_ratio >= 0.9 and 30.0 < r < 75.0 if direction == "UP" \
        else direction == "DOWN" and strength >= 40.0 and vol_ratio >= 0.9 and 25.0 < r < 70.0
    return {"last": last, "day_pct": day_pct, "recent_pct": recent_pct, "rsi": r, "atr": a, "ema9": e9, "ema21": e21,
            "vol_ratio": vol_ratio, "score": score, "strength": strength, "direction": direction,
            "qualified": bool(qualified)}


GOOD = "GOOD"
WEAK = "WEAK"
CHOP = "CHOP"

QUALITY_TEXT = {
    GOOD: "MARKET ACHHA CHAL RAHA HAI (clean trend, strong momentum)",
    WEAK: "WEAK / WAIT (no clean setup)",
    CHOP: "CHOP / DO NOT TRADE",
}


def candle_colors(candles, n: int = 20):
    """(green, red) candle counts among the last n candles."""
    recent = candles[-n:] if candles else []
    green = sum(1 for c in recent if c.c > c.o)
    red = sum(1 for c in recent if c.c < c.o)
    return green, red


def classify(m: Dict, choppy: bool, side: str) -> str:
    """GOOD only when momentum is qualified, points the way of its side (UP for green, DOWN for red) and the
    market is not choppy. CHOP when sideways and noisy. Everything else is WEAK."""
    if choppy:
        return CHOP
    want = "UP" if side == "green" else "DOWN"
    if m["qualified"] and m["direction"] == want:
        return GOOD
    return WEAK


def scan_row(symbol: str, candles) -> Optional[Dict]:
    m = momentum(candles)
    if m is None:
        return None
    m = dict(m)
    m["symbol"] = symbol
    chop = chop_check(candles)
    m["choppy"] = bool(chop["choppy"])
    m["chop_why"] = chop["why"]
    g, r = candle_colors(candles)
    m["green_candles"], m["red_candles"] = g, r
    side = "green" if m["day_pct"] >= 0 else "red"
    m["side"] = side
    m["quality"] = classify(m, m["choppy"], side)
    m["why"] = quality_why(m, side)
    return m


def quality_why(m: Dict, side: str) -> str:
    """One short reason for the rating, in plain words."""
    if m["quality"] == CHOP:
        return "sideways and noisy: " + m["chop_why"]
    if m["quality"] == GOOD:
        return "trend %s, momentum %.0f, volume %.1fx" % (m["direction"], m["score"], m["vol_ratio"])
    want = "UP" if side == "green" else "DOWN"
    why = []
    if m["direction"] != want:
        why.append("trend is %s, not %s" % (m["direction"], want))
    if m["strength"] < 40.0:
        why.append("momentum is weak")
    if m["vol_ratio"] < 0.9:
        why.append("volume is low")
    if side == "green" and m["rsi"] >= 75.0:
        why.append("price is stretched (RSI %.0f), chasing is risky" % m["rsi"])
    if side == "red" and m["rsi"] <= 25.0:
        why.append("price is stretched down (RSI %.0f), bounce risk" % m["rsi"])
    return "; ".join(why) or "no clean setup"


_ORDER = {GOOD: 0, WEAK: 1, CHOP: 2}


def split_green_red(rows: List[Dict]):
    """(green, red): two separate lists. Rising stocks (green candles) and falling stocks (red candles) are never
    mixed. Inside each list the stocks with a clean trend come first, then weak ones, then choppy ones."""
    green = sorted((r for r in rows if r["day_pct"] >= 0), key=lambda r: (_ORDER[r["quality"]], -r["strength"]))
    red = sorted((r for r in rows if r["day_pct"] < 0), key=lambda r: (_ORDER[r["quality"]], -r["strength"]))
    return green, red


def best_of(rows: List[Dict]) -> Optional[Dict]:
    """The stock in one list that is trending cleanest, or None when no stock is GOOD."""
    good = [r for r in rows if r["quality"] == GOOD]
    return good[0] if good else None


def chop_check(candles) -> Dict:
    """Is the market sideways and noisy? Then no lines should be drawn and no trade taken."""
    if not _ok(candles):
        return {"choppy": True, "why": "Not enough candles to judge."}
    recent = candles[-30:]
    closes = [c.c for c in recent]
    hi, lo = max(c.h for c in recent), min(c.l for c in recent)
    a = atr(candles) or 1e-9
    rng_atr = (hi - lo) / a
    e = ema(closes, 9)
    crosses = sum(1 for i in range(1, len(closes)) if (closes[i] - e[i]) * (closes[i - 1] - e[i - 1]) < 0)
    net = abs(closes[-1] - closes[0]) / a
    choppy = crosses >= 8 or rng_atr < 4.0 or net < 1.0
    why = "price crossed its 9 average %d times, moved %.1f ATR net, range %.1f ATR" % (crosses, net, rng_atr)
    return {"choppy": bool(choppy), "why": why, "crosses": crosses}


def drawing_guide(symbol: str, candles) -> str:
    """Where to draw support, resistance and a trendline, and where NOT to draw."""
    if not _ok(candles):
        return "Not enough candles yet to guide any drawing."
    price = candles[-1].c
    sw = swings(candles, 3)
    highs = sorted({round(p, 2) for _i, k, p in sw if k == "H" and p > price})[:2]
    lows = sorted({round(p, 2) for _i, k, p in sw if k == "L" and p < price}, reverse=True)[:2]
    chop = chop_check(candles)
    lines = []
    if chop["choppy"]:
        lines.append("DO NOT DRAW on %s right now. The market is sideways and noisy (%s). Lines drawn in chop get "
                     "broken again and again and trap beginners. Wait for a clean move." % (symbol, chop["why"]))
    else:
        lines.append("%s has a clean structure right now (%s)." % (symbol, chop["why"]))
    if lows:
        lines.append("DRAW support (a line under price) near %s: price turned up from there before, a valid swing low." %
                     " and ".join("%.2f" % x for x in lows))
    if highs:
        lines.append("DRAW resistance (a line above price) near %s: price turned down from there before, a valid swing high." %
                     " and ".join("%.2f" % x for x in highs))
    swl = [(i, p) for i, k, p in sw if k == "L"][-3:]
    swh = [(i, p) for i, k, p in sw if k == "H"][-3:]
    if not chop["choppy"]:
        if len(swl) >= 2 and all(swl[j][1] < swl[j + 1][1] for j in range(len(swl) - 1)):
            lines.append("DRAW a rising trendline under the last %d swing lows. It is valid because each low is higher "
                         "than the one before." % len(swl))
        elif len(swh) >= 2 and all(swh[j][1] > swh[j + 1][1] for j in range(len(swh) - 1)):
            lines.append("DRAW a falling trendline over the last %d swing highs. It is valid because each high is lower "
                         "than the one before." % len(swh))
        else:
            lines.append("DO NOT draw a trendline: the swing points do not step up or down in order, so the line would be forced.")
    lines.append("DO NOT draw lines through the middle of candles, on one-candle spikes, or on weak levels touched only once.")
    return "\n".join(lines)


def trade_guidance(decision, risk_status: Optional[Dict] = None, choppy: bool = False) -> Dict:
    """TRADE or NO TRADE with plain reasons. 'ok' is True only when the agents approve and the rules allow it."""
    reasons = []
    if risk_status:
        if risk_status.get("killed"):
            reasons.append("System is disarmed (kill switch).")
        if risk_status.get("locked"):
            reasons.append("Trading is locked for today after losses or the daily limit.")
    if decision is None:
        reasons.append("No scan yet. Tap SCAN NOW.")
    elif not decision.approved:
        reasons.append("Only %d of %d agents agree (need 9). %s" % (decision.agree, decision.total, decision.reason))
    if choppy:
        reasons.append("The market is choppy.")
    if reasons:
        return {"ok": False, "verdict": "NO TRADE", "text": " ".join(reasons)}
    d = decision
    return {"ok": True, "verdict": "TRADE %s" % d.direction,
            "text": "%d of %d agents agree on %s. Entry %.2f, stop-loss %.2f, target %.2f, reward to risk 1:%.1f. "
                    "Risk is capped at 1 percent." % (d.agree, d.total, d.direction, d.entry, d.sl, d.tp, d.rr)}


def biodata(symbol: str, candles, decision=None) -> str:
    """Full movement details of one stock, in plain words."""
    m = momentum(candles)
    if m is None:
        return "%s: not enough data yet." % symbol
    hi, lo = max(c.h for c in candles), min(c.l for c in candles)
    pos_in_range = (m["last"] - lo) / (hi - lo) * 100.0 if hi > lo else 50.0
    lines = ["%s  last %.2f  (%+.2f%% today)" % (symbol, m["last"], m["day_pct"]),
             "Day range %.2f to %.2f; price sits at %.0f%% of the range." % (lo, hi, pos_in_range),
             "Last 15 minutes: %+.2f%%. Momentum score %.0f of 100 (%s, strength %.0f%%)." % (
                 m["recent_pct"], m["score"], m["direction"], m["strength"]),
             "RSI %.0f. Average move per candle (ATR) %.2f. Volume now is %.1f times normal." % (m["rsi"], m["atr"], m["vol_ratio"]),
             "9 average %.2f is %s the 21 average %.2f." % (m["ema9"], "above" if m["ema9"] > m["ema21"] else "below", m["ema21"]),
             "Candles: %d green and %d red among the last 20." % candle_colors(candles),
             "Market quality: %s." % QUALITY_TEXT[classify(m, chop_check(candles)["choppy"], "green" if m["day_pct"] >= 0 else "red")],
             "Momentum filter: %s." % ("QUALIFIED, strong and supported by volume" if m["qualified"] else "NOT qualified, skip it")]
    if decision is not None and getattr(decision, "symbol", "") == symbol:
        lines.append("Agents: %s." % (("%s approved by %d of %d" % (decision.direction, decision.agree, decision.total))
                                      if decision.approved else "no trade, %d of %d agree" % (decision.agree, decision.total)))
    return "\n".join(lines)


def spoken_biodata(symbol: str, candles) -> str:
    m = momentum(candles)
    if m is None:
        return "%s has no data yet." % symbol
    word = {"UP": "rising", "DOWN": "falling", "FLAT": "sideways"}[m["direction"]]
    q = "It passes the momentum filter." if m["qualified"] else "It does not pass the momentum filter."
    return "%s is %s, %+.1f percent today, RSI %.0f, volume %.1f times normal. %s" % (
        symbol, word, m["day_pct"], m["rsi"], m["vol_ratio"], q)


def tracker(pos: Optional[Dict], price: float, candles=None, risk_status: Optional[Dict] = None) -> str:
    """The live 4-point tracker for the open trade."""
    if not pos:
        return "No open trade. When a trade is open this shows: 1 price and direction, 2 running profit or loss, 3 distance to stop-loss, 4 momentum and rule status."
    side = pos["side"]
    sign = 1 if side == BUY else -1
    if not price or not math.isfinite(price):
        return "Waiting for a live price for %s." % pos["symbol"]
    pnl = (price - pos["entry"]) * pos["qty"] * sign
    risk = abs(pos["entry"] - pos.get("sl0", pos["sl"])) * pos["qty"]
    r_mult = pnl / risk if risk > 0 else 0.0
    dist = (price - pos["sl"]) * sign
    dist_pct = dist / price * 100.0 if price else 0.0
    to_tp = (pos["tp"] - price) * sign
    arrow = "UP" if price > pos["entry"] else "DOWN" if price < pos["entry"] else "FLAT"
    m = momentum(candles) if candles else None
    if m is None:
        mom = "momentum unknown"
        with_trade = True
    else:
        with_trade = (m["direction"] == "UP") == (side == BUY) and m["direction"] != "FLAT"
        mom = "momentum %s (%s)" % (m["direction"], "with your trade" if with_trade else "against your trade")
    rules = []
    if risk_status:
        rules.append("trades %d, losses in a row %d" % (risk_status.get("trades_today", 0), risk_status.get("consecutive_losses", 0)))
        if risk_status.get("killed"):
            rules.append("DISARMED")
    if dist <= 0:
        rules.append("STOP-LOSS HIT, exit")
    elif dist_pct < 0.15:
        rules.append("price is very close to the stop-loss")
    return ("1 PRICE %.2f  %s  (entry %.2f, %s %d)\n2 RUNNING P&L %+.2f  (%.1fR)\n"
            "3 STOP-LOSS %.2f  distance %.2f (%.2f%%), target %.2f is %.2f away\n4 STATUS %s; %s" % (
                price, arrow, pos["entry"], side, pos["qty"], pnl, r_mult, pos["sl"], dist, dist_pct, pos["tp"], to_tp,
                mom, "; ".join(rules) or "rules ok"))
