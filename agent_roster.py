# -*- coding: utf-8 -*-
"""agent_roster.py - the 25 agents of Tactical Neural Matrix with name, fixed job, status, live task and source.

20 VOTING agents (agents.py) decide the consensus: each looks at the real live candles and votes BUY, SELL or NEUTRAL.
5 SUPPORT agents never vote and never touch an order. They watch the system: data quality, session clock, budget,
memory and voice. The consensus rule (at least 9 of the 20 voting agents must agree) is unchanged.

Status and live task are built from what the app really knows at this moment (the last scan, the feed state, the
saved records). Nothing here is a made-up activity text.
"""
from typing import Any, Dict, List, Optional

from agents import AGENT_NAMES

VOTING_ROLES: Dict[str, str] = {
    "Market Structure": "Highs and lows: is the market making higher or lower swings",
    "Liquidity Sweep": "Spots stop hunts above or below recent swing points",
    "CHoCH": "Change of character: the first break against the trend",
    "Trend EMA": "Fast and slow moving average trend",
    "Momentum RSI": "RSI strength and exhaustion",
    "Volume Flow": "Volume and participation behind the move",
    "Support/Resistance": "Nearby support and resistance levels",
    "Volatility Guard": "Blocks trades when volatility is extreme",
    "Risk Manager 1%": "Checks the 1% risk and the 2% daily limit",
    "MACD": "MACD momentum crossover",
    "Bollinger": "Band squeeze and band-edge behaviour",
    "VWAP": "Price against the volume weighted average price",
    "Stochastic": "Stochastic overbought and oversold",
    "ADX Strength": "Trend strength (ADX)",
    "Candle Pattern": "Candlestick formations on the last candles",
    "Breakout": "Breakouts from the recent range",
    "Pullback": "Pullbacks inside a trend",
    "Range Filter": "Avoids sideways, range-bound markets",
    "Momentum Slope": "Slope and speed of the price move",
    "Fakeout Shield": "Rejects breakouts that look like traps",
}
SUPPORT_ROLES: Dict[str, str] = {
    "Data Quality": "Feed freshness: blocks everything on NO LIVE DATA",
    "Session Clock": "Market session open or closed",
    "Budget Planner": "Budget tier, daily allowances and provider problems",
    "Memory Keeper": "Saves each agent's memory log, past story and fixes",
    "Voice Router": "Voice source: your clips, AI voice or phone voice",
}
SUPPORT_NAMES = list(SUPPORT_ROLES)
ALL_NAMES = list(AGENT_NAMES) + SUPPORT_NAMES


def role_of(name: str) -> str:
    return VOTING_ROLES.get(name) or SUPPORT_ROLES.get(name) or ""


def is_voting(name: str) -> bool:
    return name in VOTING_ROLES


def rows(votes: Optional[List[Any]], data_problem: str, session_open: bool, market: str, source: str,
         memory_line: str, budget_line: str, voice_line: str, feed_label: str = "") -> List[Dict[str, str]]:
    """One row per agent: {name, kind, role, status, task, source}. votes is the list of Vote objects of the last scan
    (name, signal, confidence, detail, veto), or None when there was none."""
    by_name = {getattr(v, "agent", ""): v for v in (votes or [])}
    out: List[Dict[str, str]] = []
    for name in AGENT_NAMES:
        v = by_name.get(name)
        if data_problem:
            status, task = "Standby", "No vote: " + data_problem
        elif v is None:
            status, task = "Scanning", "Waiting for the next scan of the live candles"
        elif getattr(v, "veto", False):
            status, task = "Guarded", "VETO: " + str(getattr(v, "detail", ""))[:90]
        else:
            status = "Voting %s" % v.signal
            task = "%s (%d%%) %s" % (v.signal, int(round(v.confidence * 100)), str(v.detail)[:80])
        out.append({"name": name, "kind": "VOTING", "role": VOTING_ROLES[name], "status": status, "task": task.strip(),
                    "source": "Live candles (%s)" % (source or "NONE") if not data_problem else "No live data"})
    out.append({"name": "Data Quality", "kind": "SUPPORT", "role": SUPPORT_ROLES["Data Quality"],
                "status": "Guarded" if data_problem else "Active",
                "task": data_problem or "Feed is fresh (%s)" % (feed_label or source or "NONE"),
                "source": "Feed timestamps"})
    out.append({"name": "Session Clock", "kind": "SUPPORT", "role": SUPPORT_ROLES["Session Clock"], "status": "Active",
                "task": "%s market is %s" % (market, "OPEN" if session_open else "CLOSED"), "source": "Clock"})
    out.append({"name": "Budget Planner", "kind": "SUPPORT", "role": SUPPORT_ROLES["Budget Planner"], "status": "Active",
                "task": budget_line, "source": "budget.json (this app's counters)"})
    out.append({"name": "Memory Keeper", "kind": "SUPPORT", "role": SUPPORT_ROLES["Memory Keeper"], "status": "Active",
                "task": memory_line, "source": "agent_memory.sqlite (private)"})
    out.append({"name": "Voice Router", "kind": "SUPPORT", "role": SUPPORT_ROLES["Voice Router"], "status": "Active",
                "task": voice_line, "source": "Voice settings"})
    return out


def row_for(table: List[Dict[str, str]], name: str) -> Optional[Dict[str, str]]:
    for r in table:
        if r["name"] == name:
            return r
    return None


def line(r: Dict[str, str]) -> str:
    return "%s [%s] %s | %s" % (r["name"], r["kind"], r["status"], r["task"])
