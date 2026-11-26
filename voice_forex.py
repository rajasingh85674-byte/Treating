# -*- coding: utf-8 -*-
"""voice_forex.py - spoken forex commands for Tactical Neural Matrix (TDN).

parse_fx(text, fx_active, current_pair) turns a sentence into an FxCommand. It never raises and never places an order:
a voice command is only a REQUEST. main.py turns a TRADE request into the normal analysis and the normal CONFIRM popup,
so a misheard word can at worst open a popup that you then decline.

What it understands (pair, action, size/risk, readout):
  pair     EURUSD, "euro dollar", "eur usd", GBPUSD, "pound dollar", "cable", USDJPY, "dollar yen", AUDUSD, "aussie",
           NZDUSD, "kiwi", USDCAD, "loonie", USDCHF, "swissie", "dollar franc" and similar
  action   MARKET (forex mode / stock market), SELECT (show a pair), STATUS, PRICE (read out), TRADE (buy or sell),
           CLOSE (close the open forex position)
  risk     "risk half percent", "risk 0.5 percent", "one percent". Never more than 1 percent: bigger numbers are
           clamped to 1. Position sizes ("100000 units", "two lots") are ignored: the size always comes from the
           risk and the stop-loss distance.

Refused as unclear (action REJECT, with a reason that is read back):
  two different pairs, buy and sell together, negations ("don't buy"), "euro" alone, unknown words after the pair,
  trade commands that are questions, sentences longer than MAX_WORDS.

FxVoiceGate adds time guards: a spoken request expires after 25 seconds, a repeat of the same request is ignored for a
few seconds and a new request needs a short cooldown.
"""
import re
import time
from typing import List, NamedTuple, Optional, Tuple

MAX_WORDS = 12
MAX_RISK_PCT = 1.0
DEFAULT_RISK_PCT = 1.0
REQUEST_TTL = 25.0
COOLDOWN = 4.0

# phrases (after cleaning) -> pair. Longer phrases are tried first.
_ALIASES = {
    "eurusd": "EURUSD", "eur usd": "EURUSD", "eur/usd": "EURUSD", "euro dollar": "EURUSD", "euro usd": "EURUSD",
    "euro us dollar": "EURUSD", "fiber": "EURUSD",
    "gbpusd": "GBPUSD", "gbp usd": "GBPUSD", "gbp/usd": "GBPUSD", "pound dollar": "GBPUSD", "pound usd": "GBPUSD",
    "sterling dollar": "GBPUSD", "cable": "GBPUSD",
    "usdjpy": "USDJPY", "usd jpy": "USDJPY", "usd/jpy": "USDJPY", "dollar yen": "USDJPY", "dollar jpy": "USDJPY",
    "usd yen": "USDJPY",
    "audusd": "AUDUSD", "aud usd": "AUDUSD", "aud/usd": "AUDUSD", "aussie dollar": "AUDUSD", "aussie": "AUDUSD",
    "nzdusd": "NZDUSD", "nzd usd": "NZDUSD", "nzd/usd": "NZDUSD", "kiwi dollar": "NZDUSD", "kiwi": "NZDUSD",
    "usdcad": "USDCAD", "usd cad": "USDCAD", "usd/cad": "USDCAD", "dollar cad": "USDCAD", "dollar canadian": "USDCAD",
    "loonie": "USDCAD",
    "usdchf": "USDCHF", "usd chf": "USDCHF", "usd/chf": "USDCHF", "dollar swiss": "USDCHF", "dollar franc": "USDCHF",
    "dollar chf": "USDCHF", "swissie": "USDCHF",
}
_ALIAS_ORDER = sorted(_ALIASES, key=len, reverse=True)

# a bare currency word is too ambiguous to trade on
_AMBIGUOUS = ("euro", "pound", "yen", "franc", "sterling", "dollar")

_BUY = ("buy", "long", "go long", "kharid", "kharido")
_SELL = ("sell", "short", "go short", "becho", "bech")
_NEGATIONS = ("not", "dont", "don't", "do not", "no", "never", "cancel", "nahi", "mat", "wrong", "undo", "abort")
_CLOSE = ("close", "exit", "square off", "squareoff", "square-off", "band karo")
_STATUS = ("status", "state", "report", "how is", "summary", "position", "positions")
_PRICE = ("price", "rate", "quote", "readout", "read out", "read", "level", "kitna")
_FX_WORDS = ("forex", "fx", "currency", "currencies", "foreign exchange")
_FX_OFF = ("nse", "stock market", "stocks", "shares", "equity", "indian market")
_STOP_WORDS = {"the", "a", "an", "of", "on", "in", "to", "for", "at", "now", "please", "me", "my", "and", "with", "is",
               "it", "this", "that", "pair", "trade", "position", "percent", "per", "cent", "risk", "only", "just",
               "switch", "show", "open", "go", "mode", "market", "forex", "fx", "currency", "view", "screen", "tab",
               "status", "state", "report", "price", "rate", "quote", "readout", "read", "out", "level", "kitna",
               "how", "summary", "positions", "close", "exit", "square", "off", "buy", "sell", "long", "short",
               "kharid", "kharido", "becho", "bech", "karo", "band", "take", "place", "enter", "back", "stock",
               "stocks", "nse", "shares", "equity", "indian", "foreign", "exchange", "currencies", "units", "lots",
               "lot", "unit", "size", "number", "point", "half", "quarter", "zero", "one", "two", "three", "four",
               "five", "ten", "twenty", "fifty", "hundred", "thousand", "tenth", "tenths", "next", "current",
               "same", "again", "go", "do", "i", "want", "need", "like", "from", "usd"}

_NUMBER_WORDS = {"zero": 0.0, "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0, "six": 6.0,
                 "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0, "half": 0.5, "quarter": 0.25}


class FxCommand(NamedTuple):
    action: str                 # NONE, MARKET, SELECT, STATUS, PRICE, TRADE, CLOSE, REJECT
    pair: str = ""              # for example EURUSD, or "" when none was spoken
    side: str = ""              # BUY / SELL for TRADE
    risk_pct: float = 0.0       # 0 unless risk was spoken or the action is TRADE (then 0 < risk <= 1)
    market: str = ""            # FX or NSE for MARKET
    spoken_pair: bool = False   # True when the pair was said, False when taken from the screen
    notes: tuple = ()
    reason: str = ""            # for REJECT: why, in words that can be read aloud
    heard: str = ""             # the cleaned text the parser worked on


def _norm(text: str) -> str:
    t = str(text or "").lower()
    t = t.replace("%", " percent ").replace("'", "")
    t = re.sub(r"[^a-z0-9./ ]", " ", t)
    t = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", t)
    t = re.sub(r"(?<![0-9])\.|\.(?![0-9])", " ", t)
    return " ".join(t.split())


def _has(t: str, phrases) -> bool:
    return any(re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", t) for p in phrases)


def _find_pairs(t: str) -> Tuple[List[str], str]:
    """(distinct pairs found, text with the pair phrases removed)."""
    found: List[str] = []
    rest = " " + t + " "
    for alias in _ALIAS_ORDER:
        pat = r"(?<![a-z0-9])" + re.escape(alias).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
        if re.search(pat, rest):
            pair = _ALIASES[alias]
            if pair not in found:
                found.append(pair)
            rest = re.sub(pat, " ", rest)
    return found, " ".join(rest.split())


def _risk(t: str) -> Tuple[float, List[str]]:
    """Risk percent spoken in the text (0.0 when none) and notes about clamping."""
    notes: List[str] = []
    value = 0.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:percent|per cent|pct)", t)
    if m:
        value = float(m.group(1))
    else:
        m = re.search(r"(?:risk|risking)\s+(\d+(?:\.\d+)?)", t)
        if m:
            value = float(m.group(1))
        else:
            words = re.findall(r"[a-z]+|\d+(?:\.\d+)?", t)
            for i, w in enumerate(words):
                if w in ("percent", "pct") and i > 0:
                    j = i - 1
                    if words[j] in _NUMBER_WORDS:
                        value = _NUMBER_WORDS[words[j]]
                        if j >= 2 and words[j - 1] == "point" and words[j - 2] in _NUMBER_WORDS:
                            value = _NUMBER_WORDS[words[j - 2]] + value / 10.0
                        elif j >= 1 and words[j - 1] == "point":
                            value = value / 10.0
                    break
            if value == 0.0:
                for i, w in enumerate(words):
                    if w == "risk" and i + 1 < len(words) and words[i + 1] in _NUMBER_WORDS:
                        value = _NUMBER_WORDS[words[i + 1]]
                        break
    if value > MAX_RISK_PCT:
        notes.append("risk limited to %.1f percent" % MAX_RISK_PCT)
        value = MAX_RISK_PCT
    return round(value, 2), notes


def readback(cmd: FxCommand) -> str:
    """The sentence the app speaks back so you can hear what it understood."""
    if cmd.action == "TRADE":
        return "I heard: %s %s, risk %s percent. Check the plan and confirm on screen." % (
            cmd.side, cmd.pair, ("%g" % cmd.risk_pct))
    if cmd.action == "CLOSE":
        return "I heard: close the forex position%s. Confirm on screen." % ((" in " + cmd.pair) if cmd.pair else "")
    if cmd.action == "SELECT":
        return "Showing %s." % cmd.pair
    if cmd.action == "MARKET":
        return "Switching to the %s market." % ("forex" if cmd.market == "FX" else "NSE stock")
    if cmd.action in ("STATUS", "PRICE"):
        return "Reading %s%s." % (cmd.pair or "the forex desk", " price" if cmd.action == "PRICE" else " status")
    return cmd.reason


def parse_fx(text: str, fx_active: bool = False, current_pair: str = "") -> FxCommand:
    """Never raises. action NONE means: not a forex command, let the normal parser handle it."""
    try:
        return _parse(text, bool(fx_active), str(current_pair or "").upper())
    except Exception:
        return FxCommand("NONE")


def _parse(text: str, fx_active: bool, current_pair: str) -> FxCommand:
    t = _norm(text)
    if not t:
        return FxCommand("NONE")
    # emergency and safety words always belong to the normal parser
    if _has(t, ("panic", "kill", "disarm", "unlock", "arm", "rearm", "re arm", "live mode", "real money", "go live")):
        return FxCommand("NONE", heard=t)
    pairs, rest = _find_pairs(t)
    fx_word = _has(t, _FX_WORDS)
    words = t.split()
    side_buy, side_sell = _has(t, _BUY), _has(t, _SELL)
    closing, status, price = _has(t, _CLOSE), _has(t, _STATUS), _has(t, _PRICE)
    ambiguous = [w for w in _AMBIGUOUS if re.search(r"(?<![a-z])" + w + r"(?![a-z])", rest)]
    if not pairs and not fx_word:
        # a plain buy/sell/status/close is only an FX command while the forex screen is active; a bare currency
        # word with a side ("buy euro") is refused as unclear instead of being passed on
        if ambiguous and (side_buy or side_sell):
            return FxCommand("REJECT", reason="Say the full pair, for example euro dollar or EURUSD.", heard=t)
        if not fx_active or not (side_buy or side_sell or closing or status or price or _has(t, _FX_OFF)):
            return FxCommand("NONE", heard=t)
        pairs_from_screen = True
    else:
        pairs_from_screen = False
    if len(words) > MAX_WORDS:
        return FxCommand("REJECT", reason="That was too long for a forex command. Say it shorter.", heard=t)
    if len(pairs) > 1:
        return FxCommand("REJECT", reason="I heard two pairs, %s. Say one pair." % " and ".join(pairs), heard=t)
    if _has(t, _NEGATIONS) and (side_buy or side_sell or closing):
        return FxCommand("REJECT", reason="That sounded like a cancel, so I did nothing.", heard=t)
    spoken = pairs[0] if pairs else ""
    pair = spoken or current_pair
    is_question = words[0] in ("should", "what", "why", "how", "when", "can", "could", "would", "is", "are", "do",
                               "does", "did", "tell", "explain", "kya", "kyun", "kaise", "kab", "kitna", "batao") \
        or str(text).strip().endswith("?")
    if side_buy and side_sell:
        return FxCommand("REJECT", reason="I heard both buy and sell. Say only one.", heard=t)
    if is_question and (side_buy or side_sell or closing):
        return FxCommand("NONE", heard=t)
    # unknown words next to a trade are a sign of a mishearing
    if side_buy or side_sell or closing:
        leftovers = [w for w in rest.split() if w not in _STOP_WORDS and not re.fullmatch(r"\d+(?:\.\d+)?", w)
                     and w not in _AMBIGUOUS]
        if leftovers:
            return FxCommand("REJECT", reason="I did not understand '%s'. Nothing was done." % " ".join(leftovers[:3]),
                             heard=t)
    if (side_buy or side_sell) and not closing:
        if is_question:
            return FxCommand("NONE", heard=t)
        if not pair:
            if ambiguous:
                return FxCommand("REJECT", reason="Say the full pair, for example euro dollar or EURUSD.", heard=t)
            return FxCommand("REJECT", reason="Which pair? Say for example buy EURUSD.", heard=t)
        if ambiguous and not spoken:
            return FxCommand("REJECT", reason="Say the full pair, for example euro dollar or EURUSD.", heard=t)
        risk, notes = _risk(t)
        if risk <= 0.0:
            risk = DEFAULT_RISK_PCT
        if re.search(r"\d+(?:\.\d+)?\s*(?:units|unit|lots|lot)|(?:units|lots)\b", t):
            notes.append("size ignored, it is calculated from the risk")
        return FxCommand("TRADE", pair, "BUY" if side_buy else "SELL", risk, "FX", not pairs_from_screen and bool(spoken),
                         tuple(notes), "", t)
    if closing:
        if is_question:
            return FxCommand("NONE", heard=t)
        if not (fx_word or spoken or fx_active):
            return FxCommand("NONE", heard=t)
        return FxCommand("CLOSE", pair, "", 0.0, "FX", bool(spoken), (), "", t)
    if fx_word and _has(t, _FX_OFF):
        return FxCommand("MARKET", "", "", 0.0, "NSE", False, (), "", t)
    if _has(t, _FX_OFF) and not fx_word and not spoken and fx_active and _has(t, ("switch", "back", "go", "show", "open")):
        return FxCommand("MARKET", "", "", 0.0, "NSE", False, (), "", t)
    if price:
        return FxCommand("PRICE", pair, "", 0.0, "FX", bool(spoken), (), "", t)
    if status:
        return FxCommand("STATUS", pair, "", 0.0, "FX", bool(spoken), (), "", t)
    if spoken and not fx_active:
        return FxCommand("SELECT", spoken, "", 0.0, "FX", True, (), "", t)
    if spoken:
        return FxCommand("SELECT", spoken, "", 0.0, "FX", True, (), "", t)
    if fx_word:
        return FxCommand("MARKET", "", "", 0.0, "FX", False, (), "", t)
    return FxCommand("NONE", heard=t)


class FxVoiceGate:
    """Time guards for spoken trade requests. The CONFIRM popup is still required for every order."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._pending: Optional[Tuple[str, str, float]] = None   # (pair, side, time)
        self._last = -1e9

    def begin(self, cmd: FxCommand) -> Tuple[bool, str]:
        """Called with a TRADE command. Returns (allowed, message)."""
        now = self.clock()
        if cmd.action != "TRADE":
            return False, "Not a trade request."
        if now - self._last < COOLDOWN:
            return False, "Too fast. Wait a few seconds between voice trade requests."
        if self._pending is not None and now - self._pending[2] < REQUEST_TTL \
                and (self._pending[0], self._pending[1]) == (cmd.pair, cmd.side):
            return False, "That request is already waiting for your confirmation."
        self._pending = (cmd.pair, cmd.side, now)
        self._last = now
        return True, ""

    def pending_valid(self, pair: str, side: str) -> bool:
        """True while the last spoken request is fresh and matches the plan that is about to be confirmed."""
        if self._pending is None:
            return False
        p, s, t0 = self._pending
        return (p, s) == (pair, side) and self.clock() - t0 <= REQUEST_TTL

    def clear(self) -> None:
        self._pending = None
