# -*- coding: utf-8 -*-
"""market_flow.py - live market flow, inflow/outflow ranking, correlation and the live collector of TDN.

Everything here is computed from REAL 1-minute candles that a live feed (broker or Yahoo) delivered. There is no
sample data and no fallback numbers: a symbol whose last candle is older than MAX_AGE seconds is called STALE and is
left out; with fewer than MIN_SYMBOLS fresh symbols there is no ranking at all ("NO LIVE DATA").

Meaning of the numbers (kept simple on purpose, they are estimates from price and volume, not order-book data)
  change   percent move over the last WINDOW minutes
  flow     -1 .. +1, how one-sided the last WINDOW minutes were.
           With volume (NSE shares): volume of up-candles minus volume of down-candles, divided by total volume.
           Without volume (forex has none): sum of candle bodies divided by sum of candle ranges.
  activity how busy the last WINDOW minutes were compared with the hour before (volume, or candle range for forex)
  score    flow x activity (activity limited to 0.25 .. 2.0). Ranking uses the score:
           INFLOW (>= +0.20), OUTFLOW (<= -0.20), otherwise NEUTRAL; |score| >= 0.35 is called strong.
  correlation  Pearson correlation of 1-minute returns over the last CORR_MINUTES minutes between two symbols
           (needs CORR_MIN_POINTS common minutes): TOGETHER (>= +0.60) or INVERSE (<= -0.60).

The flow context can only make the app MORE careful (one more agreeing agent is required when a trade goes against a
strong flow); it can never approve a trade by itself.

FlowCollector polls the symbols of the active market round-robin while the app is open and the market is open, stores
snapshots in agent_brain.AgentBrain and keeps the latest snapshot for everyone. Android may pause or kill background
work; the collector keeps no promise of 24-hour coverage and the brain reports the real coverage instead.
"""
import math
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from agents import Candle

MAX_AGE = 300.0                 # a candle older than this is stale (seconds)
WINDOW = 15                     # minutes for change, flow and activity
BASE_WINDOW = 60                # minutes before the window used as the activity baseline
MIN_CANDLES = WINDOW + 10
MIN_SYMBOLS = 3
FLOW_LABEL = 0.20
FLOW_STRONG = 0.35
CORR_MINUTES = 60
CORR_MIN_POINTS = 20
CORR_STRONG = 0.60
CORR_PEER = 0.70
TOP_N = 3
FLOW_WORDS = ("flow", "inflow", "outflow", "correlation", "correlated", "correlations", "money flow")


def is_flow_request(text: str) -> bool:
    """True for sentences like 'market flow', 'show correlation', 'where is money flowing in'."""
    t = " " + " ".join(str(text or "").lower().replace("?", " ").split()) + " "
    if " flowing " in t or " inflow " in t or " outflow " in t or " flow " in t:
        return True
    return any((" " + w + " ") in t for w in ("correlation", "correlated", "correlations"))


# --------------------------------------------------------------------------- #
# per-symbol numbers
# --------------------------------------------------------------------------- #
def metrics(candles: List[Candle], now: Optional[float] = None) -> Optional[Dict]:
    """Flow numbers of one symbol, or None when the candles are too few or stale."""
    if not candles or len(candles) < MIN_CANDLES:
        return None
    t_now = time.time() if now is None else float(now)
    age = t_now - candles[-1].t
    if age > MAX_AGE or age < -120.0:
        return None
    last = candles[-1]
    if last.c <= 0:
        return None
    win = candles[-WINDOW:]
    base_c = candles[-WINDOW - 1].c
    if base_c <= 0:
        return None
    chg = (last.c / base_c - 1.0) * 100.0
    vol = float(sum(max(0.0, c.v) for c in win))
    has_vol = vol > 0
    if has_vol:
        signed = 0.0
        for c in win:
            if c.c > c.o:
                signed += max(0.0, c.v)
            elif c.c < c.o:
                signed -= max(0.0, c.v)
        flow = signed / vol
        basis = "volume"
        cur = vol / len(win)
        older = candles[-WINDOW - BASE_WINDOW:-WINDOW]
        ref = sum(max(0.0, c.v) for c in older) / len(older) if older else 0.0
    else:
        body = sum(c.c - c.o for c in win)
        rng = sum(max(0.0, c.h - c.l) for c in win)
        flow = body / rng if rng > 0 else 0.0
        basis = "range"
        cur = rng / len(win)
        older = candles[-WINDOW - BASE_WINDOW:-WINDOW]
        ref = sum(max(0.0, c.h - c.l) for c in older) / len(older) if older else 0.0
    act = (cur / ref) if ref > 0 else 1.0
    act = max(0.0, min(act, 10.0))
    score = max(-1.0, min(1.0, flow)) * max(0.25, min(act, 2.0))
    return {"price": last.c, "chg": round(chg, 4), "vol": vol, "flow": round(max(-1.0, min(1.0, flow)), 4),
            "act": round(act, 3), "score": round(score, 4), "basis": basis, "age": round(age, 1)}


def label_of(score: float) -> str:
    if score >= FLOW_LABEL:
        return "INFLOW"
    if score <= -FLOW_LABEL:
        return "OUTFLOW"
    return "NEUTRAL"


# --------------------------------------------------------------------------- #
# correlation
# --------------------------------------------------------------------------- #
def _minute_closes(candles: List[Candle]) -> Dict[int, float]:
    out: Dict[int, float] = {}
    for c in candles[-(CORR_MINUTES + 5):]:
        if c.c > 0:
            out[int(c.t // 60)] = c.c
    return out


def correlation(a: List[Candle], b: List[Candle]) -> Optional[Tuple[float, int]]:
    """(Pearson r of 1-minute returns, number of common minutes) or None when there is not enough overlap."""
    ca, cb = _minute_closes(a), _minute_closes(b)
    ra: List[float] = []
    rb: List[float] = []
    for k in sorted(set(ca) & set(cb)):
        if (k - 1) in ca and (k - 1) in cb:
            ra.append(ca[k] / ca[k - 1] - 1.0)
            rb.append(cb[k] / cb[k - 1] - 1.0)
    n = len(ra)
    if n < CORR_MIN_POINTS:
        return None
    ma, mb = sum(ra) / n, sum(rb) / n
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((y - mb) ** 2 for y in rb)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    r = cov / math.sqrt(va * vb)
    return max(-1.0, min(1.0, r)), n


# --------------------------------------------------------------------------- #
# the snapshot of a whole market
# --------------------------------------------------------------------------- #
def build_snapshot(market: str, series: Dict[str, List[Candle]], now: Optional[float] = None) -> Dict:
    """Ranking and correlation of every symbol that has fresh live candles."""
    t_now = time.time() if now is None else float(now)
    live: Dict[str, Dict] = {}
    stale: List[str] = []
    for sym, candles in series.items():
        m = metrics(candles, t_now)
        if m is None:
            stale.append(sym)
        else:
            live[sym] = m
    snap: Dict = {"market": market, "ts": t_now, "ok": len(live) >= MIN_SYMBOLS, "symbols": live, "stale": sorted(stale),
                  "inflow": [], "outflow": [], "together": [], "inverse": [], "note": ""}
    if not snap["ok"]:
        snap["note"] = "NO LIVE DATA: only %d of %d symbols have fresh live candles (need %d)." % (
            len(live), len(series), MIN_SYMBOLS)
        return snap
    ranked = sorted(live.items(), key=lambda kv: kv[1]["score"], reverse=True)
    snap["inflow"] = [(s, m["score"]) for s, m in ranked if label_of(m["score"]) == "INFLOW"][:TOP_N]
    snap["outflow"] = [(s, m["score"]) for s, m in reversed(ranked) if label_of(m["score"]) == "OUTFLOW"][:TOP_N]
    names = sorted(live)
    pairs: List[Tuple[str, str, float, int]] = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            res = correlation(series[a], series[b])
            if res is not None:
                pairs.append((a, b, round(res[0], 3), res[1]))
    snap["together"] = sorted([p for p in pairs if p[2] >= CORR_STRONG], key=lambda p: -p[2])[:5]
    snap["inverse"] = sorted([p for p in pairs if p[2] <= -CORR_STRONG], key=lambda p: p[2])[:5]
    snap["pairs"] = len(pairs)
    return snap


def compact(snap: Dict) -> Dict:
    """The small version of a snapshot that is stored in the Agent Brain and shared with every agent."""
    return {"ts": snap.get("ts", 0.0), "ok": bool(snap.get("ok")), "inflow": snap.get("inflow", []),
            "outflow": snap.get("outflow", []), "together": snap.get("together", []), "inverse": snap.get("inverse", []),
            "labels": {s: [label_of(m["score"]), m["score"], m["chg"]] for s, m in snap.get("symbols", {}).items()},
            "stale": snap.get("stale", [])}


def symbol_context(snap: Optional[Dict], symbol: str, direction: str) -> Dict:
    """What the market flow says about one symbol and one trade direction (BUY or SELL).
    Keys: known, label, score, rank, of, together, inverse, conflict, note.  conflict=True means: be more careful."""
    ctx: Dict = {"known": False, "label": "UNKNOWN", "score": 0.0, "rank": 0, "of": 0, "together": [], "inverse": [],
                 "conflict": False, "note": "No live market flow data for %s." % symbol}
    if not snap or not snap.get("ok") or symbol not in snap.get("symbols", {}):
        return ctx
    live = snap["symbols"]
    m = live[symbol]
    order = sorted(live, key=lambda s: live[s]["score"], reverse=True)
    ctx.update({"known": True, "score": m["score"], "label": label_of(m["score"]), "rank": order.index(symbol) + 1,
                "of": len(order)})
    together = [(p[1] if p[0] == symbol else p[0], p[2]) for p in snap.get("together", []) if symbol in (p[0], p[1])]
    inverse = [(p[1] if p[0] == symbol else p[0], p[2]) for p in snap.get("inverse", []) if symbol in (p[0], p[1])]
    ctx["together"], ctx["inverse"] = together, inverse
    want = 1.0 if direction == "BUY" else -1.0 if direction == "SELL" else 0.0
    notes: List[str] = []
    if want != 0.0 and m["score"] * want <= -FLOW_STRONG:
        ctx["conflict"] = True
        notes.append("%s shows strong %s (%+.2f) against this %s" % (symbol, ctx["label"].lower(), m["score"], direction))
    if want != 0.0:
        opposed = [s for s, r in together if r >= CORR_PEER and s in live and live[s]["score"] * want <= -FLOW_STRONG]
        if opposed:
            ctx["conflict"] = True
            notes.append("its close partners %s move against this %s" % (", ".join(opposed[:3]), direction))
    ctx["note"] = ("Market flow: %s %s, rank %d of %d%s." % (
        symbol, ctx["label"], ctx["rank"], ctx["of"], ("; " + "; ".join(notes)) if notes else ""))
    return ctx


def report(snap: Optional[Dict], brain_line: str = "") -> str:
    """Plain text for the chat and for reading aloud."""
    if not snap:
        return "NO LIVE DATA for market flow yet. The collector needs fresh live candles from the feed. Nothing is simulated."
    if not snap.get("ok"):
        return snap.get("note") or "NO LIVE DATA for market flow."
    def names(rows):
        return ", ".join("%s %+.2f" % (s, v) for s, v in rows) or "none"
    lines = ["Inflow: %s." % names(snap["inflow"]), "Outflow: %s." % names(snap["outflow"])]
    lines.append("Move together: %s." % (", ".join("%s/%s %+.2f" % (a, b, r) for a, b, r, n in snap["together"]) or "none"))
    lines.append("Move opposite: %s." % (", ".join("%s/%s %+.2f" % (a, b, r) for a, b, r, n in snap["inverse"]) or "none"))
    if snap.get("stale"):
        lines.append("Stale, left out: %s." % ", ".join(snap["stale"][:6]))
    first = next(iter(snap["symbols"].values()))
    lines.append("Basis: %s, last %d minutes. Estimates from live prices, not order-book data." % (first["basis"], WINDOW))
    if brain_line:
        lines.append(brain_line)
    return " ".join(lines)


# --------------------------------------------------------------------------- #
# the live collector
# --------------------------------------------------------------------------- #
class FlowCollector:
    """Background thread: while the app is open and the market is open it fetches the 1-minute candles of every
    symbol of the active market round-robin, rebuilds the flow snapshot after each round and records it in the brain.

    feed_factory()  -> an object with bootstrap(symbol) -> List[Candle], or None when no live feed exists
    symbols_fn()    -> symbols of the active market        market_fn() -> "NSE" or "FX"
    open_fn()       -> True while the market is open        scrub(text) -> text without secrets
    """

    def __init__(self, brain, feed_factory: Callable[[], object], symbols_fn: Callable[[], List[str]],
                 market_fn: Callable[[], str], open_fn: Callable[[], bool], interval: float = 60.0,
                 scrub: Optional[Callable[[str], str]] = None, clock: Callable[[], float] = time.time):
        self.brain = brain
        self.feed_factory = feed_factory
        self.symbols_fn = symbols_fn
        self.market_fn = market_fn
        self.open_fn = open_fn
        self.interval = max(20.0, float(interval))
        self.scrub = scrub or (lambda text: text)
        self.clock = clock
        self._series: Dict[str, Dict[str, List[Candle]]] = {"NSE": {}, "FX": {}}
        self._latest: Dict[str, Dict] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.error = ""
        self.state = "stopped"

    # ---- control ----
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def pause(self) -> None:
        """Called when the app goes to the background: the phone may stop network work anyway."""
        self._paused.set()
        self.state = "paused (app in background)"

    def resume(self) -> None:
        self._paused.clear()

    # ---- reading ----
    def latest(self, market: str) -> Optional[Dict]:
        """The newest snapshot of a market, or None when it is missing or older than three collection rounds."""
        with self._lock:
            snap = self._latest.get(market)
        if snap is None or self.clock() - snap["ts"] > 3.0 * self.interval + MAX_AGE:
            return None
        return snap

    def context_for(self, market: str, symbol: str, direction: str) -> Dict:
        return symbol_context(self.latest(market), symbol, direction)

    def status(self) -> str:
        return self.state if not self.error else "%s | last problem: %s" % (self.state, self.error)

    # ---- one step (also used by the tests) ----
    def collect_symbol(self, market: str, symbol: str, feed) -> bool:
        try:
            candles = feed.bootstrap(symbol)
        except Exception as exc:
            self.error = self.scrub(str(exc))[:100]
            return False
        if not candles:
            return False
        with self._lock:
            self._series.setdefault(market, {})[symbol] = list(candles[-(WINDOW + BASE_WINDOW + 10):])
        return True

    def publish(self, market: str) -> Dict:
        """Builds the snapshot from the stored live candles, records it in the brain and makes it the current one."""
        with self._lock:
            series = dict(self._series.get(market, {}))
        snap = build_snapshot(market, series, self.clock())
        with self._lock:
            self._latest[market] = snap
        if snap["ok"]:
            now = self.clock()
            for sym, m in snap["symbols"].items():
                self.brain.record_snapshot(market, sym, m["price"], m["chg"], m["vol"], m["score"], m["act"], now)
            self.brain.record_context(market, compact(snap), now)
        return snap

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and not self._stop.is_set():
            time.sleep(min(1.0, max(0.0, end - time.time())))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._paused.is_set():
                    self._sleep(3.0)
                    continue
                market = self.market_fn()
                if not self.open_fn():
                    self.state = "waiting: %s market is closed" % market
                    self._sleep(10.0)
                    continue
                feed = self.feed_factory()
                if feed is None:
                    self.state = "no live feed"
                    self._sleep(10.0)
                    continue
                symbols = list(self.symbols_fn())
                if not symbols:
                    self._sleep(5.0)
                    continue
                self.state = "collecting %s" % market
                gap = max(2.0, self.interval / float(len(symbols)))
                for sym in symbols:
                    if self._stop.is_set() or self._paused.is_set() or self.market_fn() != market:
                        break
                    self.collect_symbol(market, sym, feed)
                    self._sleep(gap)
                else:
                    self.publish(market)
            except Exception as exc:                          # the collector must never crash the app
                self.error = self.scrub(str(exc))[:100]
                self._sleep(10.0)
        self.state = "stopped"
