# -*- coding: utf-8 -*-
"""agent_brain.py - the bounded local memory ("Agent Brain") of Tactical Neural Matrix (TDN).

It stores only what the app really observed from live feeds:
  * snaps   one row per symbol per minute at most: price, change, volume, flow score, activity
  * ctx     the shared market context (inflow and outflow ranking, correlated and inverse pairs) every few minutes
  * trades  closed PAPER and LIVE trades with the market context they were taken in (for learning and review)

Nothing in here is generated, sampled or simulated. An empty brain simply means nothing has been collected yet.

Storage is bounded on purpose so the phone never fills up:
  * every table has a row cap (the oldest rows go first),
  * rows older than MAX_AGE_DAYS are removed,
  * if the database file grows past MAX_BYTES the oldest half is deleted and the file is compacted (VACUUM),
  * if SQLite is not available the brain keeps a small in-memory window only and says so (see mode()).

Android may stop the app in the background. The brain therefore never claims continuous collection: coverage() tells
how long the app really collected (first and last snapshot) so gaps are visible.
"""
import json
import math
import os
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

try:
    import sqlite3
except Exception:                                   # python-for-android builds without the sqlite3 recipe
    sqlite3 = None                                  # type: ignore

MAX_SNAP_ROWS = 30000
MAX_CTX_ROWS = 600
MAX_TRADE_ROWS = 2000
MAX_AGE_DAYS = 7
MAX_BYTES = 6 * 1024 * 1024
SNAP_MIN_GAP = 55.0                                 # seconds between two stored snapshots of the same symbol
PRUNE_EVERY = 200                                   # writes between two clean-ups
MEMORY_SNAPS = 3000                                 # window kept when SQLite is not available


class AgentBrain:
    def __init__(self, data_dir: str, filename: str = "agent_brain.sqlite",
                 max_snaps: int = MAX_SNAP_ROWS, max_bytes: int = MAX_BYTES, max_age_days: float = MAX_AGE_DAYS):
        self.path = os.path.join(data_dir, filename)
        self.max_snaps = int(max_snaps)
        self.max_bytes = int(max_bytes)
        self.max_age = float(max_age_days) * 86400.0
        self._lock = threading.RLock()
        self._db: Any = None
        self._writes = 0
        self._last_snap: Dict[Tuple[str, str], float] = {}
        self._ctx_cache: Dict[str, Dict] = {}
        self._mem_snaps: Deque[Tuple] = deque(maxlen=MEMORY_SNAPS)
        self._mem_trades: Deque[Dict] = deque(maxlen=200)
        self.problem = ""
        try:
            os.makedirs(data_dir, exist_ok=True)
        except OSError as exc:
            self.problem = "storage folder: %s" % str(exc)[:60]
        self._open()

    # ------------------------------------------------------------------ #
    # setup
    # ------------------------------------------------------------------ #
    def _open(self) -> None:
        if sqlite3 is None:
            self.problem = "SQLite is not available on this build: only a short in-memory window is kept."
            return
        try:
            db = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
            db.execute("PRAGMA journal_mode=DELETE")          # no growing WAL file: the size cap stays honest
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("CREATE TABLE IF NOT EXISTS snaps (ts REAL, market TEXT, symbol TEXT, price REAL, chg REAL, "
                       "vol REAL, flow REAL, act REAL)")
            db.execute("CREATE INDEX IF NOT EXISTS snaps_sym ON snaps (market, symbol, ts)")
            db.execute("CREATE TABLE IF NOT EXISTS ctx (ts REAL, market TEXT, body TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS trades (ts REAL, market TEXT, symbol TEXT, side TEXT, pnl REAL, "
                       "reason TEXT, body TEXT)")
            db.commit()
            self._db = db
            self.prune()
        except Exception as exc:
            self._db = None
            self.problem = "database could not be opened (%s): only a short in-memory window is kept." % str(exc)[:60]

    def mode(self) -> str:
        return "SQLITE" if self._db is not None else "MEMORY"

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                try:
                    self._db.commit()
                    self._db.close()
                except Exception:
                    pass
                self._db = None

    # ------------------------------------------------------------------ #
    # writing
    # ------------------------------------------------------------------ #
    def record_snapshot(self, market: str, symbol: str, price: float, chg: float, vol: float, flow: float,
                        act: float, now: Optional[float] = None) -> bool:
        """Stores one live observation. Returns False when it was skipped (too soon after the last one, or invalid)."""
        t = float(now if now is not None else time.time())
        try:
            price, chg, vol, flow, act = float(price), float(chg), float(vol), float(flow), float(act)
        except (TypeError, ValueError):
            return False
        if price <= 0 or not all(math.isfinite(x) for x in (price, chg, vol, flow, act)):
            return False
        key = (market, symbol)
        with self._lock:
            if t - self._last_snap.get(key, 0.0) < SNAP_MIN_GAP:
                return False
            self._last_snap[key] = t
            row = (t, market, symbol, price, chg, vol, flow, act)
            if self._db is None:
                self._mem_snaps.append(row)
                return True
            try:
                self._db.execute("INSERT INTO snaps VALUES (?,?,?,?,?,?,?,?)", row)
                self._after_write()
                return True
            except Exception as exc:
                self.problem = "write failed: %s" % str(exc)[:60]
                return False

    def record_context(self, market: str, ctx: Dict, now: Optional[float] = None) -> None:
        """Stores the shared market context and makes it the current one for every reader."""
        t = float(now if now is not None else time.time())
        with self._lock:
            self._ctx_cache[market] = dict(ctx)
            if self._db is None:
                return
            try:
                self._db.execute("INSERT INTO ctx VALUES (?,?,?)", (t, market, json.dumps(ctx, separators=(",", ":"))[:6000]))
                self._after_write()
            except Exception as exc:
                self.problem = "write failed: %s" % str(exc)[:60]

    def record_trade(self, market: str, trade: Dict, context: Optional[Dict] = None, now: Optional[float] = None) -> None:
        """A closed trade with the market context it was taken in. Secrets never reach this table: only these fields are kept."""
        t = float(now if now is not None else time.time())
        body = {"entry": trade.get("entry"), "exit": trade.get("exit"), "qty": trade.get("qty"),
                "mode": trade.get("mode", ""), "context": context or {}}
        try:
            pnl = float(trade.get("pnl", 0.0))
        except (TypeError, ValueError):
            pnl = 0.0
        row = (t, market, str(trade.get("symbol", ""))[:20], str(trade.get("side", ""))[:6], pnl,
               str(trade.get("reason", ""))[:40], json.dumps(body, separators=(",", ":"), default=str)[:3000])
        with self._lock:
            if self._db is None:
                self._mem_trades.append({"ts": t, "market": market, "symbol": row[2], "side": row[3], "pnl": pnl,
                                         "reason": row[5], "context": context or {}})
                return
            try:
                self._db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?)", row)
                self._after_write()
            except Exception as exc:
                self.problem = "write failed: %s" % str(exc)[:60]

    def _after_write(self) -> None:
        self._writes += 1
        if self._writes % 25 == 0 and self._db is not None:
            self._db.commit()
        if self._writes % PRUNE_EVERY == 0:
            self.prune()

    # ------------------------------------------------------------------ #
    # keeping the storage bounded
    # ------------------------------------------------------------------ #
    def prune(self) -> None:
        with self._lock:
            if self._db is None:
                return
            try:
                cutoff = time.time() - self.max_age
                self._db.execute("DELETE FROM snaps WHERE ts < ?", (cutoff,))
                self._db.execute("DELETE FROM ctx WHERE ts < ?", (cutoff,))
                self._db.execute("DELETE FROM trades WHERE ts < ?", (cutoff * 4,))
                for table, cap in (("snaps", self.max_snaps), ("ctx", MAX_CTX_ROWS), ("trades", MAX_TRADE_ROWS)):
                    self._db.execute("DELETE FROM %s WHERE rowid <= (SELECT MAX(rowid) FROM %s) - ?" % (table, table), (cap,))
                self._db.commit()
                if self.size_bytes() > self.max_bytes:
                    self._db.execute("DELETE FROM snaps WHERE rowid <= (SELECT MAX(rowid) FROM snaps) - ?",
                                     (max(100, self.max_snaps // 4),))
                    self._db.execute("DELETE FROM ctx WHERE rowid <= (SELECT MAX(rowid) FROM ctx) - ?", (MAX_CTX_ROWS // 4,))
                    self._db.commit()
                    self._db.execute("VACUUM")
                    self._db.commit()
            except Exception as exc:
                self.problem = "clean-up failed: %s" % str(exc)[:60]

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total

    def clear(self) -> None:
        """Deletes everything that was collected (the CONFIG screen offers this)."""
        with self._lock:
            self._last_snap.clear()
            self._ctx_cache.clear()
            self._mem_snaps.clear()
            self._mem_trades.clear()
            if self._db is not None:
                try:
                    for table in ("snaps", "ctx", "trades"):
                        self._db.execute("DELETE FROM %s" % table)
                    self._db.commit()
                    self._db.execute("VACUUM")
                    self._db.commit()
                except Exception as exc:
                    self.problem = "clear failed: %s" % str(exc)[:60]

    # ------------------------------------------------------------------ #
    # reading (the shared context every agent and the mentor can use)
    # ------------------------------------------------------------------ #
    def context(self, market: str) -> Dict:
        """The latest shared market context, or an empty dict when none was collected."""
        with self._lock:
            return dict(self._ctx_cache.get(market, {}))

    def recent(self, market: str, symbol: str, n: int = 60) -> List[Tuple[float, float, float, float]]:
        """Newest-last list of (ts, price, flow, act) for one symbol."""
        n = max(1, min(int(n), 2000))
        with self._lock:
            if self._db is None:
                rows = [(r[0], r[3], r[6], r[7]) for r in self._mem_snaps if r[1] == market and r[2] == symbol]
                return rows[-n:]
            try:
                cur = self._db.execute("SELECT ts, price, flow, act FROM snaps WHERE market=? AND symbol=? "
                                       "ORDER BY ts DESC LIMIT ?", (market, symbol, n))
                return [tuple(r) for r in reversed(cur.fetchall())]       # type: ignore
            except Exception as exc:
                self.problem = "read failed: %s" % str(exc)[:60]
                return []

    def trades(self, market: str, n: int = 20) -> List[Dict]:
        n = max(1, min(int(n), 200))
        with self._lock:
            if self._db is None:
                return [t for t in self._mem_trades if t["market"] == market][-n:]
            try:
                cur = self._db.execute("SELECT ts, symbol, side, pnl, reason, body FROM trades WHERE market=? "
                                       "ORDER BY ts DESC LIMIT ?", (market, n))
                out = []
                for ts, sym, side, pnl, reason, body in cur.fetchall():
                    try:
                        ctx = json.loads(body).get("context", {})
                    except ValueError:
                        ctx = {}
                    out.append({"ts": ts, "symbol": sym, "side": side, "pnl": pnl, "reason": reason, "context": ctx})
                return list(reversed(out))
            except Exception as exc:
                self.problem = "read failed: %s" % str(exc)[:60]
                return []

    def coverage(self, market: str) -> Dict:
        """How much was really collected: rows, first and last snapshot time and the number of symbols."""
        with self._lock:
            if self._db is None:
                rows = [r for r in self._mem_snaps if r[1] == market]
                if not rows:
                    return {"rows": 0, "first": 0.0, "last": 0.0, "symbols": 0}
                return {"rows": len(rows), "first": rows[0][0], "last": rows[-1][0], "symbols": len({r[2] for r in rows})}
            try:
                cur = self._db.execute("SELECT COUNT(*), MIN(ts), MAX(ts), COUNT(DISTINCT symbol) FROM snaps WHERE market=?",
                                       (market,))
                n, first, last, syms = cur.fetchone()
                return {"rows": int(n or 0), "first": float(first or 0.0), "last": float(last or 0.0), "symbols": int(syms or 0)}
            except Exception as exc:
                self.problem = "read failed: %s" % str(exc)[:60]
                return {"rows": 0, "first": 0.0, "last": 0.0, "symbols": 0}

    def describe(self, market: str) -> str:
        cov = self.coverage(market)
        if not cov["rows"]:
            return "Agent Brain (%s): empty, nothing collected yet." % self.mode()
        hours = max(0.0, (cov["last"] - cov["first"]) / 3600.0)
        return "Agent Brain (%s): %d snapshots of %d symbols over %.1f hours (gaps possible when the app was closed), %.1f MB." % (
            self.mode(), cov["rows"], cov["symbols"], hours, self.size_bytes() / 1048576.0)
