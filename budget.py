# -*- coding: utf-8 -*-
"""budget.py - budget-wise feature scaling and API credit / rate-limit monitoring.

The tier (BASIC, STANDARD, PRO) only decides how much the app may use of the paid or limited services per day. It does
not buy credits, does not change any provider quota and does not touch a trading rule.

What is monitored (honestly): this app counts its own calls per service per day (AI chat, cloud voice characters,
multi-chart scans) and remembers the last problem a provider reported (rejected key, no credit or quota left, rate
limit, no internet). It cannot read your real balance at OpenAI or ElevenLabs, so "left today" is the left of THIS
app's daily allowance, not of your provider account.

When an allowance is used up or a minimum gap is not kept, allow() says no and the caller falls back: Direct Talk
answers from the saved records, cloud voice falls back to the phone voice. Nothing stops or crashes.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple

TIERS = ("BASIC", "STANDARD", "PRO")
TIER_INFO = {"BASIC": "Rs 0-1K: local scans, a few AI answers a day, phone voice mostly",
             "STANDARD": "Rs 1K-10K: more AI answers and cloud voice, all minute charts",
             "PRO": "Rs 10K+: highest daily allowances, subject to your provider costs and quotas"}
# per day allowances: chat calls, cloud voice characters, multi-chart scans; min seconds between chat calls
LIMITS: Dict[str, Dict[str, int]] = {
    "BASIC": {"chat": 12, "cloud_voice": 0, "scan": 40, "chat_gap": 4},
    "STANDARD": {"chat": 80, "cloud_voice": 6000, "scan": 200, "chat_gap": 2},
    "PRO": {"chat": 400, "cloud_voice": 40000, "scan": 1000, "chat_gap": 1},
}
SERVICES = ("chat", "cloud_voice", "scan")
# which multi-chart scan charts each tier may read
TIER_CHARTS = {"BASIC": ("1m", "3m", "5m"), "STANDARD": ("1m", "3m", "5m", "15m"),
               "PRO": ("1m", "3m", "5m", "15m", "1h", "1D")}
IST = timezone(timedelta(hours=5, minutes=30))
KIND_WORDS = {"auth": "key rejected", "quota": "no credit or quota left", "rate": "rate limit, wait a little",
              "network": "no internet", "other": "service error"}


def _today() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


class Budget:
    def __init__(self, data_dir: str, filename: str = "budget.json"):
        self._lock = threading.RLock()
        self._path = os.path.join(data_dir, filename)
        self.tier = "BASIC"
        self.day = _today()
        self.used: Dict[str, int] = {s: 0 for s in SERVICES}
        self.denied: Dict[str, int] = {s: 0 for s in SERVICES}
        self.last_call: Dict[str, float] = {}
        self.problems: Dict[str, Tuple[str, float]] = {}      # service -> (kind, when)
        self._load()

    # ---- persistence ----
    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if data.get("tier") in TIERS:
            self.tier = data["tier"]
        if data.get("day") == self.day and isinstance(data.get("used"), dict):
            for s in SERVICES:
                try:
                    self.used[s] = max(0, int(data["used"].get(s, 0)))
                except (TypeError, ValueError):
                    self.used[s] = 0

    def _save(self) -> None:
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"tier": self.tier, "day": self.day, "used": self.used}, fh)
            os.replace(tmp, self._path)
        except OSError:
            pass

    def _roll(self) -> None:
        today = _today()
        if today != self.day:
            self.day = today
            self.used = {s: 0 for s in SERVICES}
            self.denied = {s: 0 for s in SERVICES}

    # ---- tier ----
    def set_tier(self, tier: str) -> bool:
        if tier not in TIERS:
            return False
        with self._lock:
            self.tier = tier
            self._save()
            return True

    def limit(self, service: str) -> int:
        return LIMITS[self.tier].get(service, 0)

    # ---- allowance ----
    def allow(self, service: str, amount: int = 1) -> Tuple[bool, str]:
        """Asks for permission AND books the use. Returns (allowed, reason). Call record_problem() when the provider
        then fails; the booked use stays (it was an attempt)."""
        if service not in SERVICES:
            return False, "unknown service"
        with self._lock:
            self._roll()
            amount = max(1, int(amount))
            cap = self.limit(service)
            if cap <= 0:
                self.denied[service] += 1
                return False, "%s is not included in the %s tier" % (service.replace("_", " "), self.tier)
            if self.used[service] + amount > cap:
                self.denied[service] += 1
                return False, "today's %s allowance (%d) of the %s tier is used up" % (service.replace("_", " "), cap,
                                                                                      self.tier)
            if service == "chat":
                gap = LIMITS[self.tier]["chat_gap"]
                now = time.time()
                if now - self.last_call.get("chat", 0.0) < gap:
                    self.denied[service] += 1
                    return False, "wait %d seconds between AI answers" % gap
                self.last_call["chat"] = now
            self.used[service] += amount
            self._save()
            return True, ""

    def give_back(self, service: str, amount: int = 1) -> None:
        """Returns a booking that was never sent (for example no key saved)."""
        with self._lock:
            if service in self.used:
                self.used[service] = max(0, self.used[service] - max(1, int(amount)))
                self._save()

    # ---- provider problems ----
    def record_problem(self, service: str, kind: str) -> None:
        with self._lock:
            self.problems[service] = (kind if kind in KIND_WORDS else "other", time.time())

    def record_ok(self, service: str) -> None:
        with self._lock:
            self.problems.pop(service, None)

    @staticmethod
    def kind_of(message: str) -> str:
        t = str(message or "").lower()
        if "429" in t or "rate" in t:
            return "rate"
        if "quota" in t or "credit" in t or "billing" in t or "insufficient" in t:
            return "quota"
        if "401" in t or "403" in t or "key" in t and ("reject" in t or "invalid" in t or "incorrect" in t):
            return "auth"
        if "internet" in t or "connect" in t or "dns" in t or "timed out" in t or "network" in t:
            return "network"
        return "other"

    # ---- report ----
    def left(self, service: str) -> int:
        with self._lock:
            self._roll()
            return max(0, self.limit(service) - self.used[service])

    def summary(self) -> str:
        with self._lock:
            self._roll()
            lines = ["Budget tier: %s (%s)" % (self.tier, TIER_INFO[self.tier]), "Day: %s (India time)" % self.day]
            for s in SERVICES:
                cap = self.limit(s)
                unit = " characters" if s == "cloud_voice" else ""
                lines.append("%s: used %d of %d%s today%s" % (
                    s.replace("_", " "), self.used[s], cap, unit,
                    (", %d refused" % self.denied[s]) if self.denied[s] else ""))
            for s, (kind, when) in sorted(self.problems.items()):
                lines.append("PROVIDER PROBLEM %s: %s (%d min ago)" % (
                    s.replace("_", " "), KIND_WORDS[kind], int((time.time() - when) // 60)))
            lines.append("Counts are this app's own allowance. Your real OpenAI / ElevenLabs balance is not readable "
                         "from here: check it in your provider account.")
            return "\n".join(lines)

    def short(self) -> str:
        with self._lock:
            self._roll()
            bad = "; ".join("%s: %s" % (s.replace("_", " "), KIND_WORDS[k]) for s, (k, _w) in sorted(self.problems.items()))
            return "%s | AI %d/%d | scan %d/%d%s" % (self.tier, self.used["chat"], self.limit("chat"), self.used["scan"],
                                                    self.limit("scan"), (" | " + bad) if bad else "")
