# -*- coding: utf-8 -*-
"""agent_memory.py - independent persistent memory of every agent (SQLite, bounded, private app folder).

Every agent has three lists and one conversation:
  memoryLog         what the agent was asked or did recently (newest first, at most 80 entries)
  pastStory         short history of sessions and closed paper trades it took part in (at most 40)
  mistakesAndFixes  what went wrong and the fix to apply next time (at most 40)
  chat              the Direct Talk conversation with that agent (at most 100 lines)

Rules
  * Only facts the app really observed are written (a closed paper trade, a question you asked, an error). Nothing is
    invented and no sample data is stored.
  * Text is cleaned: control characters removed, secret-looking strings (sk-..., token=...) replaced, 400 characters
    at most per entry. API keys are never stored here.
  * The file is bounded: each list has its own cap, and when the file passes MAX_BYTES the oldest rows of every list
    are dropped. If SQLite cannot be opened the memory still works in RAM for this session and says so.
"""
import math
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

KINDS = ("memoryLog", "pastStory", "mistakesAndFixes", "chat")
KIND_LABELS = {"memoryLog": "MEMORY LOG", "pastStory": "PAST STORY", "mistakesAndFixes": "MISTAKES AND FIXES",
               "chat": "DIRECT TALK"}
CAPS = {"memoryLog": 80, "pastStory": 40, "mistakesAndFixes": 40, "chat": 100}
MAX_TEXT = 400
MAX_BYTES = 3 * 1024 * 1024
_SECRET = re.compile(r"sk-[A-Za-z0-9_\-]{12,}|(?i:token|secret|password|api[_-]?key)\s*[=:]\s*\S+")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(text: Any, limit: int = MAX_TEXT) -> str:
    s = _CTRL.sub(" ", str(text if text is not None else ""))
    s = _SECRET.sub("[removed]", s)
    s = " ".join(s.split())
    return s[:limit]


class AgentMemory:
    def __init__(self, data_dir: str, filename: str = "agent_memory.sqlite"):
        self._lock = threading.RLock()
        self.path = os.path.join(data_dir, filename)
        self.problem = ""
        self._db: Any = None
        self._ram: List[Dict[str, Any]] = []
        self._open()

    # ---- storage ----
    def _open(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("CREATE TABLE IF NOT EXISTS mem (id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL, "
                       "kind TEXT NOT NULL, ts REAL NOT NULL, text TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS mem_ak ON mem(agent, kind, id)")
            db.commit()
            self._db = db
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except Exception as exc:
            self._db = None
            self.problem = "memory file unavailable (%s): kept in RAM for this session only" % str(exc)[:60]

    @property
    def mode(self) -> str:
        return "SQLITE" if self._db is not None else "RAM"

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                try:
                    self._db.commit()
                    self._db.close()
                except Exception:
                    pass
                self._db = None

    def size_bytes(self) -> int:
        try:
            return os.path.getsize(self.path) if self._db is not None else 0
        except OSError:
            return 0

    # ---- writing ----
    def add(self, agent: str, kind: str, text: Any, ts: Optional[float] = None) -> bool:
        """Adds one entry (never raises). False when the entry was rejected or could not be stored."""
        agent = clean(agent, 40)
        if kind not in KINDS or not agent:
            return False
        body = clean(text)
        if not body:
            return False
        when = time.time() if ts is None or not math.isfinite(float(ts)) else float(ts)
        with self._lock:
            if self._db is None:
                self._ram.append({"agent": agent, "kind": kind, "ts": when, "text": body})
                mine = [r for r in self._ram if r["agent"] == agent and r["kind"] == kind]
                for r in mine[:-CAPS[kind]]:
                    self._ram.remove(r)
                return True
            try:
                self._db.execute("INSERT INTO mem(agent, kind, ts, text) VALUES (?,?,?,?)", (agent, kind, when, body))
                self._db.execute("DELETE FROM mem WHERE agent=? AND kind=? AND id NOT IN "
                                 "(SELECT id FROM mem WHERE agent=? AND kind=? ORDER BY id DESC LIMIT ?)",
                                 (agent, kind, agent, kind, CAPS[kind]))
                self._db.commit()
                if self.size_bytes() > MAX_BYTES:
                    self._shrink()
                return True
            except Exception as exc:
                self.problem = "memory write failed: %s" % str(exc)[:60]
                return False

    def _shrink(self) -> None:
        try:
            self._db.execute("DELETE FROM mem WHERE id IN (SELECT id FROM mem ORDER BY id ASC LIMIT "
                             "(SELECT COUNT(*) / 2 FROM mem))")
            self._db.commit()
            self._db.execute("VACUUM")
        except Exception as exc:
            self.problem = "memory shrink failed: %s" % str(exc)[:60]

    # ---- reading ----
    def recent(self, agent: str, kind: str, n: int = 10) -> List[Dict[str, Any]]:
        """Newest first: [{ts, text}]."""
        agent = clean(agent, 40)
        n = max(1, min(int(n), CAPS.get(kind, 40)))
        with self._lock:
            if kind not in KINDS:
                return []
            if self._db is None:
                rows = [r for r in self._ram if r["agent"] == agent and r["kind"] == kind]
                return [{"ts": r["ts"], "text": r["text"]} for r in reversed(rows)][:n]
            try:
                cur = self._db.execute("SELECT ts, text FROM mem WHERE agent=? AND kind=? ORDER BY id DESC LIMIT ?",
                                       (agent, kind, n))
                return [{"ts": float(t), "text": str(x)} for t, x in cur.fetchall()]
            except Exception as exc:
                self.problem = "memory read failed: %s" % str(exc)[:60]
                return []

    def counts(self, agent: str) -> Dict[str, int]:
        return {k: len(self.recent(agent, k, CAPS[k])) for k in KINDS}

    def total_rows(self) -> int:
        with self._lock:
            if self._db is None:
                return len(self._ram)
            try:
                return int(self._db.execute("SELECT COUNT(*) FROM mem").fetchone()[0])
            except Exception:
                return 0

    def clear_agent(self, agent: str) -> None:
        agent = clean(agent, 40)
        with self._lock:
            if self._db is None:
                self._ram = [r for r in self._ram if r["agent"] != agent]
                return
            try:
                self._db.execute("DELETE FROM mem WHERE agent=?", (agent,))
                self._db.commit()
            except Exception as exc:
                self.problem = "memory clear failed: %s" % str(exc)[:60]

    def clear(self) -> None:
        with self._lock:
            self._ram = []
            if self._db is not None:
                try:
                    self._db.execute("DELETE FROM mem")
                    self._db.commit()
                    self._db.execute("VACUUM")
                except Exception as exc:
                    self.problem = "memory clear failed: %s" % str(exc)[:60]

    def describe(self) -> str:
        return "Agent memory: %s, %d entries, %.0f KB%s." % (
            self.mode, self.total_rows(), self.size_bytes() / 1024.0, (" | " + self.problem) if self.problem else "")

    # ---- learning from real events ----
    def learn_from_trade(self, trade: Dict[str, Any], votes: Optional[Dict[str, str]] = None) -> int:
        """Writes what a CLOSED paper trade taught each agent. votes maps agent name -> BUY / SELL / NEUTRAL at entry.
        An agent that voted with a losing trade gets a mistakesAndFixes entry with a fixed review checklist; an agent
        that voted with a winner gets a pastStory entry. Agents that stayed neutral or voted against are credited in
        their pastStory. Returns the number of entries written."""
        try:
            pnl = float(trade.get("pnl"))
            if not math.isfinite(pnl):
                return 0
        except (TypeError, ValueError, AttributeError):
            return 0
        side = str(trade.get("side", "")).upper()
        symbol = clean(trade.get("symbol", ""), 20)
        reason = clean(trade.get("reason", ""), 40)
        written = 0
        for agent, vote in (votes or {}).items():
            vote = str(vote).upper()
            if vote == side:
                if pnl < 0:
                    ok = self.add(agent, "mistakesAndFixes",
                                  "Voted %s on %s and the paper trade lost %.2f (%s). Fix: before the next %s, "
                                  "check the higher timeframe, the flow and whether the stop sits beyond the "
                                  "invalidation level." % (vote, symbol, abs(pnl), reason or "closed", vote))
                else:
                    ok = self.add(agent, "pastStory", "Voted %s on %s: paper trade won %.2f (%s)." % (
                        vote, symbol, pnl, reason or "closed"))
            elif vote in ("BUY", "SELL"):
                ok = self.add(agent, "pastStory", "Voted %s on %s while the trade was %s: it %s %.2f." % (
                    vote, symbol, side, "lost" if pnl < 0 else "won", abs(pnl)))
            else:
                ok = self.add(agent, "pastStory", "Stayed neutral on %s; the %s trade %s %.2f." % (
                    symbol, side, "lost" if pnl < 0 else "won", abs(pnl)))
            written += 1 if ok else 0
        return written
