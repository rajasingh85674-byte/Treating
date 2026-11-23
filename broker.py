# -*- coding: utf-8 -*-
"""broker.py - universal live broker connector for NSE equities (Zerodha Kite Connect and Upstox).

REAL MONEY. Orders sent from this file are real orders on your broker account. There is no paper mode.
Pure Python (standard library only; certifi is used for HTTPS certificates when it is installed).

Live data: WebSocket ticks and market depth (Kite binary feed, Upstox protobuf feed) with an automatic fall-back
to REST polling. A small RFC 6455 WebSocket client is built in, so no extra package is needed.

Order flow for every trade (see BaseLiveBroker.open_trade):
  1. RiskManager gate: kill switch, loss lockout, daily limits, 1% sizing.
  2. Real broker price check: the signal price must be within 0.5% of the broker's own last price.
  3. MARKET entry (intraday product), wait for the fill.
  4. Protective SL-M order placed on the exchange side immediately. If that fails, the position is flattened.
  5. Trailing stop-loss (break-even at +1R, then 1R behind price from +1.5R) moves the exchange SL-M order.
  6. Target and end-of-day exit (15:15 IST) are watched by the app in check_exits().

Market data comes from the broker when your plan includes it, otherwise from Yahoo (delayed) or from a
simulated DEMO feed. The DEMO feed is for looking at the screen only: live orders are refused on it.
"""
import base64
import csv
import gzip
import hashlib
import io
import json
import os
import random
import re
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from agents import BUY, SELL, Candle, TrailingStop
from risk_manager import RiskError, RiskManager

IST = timezone(timedelta(hours=5, minutes=30))
EOD_EXIT = (15, 15)              # open intraday positions are closed at 15:15 IST
MAX_LTP_DEVIATION = 0.005        # signal price vs broker price, 0.5%
TICK = 0.05
HISTORY = 240
MAX_CANDLES = 400

KITE_ROOT = "https://api.kite.trade"
KITE_LOGIN = "https://kite.zerodha.com/connect/login"
UPSTOX_ROOT = "https://api.upstox.com/v2"
UPSTOX_INSTRUMENTS = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
KITE_WS = "wss://ws.kite.trade"

# ISIN based Upstox keys for common NSE stocks. Every key is double-checked against the broker's own
# quote response before any order, so a wrong entry here can only cause a refusal, never a wrong order.
UPSTOX_KEYS = {
    "RELIANCE": "NSE_EQ|INE002A01018", "TCS": "NSE_EQ|INE467B01029", "INFY": "NSE_EQ|INE009A01021",
    "HDFCBANK": "NSE_EQ|INE040A01034", "SBIN": "NSE_EQ|INE062A01020", "ICICIBANK": "NSE_EQ|INE090A01021",
    "ITC": "NSE_EQ|INE154A01025", "LT": "NSE_EQ|INE018A01030", "AXISBANK": "NSE_EQ|INE238A01034",
    "KOTAKBANK": "NSE_EQ|INE237A01028", "BHARTIARTL": "NSE_EQ|INE397D01024", "WIPRO": "NSE_EQ|INE075A01022",
    "MARUTI": "NSE_EQ|INE585B01010",
}
DEMO_BASE = {"RELIANCE": 1450.0, "TCS": 3900.0, "INFY": 1650.0, "HDFCBANK": 1700.0, "SBIN": 820.0,
             "ICICIBANK": 1300.0, "ITC": 420.0, "LT": 3600.0, "AXISBANK": 1150.0, "KOTAKBANK": 2100.0}


_SECRET_PATTERNS = [
    re.compile(r"sk[-_][A-Za-z0-9_\-]{8,}"),                                # OpenAI and ElevenLabs style keys
    re.compile(r"(?i)(access_token|api_secret|api_key|secret|token|authorization|bearer|enctoken)"
               r"(\s*[=:]\s*|\s+)[^\s,;&\"']{6,}"),
    re.compile(r"[A-Za-z0-9_\-]{32,}"),                                      # any long opaque string
]


def redact(text) -> str:
    """Removes anything that looks like a key or token. Used for every log line, status line and error text."""
    out = str(text)
    out = _SECRET_PATTERNS[0].sub("sk-***", out)
    out = _SECRET_PATTERNS[1].sub(lambda m: m.group(1) + "=***", out)
    out = _SECRET_PATTERNS[2].sub("***", out)
    return out


def clean_text(text, limit: int = 400) -> str:
    """Printable ASCII/Unicode text only: control and zero-width characters are dropped, length is capped."""
    out = []
    for ch in str(text if text is not None else ""):
        if ch in "\n\t":
            out.append(" ")
        elif ch.isprintable() and ch not in "\u200b\u200c\u200d\u2060\ufeff\u202e\u202d":
            out.append(ch)
    return "".join(out).strip()[:limit]


def clean_secret(text, limit: int = 300) -> str:
    """For pasted keys and tokens: keeps only characters that can appear in one (no spaces, quotes or invisibles)."""
    return "".join(ch for ch in str(text or "") if ch.isascii() and (ch.isalnum() or ch in "-_.~+/=:%?&@"))[:limit]


class BrokerError(Exception):
    def __init__(self, message=""):
        super().__init__(redact(message))


def ist_now() -> datetime:
    return datetime.now(IST)


def round_tick(x: float) -> float:
    return round(round(x / TICK) * TICK, 2)


def opposite(side: str) -> str:
    return SELL if side == BUY else BUY


# --------------------------------------------------------------------------- #
# HTTP (standard library only)
# --------------------------------------------------------------------------- #
_CTX: Optional[ssl.SSLContext] = None


def _ssl_context() -> ssl.SSLContext:
    global _CTX
    if _CTX is None:
        try:
            import certifi
            _CTX = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _CTX = ssl.create_default_context()
    return _CTX


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    """A redirect may never leave HTTPS, so a token header can not be sent over plain HTTP."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise BrokerError("Blocked an insecure redirect.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


MAX_BODY = 40 * 1024 * 1024


def http(method: str, url: str, headers: Optional[Dict[str, str]] = None, params: Optional[Dict] = None,
         form: Optional[Dict] = None, body: Optional[Dict] = None, timeout: int = 15) -> Tuple[int, str]:
    """Returns (status code, text). HTTPS only, certificate checked. Raises BrokerError on network failure."""
    if not url.lower().startswith("https://"):
        raise BrokerError("Only secure https addresses are allowed.")
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    hdrs = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    hdrs.setdefault("User-Agent", "Mozilla/5.0 TDN")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    opener = urllib.request.build_opener(_HttpsOnly, urllib.request.HTTPSHandler(context=_ssl_context()))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise BrokerError("Reply too large.")
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read(MAX_BODY).decode("utf-8", "replace")
        except Exception:
            text = ""
        return exc.code, text
    except BrokerError:
        raise
    except Exception as exc:
        raise BrokerError("Network error: %s" % str(exc)[:100])


# --------------------------------------------------------------------------- #
# Credentials (private app storage, file mode 600)
# --------------------------------------------------------------------------- #
class CredentialStore:
    """Broker keys, OpenAI key and daily access tokens. Stored only inside the app's private folder (file mode 600).
    The file is not encrypted: do not install this app on a phone you do not control.
    Every save is read back from disk, so the screen can tell the truth about whether a key really stayed saved."""

    FIELDS = ("broker", "kite_key", "kite_secret", "kite_token", "kite_day",
              "upstox_key", "upstox_secret", "upstox_redirect", "upstox_token", "upstox_day",
              "openai_key", "mic_engine", "eleven_key", "eleven_voice", "openai_voice", "voice_source")
    _SECRET_FIELDS = ("kite_key", "kite_secret", "kite_token", "upstox_key", "upstox_secret", "upstox_token",
                      "openai_key", "eleven_key")
    _CHOICES = {"broker": ("KITE", "UPSTOX"), "mic_engine": ("AUTO", "ANDROID", "OPENAI"),
                "voice_source": ("AUTO", "ELEVENLABS", "OPENAI", "PHONE")}
    _OPENAI_VOICES = ("onyx", "ash", "echo", "sage", "fable", "alloy", "nova", "coral", "shimmer")

    def __init__(self, path: str):
        self.path = path
        self.last_error = ""
        self._lock = threading.RLock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.data = self._defaults()
        self.reload()

    def _defaults(self) -> Dict[str, str]:
        data = {k: "" for k in self.FIELDS}
        data["broker"] = "UPSTOX"
        data["mic_engine"] = "AUTO"
        data["voice_source"] = "AUTO"
        data["upstox_redirect"] = "https://127.0.0.1/"
        return data

    def _clean_value(self, key: str, value) -> str:
        text = str(value if value is not None else "").strip()
        if key in self._SECRET_FIELDS:
            return clean_secret(text)
        if key in self._CHOICES:
            text = text.upper()
            return text if text in self._CHOICES[key] else self._defaults()[key]
        if key == "openai_voice":                      # "" means: use the voice of the chosen profile
            text = text.lower()
            return text if text in self._OPENAI_VOICES else ""
        if key == "eleven_voice":                      # ElevenLabs voice IDs are letters and digits only
            return "".join(ch for ch in text if ch.isascii() and ch.isalnum())[:40]
        return clean_text(text, 200)

    def reload(self) -> None:
        """Re-reads the file. A missing file is normal on first start; a damaged one is kept as .bad."""
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    saved = json.load(fh)
            except FileNotFoundError:
                return
            except Exception as exc:
                self.last_error = "Credential file unreadable: %s" % redact(str(exc))[:60]
                try:
                    os.replace(self.path, self.path + ".bad")
                except OSError:
                    pass
                return
            if isinstance(saved, dict):
                for k in self.FIELDS:
                    if k in saved and isinstance(saved[k], str):
                        self.data[k] = self._clean_value(k, saved[k])

    def get(self, key: str) -> str:
        with self._lock:
            return self.data.get(key, "")

    def masked(self, key: str) -> str:
        """For the screen: shows that a secret exists and its last four characters, never the whole value."""
        value = self.get(key)
        if not value:
            return ""
        return "saved, ends with " + value[-4:] if len(value) >= 12 else "saved"

    def update(self, **kw) -> bool:
        """Saves the given fields. Returns True only when the data was written AND read back correctly."""
        with self._lock:
            previous = dict(self.data)
            for k, v in kw.items():
                if k in self.FIELDS:
                    self.data[k] = self._clean_value(k, v)
            tmp = self.path + ".tmp"
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self.data, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
                with open(self.path, "r", encoding="utf-8") as fh:
                    check = json.load(fh)
                for k in kw:
                    if k in self.FIELDS and check.get(k) != self.data[k]:
                        raise OSError("read-back mismatch")
                self.last_error = ""
                return True
            except Exception as exc:
                self.data = previous
                self.last_error = "Could not save: %s" % redact(str(exc))[:80]
                return False


def _today() -> str:
    return ist_now().date().isoformat()


def extract_param(text: str, name: str) -> str:
    """Accepts a bare token or a whole redirect URL and returns the value of `name`."""
    text = clean_secret(text, 2000) if "=" not in (text or "") else clean_text(text, 2000).replace(" ", "")
    if "=" in text:
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
            if name in q and q[name]:
                return q[name][0].strip()
            q = urllib.parse.parse_qs(text.split("?", 1)[-1])
            if name in q and q[name]:
                return q[name][0].strip()
        except Exception:
            pass
    return text


# --------------------------------------------------------------------------- #
# Minimal WebSocket client (RFC 6455) and protobuf reader, standard library only
# --------------------------------------------------------------------------- #
class WebSocketClient:
    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 5.0):
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._buf = b""
        self._frag = b""
        self._frag_op = 0

    def connect(self) -> None:
        u = urllib.parse.urlparse(self.url)
        host = u.hostname or ""
        secure = u.scheme == "wss"
        port = u.port or (443 if secure else 80)
        try:
            raw = socket.create_connection((host, port), timeout=15)
            sock = _ssl_context().wrap_socket(raw, server_hostname=host) if secure else raw
        except Exception as exc:
            raise BrokerError("WebSocket connect failed: %s" % str(exc)[:80])
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        lines = ["GET %s HTTP/1.1" % path, "Host: %s" % host, "Upgrade: websocket", "Connection: Upgrade",
                 "Sec-WebSocket-Key: %s" % key, "Sec-WebSocket-Version: 13"]
        lines += ["%s: %s" % (k, v) for k, v in self.headers.items()]
        try:
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            data = b""
            sock.settimeout(15)
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    raise BrokerError("WebSocket closed during handshake")
                data += chunk
                if len(data) > 65536:
                    raise BrokerError("WebSocket handshake too long")
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError("WebSocket handshake failed: %s" % str(exc)[:80])
        head, _, rest = data.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0] + b" ":
            raise BrokerError("WebSocket refused: %s" % head.split(b"\r\n")[0].decode("latin1")[:60])
        self._buf = rest
        sock.settimeout(self.timeout)
        self.sock = sock

    def send(self, opcode: int, payload: bytes) -> None:
        if self.sock is None:
            raise BrokerError("WebSocket is not connected")
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            self.sock.sendall(head + mask + body)
        except Exception as exc:
            raise BrokerError("WebSocket send failed: %s" % str(exc)[:60])

    def _parse(self):
        buf = self._buf
        if len(buf) < 2:
            return None
        b0, b1 = buf[0], buf[1]
        ln, off = b1 & 0x7F, 2
        if ln == 126:
            if len(buf) < 4:
                return None
            ln, off = struct.unpack(">H", buf[2:4])[0], 4
        elif ln == 127:
            if len(buf) < 10:
                return None
            ln, off = struct.unpack(">Q", buf[2:10])[0], 10
        masked = bool(b1 & 0x80)
        if masked:
            off += 4
        if len(buf) < off + ln:
            return None
        payload = buf[off:off + ln]
        if masked:
            m = buf[off - 4:off]
            payload = bytes(b ^ m[i % 4] for i, b in enumerate(payload))
        self._buf = buf[off + ln:]
        return bool(b0 & 0x80), b0 & 0x0F, payload

    def recv(self):
        """Returns (opcode, payload) for a complete message, or None if nothing arrived in time."""
        while True:
            frame = self._parse()
            if frame is None:
                if self.sock is None:
                    raise BrokerError("WebSocket closed")
                try:
                    chunk = self.sock.recv(65536)
                except socket.timeout:
                    return None
                except Exception as exc:
                    raise BrokerError("WebSocket read failed: %s" % str(exc)[:60])
                if not chunk:
                    raise BrokerError("WebSocket closed by server")
                self._buf += chunk
                continue
            fin, op, payload = frame
            if op == 0x9:
                self.send(0xA, payload)
                continue
            if op == 0xA:
                continue
            if op == 0x8:
                raise BrokerError("WebSocket closed by server")
            if op in (0x1, 0x2):
                self._frag_op, self._frag = op, payload
            else:
                self._frag += payload
            if fin:
                msg = (self._frag_op, self._frag)
                self._frag, self._frag_op = b"", 0
                return msg

    def close(self) -> None:
        try:
            if self.sock is not None:
                self.sock.close()
        except Exception:
            pass
        self.sock = None


def _varint(buf: bytes, i: int) -> Tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 70:
            raise ValueError("bad varint")


def pb_fields(buf: bytes):
    """Yields (field number, wire type, value) of one protobuf message."""
    i, n = 0, len(buf)
    while i < n:
        tag, i = _varint(buf, i)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 1:
            v, i = buf[i:i + 8], i + 8
        elif wt == 2:
            ln, i = _varint(buf, i)
            v, i = buf[i:i + ln], i + ln
        elif wt == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            raise ValueError("unsupported wire type %d" % wt)
        yield fn, wt, v


def _dbl(raw: bytes) -> float:
    return struct.unpack("<d", raw)[0] if isinstance(raw, (bytes, bytearray)) and len(raw) == 8 else 0.0


def normalise_depth(buy_rows, sell_rows) -> Dict:
    """Rows of (price, quantity, orders) -> {'bids': [...], 'asks': [...]} with the best price first."""
    bids = sorted([(float(p), int(q), int(o)) for p, q, o in buy_rows if p], key=lambda r: -r[0])[:5]
    asks = sorted([(float(p), int(q), int(o)) for p, q, o in sell_rows if p], key=lambda r: r[0])[:5]
    return {"bids": bids, "asks": asks}


# --------------------------------------------------------------------------- #
# Live broker base class
# --------------------------------------------------------------------------- #
class BaseLiveBroker:
    name = "BASE"
    product_label = ""

    def __init__(self, store: CredentialStore, data_dir: str):
        self.store = store
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._lock = threading.RLock()
        self.last_error = ""
        self._state_path = os.path.join(data_dir, "positions_%s.json" % self.name.lower())
        self.state = self._load_state()
        self.trailing = bool(self.state.get("trailing", True))
        self.events: List[str] = []

    # -- to be provided by each broker --
    def has_session(self) -> bool:
        raise NotImplementedError

    def login_url(self) -> str:
        raise NotImplementedError

    def login(self, token_or_url: str) -> str:
        raise NotImplementedError

    def clear_session(self) -> None:
        raise NotImplementedError

    def funds(self) -> Dict[str, float]:
        """{'equity': total account value, 'cash': money available for new positions}"""
        raise NotImplementedError

    def ltp(self, symbol: str) -> float:
        raise NotImplementedError

    def candles(self, symbol: str, count: int = HISTORY) -> List[Candle]:
        raise NotImplementedError

    def _place(self, symbol: str, side: str, qty: int, order_type: str, trigger: Optional[float]) -> str:
        raise NotImplementedError

    def _order_status(self, order_id: str) -> Dict:
        raise NotImplementedError

    def _cancel(self, order_id: str) -> None:
        raise NotImplementedError

    def _modify_sl(self, pos: Dict, new_trigger: float) -> None:
        raise NotImplementedError

    def broker_positions(self) -> List[Dict]:
        """Open intraday positions as the broker sees them: [{'symbol', 'qty'}], qty > 0 long, < 0 short."""
        raise NotImplementedError

    def quote_depth(self, symbol: str) -> Dict:
        """REST snapshot: {'ltp', 'vol', 'depth': {'bids': [...], 'asks': [...]}}"""
        raise NotImplementedError

    def ws_connect_info(self, symbol: str):
        """(url, extra headers, [(opcode, payload bytes) to send after connecting])"""
        raise NotImplementedError

    def ws_decode(self, opcode: int, data: bytes, symbol: str) -> Optional[Dict]:
        raise NotImplementedError

    # -- state --
    def _load_state(self) -> Dict:
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                s = json.load(fh)
            if isinstance(s, dict) and "positions" in s:
                return s
        except Exception:
            pass
        return {"positions": [], "trades": [], "seq": 0}

    def _save_state(self) -> None:
        tmp = self._state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, self._state_path)
        except OSError:
            pass

    def positions(self) -> List[Dict]:
        with self._lock:
            return [dict(p) for p in self.state["positions"]]

    def trades(self, n: int = 20) -> List[Dict]:
        with self._lock:
            return [dict(t) for t in self.state["trades"][-n:]]

    # -- order helpers --
    def _wait_fill(self, order_id: str, qty: int, fallback: float) -> Tuple[float, int]:
        status: Dict = {}
        for _ in range(10):
            status = self._order_status(order_id)
            if status["status"] == "COMPLETE":
                return (status["average_price"] or fallback), (status["filled_quantity"] or qty)
            if status["status"] in ("REJECTED", "CANCELLED"):
                raise BrokerError("Entry order %s: %s" % (status["status"], status["message"] or "no reason given"))
            time.sleep(0.6)
        try:
            self._cancel(order_id)
        except BrokerError:
            pass
        status = self._order_status(order_id)
        if status["filled_quantity"] > 0:
            return (status["average_price"] or fallback), status["filled_quantity"]
        raise BrokerError("Entry order was not filled (status %s). Cancel attempted - check your broker app."
                          % status.get("status"))

    def _flatten(self, symbol: str, side: str, qty: int) -> None:
        self._place(symbol, opposite(side), qty, "MARKET", None)

    # -- trade lifecycle --
    def open_trade(self, symbol: str, side: str, signal_entry: float, sl: float, tp: float,
                   risk: RiskManager) -> Dict:
        side = side.upper()
        if side not in (BUY, SELL):
            raise BrokerError("Direction must be BUY or SELL")
        if not self.has_session():
            raise BrokerError("Not logged in to %s today. Open SETTINGS and log in." % self.name)
        with self._lock:
            if self.state["positions"]:
                raise BrokerError("One open position at a time. Close it first.")
        f = self.funds()
        equity, cash = f["equity"], f["cash"]
        try:
            risk.assert_can_trade(equity)
        except RiskError as exc:
            raise BrokerError(str(exc))
        ltp = self.ltp(symbol)
        if signal_entry <= 0 or abs(ltp - signal_entry) / signal_entry > MAX_LTP_DEVIATION:
            raise BrokerError("Signal price %.2f is more than 0.5%% away from the broker price %.2f. Skipped."
                              % (signal_entry, ltp))
        if side == BUY and not (sl < ltp < tp):
            raise BrokerError("Plan no longer valid at price %.2f (need SL < price < TP)." % ltp)
        if side == SELL and not (tp < ltp < sl):
            raise BrokerError("Plan no longer valid at price %.2f (need TP < price < SL)." % ltp)
        qty = risk.position_size(equity, ltp, sl, cash)
        try:
            risk_amount = risk.validate_order(equity, qty, ltp, sl)
        except RiskError as exc:
            raise BrokerError(str(exc))

        entry_oid = self._place(symbol, side, qty, "MARKET", None)
        entry, filled = self._wait_fill(entry_oid, qty, ltp)
        qty = int(filled)
        stopped_out = (side == BUY and entry <= sl) or (side == SELL and entry >= sl)
        if stopped_out:
            self._flatten(symbol, side, qty)
            raise BrokerError("Fill price %.2f was already beyond the stop. Position flattened." % entry)
        try:
            sl_oid = self._place(symbol, opposite(side), qty, "SL-M", round_tick(sl))
        except BrokerError as exc:
            try:
                self._flatten(symbol, side, qty)
            except BrokerError as exc2:
                raise BrokerError("SL order failed (%s) AND closing failed (%s). CLOSE THE POSITION IN YOUR "
                                  "BROKER APP NOW." % (exc, exc2))
            raise BrokerError("Stop-loss order failed, position closed again: %s" % exc)
        with self._lock:
            self.state["seq"] += 1
            pos = {"id": "T%d" % self.state["seq"], "symbol": symbol, "side": side, "qty": qty,
                   "entry": round(entry, 2), "sl": round_tick(sl), "sl0": round_tick(sl), "tp": round_tick(tp),
                   "risk": round(risk_amount, 2), "entry_order_id": entry_oid, "sl_order_id": sl_oid,
                   "opened_at": ist_now().isoformat(timespec="seconds")}
            self.state["positions"].append(pos)
            self._save_state()
        risk.note_trade_opened()
        return dict(pos)

    def _finalize(self, pos: Dict, exit_price: float, reason: str) -> Dict:
        sign = 1 if pos["side"] == BUY else -1
        pnl = round((exit_price - pos["entry"]) * pos["qty"] * sign, 2)
        trade = dict(pos, exit=round(exit_price, 2), pnl=pnl, reason=reason,
                     closed_at=ist_now().isoformat(timespec="seconds"))
        with self._lock:
            self.state["positions"] = [p for p in self.state["positions"] if p["id"] != pos["id"]]
            self.state["trades"] = (self.state["trades"] + [trade])[-300:]
            self._book(trade)
            self._save_state()
        return trade

    def _book(self, trade: Dict) -> None:
        """Running totals that survive restarts and are not limited to the last 300 trades."""
        tot = self.state.setdefault("totals", {})
        day = _today()
        if tot.get("day") != day:
            tot["day"] = day
            tot["day_profit"] = tot["day_loss"] = 0.0
            tot["day_trades"] = 0
        pnl = float(trade["pnl"])
        key = "profit" if pnl >= 0 else "loss"
        tot[key] = round(tot.get(key, 0.0) + abs(pnl), 2)
        tot["day_" + key] = round(tot.get("day_" + key, 0.0) + abs(pnl), 2)
        tot["trades"] = tot.get("trades", 0) + 1
        tot["day_trades"] = tot.get("day_trades", 0) + 1
        tot["wins" if pnl >= 0 else "losses"] = tot.get("wins" if pnl >= 0 else "losses", 0) + 1

    def ledger(self) -> Dict:
        """Realized results: all time and today. 'profit' and 'loss' are positive amounts."""
        with self._lock:
            t = dict(self.state.get("totals", {}))
        today = t.get("day") == _today()
        out = {"profit": t.get("profit", 0.0), "loss": t.get("loss", 0.0), "trades": t.get("trades", 0),
               "wins": t.get("wins", 0), "losses": t.get("losses", 0),
               "day_profit": t.get("day_profit", 0.0) if today else 0.0,
               "day_loss": t.get("day_loss", 0.0) if today else 0.0,
               "day_trades": t.get("day_trades", 0) if today else 0}
        out["net"] = round(out["profit"] - out["loss"], 2)
        out["day_net"] = round(out["day_profit"] - out["day_loss"], 2)
        return out

    def _exit_now(self, pos: Dict, reason: str) -> Dict:
        """Cancel the protective stop, close at market. If closing fails the stop is placed again."""
        if pos.get("sl_order_id"):
            try:
                self._cancel(pos["sl_order_id"])
            except BrokerError:
                pass
        try:
            oid = self._place(pos["symbol"], opposite(pos["side"]), pos["qty"], "MARKET", None)
            price, _ = self._wait_fill(oid, pos["qty"], self.ltp(pos["symbol"]))
        except BrokerError as exc:
            try:
                new_sl = self._place(pos["symbol"], opposite(pos["side"]), pos["qty"], "SL-M", pos["sl"])
                with self._lock:
                    for p in self.state["positions"]:
                        if p["id"] == pos["id"]:
                            p["sl_order_id"] = new_sl
                    self._save_state()
            except BrokerError:
                pass
            raise BrokerError("Exit failed (%s). CHECK YOUR BROKER APP." % exc)
        return self._finalize(pos, price, reason)

    def set_trailing(self, on: bool) -> None:
        with self._lock:
            self.trailing = bool(on)
            self.state["trailing"] = self.trailing
            self._save_state()

    def pop_events(self) -> List[str]:
        with self._lock:
            ev, self.events = self.events, []
            return ev

    def _trail(self, pos: Dict, price: float) -> None:
        """Move the exchange stop closer to price when the trade is in profit. Never loosens the stop."""
        new = TrailingStop.new_stop(pos["side"], pos["entry"], pos.get("sl0", pos["sl"]), pos["sl"], price)
        if new is None:
            return
        self._modify_sl(pos, new)
        with self._lock:
            for p in self.state["positions"]:
                if p["id"] == pos["id"]:
                    p["sl"] = new
            self._save_state()
            self.events.append("Trailing stop for %s moved to %.2f (%s)" % (
                pos["symbol"], new, TrailingStop.stage(pos["side"], pos["entry"], pos.get("sl0", pos["sl"]), new)))
        pos["sl"] = new

    def check_exits(self) -> List[Dict]:
        """Called every few seconds. Handles stop fills, trailing stop, target and end of day. Never raises."""
        closed: List[Dict] = []
        for pos in self.positions():
            try:
                st = self._order_status(pos["sl_order_id"]) if pos.get("sl_order_id") else None
                if st and st["status"] == "COMPLETE":
                    closed.append(self._finalize(pos, st["average_price"] or pos["sl"], "STOP LOSS"))
                    continue
                if st and st["status"] in ("REJECTED", "CANCELLED"):
                    closed.append(self._exit_now(pos, "STOP ORDER LOST"))
                    continue
                now = ist_now()
                if (now.hour, now.minute) >= EOD_EXIT:
                    closed.append(self._exit_now(pos, "END OF DAY"))
                    continue
                price = self.ltp(pos["symbol"])
                if (pos["side"] == BUY and price >= pos["tp"]) or (pos["side"] == SELL and price <= pos["tp"]):
                    closed.append(self._exit_now(pos, "TARGET"))
                    continue
                if self.trailing and pos.get("sl_order_id"):
                    self._trail(pos, price)
                self.last_error = ""
            except BrokerError as exc:
                self.last_error = str(exc)[:140]
        return closed

    def close_all(self, reason: str = "MANUAL") -> Tuple[List[Dict], List[str]]:
        closed, errors = [], []
        for pos in self.positions():
            try:
                closed.append(self._exit_now(pos, reason))
            except BrokerError as exc:
                errors.append("%s: %s" % (pos["symbol"], exc))
        return closed, errors

    def emergency_exit(self, risk: RiskManager) -> Tuple[List[Dict], List[str]]:
        """Kill switch. The backend lock is set FIRST, then every open position is closed."""
        risk.kill("emergency kill switch")
        return self.close_all("KILL SWITCH")

    def square_off_all(self, reason: str = "PANIC SQUARE-OFF") -> Tuple[List[Dict], List[str], int]:
        """Closes every open intraday position: the ones this app tracks AND any others the broker reports.
        Returns (closed tracked trades, error texts, number of extra broker positions closed)."""
        errors: List[str] = []
        try:
            before = self.broker_positions()
        except BrokerError as exc:
            before = []
            errors.append("broker position list: %s" % exc)
        tracked: Dict[str, int] = {}
        for p in self.positions():
            tracked[p["symbol"]] = tracked.get(p["symbol"], 0) + (p["qty"] if p["side"] == BUY else -p["qty"])
        closed, errs = self.close_all(reason)
        errors += errs
        extra = 0
        for bp in before:
            residual = int(bp["qty"]) - tracked.get(bp["symbol"], 0)
            if residual == 0:
                continue
            try:
                self._place(bp["symbol"], SELL if residual > 0 else BUY, abs(residual), "MARKET", None)
                extra += 1
            except BrokerError as exc:
                errors.append("%s: %s" % (bp["symbol"], exc))
        return closed, errors, extra


# --------------------------------------------------------------------------- #
# Zerodha Kite Connect
# --------------------------------------------------------------------------- #
class KiteBroker(BaseLiveBroker):
    name = "KITE"

    def has_session(self) -> bool:
        s = self.store
        return bool(s.get("kite_key") and s.get("kite_token") and s.get("kite_day") == _today())

    def clear_session(self) -> None:
        self.store.update(kite_token="", kite_day="")

    def login_url(self) -> str:
        key = self.store.get("kite_key")
        if not key:
            raise BrokerError("Enter your Kite API key first.")
        return "%s?v=3&api_key=%s" % (KITE_LOGIN, urllib.parse.quote(key))

    def login(self, token_or_url: str) -> str:
        s = self.store
        req_token = extract_param(token_or_url, "request_token")
        if not (s.get("kite_key") and s.get("kite_secret") and req_token):
            raise BrokerError("API key, API secret and request token are all needed.")
        checksum = hashlib.sha256((s.get("kite_key") + req_token + s.get("kite_secret")).encode()).hexdigest()
        code, text = http("POST", KITE_ROOT + "/session/token", headers={"X-Kite-Version": "3"},
                          form={"api_key": s.get("kite_key"), "request_token": req_token, "checksum": checksum},
                          timeout=20)
        try:
            payload = json.loads(text)
        except ValueError:
            raise BrokerError("Kite login: unreadable answer (HTTP %s)" % code)
        if code != 200 or payload.get("status") != "success":
            raise BrokerError("Kite login failed: %s" % payload.get("message", "HTTP %s" % code))
        data = payload["data"]
        s.update(kite_token=data["access_token"], kite_day=_today())
        return data.get("user_id", "OK")

    def _call(self, method: str, path: str, params=None, form=None, raw: bool = False, timeout: int = 15):
        if not self.has_session():
            raise BrokerError("Kite session missing or expired. Log in again (tokens last one day).")
        headers = {"X-Kite-Version": "3",
                   "Authorization": "token %s:%s" % (self.store.get("kite_key"), self.store.get("kite_token"))}
        code, text = http(method, KITE_ROOT + path, headers=headers, params=params, form=form, timeout=timeout)
        if raw:
            if code != 200:
                raise BrokerError("Kite HTTP %s on %s" % (code, path))
            return text
        try:
            payload = json.loads(text)
        except ValueError:
            raise BrokerError("Kite: unreadable answer (HTTP %s)" % code)
        if code != 200 or payload.get("status") != "success":
            if payload.get("error_type") == "TokenException":
                self.clear_session()
            raise BrokerError("Kite: %s" % (payload.get("message") or "HTTP %s" % code))
        return payload.get("data")

    def funds(self) -> Dict[str, float]:
        data = self._call("GET", "/user/margins")
        eq = data["equity"]
        cash = float(eq["available"].get("live_balance", eq["available"].get("cash", 0.0)))
        total = float(eq.get("net", cash))
        return {"equity": max(total, cash), "cash": cash}

    def ltp(self, symbol: str) -> float:
        key = "NSE:%s" % symbol
        data = self._call("GET", "/quote/ltp", params={"i": key})
        if key not in data:
            raise BrokerError("Kite returned no price for %s (market data may need the paid Connect plan)." % key)
        return float(data[key]["last_price"])

    def _token(self, symbol: str) -> str:
        path = os.path.join(self.data_dir, "kite_instruments.json")
        mapping = None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
            if blob.get("day") == _today():
                mapping = blob["map"]
        except Exception:
            mapping = None
        if mapping is None:
            text = self._call("GET", "/instruments/NSE", raw=True, timeout=60)
            mapping = {}
            for row in csv.DictReader(io.StringIO(text)):
                if row.get("instrument_type") == "EQ" and row.get("segment") == "NSE":
                    mapping[row.get("tradingsymbol", "").upper()] = row.get("instrument_token", "")
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"day": _today(), "map": mapping}, fh)
            except OSError:
                pass
        token = mapping.get(symbol.upper())
        if not token:
            raise BrokerError("%s was not found on NSE." % symbol)
        return token

    def candles(self, symbol: str, count: int = HISTORY) -> List[Candle]:
        token = self._token(symbol)
        now = ist_now()
        frm = now - timedelta(days=5)
        data = self._call("GET", "/instruments/historical/%s/minute" % token,
                          params={"from": frm.strftime("%Y-%m-%d %H:%M:%S"), "to": now.strftime("%Y-%m-%d %H:%M:%S")},
                          timeout=30)
        rows = data.get("candles", []) if isinstance(data, dict) else []
        out = []
        for r in rows:
            ts = datetime.strptime(str(r[0]), "%Y-%m-%dT%H:%M:%S%z").timestamp()
            out.append(Candle(ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
        if len(out) < 30:
            raise BrokerError("Kite returned %d candles (historical data needs the paid Connect plan)." % len(out))
        return out[-count:]

    def _place(self, symbol: str, side: str, qty: int, order_type: str, trigger: Optional[float]) -> str:
        form = {"tradingsymbol": symbol, "exchange": "NSE", "transaction_type": side, "order_type": order_type,
                "quantity": int(qty), "product": "MIS", "validity": "DAY", "tag": "TDN"}
        if order_type == "SL-M":
            form["trigger_price"] = round_tick(float(trigger))
        data = self._call("POST", "/orders/regular", form=form)
        return str(data["order_id"])

    def _order_status(self, order_id: str) -> Dict:
        data = self._call("GET", "/orders/%s" % order_id)
        last = data[-1] if isinstance(data, list) and data else {}
        return {"status": str(last.get("status", "UNKNOWN")).upper(),
                "average_price": float(last.get("average_price") or 0.0),
                "filled_quantity": int(last.get("filled_quantity") or 0),
                "message": last.get("status_message") or ""}

    def _cancel(self, order_id: str) -> None:
        self._call("DELETE", "/orders/regular/%s" % order_id)

    def _modify_sl(self, pos: Dict, new_trigger: float) -> None:
        self._call("PUT", "/orders/regular/%s" % pos["sl_order_id"],
                   form={"order_type": "SL-M", "quantity": int(pos["qty"]),
                         "trigger_price": round_tick(float(new_trigger)), "validity": "DAY"})

    def broker_positions(self) -> List[Dict]:
        data = self._call("GET", "/portfolio/positions")
        out = []
        for r in (data.get("net") or []) if isinstance(data, dict) else []:
            qty = int(r.get("quantity") or 0)
            if r.get("exchange") == "NSE" and r.get("product") == "MIS" and qty != 0:
                out.append({"symbol": str(r.get("tradingsymbol", "")).upper(), "qty": qty})
        return out

    def quote_depth(self, symbol: str) -> Dict:
        key = "NSE:%s" % symbol
        data = self._call("GET", "/quote", params={"i": key})
        q = data.get(key) if isinstance(data, dict) else None
        if not q:
            raise BrokerError("Kite returned no quote for %s (market data needs the paid Connect plan)." % key)
        d = q.get("depth") or {}
        depth = normalise_depth([(r.get("price"), r.get("quantity", 0), r.get("orders", 0)) for r in d.get("buy", [])],
                                [(r.get("price"), r.get("quantity", 0), r.get("orders", 0)) for r in d.get("sell", [])])
        return {"ltp": float(q["last_price"]), "vol": int(q.get("volume") or 0), "depth": depth}

    _ws_token = 0

    def ws_connect_info(self, symbol: str):
        self._ws_token = int(self._token(symbol))
        url = "%s?api_key=%s&access_token=%s" % (KITE_WS, urllib.parse.quote(self.store.get("kite_key")),
                                                 urllib.parse.quote(self.store.get("kite_token")))
        msgs = [(1, json.dumps({"a": "subscribe", "v": [self._ws_token]}).encode()),
                (1, json.dumps({"a": "mode", "v": ["full", [self._ws_token]]}).encode())]
        return url, {}, msgs

    def ws_decode(self, opcode: int, data: bytes, symbol: str) -> Optional[Dict]:
        """Kite binary feed: 2 bytes packet count, then (2 bytes length + packet) repeated. Prices are in paise."""
        if opcode != 2 or len(data) < 4:
            return None
        out: Optional[Dict] = None
        try:
            count = struct.unpack(">H", data[:2])[0]
            pos = 2
            for _ in range(count):
                ln = struct.unpack(">H", data[pos:pos + 2])[0]
                pkt = data[pos + 2:pos + 2 + ln]
                pos += 2 + ln
                if len(pkt) < 8:
                    continue
                token, ltp = struct.unpack(">ii", pkt[:8])
                if token != self._ws_token:
                    continue
                res: Dict = {"ltp": ltp / 100.0}
                if len(pkt) >= 44:
                    res["vol"] = struct.unpack(">I", pkt[16:20])[0]
                if len(pkt) >= 184:
                    rows = []
                    for i in range(10):
                        q, p, o = struct.unpack(">IiH", pkt[64 + 12 * i:64 + 12 * i + 10])
                        rows.append((p / 100.0, q, o))
                    res["depth"] = normalise_depth(rows[:5], rows[5:])
                out = res
        except (struct.error, IndexError):
            return out
        return out


# --------------------------------------------------------------------------- #
# Upstox
# --------------------------------------------------------------------------- #
class UpstoxBroker(BaseLiveBroker):
    name = "UPSTOX"

    def has_session(self) -> bool:
        s = self.store
        return bool(s.get("upstox_token") and s.get("upstox_day") == _today())

    def clear_session(self) -> None:
        self.store.update(upstox_token="", upstox_day="")

    def login_url(self) -> str:
        s = self.store
        if not (s.get("upstox_key") and s.get("upstox_redirect")):
            raise BrokerError("Enter your Upstox API key and redirect URL first.")
        return "%s/login/authorization/dialog?%s" % (UPSTOX_ROOT, urllib.parse.urlencode(
            {"response_type": "code", "client_id": s.get("upstox_key"), "redirect_uri": s.get("upstox_redirect")}))

    def login(self, token_or_url: str) -> str:
        s = self.store
        code_value = extract_param(token_or_url, "code")
        if not (s.get("upstox_key") and s.get("upstox_secret") and s.get("upstox_redirect") and code_value):
            raise BrokerError("API key, API secret, redirect URL and the code are all needed.")
        code, text = http("POST", UPSTOX_ROOT + "/login/authorization/token",
                          headers={"Accept": "application/json"},
                          form={"code": code_value, "client_id": s.get("upstox_key"),
                                "client_secret": s.get("upstox_secret"), "redirect_uri": s.get("upstox_redirect"),
                                "grant_type": "authorization_code"}, timeout=20)
        try:
            payload = json.loads(text)
        except ValueError:
            raise BrokerError("Upstox login: unreadable answer (HTTP %s)" % code)
        if code != 200 or "access_token" not in payload:
            raise BrokerError("Upstox login failed: %s" % self._error_text(payload, code))
        s.update(upstox_token=payload["access_token"], upstox_day=_today())
        return payload.get("user_name") or payload.get("user_id") or "OK"

    @staticmethod
    def _error_text(payload, code) -> str:
        try:
            errs = payload.get("errors")
            if errs:
                return str(errs[0].get("message", errs[0]))[:140]
            return str(payload.get("message") or "HTTP %s" % code)[:140]
        except Exception:
            return "HTTP %s" % code

    def _call(self, method: str, path: str, params=None, body=None, timeout: int = 15):
        if not self.has_session():
            raise BrokerError("Upstox session missing or expired. Log in again (tokens last one day).")
        headers = {"Accept": "application/json", "Authorization": "Bearer %s" % self.store.get("upstox_token")}
        code, text = http(method, UPSTOX_ROOT + path, headers=headers, params=params, body=body, timeout=timeout)
        try:
            payload = json.loads(text)
        except ValueError:
            raise BrokerError("Upstox: unreadable answer (HTTP %s)" % code)
        if code == 401:
            self.clear_session()
            raise BrokerError("Upstox session expired. Log in again.")
        if code != 200 or payload.get("status") != "success":
            raise BrokerError("Upstox: %s" % self._error_text(payload, code))
        return payload.get("data")

    def _key(self, symbol: str) -> str:
        symbol = symbol.upper()
        if symbol in UPSTOX_KEYS:
            return UPSTOX_KEYS[symbol]
        path = os.path.join(self.data_dir, "upstox_instruments.json")
        mapping = None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
            if blob.get("day") == _today():
                mapping = blob["map"]
        except Exception:
            mapping = None
        if mapping is None:
            code, text = http("GET", UPSTOX_INSTRUMENTS, timeout=90)
            if code != 200:
                raise BrokerError("Could not download the Upstox instrument list (HTTP %s)." % code)
            mapping = {}
            for row in json.loads(text):
                if row.get("segment") == "NSE_EQ" and row.get("instrument_type") == "EQ":
                    mapping[str(row.get("trading_symbol", "")).upper()] = row.get("instrument_key", "")
            del text
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"day": _today(), "map": mapping}, fh)
            except OSError:
                pass
        key = mapping.get(symbol)
        if not key:
            raise BrokerError("%s was not found on NSE." % symbol)
        return key

    def funds(self) -> Dict[str, float]:
        data = self._call("GET", "/user/get-funds-and-margin", params={"segment": "SEC"})
        eq = data["equity"]
        cash = float(eq.get("available_margin", 0.0))
        used = float(eq.get("used_margin", 0.0))
        return {"equity": cash + used, "cash": cash}

    def ltp(self, symbol: str) -> float:
        key = self._key(symbol)
        data = self._call("GET", "/market-quote/ltp", params={"instrument_key": key})
        if not isinstance(data, dict) or not data:
            raise BrokerError("Upstox returned no price for %s." % symbol)
        for k, v in data.items():
            if str(k).split(":")[-1].upper() == symbol.upper() or str(v.get("instrument_token", "")) == key:
                return float(v["last_price"])
        raise BrokerError("Upstox price answer does not match %s. Refusing to trade it." % symbol)

    def candles(self, symbol: str, count: int = HISTORY) -> List[Candle]:
        key = urllib.parse.quote(self._key(symbol), safe="")
        data = self._call("GET", "/historical-candle/intraday/%s/1minute" % key, timeout=30)
        rows = data.get("candles", []) if isinstance(data, dict) else []
        out = []
        for r in reversed(rows):                      # Upstox sends the newest candle first
            ts = datetime.fromisoformat(str(r[0])).timestamp()
            out.append(Candle(ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
        if len(out) < 30:
            raise BrokerError("Upstox returned only %d candles today (market closed or just opened)." % len(out))
        return out[-count:]

    def _place(self, symbol: str, side: str, qty: int, order_type: str, trigger: Optional[float]) -> str:
        body = {"quantity": int(qty), "product": "I", "validity": "DAY", "price": 0, "tag": "TDN",
                "instrument_token": self._key(symbol), "order_type": order_type, "transaction_type": side,
                "disclosed_quantity": 0, "trigger_price": round_tick(float(trigger)) if trigger else 0,
                "is_amo": False}
        data = self._call("POST", "/order/place", body=body)
        return str(data["order_id"])

    def _order_status(self, order_id: str) -> Dict:
        data = self._call("GET", "/order/details", params={"order_id": order_id})
        raw = str(data.get("status", "unknown")).lower()
        status = {"complete": "COMPLETE", "rejected": "REJECTED", "cancelled": "CANCELLED"}.get(raw, raw.upper())
        return {"status": status, "average_price": float(data.get("average_price") or 0.0),
                "filled_quantity": int(data.get("filled_quantity") or 0),
                "message": data.get("status_message") or ""}

    def _cancel(self, order_id: str) -> None:
        self._call("DELETE", "/order/cancel", params={"order_id": order_id})

    def _modify_sl(self, pos: Dict, new_trigger: float) -> None:
        self._call("PUT", "/order/modify", body={
            "quantity": int(pos["qty"]), "validity": "DAY", "price": 0, "order_id": pos["sl_order_id"],
            "order_type": "SL-M", "disclosed_quantity": 0, "trigger_price": round_tick(float(new_trigger))})

    def broker_positions(self) -> List[Dict]:
        data = self._call("GET", "/portfolio/short-term-positions")
        out = []
        for r in data if isinstance(data, list) else []:
            sym = str(r.get("trading_symbol") or r.get("tradingsymbol") or "").upper()
            qty = int(r.get("quantity") or 0)
            if sym and qty != 0 and r.get("product") in ("I", "MIS"):
                out.append({"symbol": sym, "qty": qty})
        return out

    def quote_depth(self, symbol: str) -> Dict:
        key = self._key(symbol)
        data = self._call("GET", "/market-quote/quotes", params={"instrument_key": key})
        for k, v in (data or {}).items():
            if str(k).split(":")[-1].upper() == symbol.upper() or str(v.get("instrument_token", "")) == key:
                d = v.get("depth") or {}
                depth = normalise_depth([(r.get("price"), r.get("quantity", 0), r.get("orders", 0)) for r in d.get("buy", [])],
                                        [(r.get("price"), r.get("quantity", 0), r.get("orders", 0)) for r in d.get("sell", [])])
                return {"ltp": float(v["last_price"]), "vol": int(v.get("volume") or 0), "depth": depth}
        raise BrokerError("Upstox returned no quote for %s." % symbol)

    _ws_key = ""

    def ws_connect_info(self, symbol: str):
        data = self._call("GET", "/feed/market-data-feed/authorize")
        url = (data or {}).get("authorized_redirect_uri") or (data or {}).get("authorizedRedirectUri")
        if not url:
            raise BrokerError("Upstox did not return a WebSocket address.")
        self._ws_key = self._key(symbol)
        msg = {"guid": "tdn", "method": "sub", "data": {"mode": "full", "instrumentKeys": [self._ws_key]}}
        return url, {}, [(2, json.dumps(msg).encode())]

    @staticmethod
    def _decode_market_ff(buf: bytes) -> Dict:
        res: Dict = {}
        buys: List = []
        sells: List = []
        for fn, _wt, v in pb_fields(buf):
            if fn == 1:                                   # LTPC
                for f2, w2, v2 in pb_fields(v):
                    if f2 == 1:
                        res["ltp"] = _dbl(v2)
            elif fn == 2:                                 # MarketLevel -> repeated BidAskQuote
                for f2, _w2, v2 in pb_fields(v):
                    if f2 != 1:
                        continue
                    bq = bp = aq = ap = 0
                    for f3, _w3, v3 in pb_fields(v2):
                        if f3 == 1:
                            bq = v3
                        elif f3 == 2:
                            bp = _dbl(v3)
                        elif f3 == 3:
                            aq = v3
                        elif f3 == 4:
                            ap = _dbl(v3)
                    buys.append((bp, bq, 0))
                    sells.append((ap, aq, 0))
            elif fn == 6 and isinstance(v, int):          # volume traded today
                res["vol"] = v
        if buys or sells:
            res["depth"] = normalise_depth(buys, sells)
        return res

    def ws_decode(self, opcode: int, data: bytes, symbol: str) -> Optional[Dict]:
        """Upstox protobuf feed: FeedResponse.feeds map -> Feed.ff.marketFF (LTPC, depth, volume)."""
        if opcode != 2:
            return None
        out: Optional[Dict] = None
        try:
            for fn, wt, v in pb_fields(data):
                if fn != 2 or wt != 2:
                    continue
                fkey, feed = "", b""
                for f2, _w2, v2 in pb_fields(v):
                    if f2 == 1:
                        fkey = v2.decode("utf-8", "replace")
                    elif f2 == 2:
                        feed = v2
                if fkey != self._ws_key:
                    continue
                for f3, _w3, v3 in pb_fields(feed):
                    if f3 == 2:                           # FullFeed
                        for f4, _w4, v4 in pb_fields(v3):
                            if f4 == 1:                   # marketFF
                                res = self._decode_market_ff(v4)
                                if "ltp" in res:
                                    out = res
        except (ValueError, IndexError, struct.error):
            return out
        return out


BROKERS = {"KITE": KiteBroker, "ZERODHA": KiteBroker, "UPSTOX": UpstoxBroker}


def make_broker(name: str, store: CredentialStore, data_dir: str) -> BaseLiveBroker:
    cls = BROKERS.get(str(name).upper())
    if cls is None:
        raise BrokerError("Unknown broker %s. Choose KITE or UPSTOX." % name)
    return cls(store, data_dir)


# --------------------------------------------------------------------------- #
# Market data feeds (for the chart and the agents)
# --------------------------------------------------------------------------- #
class DemoFeed:
    """Simulated prices. For looking at the screen only: live orders are refused on this feed."""
    label = "DEMO"
    poll_seconds = 1.0

    def __init__(self):
        self._st: Dict[str, Dict] = {}

    def _state(self, symbol: str) -> Dict:
        if symbol not in self._st:
            seed = sum(ord(ch) * (i + 1) for i, ch in enumerate(symbol))
            rnd = random.Random(seed + int(time.time() // 3600))
            base = DEMO_BASE.get(symbol, 100.0 + (seed % 900))
            self._st[symbol] = {"rnd": rnd, "px": base, "vol": base * 0.0006, "drift": 0.0, "left": 0}
        return self._st[symbol]

    def _step(self, st: Dict) -> float:
        rnd = st["rnd"]
        if st["left"] <= 0:
            st["left"] = rnd.randint(25, 90)
            st["drift"] = rnd.choice([-1.0, 0.0, 1.0, 1.0, -1.0]) * st["vol"] * 0.35
        st["left"] -= 1
        st["px"] = max(1.0, st["px"] + st["drift"] + rnd.gauss(0.0, st["vol"]))
        return st["px"]

    def bootstrap(self, symbol: str) -> List[Candle]:
        st = self._state(symbol)
        now_min = int(time.time() // 60) * 60
        out: List[Candle] = []
        for i in range(HISTORY, 0, -1):
            o = st["px"]
            hi = lo = o
            for _ in range(6):
                p = self._step(st)
                hi, lo = max(hi, p), min(lo, p)
            vol = abs(st["rnd"].gauss(1000, 300)) + 100 + 400 * abs(st["px"] - o) / (st["vol"] + 1e-9)
            out.append(Candle(now_min - i * 60, o, hi, lo, st["px"], round(vol)))
        return out

    def poll(self, symbol: str, candles: List[Candle]) -> None:
        st = self._state(symbol)
        p = self._step(st)
        now_min = int(time.time() // 60) * 60
        last = candles[-1]
        vol = abs(st["rnd"].gauss(40, 15)) + 5
        if now_min > last.t:
            candles.append(Candle(now_min, last.c, max(last.c, p), min(last.c, p), p, round(vol)))
        else:
            last.h, last.l, last.c = max(last.h, p), min(last.l, p), p
            last.v += round(vol)
        del candles[:-MAX_CANDLES]


class YahooFeed:
    """Public Yahoo Finance chart data for NSE stocks (delayed, unofficial)."""
    label = "YAHOO"
    poll_seconds = 15.0
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1m&range=1d"

    def _fetch(self, symbol: str) -> List[Candle]:
        code, text = http("GET", self.URL % urllib.parse.quote(symbol + ".NS"), timeout=12)
        if code != 200:
            raise BrokerError("Yahoo feed: HTTP %s" % code)
        try:
            res = json.loads(text)["chart"]["result"][0]
            ts, q = res["timestamp"], res["indicators"]["quote"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            raise BrokerError("Yahoo feed: unreadable answer")
        out = []
        for i, t in enumerate(ts):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if None in (o, h, l, c):
                continue
            out.append(Candle(float(t), float(o), float(h), float(l), float(c), float(q["volume"][i] or 0)))
        if len(out) < 30:
            raise BrokerError("Yahoo returned only %d candles (market closed?)." % len(out))
        return out[-MAX_CANDLES:]

    def bootstrap(self, symbol: str) -> List[Candle]:
        return self._fetch(symbol)

    def poll(self, symbol: str, candles: List[Candle]) -> None:
        candles[:] = self._fetch(symbol)


class BrokerFeed:
    """Candles straight from the connected broker."""
    poll_seconds = 10.0

    def __init__(self, broker: BaseLiveBroker):
        self.broker = broker
        self.label = broker.name

    def bootstrap(self, symbol: str) -> List[Candle]:
        return self.broker.candles(symbol, HISTORY)

    def poll(self, symbol: str, candles: List[Candle]) -> None:
        candles[:] = self.broker.candles(symbol, MAX_CANDLES)


class TickStream:
    """Live ticks and market depth for one symbol. WebSocket first; if it fails the stream polls REST quotes
    for 45 seconds and then tries the WebSocket again. latest() always says which mode is active."""

    def __init__(self, broker: BaseLiveBroker):
        self.broker = broker
        self.symbol = ""
        self.mode = "OFF"
        self.note = ""
        self._tick: Dict = {}
        self._depth: Optional[Dict] = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._gen = 0

    def set_symbol(self, symbol: str) -> None:
        with self._lock:
            self.symbol = symbol.upper()
            self._gen += 1
            self._tick, self._depth = {}, None

    def set_broker(self, broker: BaseLiveBroker) -> None:
        with self._lock:
            self.broker = broker
            self._gen += 1
            self._tick, self._depth, self.mode = {}, None, "OFF"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> Dict:
        with self._lock:
            t = dict(self._tick)
            t["mode"] = self.mode
            t["note"] = self.note
            t["depth"] = self._depth
            t["age"] = (time.time() - t["ts"]) if "ts" in t else None
            return t

    def _update(self, upd: Dict, mode: str, gen: int) -> None:
        with self._lock:
            if gen != self._gen:
                return
            if upd.get("ltp"):
                self._tick = {"ltp": float(upd["ltp"]), "vol": upd.get("vol", self._tick.get("vol", 0)), "ts": time.time()}
            if upd.get("depth"):
                self._depth = upd["depth"]
            self.mode = mode

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                sym, gen, broker = self.symbol, self._gen, self.broker
            if not sym or not broker.has_session():
                self.mode = "OFF"
                self._stop.wait(1.0)
                continue
            try:
                self._ws_session(broker, sym, gen)
            except BrokerError as exc:
                self.note = "WebSocket: %s" % str(exc)[:80]
            except Exception as exc:
                self.note = "WebSocket error: %s" % str(exc)[:80]
            if self._stop.is_set():
                break
            self._rest_session(broker, sym, gen, 45.0)

    def _alive(self, gen: int) -> bool:
        return (not self._stop.is_set()) and gen == self._gen

    def _ws_session(self, broker: BaseLiveBroker, sym: str, gen: int) -> None:
        url, headers, msgs = broker.ws_connect_info(sym)
        ws = WebSocketClient(url, headers)
        ws.connect()
        try:
            for op, payload in msgs:
                ws.send(op, payload)
            last_msg = last_ping = time.time()
            self.note = ""
            while self._alive(gen):
                m = ws.recv()
                now = time.time()
                if m is not None:
                    last_msg = now
                    upd = broker.ws_decode(m[0], m[1], sym)
                    if upd:
                        self._update(upd, "WS", gen)
                if now - last_ping > 15:
                    ws.send(0x9, b"")
                    last_ping = now
                if now - last_msg > 20:
                    raise BrokerError("no data for 20 seconds")
        finally:
            ws.close()

    def _rest_session(self, broker: BaseLiveBroker, sym: str, gen: int, seconds: float) -> None:
        end = time.time() + seconds
        while self._alive(gen) and time.time() < end:
            try:
                self._update(broker.quote_depth(sym), "REST", gen)
            except BrokerError as exc:
                self.note = "REST: %s" % str(exc)[:80]
            except Exception as exc:
                self.note = "REST error: %s" % str(exc)[:80]
            self._stop.wait(2.0)


class MarketStream:
    """Keeps the candles of one symbol fresh. The feed chain is tried in order and moves down on repeated failure."""

    def __init__(self, symbol: str = "SBIN"):
        self._lock = threading.RLock()
        self.symbol = symbol.upper()
        self._candles: List[Candle] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.feed = None
        self.source = "NONE"
        self.note = ""
        self.chain: List = []
        self._fails = 0
        self._index = 0
        self._next_poll = 0.0
        self._ticks: Optional[TickStream] = None

    def attach_ticks(self, ticks: "TickStream") -> None:
        self._ticks = ticks

    def configure(self, chain: List) -> None:
        """chain: list of feed objects, best first. The last one should be a DemoFeed."""
        self.chain = list(chain)
        self._activate(0)

    def _activate(self, start: int) -> None:
        notes = []
        for i in range(start, len(self.chain)):
            feed = self.chain[i]
            try:
                candles = feed.bootstrap(self.symbol)
            except BrokerError as exc:
                notes.append("%s: %s" % (feed.label, str(exc)[:70]))
                continue
            except Exception as exc:
                notes.append("%s: %s" % (feed.label, str(exc)[:70]))
                continue
            with self._lock:
                self.feed, self._candles, self._fails = feed, candles, 0
                self.source = feed.label
                self.note = " | ".join(notes)
                self._index = i
                self._next_poll = time.time() + feed.poll_seconds
            return
        with self._lock:
            self.feed, self._candles, self.source = None, [], "NONE"
            self.note = " | ".join(notes) or "no data feed available"

    def set_symbol(self, symbol: str) -> None:
        self.symbol = symbol.upper()
        self._activate(0)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            with self._lock:
                feed, sym = self.feed, self.symbol
                work = list(self._candles)
                due = now >= self._next_poll
            if feed is not None and due:
                try:
                    feed.poll(sym, work)
                    with self._lock:
                        if sym == self.symbol and feed is self.feed:
                            self._candles, self._fails = work, 0
                except Exception as exc:
                    with self._lock:
                        self._fails += 1
                        self.note = "%s: %s" % (feed.label, str(exc)[:80])
                        failed = self._fails >= 3
                    if failed and self._index + 1 < len(self.chain):
                        self._activate(self._index + 1)
                with self._lock:
                    self._next_poll = time.time() + getattr(self.feed or feed, "poll_seconds", 5.0)
            self._stop.wait(0.25)

    def snapshot(self) -> Tuple[List[Candle], float, str, str]:
        with self._lock:
            copy = [Candle(c.t, c.o, c.h, c.l, c.c, c.v) for c in self._candles]
            tick = self._ticks.latest() if self._ticks is not None else None
            if copy and tick and tick.get("ltp") and self.source in ("KITE", "UPSTOX") and time.time() - tick["ts"] < 15:
                ltp, minute, last = tick["ltp"], int(tick["ts"] // 60) * 60, copy[-1]
                if minute > last.t:
                    copy.append(Candle(minute, last.c, max(last.c, ltp), min(last.c, ltp), ltp, 0.0))
                elif minute == last.t:
                    last.h, last.l, last.c = max(last.h, ltp), min(last.l, ltp), ltp
            price = copy[-1].c if copy else 0.0
            return copy, price, self.source, self.note
