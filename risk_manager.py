# -*- coding: utf-8 -*-
"""risk_manager.py - hard risk limits and the backend kill switch for Tactical Neural Matrix.

Pure Python (standard library only). Every real order must pass through this module:

  * 1% rule        : a stop-out may never lose more than 1% of account equity (hard cap, not configurable upward).
  * Kill switch    : kill() locks all trading in the BACKEND and the lock is saved to disk, so it survives an app
                     restart. Only an explicit rearm() call clears it.
  * Loss lockout   : 2 losing trades in a row lock trading until the next calendar day (India date). No unlock method.
  * Daily limits   : daily loss limit (2% of equity) and a maximum number of trades per day.

Tamper and corruption protection
  * The state is written to two files (main + .bak), each with a checksum, through a private (mode 600) temporary
    file that is flushed to disk and then renamed, so a crash can never leave half a file.
  * Every value read from disk is type- and range-checked.
  * The module FAILS CLOSED: if a state file exists but is damaged and the other copy cannot repair it, trading
    starts locked (kill switch ON) until the owner taps ARM. A missing file on first start is normal.
  * If the state cannot be saved (disk full, storage error) new orders are refused, because a restart would
    otherwise forget the kill switch.
"""
import hashlib
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

MAX_RISK_PCT = 1.0               # hard cap per trade, % of equity
MAX_CONSECUTIVE_LOSSES = 2       # lock for the rest of the day
DAILY_LOSS_LIMIT_PCT = 2.0       # % of equity
MAX_TRADES_PER_DAY = 4
RISK_TOLERANCE = 1.0001          # rounding tolerance only

_IST = timezone(timedelta(hours=5, minutes=30))


def _today() -> str:
    """Calendar day in India (the NSE day), whatever time zone the phone is set to."""
    return datetime.now(_IST).date().isoformat()


def _finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError, OverflowError):
        return False


class RiskError(Exception):
    """Raised when an action would break a hard risk rule."""


class RiskManager:
    def __init__(self, path: str):
        self.path = path
        self.backup = path + ".bak"
        self._lock = threading.RLock()
        self.persist_ok = True
        self.persist_error = ""
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.data = self._load()
        self._roll_day()

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fresh() -> Dict:
        return {"day": _today(), "killed": False, "kill_reason": "", "kill_ts": 0.0,
                "consecutive_losses": 0, "locked": False, "daily_pnl": 0.0, "trades_today": 0,
                "wins": 0, "losses": 0}

    @staticmethod
    def _checksum(body: Dict) -> str:
        blob = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    @staticmethod
    def _clean(raw) -> Optional[Dict]:
        """Type and range check. Returns a clean dict, or None when anything is wrong."""
        if not isinstance(raw, dict):
            return None
        out = RiskManager._fresh()
        try:
            day = raw["day"]
            if not isinstance(day, str) or len(day) != 10:
                return None
            datetime.strptime(day, "%Y-%m-%d")
            out["day"] = day
            for key in ("killed", "locked"):
                if not isinstance(raw[key], bool):
                    return None
                out[key] = raw[key]
            reason = raw["kill_reason"]
            if not isinstance(reason, str):
                return None
            out["kill_reason"] = "".join(ch for ch in reason if ch.isprintable())[:120]
            for key, hi in (("consecutive_losses", 1000), ("trades_today", 1000), ("wins", 10 ** 7),
                            ("losses", 10 ** 7)):
                val = raw[key]
                if isinstance(val, bool) or not isinstance(val, int) or not 0 <= val <= hi:
                    return None
                out[key] = val
            for key, lim in (("kill_ts", 4e9), ("daily_pnl", 1e12)):
                val = raw[key]
                if isinstance(val, bool) or not isinstance(val, (int, float)) or not _finite(val) or abs(val) > lim:
                    return None
                out[key] = float(val)
        except (KeyError, ValueError, TypeError):
            return None
        return out

    def _read_one(self, path: str) -> Tuple[str, Optional[Dict]]:
        """('missing' | 'bad' | 'ok', data)"""
        if not os.path.exists(path):
            return "missing", None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                wrapper = json.load(fh)
            body = wrapper["state"]
            if wrapper["sum"] != self._checksum(body):
                return "bad", None
            clean = self._clean(body)
            return ("ok", clean) if clean is not None else ("bad", None)
        except Exception:
            return "bad", None

    def _load(self) -> Dict:
        s1, d1 = self._read_one(self.path)
        s2, d2 = self._read_one(self.backup)
        if s1 == "ok" and s2 == "ok":
            # both fine: believe the stricter one (a kill or lock must never be lost by picking the older copy)
            return d1 if (d1["killed"], d1["locked"], d1["trades_today"]) >= (d2["killed"], d2["locked"],
                                                                              d2["trades_today"]) else d2
        if s1 == "ok":
            return d1
        if s2 == "ok":
            return d2
        fresh = self._fresh()
        if s1 == "missing" and s2 == "missing":
            return fresh                                    # first start
        fresh["killed"] = True                              # damaged state: fail closed
        fresh["kill_reason"] = "risk state file was damaged - check your account, then tap ARM"
        fresh["kill_ts"] = time.time()
        return fresh

    def _write_one(self, path: str, text: str) -> None:
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        os.replace(tmp, path)

    def _save(self) -> None:
        try:
            body = dict(self.data)
            text = json.dumps({"state": body, "sum": self._checksum(body)})
            self._write_one(self.path, text)
            self._write_one(self.backup, text)
            self.persist_ok = True
            self.persist_error = ""
        except Exception as exc:
            self.persist_ok = False
            self.persist_error = "risk state could not be saved: %s" % str(exc)[:60]

    def _roll_day(self) -> None:
        """New India calendar day: clears the loss lockout and daily counters. The kill switch stays as it is."""
        today = _today()
        if self.data.get("day") != today:
            self.data.update({"day": today, "locked": False, "consecutive_losses": 0,
                              "daily_pnl": 0.0, "trades_today": 0})
            self._save()

    # ------------------------------------------------------------------ #
    # kill switch
    # ------------------------------------------------------------------ #
    def kill(self, reason: str = "manual kill switch") -> None:
        with self._lock:
            self.data["killed"] = True
            text = "".join(ch for ch in str(reason) if ch.isprintable())
            self.data["kill_reason"] = text[:120]
            self.data["kill_ts"] = time.time()
            self._save()

    def rearm(self) -> None:
        """The only way to clear the kill switch. The loss lockout is NOT cleared by this."""
        with self._lock:
            self.data["killed"] = False
            self.data["kill_reason"] = ""
            self._save()

    def is_killed(self) -> bool:
        with self._lock:
            return bool(self.data["killed"])

    def is_locked(self) -> bool:
        with self._lock:
            self._roll_day()
            return bool(self.data["locked"])

    # ------------------------------------------------------------------ #
    # gate for every order
    # ------------------------------------------------------------------ #
    def can_trade(self, equity: float) -> Tuple[bool, str]:
        with self._lock:
            self._roll_day()
            d = self.data
            if d["killed"]:
                return False, "Kill switch is ON (%s). Re-arm first." % (d["kill_reason"] or "manual")
            if not self.persist_ok:
                return False, "Trading paused: %s." % self.persist_error
            if d["locked"]:
                return False, "Locked for today after %d losses in a row." % MAX_CONSECUTIVE_LOSSES
            if not _finite(equity) or float(equity) <= 0:
                return False, "Account equity is unknown, so the 1 percent rule cannot be applied."
            if d["daily_pnl"] <= -float(equity) * DAILY_LOSS_LIMIT_PCT / 100.0:
                return False, "Daily loss limit of %.0f%% reached." % DAILY_LOSS_LIMIT_PCT
            if d["trades_today"] >= MAX_TRADES_PER_DAY:
                return False, "Maximum %d trades per day reached." % MAX_TRADES_PER_DAY
            return True, "ok"

    def assert_can_trade(self, equity: float) -> None:
        ok, why = self.can_trade(equity)
        if not ok:
            raise RiskError(why)

    @staticmethod
    def max_loss(equity: float) -> float:
        if not _finite(equity):
            return 0.0
        return max(0.0, float(equity)) * MAX_RISK_PCT / 100.0

    @staticmethod
    def position_size(equity: float, entry: float, sl: float, cash: Optional[float] = None,
                      max_qty: int = 100000) -> int:
        """Largest whole quantity whose stop-out loss is within 1% of equity and whose value fits the cash."""
        if not all(_finite(v) for v in (equity, entry, sl)):
            return 0
        equity, entry, sl = float(equity), float(entry), float(sl)
        per_unit = abs(entry - sl)
        if per_unit <= 0 or equity <= 0 or entry <= 0:
            return 0
        qty = int((equity * MAX_RISK_PCT / 100.0) // per_unit)
        budget = equity
        if cash is not None and _finite(cash):
            budget = min(equity, float(cash))
        qty = min(qty, int(budget // entry))
        return max(0, min(qty, int(max_qty)))

    def validate_order(self, equity: float, qty: int, entry: float, sl: float) -> float:
        """Raises RiskError if the order breaks the 1% rule. Returns the money at risk."""
        if not all(_finite(v) for v in (equity, qty, entry, sl)):
            raise RiskError("Order numbers are not valid.")
        if int(qty) < 1:
            raise RiskError("Quantity must be at least 1.")
        risk = int(qty) * abs(float(entry) - float(sl))
        cap = self.max_loss(equity)
        if risk > cap * RISK_TOLERANCE:
            raise RiskError("Risk %.2f is above the 1%% cap of %.2f." % (risk, cap))
        return round(risk, 2)

    # ------------------------------------------------------------------ #
    # results
    # ------------------------------------------------------------------ #
    def note_trade_opened(self) -> None:
        with self._lock:
            self._roll_day()
            self.data["trades_today"] += 1
            self._save()

    def record_result(self, pnl: float) -> None:
        if not _finite(pnl):
            return
        pnl = float(pnl)
        with self._lock:
            self._roll_day()
            d = self.data
            d["daily_pnl"] = round(d["daily_pnl"] + pnl, 2)
            if pnl < 0:
                d["losses"] += 1
                d["consecutive_losses"] += 1
                if d["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
                    d["locked"] = True
            else:
                d["wins"] += 1
                d["consecutive_losses"] = 0
            self._save()

    def status(self) -> Dict:
        with self._lock:
            self._roll_day()
            out = dict(self.data)
            out["persist_ok"] = self.persist_ok
            out["persist_error"] = self.persist_error
            return out
