# -*- coding: utf-8 -*-
"""forex.py - forex market support for Tactical Neural Matrix (TDN).

What is in here
  * PAIRS / helpers   - the seven USD majors (EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, NZDUSD): decimals,
                        tick, Yahoo and OANDA names, session hours, price formatting.
  * FxMarket          - a small mixin that teaches the shared trade engine (broker.BaseLiveBroker) how forex differs
                        from NSE shares: price decimals, units instead of shares, quote-to-USD conversion for risk
                        sizing, no end-of-day exit but a weekend exit, tighter signal-vs-price check.
  * YahooFxFeed       - live public candles for the pairs (Yahoo Finance, may lag a little). No data means no data:
                        nothing is ever simulated.
  * FxPaperBroker     - PAPER trading on live forex prices with virtual USD (same lifecycle as live trading).
  * OandaBroker       - the LIVE broker interface (OANDA v20 REST API). The stop-loss and target are sent WITH the
                        order, so the broker holds them even if the phone goes offline, and a trade whose stop is not
                        confirmed is closed again at once.
  * NoTicks           - a stand-in tick stream for forex (there is no NSE-style WebSocket depth).

Safety rules that apply here exactly as for NSE
  * 1% risk per trade and 2% daily loss limit (risk_manager.RiskManager, separate state per book), 2 losses in a row
    lock the day, one open position at a time, every order needs ARM + UNLOCK LIVE + the CONFIRM popup.
  * Risk is measured in USD: the stop distance is converted from the quote currency (for USDJPY the loss in yen is
    divided by the price). Only USD accounts are supported; any other account currency is refused.
  * Leverage is assumed to be FX_LEVERAGE (5x) when checking that the position fits the margin. That never raises the
    risk above 1%: the stop-loss distance still decides the size.

Honest limits
  * OANDA does not accept clients from India (BrokerChooser, September 2026), and Indian residents have regulatory
    limits on forex. The interface is written so another broker can be added by subclassing FxMarket + BaseLiveBroker;
    NSE currency futures through Kite or Upstox would be the Indian route and are NOT implemented here because the
    contract naming could not be verified. Check the rules for your own situation before trading.
  * Yahoo prices are public mid prices, not tradable quotes; paper fills add a small slippage. Forex has no volume,
    so the volume agents stay neutral.
"""
import json
from datetime import datetime, timezone
from typing import Dict, List, NamedTuple, Optional, Tuple

from agents import BUY, SELL, Candle, TrailingStop
from broker import (HISTORY, BaseLiveBroker, BrokerError, CredentialStore, PaperBroker, YahooFeed, http, redact)
from risk_manager import RiskError, RiskManager

FX_ACCOUNT_CURRENCY = "USD"
FX_LEVERAGE = 5.0              # used only to check that the position fits the margin
FX_MAX_UNITS = 1000000
FX_MAX_DEVIATION = 0.001       # signal price vs live price, 0.1%
FX_SLIPPAGE = 0.00005          # paper fills: about half a pip on EURUSD
FX_MIN_CANDLES = 30


class Pair(NamedTuple):
    symbol: str
    base: str
    quote: str
    dp: int
    yahoo: str
    oanda: str


def _pair(base: str, quote: str) -> Pair:
    return Pair(base + quote, base, quote, 3 if quote == "JPY" else 5, base + quote + "=X", base + "_" + quote)


PAIRS: Dict[str, Pair] = {p.symbol: p for p in (
    _pair("EUR", "USD"), _pair("GBP", "USD"), _pair("USD", "JPY"), _pair("AUD", "USD"),
    _pair("USD", "CAD"), _pair("USD", "CHF"), _pair("NZD", "USD"))}
FX_WATCHLIST: List[str] = list(PAIRS)


def is_pair(symbol: str) -> bool:
    return str(symbol).upper() in PAIRS


def price_dp(symbol: str) -> int:
    p = PAIRS.get(str(symbol).upper())
    return p.dp if p else 5


def pip_size(symbol: str) -> float:
    return 0.01 if price_dp(symbol) == 3 else 0.0001


def fmt_price(symbol: str, x) -> str:
    try:
        return "%.*f" % (price_dp(symbol), float(x))
    except (TypeError, ValueError):
        return "-"


# --------------------------------------------------------------------------- #
# Sessions (UTC). The forex week runs from Sunday evening to Friday evening New York time.
# The window used here is the conservative one: closed from Friday 21:00 UTC to Sunday 22:00 UTC.
# --------------------------------------------------------------------------- #
def _utc(now: Optional[datetime]) -> datetime:
    return now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)


def fx_open(now: Optional[datetime] = None) -> bool:
    t = _utc(now)
    minutes = t.hour * 60 + t.minute
    wd = t.weekday()
    if wd == 5:
        return False
    if wd == 4 and minutes >= 21 * 60:
        return False
    if wd == 6 and minutes < 22 * 60:
        return False
    return True


def fx_flatten_due(now: Optional[datetime] = None) -> bool:
    """Open positions are closed before the weekend gap: Friday from 20:30 UTC."""
    t = _utc(now)
    minutes = t.hour * 60 + t.minute
    wd = t.weekday()
    return (wd == 4 and minutes >= 20 * 60 + 30) or wd == 5 or (wd == 6 and minutes < 22 * 60)


def fx_slot(now: Optional[datetime] = None) -> str:
    """Trading session of the moment, used to file paper results: ASIA, LONDON or NEWYORK."""
    h = _utc(now).hour
    if h >= 21 or h < 7:
        return "ASIA"
    if h < 13:
        return "LONDON"
    return "NEWYORK"


def session_text(now: Optional[datetime] = None) -> str:
    return "OPEN (%s session)" % fx_slot(now) if fx_open(now) else "CLOSED (weekend)"


# --------------------------------------------------------------------------- #
# The mixin that turns the shared trade engine into a forex engine
# --------------------------------------------------------------------------- #
class FxMarket:
    """Hooks for BaseLiveBroker. Put it BEFORE the broker class: class X(FxMarket, BaseLiveBroker)."""
    max_deviation = FX_MAX_DEVIATION
    max_units = FX_MAX_UNITS

    @staticmethod
    def _spec(symbol: str) -> Pair:
        p = PAIRS.get(str(symbol).upper())
        if p is None:
            raise BrokerError("%s is not a supported forex pair." % symbol)
        return p

    def rp(self, symbol: str, x: float) -> float:
        return round(float(x), self._spec(symbol).dp)

    def fmt(self, symbol: str, x: float) -> str:
        return fmt_price(symbol, x)

    def tick_of(self, symbol: str) -> float:
        return 10.0 ** (-self._spec(symbol).dp)

    def dp_of(self, symbol: str) -> int:
        return self._spec(symbol).dp

    def unit_economics(self, symbol: str, price: float) -> Tuple[float, Optional[float]]:
        """(USD value of a 1.0 price move per unit, USD margin needed per unit).
        XXXUSD pairs: the quote currency is USD, so a 1.0 move is worth 1 USD per unit.
        USDXXX pairs: the quote is the other currency, so the value is 1/price USD per unit."""
        p = self._spec(symbol)
        if price <= 0:
            raise BrokerError("No valid price for %s." % symbol)
        if p.quote == FX_ACCOUNT_CURRENCY:
            return 1.0, price / FX_LEVERAGE
        return 1.0 / price, 1.0 / FX_LEVERAGE

    def eod_due(self) -> bool:
        return fx_flatten_due()


class YahooFxFeed(YahooFeed):
    """Live 1-minute forex candles from Yahoo Finance (public, may lag, no volume)."""
    label = "YAHOO"
    poll_seconds = 10.0

    def _ticker(self, symbol: str) -> str:
        return FxMarket._spec(symbol).yahoo


class NoTicks:
    """Stand-in for broker.TickStream's broker: forex has no NSE-style tick WebSocket or depth."""
    name = "NONE"

    def has_session(self) -> bool:
        return False


# --------------------------------------------------------------------------- #
# PAPER trading on live forex prices
# --------------------------------------------------------------------------- #
class FxPaperBroker(FxMarket, PaperBroker):
    """Virtual USD account trading the pairs on live prices. Same lifecycle as live trading (stop, trailing stop,
    target, weekend exit, P&L ledger) through the shared engine; only the order desk is virtual."""
    name = "FXPAPER"
    product_label = "FX PAPER"
    START_CAPITAL = 10000.0
    MIN_CAPITAL = 1000.0
    MAX_CAPITAL = 10000000.0
    SLIPPAGE = FX_SLIPPAGE
    CURRENCY_WORD = "US dollars"

    def _fetch_candles(self, symbol: str) -> List[Candle]:
        return YahooFxFeed()._fetch(symbol)


# --------------------------------------------------------------------------- #
# LIVE broker interface: OANDA v20
# --------------------------------------------------------------------------- #
def _parse_time(text) -> float:
    """OANDA times look like 2026-11-24T10:15:00.000000000Z (or a plain number of seconds)."""
    s = str(text)
    try:
        return float(s)
    except ValueError:
        pass
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        raise BrokerError("OANDA: unreadable time %r" % s[:30])


class OandaBroker(FxMarket, BaseLiveBroker):
    """OANDA v20 REST. CONFIG holds a personal access token and the account id (private storage, masked on screen)
    and an environment switch: PRACTICE is OANDA's own sandbox with the broker's practice funds, LIVE is real money.
    The app starts on PRACTICE."""
    name = "OANDA"
    product_label = "FX"
    HOSTS = {"PRACTICE": "https://api-fxpractice.oanda.com", "LIVE": "https://api-fxtrade.oanda.com"}

    def __init__(self, store: CredentialStore, data_dir: str):
        super().__init__(store, data_dir)
        self.spread = 0.0

    # ---- session ----
    @property
    def env(self) -> str:
        return self.store.get("oanda_env") if self.store.get("oanda_env") in self.HOSTS else "PRACTICE"

    def has_session(self) -> bool:
        return bool(self.store.get("oanda_token")) and bool(self.store.get("oanda_account"))

    def login_url(self) -> str:
        raise BrokerError("OANDA uses a personal access token: create it in your OANDA account under API access and "
                          "paste it with the account id in CONFIG.")

    def login(self, token_or_url: str) -> str:
        raise BrokerError("OANDA uses a personal access token: save it with the account id in CONFIG.")

    def clear_session(self) -> None:
        self.store.update(oanda_token="", oanda_account="")

    def _scrub(self, text: str) -> str:
        """Removes the saved token and account id (and anything that looks like a key) from text shown to the user."""
        out = str(text)
        for secret in (self.store.get("oanda_token"), self.store.get("oanda_account")):
            if secret and len(secret) >= 6:
                out = out.replace(secret, "***")
        return redact(out)

    def _acct(self, tail: str) -> str:
        return "/v3/accounts/%s%s" % (self.store.get("oanda_account"), tail)

    def _call(self, method: str, path: str, params=None, body=None, timeout: int = 15) -> Dict:
        if not self.has_session():
            raise BrokerError("OANDA token or account id is missing. Open CONFIG.")
        headers = {"Authorization": "Bearer %s" % self.store.get("oanda_token"), "Accept": "application/json"}
        code, text = http(method, self.HOSTS[self.env] + path, headers=headers, params=params, body=body,
                          timeout=timeout)
        try:
            payload = json.loads(text) if text.strip() else {}
        except ValueError:
            raise BrokerError("OANDA: unreadable answer (HTTP %s)" % code)
        if code == 401:
            raise BrokerError("OANDA rejected the token (HTTP 401). Check the token and the PRACTICE/LIVE setting.")
        if code >= 400:
            msg = payload.get("errorMessage") or (payload.get("orderRejectTransaction") or {}).get("rejectReason") \
                or "HTTP %s" % code
            raise BrokerError("OANDA: %s" % self._scrub(str(msg))[:140])
        return payload

    # ---- account and prices ----
    def funds(self) -> Dict[str, float]:
        acc = self._call("GET", self._acct("/summary")).get("account") or {}
        if acc.get("currency") != FX_ACCOUNT_CURRENCY:
            raise BrokerError("This OANDA account is in %s. Only USD accounts are supported." % acc.get("currency"))
        try:
            return {"equity": float(acc["NAV"]), "cash": float(acc["marginAvailable"])}
        except (KeyError, ValueError, TypeError):
            raise BrokerError("OANDA: account summary is missing NAV or marginAvailable.")

    def quote(self, symbol: str) -> Tuple[float, float]:
        inst = self._spec(symbol).oanda
        prices = self._call("GET", self._acct("/pricing"), params={"instruments": inst}).get("prices") or []
        if not prices:
            raise BrokerError("OANDA returned no price for %s." % symbol)
        pr = prices[0]
        if pr.get("status") != "tradeable":
            raise BrokerError("OANDA says %s is not tradeable right now (status %s)." % (symbol, pr.get("status")))
        try:
            bid, ask = float(pr["bids"][0]["price"]), float(pr["asks"][0]["price"])
        except (KeyError, IndexError, ValueError, TypeError):
            raise BrokerError("OANDA: unreadable price for %s." % symbol)
        self.spread = max(0.0, ask - bid)
        return bid, ask

    def ltp(self, symbol: str) -> float:
        bid, ask = self.quote(symbol)
        return (bid + ask) / 2.0

    def candles(self, symbol: str, count: int = HISTORY) -> List[Candle]:
        inst = self._spec(symbol).oanda
        data = self._call("GET", "/v3/instruments/%s/candles" % inst,
                          params={"granularity": "M1", "count": max(30, min(int(count), 500)), "price": "M"})
        out: List[Candle] = []
        for c in data.get("candles") or []:
            try:
                m = c["mid"]
                out.append(Candle(_parse_time(c["time"]), float(m["o"]), float(m["h"]), float(m["l"]), float(m["c"]),
                                  float(c.get("volume", 0))))
            except (KeyError, ValueError, TypeError):
                continue
        if len(out) < FX_MIN_CANDLES:
            raise BrokerError("OANDA returned only %d candles (market closed?)." % len(out))
        return out

    def quote_depth(self, symbol: str) -> Dict:
        raise BrokerError("OANDA forex has no market depth.")

    def ws_connect_info(self, symbol: str):
        raise BrokerError("No WebSocket for forex.")

    def ws_decode(self, opcode: int, data: bytes, symbol: str) -> Optional[Dict]:
        return None

    # ---- orders ----
    def _place(self, symbol: str, side: str, qty: int, order_type: str, trigger: Optional[float]) -> str:
        """Only used to close leftovers (panic square-off). Entries go through open_trade with the stop attached."""
        if order_type != "MARKET":
            raise BrokerError("OANDA stops are sent with the entry order; separate stop orders are not used.")
        units = int(qty) if side == BUY else -int(qty)
        body = {"order": {"type": "MARKET", "instrument": self._spec(symbol).oanda, "units": str(units),
                          "timeInForce": "FOK", "positionFill": "REDUCE_ONLY"}}
        payload = self._call("POST", self._acct("/orders"), body=body)
        fill = payload.get("orderFillTransaction")
        if not fill:
            reason = (payload.get("orderCancelTransaction") or {}).get("reason", "no reason given")
            raise BrokerError("OANDA did not fill the closing order (%s)." % reason)
        return str(fill.get("id", ""))

    def _order_status(self, order_id: str) -> Dict:
        raise BrokerError("OANDA orders are filled or cancelled at once (fill or kill).")

    def _cancel(self, order_id: str) -> None:
        raise BrokerError("OANDA orders are filled or cancelled at once (fill or kill).")

    def _modify_sl(self, pos: Dict, new_trigger: float) -> None:
        body = {"stopLoss": {"timeInForce": "GTC", "price": self.fmt(pos["symbol"], new_trigger)}}
        self._call("PUT", self._acct("/trades/%s/orders" % pos["trade_id"]), body=body)

    def broker_positions(self) -> List[Dict]:
        trades = self._call("GET", self._acct("/openTrades")).get("trades") or []
        net: Dict[str, int] = {}
        for t in trades:
            try:
                sym = str(t["instrument"]).replace("_", "")
                net[sym] = net.get(sym, 0) + int(float(t["currentUnits"]))
            except (KeyError, ValueError, TypeError):
                continue
        return [{"symbol": s, "qty": q} for s, q in net.items() if q != 0]

    def open_trade(self, symbol: str, side: str, signal_entry: float, sl: float, tp: float,
                   risk: RiskManager) -> Dict:
        side = side.upper()
        if side not in (BUY, SELL):
            raise BrokerError("Direction must be BUY or SELL")
        spec = self._spec(symbol)
        if not self.has_session():
            raise BrokerError("OANDA token or account id is missing. Open CONFIG.")
        if not fx_open():
            raise BrokerError("The forex market is closed (weekend). No order.")
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
        if signal_entry <= 0 or abs(ltp - signal_entry) / signal_entry > self.max_deviation:
            raise BrokerError("Signal price %s is more than %.2f%% away from the broker price %s. Skipped."
                              % (self.fmt(symbol, signal_entry), self.max_deviation * 100.0, self.fmt(symbol, ltp)))
        if side == BUY and not (sl < ltp < tp):
            raise BrokerError("Plan no longer valid at price %s (need SL < price < TP)." % self.fmt(symbol, ltp))
        if side == SELL and not (tp < ltp < sl):
            raise BrokerError("Plan no longer valid at price %s (need TP < price < SL)." % self.fmt(symbol, ltp))
        vpu, notional = self.unit_economics(symbol, ltp)
        units = risk.position_size(equity * self._scale(), ltp, sl, cash, self.max_units, vpu, notional)
        try:
            risk_amount = risk.validate_order(equity, units, ltp, sl, vpu)
        except RiskError as exc:
            raise BrokerError(str(exc))
        body = {"order": {"type": "MARKET", "instrument": spec.oanda, "units": str(units if side == BUY else -units),
                          "timeInForce": "FOK", "positionFill": "DEFAULT",
                          "stopLossOnFill": {"price": self.fmt(symbol, sl), "timeInForce": "GTC"},
                          "takeProfitOnFill": {"price": self.fmt(symbol, tp)}}}
        payload = self._call("POST", self._acct("/orders"), body=body)
        fill = payload.get("orderFillTransaction")
        if not fill:
            reason = (payload.get("orderCancelTransaction") or {}).get("reason", "no reason given")
            raise BrokerError("OANDA did not fill the order (%s). No position was opened." % reason)
        opened = fill.get("tradeOpened") or {}
        trade_id = str(opened.get("tradeID", ""))
        if not trade_id:
            raise BrokerError("OANDA filled the order but returned no trade id. CHECK YOUR OANDA APP NOW.")
        try:
            entry = float(fill["price"])
            filled = abs(int(float(opened.get("units", units))))
        except (KeyError, ValueError, TypeError):
            raise BrokerError("OANDA filled the order but the fill was unreadable. CHECK YOUR OANDA APP NOW.")
        pos = {"symbol": symbol, "side": side, "qty": filled, "entry": round(entry, spec.dp),
               "sl": self.rp(symbol, sl), "sl0": self.rp(symbol, sl), "tp": self.rp(symbol, tp),
               "risk": round(risk_amount, 2), "trade_id": trade_id, "sl_order_id": "",
               "opened_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with self._lock:
            self.state["seq"] += 1
            pos["id"] = "T%d" % self.state["seq"]
            self.state["positions"].append(pos)
            self._save_state()
        risk.note_trade_opened()
        try:                                                   # the stop must really be on the trade
            trade = self._call("GET", self._acct("/trades/%s" % trade_id)).get("trade") or {}
            if "stopLossOrder" not in trade:
                raise BrokerError("OANDA did not attach the stop-loss.")
        except BrokerError as exc:
            try:
                self._exit_now(pos, "NO STOP")
            except BrokerError as exc2:
                raise BrokerError("Stop-loss not confirmed (%s) AND closing failed (%s). CLOSE THE TRADE IN YOUR "
                                  "OANDA APP NOW." % (exc, exc2))
            raise BrokerError("Stop-loss not confirmed (%s). The trade was closed again." % exc)
        return dict(pos)

    def _exit_now(self, pos: Dict, reason: str) -> Dict:
        payload = self._call("PUT", self._acct("/trades/%s/close" % pos["trade_id"]), body={"units": "ALL"})
        fill = payload.get("orderFillTransaction")
        if not fill:
            raise BrokerError("OANDA did not confirm the close. CHECK YOUR OANDA APP.")
        try:
            price = float(fill["price"])
        except (KeyError, ValueError, TypeError):
            price = self.ltp(pos["symbol"])
        pl = fill.get("pl")
        return self._finalize(pos, price, reason, float(pl) if pl is not None else None)

    def check_exits(self) -> List[Dict]:
        """Stop and target live at OANDA. This reads the trades, books the ones that closed, moves the trailing stop
        and closes everything before the weekend. Never raises."""
        closed: List[Dict] = []
        for pos in self.positions():
            try:
                tr = self._call("GET", self._acct("/trades/%s" % pos["trade_id"])).get("trade") or {}
                if tr.get("state") == "CLOSED":
                    px = float(tr.get("averageClosePrice") or pos["sl"])
                    reason = "STOP LOSS" if abs(px - pos["sl"]) <= abs(px - pos["tp"]) else "TARGET"
                    pl = tr.get("realizedPL")
                    closed.append(self._finalize(pos, px, reason, float(pl) if pl is not None else None))
                    continue
                if self.eod_due():
                    closed.append(self._exit_now(pos, "WEEKEND CLOSE"))
                    continue
                if self.trailing:
                    self._trail(pos, self.ltp(pos["symbol"]))
                self.last_error = ""
            except BrokerError as exc:
                self.last_error = str(exc)[:140]
        return closed


def make_fx_brokers(store: CredentialStore, data_dir: str, price_fn=None, on_close=None) -> Tuple[OandaBroker, FxPaperBroker]:
    """(live broker, paper broker) for forex."""
    return OandaBroker(store, data_dir), FxPaperBroker(store, data_dir, price_fn=price_fn, on_close=on_close)


