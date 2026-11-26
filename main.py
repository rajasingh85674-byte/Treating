# -*- coding: utf-8 -*-
"""Tactical Neural Matrix (TDN) - live NSE and forex trading terminal for Android.

Two modes only: PAPER (virtual money on live prices, learns from every closed trade) and LIVE (real intraday
orders to your Zerodha Kite or Upstox account, after ARM, UNLOCK LIVE and a confirm popup). There is no demo or
simulated price feed: the screen shows live broker or Yahoo data, or says NO LIVE DATA and refuses to trade.
The app opens in PAPER mode, DISARMED, with live orders LOCKED. An optional PIN guards ARM, UNLOCK and the mode switch.
Screens (bottom bar): HUD (live chart, mic, order buttons), AGENTS (tap an agent: it speaks and logs what it
sees right now), DEPTH (market depth, ticks, positions), CONFIG (theme, feed, broker keys, voice).

More screens: SCAN (watchlist scanner split into green and red, momentum filter, stock biodata, chart-drawing
guide, mentor rules) and REPLY (AI message and telecalling reply writer; it only writes text).
Live orders start LOCKED every time the app opens. Unlock them with the LIVE button or by saying "enable real
trading"; every order still needs the CONFIRM popup, a defined stop-loss and passes the 1 percent risk rules.
Discipline for every screen and voice: trend and context, reward to risk, the invalidation level, and honest
uncertainty. Nothing here promises profit.

Two markets, one HUD: NSE shares (Kite or Upstox) and forex pairs (EURUSD, GBPUSD, USDJPY and more; OANDA for
live orders, Yahoo prices for paper). Each market has its own PAPER and LIVE book with its own risk counters, so a
loss on one never hides in the other. Spoken forex commands only REQUEST a trade: they pass the pair-match check,
the read-back, the 1 percent cap and the same CONFIRM popup as a tap, and never arm or unlock anything.

Files that must sit next to this one:
  agents.py  broker.py  risk_manager.py  voice_engine.py  voice_mic.py  ui_hud.py  mentor.py  reply_agent.py
  forex.py  voice_forex.py  market_flow.py  agent_brain.py
"""
import json
import os
import threading
import time
import traceback

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.screenmanager import NoTransition, Screen, ScreenManager
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget
from kivy.utils import platform

BOOT_ERROR = ""

# words that mark a status line as an error, and hints that tell which module it came from
ERROR_WORDS = ("error", "failed", "failure", "problem", "cannot", "could not", "couldn't", "refused", "denied",
               "timed out", "timeout", "unreachable", "no internet", "damaged", "exception", "traceback",
               "not placed", "no data feed", "did not catch")
MODULE_HINTS = (
    ("OPENAI", ("openai", "whisper")),
    ("MIC", ("mic", "record", "listen", "transcri", "did not catch", "permission")),
    ("VOICE", ("tts", "voice", "speech", "speak")),
    ("SCANNER", ("scan", "watchlist")),
    ("BROKER", ("broker", "kite", "zerodha", "upstox", "order", "position", "funds", "token", "login", "square")),
    ("MARKET FEED", ("yahoo", "feed", "dns", "network", "internet", "websocket", "rest:", "market data", "candle")),
    ("AGENTS", ("analysis", "agent", "consensus")),
    ("RISK", ("risk", "kill", "lock")),
    ("REPLY", ("reply",)),
    ("CONFIG", ("config", "save", "saved", "key", "credential")),
)


def looks_like_error(text):
    low = str(text).lower()
    return any(w in low for w in ERROR_WORDS)


def guess_module(text):
    low = str(text).lower()
    for name, words in MODULE_HINTS:
        if any(w in low for w in words):
            return name
    return "APP"
try:
    import ui_hud
    from ui_hud import (ANIM, P, THEME_NAMES, THEMES, AgentTile, AutoLabel, HoldButton, HudButton, HudChart,
                        HudFrame, HudNav, MicButton, PnlPanel, RISK_NOTE, StatusGrid, apply_theme, esc, heading,
                        get_currency, hex_of, inr, px, set_currency, set_price_format)
    from agents import (AGENT_NAMES, BUY, MIN_AGREE, SELL, ConsensusEngine, TrailingStop, copilot_report, fx,
                    set_price_dp)
    from broker import (AppLock, BrokerError, BrokerFeed, CredentialStore, IntegrityGuard, MarketStream, PaperBroker,
                        TickStream,
                        YahooFeed, clean_secret, clean_text, ist_now, make_broker, redact)
    from agent_brain import AgentBrain
    from market_flow import FlowCollector, is_flow_request, report as flow_report_text, symbol_context
    from forex import (FX_WATCHLIST, FxPaperBroker, NoTicks, OandaBroker, YahooFxFeed, fx_open, fx_slot, is_pair,
                       price_dp, session_text)
    from voice_forex import FxVoiceGate, parse_fx, readback
    from risk_manager import PaperLearner, RiskManager
    from voice_engine import (OPENAI_VOICES, PROFILES, ChatAI, ElevenLabsClient, OpenAIClient, OpenAIError,
                              VoiceEngine)
    from voice_mic import HELP_TEXT, VoiceMic, is_command, parse_command
    from mentor import (CHOP, DISCIPLINE, GOOD, QUALITY_TEXT, RULES, best_of, no_hype, biodata, chop_check, drawing_guide, scan_row,
                        split_green_red, spoken_biodata, tracker, trade_guidance)
    from reply_agent import LANGUAGES, MODES, TONES, ReplyAgent
except Exception:
    BOOT_ERROR = traceback.format_exc()

FEEDS = ("AUTO", "BROKER", "YAHOO")
TRADE_MODES = ("PAPER", "LIVE")
LIVE_SOURCES = ("KITE", "UPSTOX", "OANDA", "YAHOO")     # the only sources an order may be priced on
MARKETS = ("NSE", "FX")
MAX_DATA_AGE = 300.0                  # a last candle older than this is NOT live: NO LIVE DATA, no order
APP_VERSION = "5.7.0"
WATCHLIST = ["SBIN", "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "ITC", "LT", "AXISBANK", "KOTAKBANK"]
REFERENCE_CAPITAL = 100000.0          # only for the size preview while no broker is connected (NSE, rupees)
FX_REFERENCE_CAPITAL = 10000.0        # the same for forex (US dollars)
SCREENS = [("hud", "HUD"), ("scan", "SCAN"), ("agents", "AGENTS"), ("reply", "REPLY"), ("depth", "DEPTH"),
           ("config", "CONFIG")]


def nse_open():
    """NSE cash market: Mon-Fri 09:15-15:30 India time (exchange holidays are not checked)."""
    now = ist_now()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= mins <= 15 * 60 + 30


def safe_float(value, default=0.0):
    """float() that never raises: masked text such as '***', empty, None, NaN and infinity give the default."""
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return v if v == v and v not in (float("inf"), float("-inf")) else default


def session_is_open(market):
    return fx_open() if market == "FX" else nse_open()


def money_for(symbol, value):
    """Money text in the currency of the symbol's market (a closed forex trade is shown in dollars whatever is on screen)."""
    prev = get_currency()
    set_currency("USD" if is_pair(symbol) else "INR")
    try:
        return inr(value)
    finally:
        set_currency(prev)


def open_url(url):
    try:
        if platform == "android":
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            activity.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        else:
            import webbrowser
            webbrowser.open(url)
        return True
    except Exception:
        return False


def scroll_column(spacing=12):
    """(ScrollView, content BoxLayout whose height follows its children)."""
    sv = ScrollView(do_scroll_x=False, bar_width=dp(3))
    col = BoxLayout(orientation="vertical", spacing=dp(spacing), size_hint_y=None, padding=(0, dp(4)))
    col.bind(minimum_height=col.setter("height"))
    sv.add_widget(col)
    return sv, col


class TDNApp(App):
    title = "Tactical Neural Matrix"

    # ------------------------------------------------------------------ #
    # startup
    # ------------------------------------------------------------------ #
    def build(self):
        Window.clearcolor = P.get("bg", (0.004, 0.004, 0.012, 1))
        try:
            Window.softinput_mode = "below_target"
        except Exception:
            pass
        try:
            return self._build_inner()
        except Exception:
            return self._error_screen(traceback.format_exc())

    def _error_screen(self, text):
        root = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        root.add_widget(AutoLabel(text="[b][color=ff4048]TDN - STARTUP ERROR[/color][/b]"))
        sv = ScrollView(do_scroll_x=False)
        sv.add_widget(AutoLabel(text=esc(redact(text)), font_size=sp(11)))
        root.add_widget(sv)
        return root

    def _build_inner(self):
        if BOOT_ERROR:
            return self._error_screen(BOOT_ERROR)
        self.data_dir = self.user_data_dir if platform == "android" else os.path.join(os.path.expanduser("~"), ".tdn")
        os.makedirs(self.data_dir, exist_ok=True)
        self.ui_path = os.path.join(self.data_dir, "ui.json")
        self.ui = {"theme": "TIGER HUD", "feed": "AUTO", "symbol": WATCHLIST[0], "anim": True, "speak_errors": False,
                   "market": "NSE", "fx_symbol": FX_WATCHLIST[0]}
        try:
            with open(self.ui_path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            for k in self.ui:
                if k in saved:
                    self.ui[k] = saved[k]
        except Exception:
            pass
        if self.ui["theme"] not in THEMES:
            self.ui["theme"] = "TIGER HUD"
        if self.ui["symbol"] not in WATCHLIST:
            self.ui["symbol"] = WATCHLIST[0]
        if self.ui["fx_symbol"] not in FX_WATCHLIST:
            self.ui["fx_symbol"] = FX_WATCHLIST[0]
        if self.ui["market"] not in MARKETS:
            self.ui["market"] = "NSE"
        if self.ui["feed"] not in FEEDS:             # an old settings file may still say DEMO: there is no demo feed any more
            self.ui["feed"] = "AUTO"
        apply_theme(self.ui["theme"])
        Window.clearcolor = P["bg"]
        ANIM.enabled = bool(self.ui["anim"])

        self.market = self.ui["market"]
        self.symbol = self.ui["fx_symbol"] if self.market == "FX" else self.ui["symbol"]
        self.decision = None
        self.candles = []
        self.price = 0.0
        self.source = "NONE"
        self.note = ""
        self.funds = None
        self.busy = {"eval": False, "exits": False, "funds": False, "order": False, "panic": False, "chat": False,
                     "reply": False, "scan": False}
        self.live_unlocked = False               # real orders are locked at every start until the user unlocks them
        self.mode = "PAPER"                      # PAPER or LIVE; the app always opens in PAPER
        self.app_locked = False                  # True while the PIN screen is up
        self.lock_popup = None
        self.paused_at = 0.0
        self.tamper = []                         # integrity problems: trading stays off while this is not empty
        self.guard_warning = ""
        self.cfg_mode_btn = None
        self.voice_trade = None                  # a spoken trade request waiting for the fresh scan
        self.fxgate = FxVoiceGate()              # time guards for spoken forex trade requests
        self.scan_rows = {}                      # symbol -> momentum row from the watchlist scanner
        self.scan_time = 0.0
        self.scan_candles = {}
        self.scan_first_error = ""
        self.scan_view = "green"                 # SCAN shows the green (rising) view or the red (falling) view, never both
        self.sv = {}
        self.errors = []                         # newest last: (clock text, module, detail)
        self.last_err = ("", 0.0)
        self.last_note = ""
        self.err_btn = None
        self.scan_label = "?"
        self.live_until = 0.0
        self.scan_pick = ""
        self.last_guide = 0.0
        self.reply_text = ""
        self.chat_lines = []
        self.config_refresh = None
        self.last_eval = self.last_exits = self.last_funds = 0.0
        self.last_key = None
        self.last_signal_voice = 0.0
        self.last_trade_text = ""
        self.selected_agent = None
        self.agent_log_lines = []
        self.copilot_on = False
        self.popup = None
        self.screen_name = "hud"
        self.mic_text = "HOLD THE MIC AND SPEAK"

        self.store = CredentialStore(os.path.join(self.data_dir, "credentials.json"))
        self.applock = AppLock(os.path.join(self.data_dir, "applock.json"))
        self.app_locked = self.applock.enabled
        self.guard = IntegrityGuard(self.data_dir, os.path.dirname(os.path.abspath(__file__)),
                                    "%s|%s" % (APP_VERSION, self._install_stamp()))
        try:
            if not self.guard.run():
                self.tamper = list(self.guard.problems)
        except Exception as exc:                 # a broken check must be visible, but it is not proof of tampering
            self.guard_warning = "Integrity check could not run: %s" % redact(str(exc))[:80]
        # four separate books, each with its own risk counters: NSE and forex, PAPER and LIVE
        self.risks = {("NSE", "LIVE"): RiskManager(os.path.join(self.data_dir, "risk_data.json")),
                      ("NSE", "PAPER"): RiskManager(os.path.join(self.data_dir, "risk_paper.json")),
                      ("FX", "LIVE"): RiskManager(os.path.join(self.data_dir, "risk_fx_live.json")),
                      ("FX", "PAPER"): RiskManager(os.path.join(self.data_dir, "risk_fx_paper.json"))}
        for r in self.risks.values():             # every start is DISARMED: tap ARM to trade
            if self.tamper:
                r.kill("integrity check failed")
            elif not r.is_killed():
                r.kill("app start: disarmed until you tap ARM")
        self.learner_nse = PaperLearner(os.path.join(self.data_dir, "paper_learn.json"))
        self.learner_fx = PaperLearner(os.path.join(self.data_dir, "paper_learn_fx.json"))
        self.brain = AgentBrain(self.data_dir)
        self.nse_live = self._make_broker(self.store.get("broker") or "UPSTOX")
        self.nse_paper = PaperBroker(self.store, self.data_dir, price_fn=self._paper_price,
                                     on_close=self._closed_hook("NSE", self.learner_nse))
        self.fx_live = OandaBroker(self.store, self.data_dir)
        self.fx_paper = FxPaperBroker(self.store, self.data_dir, price_fn=self._paper_price,
                                      on_close=self._closed_hook("FX", self.learner_fx))
        self.mode = "PAPER"
        if self.nse_live.positions():            # never hide a real open position behind PAPER mode
            self.market, self.mode = "NSE", "LIVE"
            self.symbol = self.ui["symbol"]
        elif self.fx_live.positions():
            self.market, self.mode = "FX", "LIVE"
            self.symbol = self.ui["fx_symbol"]
        self.engine = ConsensusEngine(self.risks[(self.market, self.mode)])
        self.ticks = TickStream(NoTicks())
        self._select_books()
        # the OpenAI key lives only in the app's private credentials file, never in the source code
        self.openai = OpenAIClient(lambda: self.store.get("openai_key"))
        self.eleven = ElevenLabsClient(lambda: self.store.get("eleven_key"), lambda: self.store.get("eleven_voice"))
        self.voice = VoiceEngine(self.data_dir, self.status, self.openai, self.eleven, self.store.get)
        self.mic = VoiceMic(on_state=self._mic_state_cb, on_level=self._mic_level_cb,
                            on_result=self._mic_result_cb, on_error=self._mic_error_cb,
                            openai=self.openai, data_dir=self.data_dir)
        self.mic.prefer = self.store.get("mic_engine") or "AUTO"
        self.chat = ChatAI(self.openai)
        self.reply_agent = ReplyAgent(self.openai)
        self.ticks.set_symbol(self.symbol)
        self.ticks.start()
        self.collector = FlowCollector(self.brain, self._scan_feed, lambda: list(self.watchlist), lambda: self.market,
                                       self.session_open, scrub=redact)
        self.collector.start()
        self.stream = MarketStream(self.symbol)
        self.stream.attach_ticks(self.ticks)
        self.stream.start()

        self.shell = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))
        self._build_ui()
        ANIM.start()
        threading.Thread(target=self._feed_worker, daemon=True).start()
        Clock.schedule_interval(self._tick, 1.0)
        self._install_error_hooks()
        Clock.schedule_once(lambda dt: self._first_run_checks(), 3.0)
        Clock.schedule_once(lambda dt: self._startup_security(), 0.6)
        Clock.schedule_once(lambda dt: self.voice.say(
            "Tactical Neural Matrix online. %s market, %s mode. Trading is disarmed until you arm it. Capital first." % (
                "Forex" if self.market == "FX" else "NSE", self.mode.title())), 2.0)
        Clock.schedule_once(lambda dt: self._voice_notice(), 9.0)
        return self.shell

    def _closed_hook(self, market, learner):
        """Called by a paper book for every closed trade: the learner learns, the brain keeps the trade with the
        market flow it was taken in."""
        def hook(trade):
            learner.record(trade)
            try:
                ctx = dict(trade.get("ctx") or {}) if isinstance(trade, dict) else {}
                self.brain.record_trade(market, trade, ctx)
            except Exception:
                pass
        return hook

    def _voice_notice(self):
        """The AI voice (your saved OpenAI or ElevenLabs key) is the main voice. Say plainly when only the basic phone
        voice is available."""
        note = self.voice.fallback_notice()
        if note:
            self.status(note)
            self.voice.say(note)

    def _select_books(self):
        """Points live_broker / paper_broker / risk_* / learner / broker / risk at the active market and refreshes the
        currency, price decimals, tick source and position sizer."""
        if self.market == "FX":
            self.live_broker, self.paper_broker = self.fx_live, self.fx_paper
            self.risk_live, self.risk_paper = self.risks[("FX", "LIVE")], self.risks[("FX", "PAPER")]
            self.learner = self.learner_fx
            set_currency("USD")
        else:
            self.live_broker, self.paper_broker = self.nse_live, self.nse_paper
            self.risk_live, self.risk_paper = self.risks[("NSE", "LIVE")], self.risks[("NSE", "PAPER")]
            self.learner = self.learner_nse
            set_currency("INR")
        self.broker = self.live_broker if self.mode == "LIVE" else self.paper_broker
        self.risk = self.risk_live if self.mode == "LIVE" else self.risk_paper
        self.engine.risk = self.risk
        self.engine.sizer = self._sizer
        self.engine.flow_provider = self._flow_for
        self._apply_symbol_format(self.symbol)
        if hasattr(self, "ticks"):
            self.ticks.set_broker(NoTicks() if self.market == "FX" else self.nse_live)

    def _flow_snapshot(self):
        try:
            return self.collector.latest(self.market)
        except Exception:
            return None

    def _flow_context(self, symbol, direction):
        """Market-flow context for one symbol and direction. Without live collected data it says unknown and
        changes nothing (the flow can only make the app stricter, never approve)."""
        try:
            return self.collector.context_for(self.market, symbol, direction)
        except Exception:
            return symbol_context(None, symbol, direction)

    def _flow_for(self, symbol):
        """Market-flow context of a symbol for every agent (read-only, from live collected data)."""
        return self._flow_context(symbol, "")

    def _sizer(self, symbol, price):
        """(value per unit, unit value, max units) of the active market, for the agents' size preview."""
        vpu, notional = self.broker.unit_economics(symbol, price)
        return vpu, notional, self.broker.max_units

    def _apply_symbol_format(self, symbol):
        dp_ = price_dp(symbol) if is_pair(symbol) else 2
        set_price_dp(dp_)
        set_price_format(dp_)

    def _all_risks(self):
        return list(self.risks.values())

    @property
    def watchlist(self):
        return FX_WATCHLIST if self.market == "FX" else WATCHLIST

    def session_open(self):
        return session_is_open(self.market)

    def session_text(self):
        if self.market == "FX":
            return "forex " + session_text()
        return "NSE " + ("open" if nse_open() else "closed")

    def set_market(self, name, confirmed=False):
        name = "FX" if str(name).upper() in ("FX", "FOREX") else "NSE"
        if name == self.market:
            return self.status("Already on the %s market." % ("forex" if name == "FX" else "NSE"))
        if self.app_locked:
            return self.alert("The app is locked. Enter your PIN first.", True, True)
        if self.paper_broker.positions() or self.live_broker.positions():
            return self.alert("Close the open position before changing market.", True, True)
        self.market = name
        self.mode = "PAPER"                      # a market switch always lands in PAPER, LIVE is a separate choice
        self.live_unlocked = False
        self.voice_trade = None
        self.fxgate.clear()
        self.decision = None
        self.funds = None
        self.last_funds = 0.0
        self.candles, self.price, self.source = [], 0.0, "NONE"
        self.symbol = self.ui["fx_symbol"] if name == "FX" else self.ui["symbol"]
        self.ui["market"] = name
        self._save_ui()
        self._select_books()
        self.ticks.set_symbol(self.symbol)
        self.reload_feed()
        threading.Thread(target=self._switch_worker, args=(self.symbol,), daemon=True).start()
        self._build_ui()
        self.alert("%s market. PAPER mode, live orders locked." % ("Forex" if name == "FX" else "NSE"), True, True)

    def toggle_market(self):
        self.set_market("NSE" if self.market == "FX" else "FX")

    def _make_broker(self, name):
        try:
            return make_broker(name, self.store, self.data_dir)
        except BrokerError:
            return make_broker("UPSTOX", self.store, self.data_dir)

    def _save_ui(self):
        try:
            with open(self.ui_path, "w", encoding="utf-8") as fh:
                json.dump(self.ui, fh)
        except OSError as exc:
            self.report_error("CONFIG", "Settings could not be saved: %s" % exc)

    # ------------------------------------------------------------------ #
    # data feed
    # ------------------------------------------------------------------ #
    def _feed_worker(self):
        try:
            mode = self.ui["feed"]
            chain = []
            if mode in ("AUTO", "BROKER") and self.live_broker.has_session():
                chain.append(BrokerFeed(self.live_broker))
            if mode in ("AUTO", "YAHOO"):
                chain.append(YahooFxFeed() if self.market == "FX" else YahooFeed())
            self.stream.symbol = self.symbol
            self.stream.configure(chain)
        except Exception as exc:
            self.report_exception("MARKET FEED", exc)

    def reload_feed(self):
        threading.Thread(target=self._feed_worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        self.shell.clear_widgets()
        self.shell.add_widget(self._header())
        self.sm = ScreenManager(transition=NoTransition())
        for key, builder in (("hud", self._screen_hud), ("scan", self._screen_scan), ("agents", self._screen_agents),
                             ("reply", self._screen_reply), ("depth", self._screen_depth),
                             ("config", self._screen_config)):
            scr = Screen(name=key)
            scr.add_widget(builder())
            self.sm.add_widget(scr)
        if self.screen_name not in dict(SCREENS):
            self.screen_name = "hud"
        self.sm.current = self.screen_name
        self.shell.add_widget(self.sm)
        self.err_btn = HudButton(text="", accent=P["danger"], filled=True, height=44)
        self.err_btn.font_size = sp(11)
        self.err_btn.bind(on_release=lambda inst: self.show_error_log())
        self.shell.add_widget(self.err_btn)
        self._refresh_err_bar()
        self.nav = HudNav(SCREENS, self.goto, self.screen_name)
        self.shell.add_widget(self.nav)
        self.lbl_status = AutoLabel(text="Ready.", font_size=sp(11), color=P["muted"])
        self.shell.add_widget(self.lbl_status)
        self._refresh_all()

    def goto(self, key):
        self.screen_name = key
        self.sm.current = key
        self.nav.select(key)
        if key == "config" and self.config_refresh is not None:
            try:
                self.config_refresh()
            except Exception as exc:
                self.status("Config refresh: %s" % str(exc)[:60])

    def _header(self):
        box = HudFrame(accent=P["a2"], scan=True, padding=(dp(12), dp(8)), spacing=dp(2))
        box.add_widget(AutoLabel(text="[b][color=%s]TACTICAL NEURAL MATRIX[/color][/b]  [color=%s]TDN | %s LIVE DATA[/color]"
                                 % (P["hx_a2"], P["hx_a1"], "FOREX" if self.market == "FX" else "NSE"), font_size=sp(14)))
        self.grid = StatusGrid(["FEED", "PRICE", "MARKET", "SYSTEM", "MODE", "BROKER", "TICKS", "GUARD", "LOCK"], cols=3)
        box.add_widget(self.grid)
        return box

    # ---- HUD screen ----
    def _screen_hud(self):
        sv, col = scroll_column()
        chart_box = HudFrame(accent=P["a1"], scan=True, padding=dp(8))
        chart_box.add_widget(heading("LIVE 1-MINUTE CHART  |  %s" % ("FOREX" if self.market == "FX" else "NSE")))
        self.chart = HudChart(height=250)
        chart_box.add_widget(self.chart)
        self.lbl_chart = AutoLabel(text="", font_size=sp(12), color=P["muted"])
        chart_box.add_widget(self.lbl_chart)
        col.add_widget(chart_box)

        # microphone, centred on the HUD
        mic_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(176))
        self.mic_btn = MicButton(on_press=self._mic_press, on_release=self._mic_release)
        mic_row.add_widget(Widget())
        mic_row.add_widget(self.mic_btn)
        mic_row.add_widget(Widget())
        col.add_widget(mic_row)
        self.lbl_mic = AutoLabel(text=self.mic_text, font_size=sp(12), color=P["muted"], halign="center")
        col.add_widget(self.lbl_mic)
        cmd_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(44), spacing=dp(6))
        self.cmd_input = TextInput(hint_text="type a command or ask me anything", multiline=False, font_size=sp(13),
                                   background_color=P["panel"], foreground_color=P["text"], hint_text_color=P["muted"],
                                   cursor_color=P["a1"], padding=(dp(8), dp(12)))
        self.cmd_input.bind(on_text_validate=lambda inst: self._send_typed())
        cmd_row.add_widget(self.cmd_input)
        send = HudButton(text="SEND", accent=P["a1"], height=44, size_hint_x=None, width=dp(80))
        send.bind(on_release=lambda inst: self._send_typed())
        cmd_row.add_widget(send)
        col.add_widget(cmd_row)

        chat_box = HudFrame(accent=P["a1"])
        chat_box.add_widget(heading("AI CHAT"))
        self.lbl_chat = AutoLabel(text="", font_size=sp(12))
        chat_box.add_widget(self.lbl_chat)
        col.add_widget(chat_box)
        self._render_chat()

        self.pnl = PnlPanel()
        col.add_widget(self.pnl)

        sig = HudFrame(accent=P["a2"])
        sig.add_widget(heading("LIVE SIGNAL", P["a2"]))
        self.lbl_signal = AutoLabel(text="Waiting for data...", font_size=sp(18))
        self.lbl_levels = AutoLabel(text="", font_size=sp(13))
        self.lbl_reason = AutoLabel(text="", font_size=sp(12), color=P["muted"])
        self.lbl_account = AutoLabel(text="", font_size=sp(12), color=P["muted"])
        self.lbl_position = AutoLabel(text="", font_size=sp(12))
        self.lbl_guidance = AutoLabel(text="", font_size=sp(13))
        for w in (self.lbl_signal, self.lbl_levels, self.lbl_reason, self.lbl_guidance, self.lbl_account,
                  self.lbl_position):
            sig.add_widget(w)
        col.add_widget(sig)

        trk = HudFrame(accent=P["a1"])
        trk.add_widget(heading("LIVE TRADE TRACKER  (4 points)"))
        self.lbl_tracker = AutoLabel(text="", font_size=sp(12))
        trk.add_widget(self.lbl_tracker)
        col.add_widget(trk)
        col.add_widget(AutoLabel(text=esc(RISK_NOTE), font_size=sp(11), color=P["muted"]))

        # realistic terminal buttons
        from kivy.uix.gridlayout import GridLayout
        grid = GridLayout(cols=2, spacing=dp(8), size_hint_y=None, row_default_height=dp(50), row_force_default=True)
        grid.bind(minimum_height=grid.setter("height"))
        self.btn_arm = HudButton(text="DISARM", accent=P["ok"], height=50)
        self.btn_arm.bind(on_release=lambda inst: self.toggle_arm())
        self.btn_trail = HudButton(text="TRAIL SL LOCK: ON", accent=P["a1"], height=50)
        self.btn_trail.bind(on_release=lambda inst: self.toggle_trailing())
        self.btn_buy = HudButton(text="BUY", accent=P["up"], filled=True, height=50)
        self.btn_buy.bind(on_release=lambda inst: self.order_request(BUY))
        self.btn_sell = HudButton(text="SELL", accent=P["down"], filled=True, height=50)
        self.btn_sell.bind(on_release=lambda inst: self.order_request(SELL))
        self.btn_copilot = HudButton(text="AI CO-PILOT HUD", accent=P["a2"], height=50)
        self.btn_copilot.bind(on_release=lambda inst: self.toggle_copilot())
        self.btn_scan = HudButton(text="SCAN NOW", accent=P["a1"], height=50)
        self.btn_scan.bind(on_release=lambda inst: self.scan_now())
        self.btn_live = HudButton(text="LIVE: LOCKED", accent=P["muted"], height=50)
        self.btn_live.bind(on_release=lambda inst: self.toggle_live())
        self.btn_scanall = HudButton(text="SCAN WATCHLIST", accent=P["a2"], height=50)
        self.btn_scanall.bind(on_release=lambda inst: self.scan_watchlist(True))
        self.btn_mode = HudButton(text="MODE: PAPER", accent=P["a1"], height=50)
        self.btn_mode.bind(on_release=lambda inst: self.toggle_mode())
        self.btn_paper = HudButton(text="PAPER REPORT", accent=P["a2"], height=50)
        self.btn_paper.bind(on_release=lambda inst: self.paper_report())
        self.btn_market = HudButton(text="MARKET: %s" % self.market, accent=P["a2"], height=50)
        self.btn_market.bind(on_release=lambda inst: self.toggle_market())
        self.btn_next = HudButton(text="NEXT SYMBOL", accent=P["a1"], height=50)
        self.btn_next.bind(on_release=lambda inst: self.next_symbol())
        self.btn_flow = HudButton(text="MARKET FLOW", accent=P["a2"], height=50)
        self.btn_flow.bind(on_release=lambda inst: self.flow_report())
        for b in (self.btn_market, self.btn_mode, self.btn_live, self.btn_arm, self.btn_buy, self.btn_sell, self.btn_trail,
                  self.btn_copilot, self.btn_scan, self.btn_scanall, self.btn_paper, self.btn_next, self.btn_flow):
            grid.add_widget(b)
        col.add_widget(grid)

        self.copilot_box = HudFrame(accent=P["a2"], scan=True)
        self.copilot_box.add_widget(heading("AI CO-PILOT HUD", P["a2"]))
        self.lbl_copilot = AutoLabel(text="", font_size=sp(12))
        self.copilot_box.add_widget(self.lbl_copilot)
        self._copilot_parent = col

        self.btn_panic = HoldButton(text="PANIC SQUARE-OFF  (HOLD 1 SECOND)", accent=P["danger"], filled=True,
                                    height=60, on_hold=self.panic)
        col.add_widget(self.btn_panic)
        return sv

    # ---- AGENTS screen ----
    def _screen_agents(self):
        sv, col = scroll_column(spacing=8)
        log = HudFrame(accent=P["a2"], scan=True)
        log.add_widget(heading("LIVE AGENT INSPECTOR  (tap any agent)", P["a2"]))
        self.lbl_agent_log = AutoLabel(text="Tap an agent below. It will speak and show exactly what it is "
                                            "analysing in the live feed right now.", font_size=sp(12))
        log.add_widget(self.lbl_agent_log)
        col.add_widget(log)
        self.lbl_tally = AutoLabel(text="", font_size=sp(13))
        col.add_widget(self.lbl_tally)
        self.tiles = {}
        for name in AGENT_NAMES:
            tile = AgentTile(name)
            tile.bind(on_release=lambda inst, n=name: self.agent_touched(n))
            self.tiles[name] = tile
            col.add_widget(tile)
        return sv

    # ---- DEPTH screen ----
    def _screen_depth(self):
        sv, col = scroll_column()
        box = HudFrame(accent=P["a1"], scan=True)
        box.add_widget(heading("MARKET DEPTH  (top 5)"))
        self.lbl_depth = AutoLabel(text="Waiting for the live feed...", font_size=sp(13))
        box.add_widget(self.lbl_depth)
        col.add_widget(box)
        pos = HudFrame(accent=P["a2"])
        pos.add_widget(heading("POSITIONS AND TRADES", P["a2"]))
        self.lbl_positions = AutoLabel(text="", font_size=sp(12))
        pos.add_widget(self.lbl_positions)
        col.add_widget(pos)
        risk = HudFrame(accent=P["danger"])
        risk.add_widget(heading("RISK CONTROL", P["danger"]))
        self.lbl_risk = AutoLabel(text="", font_size=sp(12))
        risk.add_widget(self.lbl_risk)
        col.add_widget(risk)
        return sv

    # ---- CONFIG screen ----
    def _screen_config(self):
        sv, body = scroll_column(spacing=8)
        store = self.store

        def title(t):
            body.add_widget(heading(t))

        def field(hint, value="", password=False):
            ti = TextInput(text=value, hint_text=hint, multiline=False, password=password, font_size=sp(13),
                           size_hint_y=None, height=dp(44), background_color=P["panel"], foreground_color=P["text"],
                           hint_text_color=P["muted"], cursor_color=P["a1"], padding=(dp(8), dp(12)))
            body.add_widget(ti)
            return ti

        def spinner(values, current, cb):
            s = Spinner(text=current, values=tuple(values), size_hint_y=None, height=dp(44), font_size=sp(13),
                        background_normal="", background_color=P["a1"], color=(0.01, 0.01, 0.02, 1))
            s.bind(text=lambda inst, val: cb(val))
            body.add_widget(s)
            return s

        title("THEME (applies instantly)")
        spinner(THEME_NAMES, self.ui["theme"], self._set_theme)
        anim_btn = HudButton(text="ANIMATIONS: %s" % ("ON" if self.ui["anim"] else "OFF"), accent=P["a2"], height=44)
        anim_btn.bind(on_release=lambda inst: self._toggle_anim(anim_btn))
        body.add_widget(anim_btn)
        title("MARKET DATA FEED")
        spinner(FEEDS, self.ui["feed"], self._set_feed)
        body.add_widget(AutoLabel(text="AUTO uses your broker feed first, then Yahoo Finance. Yahoo is public and may lag. "
                                       "If no live feed works the screen says NO LIVE DATA and orders are refused. "
                                       "Nothing is ever simulated.", font_size=sp(12), color=P["muted"]))

        title("TRADING MODE: PAPER or LIVE")
        body.add_widget(AutoLabel(text="PAPER = virtual money on live prices. It tracks P&L and learns from each closed "
                                       "trade (it can only make the filters stricter). LIVE = real orders, only after "
                                       "ARM, UNLOCK LIVE and the CONFIRM popup. The app always opens in PAPER mode. "
                                       "Changing mode needs no open position.", font_size=sp(12), color=P["muted"]))
        self.cfg_mode_btn = HudButton(text="MODE: %s  (TAP TO SWITCH)" % self.mode, accent=P["a2"], height=46)
        self.cfg_mode_btn.bind(on_release=lambda inst: self.toggle_mode())
        body.add_widget(self.cfg_mode_btn)
        f_cap = field("Paper starting capital in %s" % ("US dollars" if self.market == "FX" else "rupees"),
                      "%d" % int(safe_float(self.paper_broker.capital)))
        f_cap.input_filter = "int"
        lbl_paper = AutoLabel(text="", font_size=sp(12), color=P["muted"])

        def set_cap():
            try:
                msg = self.paper_broker.set_capital(f_cap.text)
                self.last_funds = 0.0
            except BrokerError as exc:
                msg = str(exc)
            lbl_paper.text = esc(msg)

        def reset_paper():
            try:
                msg = self.paper_broker.reset_account()
                self.last_funds = 0.0
            except BrokerError as exc:
                msg = str(exc)
            lbl_paper.text = esc(msg)

        def reset_learning():
            self.learner.reset()
            lbl_paper.text = "Paper learning of the %s market cleared. Filters are back to the base rules." % self.market
        for text, accent, fn in (("SET PAPER CAPITAL", P["a1"], set_cap), ("RESET PAPER ACCOUNT", P["danger"], reset_paper),
                                 ("RESET PAPER LEARNING", P["danger"], reset_learning)):
            b = HudButton(text=text, accent=accent, height=46)
            b.bind(on_release=lambda inst, f=fn: f())
            body.add_widget(b)
        body.add_widget(lbl_paper)

        title("APP LOCK (PIN) AND INTEGRITY")
        body.add_widget(AutoLabel(text="Optional PIN of 4 to 8 digits. It guards the app screen, ARM, UNLOCK LIVE, the "
                                       "switch to LIVE and key removal. It is stored only as a salted hash and is never "
                                       "accepted by voice. If you forget it, clear the app data (this also erases saved keys).",
                                  font_size=sp(12), color=P["muted"]))
        lbl_sec = AutoLabel(text="", font_size=sp(12), color=P["muted"])
        f_old = field("Current PIN (only when a PIN is set)", password=True)
        f_old.input_filter = "int"
        f_new = field("New PIN (4 to 8 digits)", password=True)
        f_new.input_filter = "int"

        def sec_text(extra=""):
            guard = "OK (%s)" % self.guard.note if not self.tamper else "FAILED: " + "; ".join(self.tamper)[:160]
            return esc("App lock: %s. Integrity: %s. %s%s" % (
                "PIN ON" if self.applock.enabled else "PIN OFF", guard, self.guard_warning, (" " + extra) if extra else ""))
        lbl_sec.text = sec_text()

        def do_set_pin():
            ok, msg = self.applock.set_pin(f_new.text, f_old.text)
            f_old.text = f_new.text = ""
            lbl_sec.text = sec_text(msg)

        def do_remove_pin():
            ok, msg = self.applock.remove_pin(f_old.text)
            f_old.text = f_new.text = ""
            lbl_sec.text = sec_text(msg)

        def do_lock_now():
            if not self.applock.enabled:
                lbl_sec.text = sec_text("Set a PIN first.")
                return
            self.app_locked = True
            self._show_lock_screen()
        for text, accent, fn in (("SET / CHANGE PIN", P["a1"], do_set_pin), ("REMOVE PIN", P["danger"], do_remove_pin),
                                 ("LOCK APP NOW", P["a2"], do_lock_now)):
            b = HudButton(text=text, accent=accent, height=46)
            b.bind(on_release=lambda inst, f=fn: f())
            body.add_widget(b)
        body.add_widget(lbl_sec)

        title("NSE BROKER (REAL ORDERS)")
        body.add_widget(AutoLabel(text="Choose your broker, save the keys, open the login page, then paste the address "
                                       "(or just the code / request token) from the browser after you log in. Tokens last "
                                       "one day.", font_size=sp(12), color=P["muted"]))
        broker_spin = spinner(("UPSTOX", "KITE"), "KITE" if self.nse_live.name == "KITE" else "UPSTOX",
                              lambda v: switch_broker(v))
        f_key = field("API key", store.get("kite_key") if self.nse_live.name == "KITE" else store.get("upstox_key"))
        f_secret = field("API secret", store.get("kite_secret") if self.nse_live.name == "KITE" else store.get("upstox_secret"),
                         password=True)
        f_redirect = field("Upstox redirect URL (must match your Upstox app)", store.get("upstox_redirect"))
        lbl_login = AutoLabel(text="", font_size=sp(12), color=P["muted"])
        f_paste = field("Paste redirect address or token here")

        def switch_broker(name):
            if name == "KITE":
                f_key.text, f_secret.text = store.get("kite_key"), store.get("kite_secret")
            else:
                f_key.text, f_secret.text = store.get("upstox_key"), store.get("upstox_secret")

        def save_keys():
            name = broker_spin.text
            fields = {"broker": name}
            key_text, secret_text = clean_secret(f_key.text), clean_secret(f_secret.text)
            prefix = "kite" if name == "KITE" else "upstox"
            if key_text:
                fields[prefix + "_key"] = key_text
            if secret_text:                     # an empty box never erases a saved secret
                fields[prefix + "_secret"] = secret_text
            if name != "KITE":
                fields["upstox_redirect"] = f_redirect.text
            if not store.update(**fields):
                lbl_login.text = esc(store.last_error or "Could not save the keys.")
                self.report_error("CONFIG", "Broker keys not saved: %s" % (store.last_error or "unknown reason"))
                return False
            self.nse_live = self._make_broker(name)
            if self.market == "NSE":
                self._select_books()
                self.ticks.set_symbol(self.symbol)
            lbl_login.text = "Saved. NSE broker is now %s." % name
            self.reload_feed()
            return True

        def show_login():
            if not save_keys():
                return
            try:
                url = self.nse_live.login_url()
            except BrokerError as exc:
                lbl_login.text = esc(redact(exc))
                return
            ok = open_url(url)
            lbl_login.text = ("Browser opened. Log in, then copy the address of the page you land on and paste it below.\n"
                              if ok else "Open this address in your browser:\n") + esc(url)

        def connect():
            if not save_keys():
                return
            text = clean_secret(f_paste.text, 1200)
            if not text:
                lbl_login.text = "Paste the address or token first."
                return
            lbl_login.text = "Connecting..."

            def work():
                try:
                    who = self.nse_live.login(text)
                    Clock.schedule_once(lambda dt: setattr(lbl_login, "text", "Connected: %s" % esc(who)), 0)
                    self.last_funds = 0.0
                    self.reload_feed()
                except BrokerError as exc:
                    msg = esc(redact(exc))
                    Clock.schedule_once(lambda dt: setattr(lbl_login, "text", msg), 0)
            threading.Thread(target=work, daemon=True).start()

        def disconnect():
            self.nse_live.clear_session()
            if self.market == "NSE":
                self.funds = None
            lbl_login.text = "Logged out."
            self.reload_feed()

        for text, accent, fn in (("SAVE KEYS", P["a1"], save_keys), ("1. OPEN LOGIN PAGE", P["a2"], show_login),
                                 ("2. CONNECT", P["a1"], connect), ("LOG OUT", P["danger"], disconnect)):
            b = HudButton(text=text, accent=accent, height=46)
            b.bind(on_release=lambda inst, f=fn: f())
            body.add_widget(b)
        body.add_widget(lbl_login)


        title("FOREX BROKER (OANDA)")
        body.add_widget(AutoLabel(text="Forex PAPER trading needs no broker. For LIVE forex, create a personal access token "
                                       "and note the account id in your OANDA account (API access). The app starts on "
                                       "PRACTICE (OANDA's own practice money); choose LIVE only when you mean real money. "
                                       "Only USD accounts are supported. The token stays on this phone, is never shown "
                                       "again and is hidden in every message. Note: OANDA does not accept clients from "
                                       "India, and other countries have their own rules. Check yours before trading.",
                                  font_size=sp(12), color=P["muted"]))
        lbl_oa = AutoLabel(text="", font_size=sp(12), color=P["muted"])
        f_oa_token = field("OANDA access token (paste to save or replace)", "", password=True)
        f_oa_acct = field("OANDA account id, for example 101-001-1234567-001", store.get("oanda_account"))
        oa_env = spinner(("PRACTICE", "LIVE"), store.get("oanda_env") or "PRACTICE", lambda v: None)

        def oa_text(extra=""):
            self.store.reload()
            masked = self.store.masked("oanda_token")
            return esc("OANDA token: %s. Account: %s. Environment: %s.%s" % (
                masked or "none saved", "saved" if self.store.get("oanda_account") else "none",
                self.store.get("oanda_env") or "PRACTICE", (" " + extra) if extra else ""))
        lbl_oa.text = oa_text()

        def save_oa():
            token = clean_secret(f_oa_token.text)
            acct = f_oa_acct.text.strip()
            env = oa_env.text if oa_env.text in ("PRACTICE", "LIVE") else "PRACTICE"
            fields = {"oanda_env": env}
            if token:                                   # an empty box never erases a saved token
                fields["oanda_token"] = token
            if acct:
                fields["oanda_account"] = acct

            def commit():
                ok = self.store.update(**fields)
                f_oa_token.text = ""
                lbl_oa.text = oa_text("Saved on this phone." if ok else (self.store.last_error or "Could not save."))
                if not ok:
                    self.report_error("CONFIG", "OANDA settings not saved: %s" % (self.store.last_error or "unknown reason"))
                self.reload_feed()
            if env == "LIVE" and self.store.get("oanda_env") != "LIVE":
                return self.require_pin("switch the forex broker to the LIVE environment", commit)
            commit()

        def test_oa():
            self.store.reload()
            if not self.fx_live.has_session():
                lbl_oa.text = oa_text("Save the token and the account id first.")
                return
            lbl_oa.text = oa_text("Testing OANDA ...")

            def work():
                try:
                    f = self.fx_live.funds()
                    msg = "OANDA works: equity %s, margin available %s." % (money_for("EURUSD", f["equity"]),
                                                                           money_for("EURUSD", f["cash"]))
                except BrokerError as exc:
                    msg = "OANDA test failed: %s" % redact(str(exc))[:140]
                    self.report_error("BROKER", msg)
                except Exception as exc:
                    msg = "OANDA test failed: %s" % redact(str(exc))[:100]
                    self.report_error("BROKER", msg)
                Clock.schedule_once(lambda dt: setattr(lbl_oa, "text", oa_text(msg)), 0)
            threading.Thread(target=work, daemon=True).start()

        def remove_oa():
            def commit():
                self.fx_live.clear_session()
                lbl_oa.text = oa_text("Token and account removed.")
                self.reload_feed()
            self.require_pin("remove the OANDA token", commit)
        for text, accent, fn in (("SAVE OANDA", P["a1"], save_oa), ("TEST OANDA", P["a2"], test_oa),
                                 ("REMOVE OANDA TOKEN", P["danger"], remove_oa)):
            b = HudButton(text=text, accent=accent, height=46)
            b.bind(on_release=lambda inst, f=fn: f())
            body.add_widget(b)
        body.add_widget(lbl_oa)

        title("MARKET FLOW AND AGENT BRAIN")
        body.add_widget(AutoLabel(text="The collector reads live candles of every symbol of the active market while the "
                                       "app is open and the market is open, ranks inflow and outflow and measures which "
                                       "charts move together or opposite. The Agent Brain keeps a small bounded local "
                                       "memory (old rows are deleted, the file stays under 6 MB). Android may pause the "
                                       "app in the background, so collection is not continuous 24 hours: gaps are shown "
                                       "honestly. Nothing is simulated.", font_size=sp(12), color=P["muted"]))
        lbl_brain = AutoLabel(text="", font_size=sp(12), color=P["muted"])

        def brain_text():
            return esc("%s Collector: %s." % (self.brain.describe(self.market), self.collector.status()))
        lbl_brain.text = brain_text()

        def clear_brain():
            self.brain.clear()
            lbl_brain.text = brain_text()
        b_refresh = HudButton(text="REFRESH STATUS", accent=P["a1"], height=44)
        b_refresh.bind(on_release=lambda inst: setattr(lbl_brain, "text", brain_text()))
        b_clear = HudButton(text="CLEAR COLLECTED DATA", accent=P["danger"], height=44)
        b_clear.bind(on_release=lambda inst: clear_brain())
        body.add_widget(b_refresh)
        body.add_widget(b_clear)
        body.add_widget(lbl_brain)

        title("VOICE PROFILE")
        labels = {label: key for key, label in self.voice.profiles()}
        current = PROFILES[self.voice.profile]["label"]

        def pick_profile(val):
            self.voice.set_profile(labels.get(val, "JARVIS"))
            self.status("Profile: %s. Tap TEST VOICE." % val)

        def pick_voice(val):
            self.voice.set_voice("" if val == "AUTOMATIC" else val)

        spinner(labels.keys(), current, pick_profile)
        voices = ["AUTOMATIC"] + [n for n, _t in self.voice.installed_voices()]
        spinner(voices, self.voice.voice_name or "AUTOMATIC", pick_voice)
        tb = HudButton(text="TEST VOICE", accent=P["a2"], height=46)
        tb.bind(on_release=lambda inst: self.test_voice())
        body.add_widget(tb)
        vb = HudButton(text="VOICE ON / OFF", accent=P["a1"], height=46)
        vb.bind(on_release=lambda inst: self.toggle_voice())
        body.add_widget(vb)
        self.lbl_voice_status = AutoLabel(text=esc(self.voice.status()), font_size=sp(12), color=P["muted"])
        body.add_widget(self.lbl_voice_status)
        title("OPENAI VOICE AND SPEECH (optional)")
        body.add_widget(AutoLabel(text="Paste your OpenAI key here. It is saved only inside this app on this phone. "
                                       "It enables the natural OpenAI voice profiles and OpenAI speech recognition. "
                                       "Never put the key in the code or on GitHub.", font_size=sp(12), color=P["muted"]))
        lbl_ai = AutoLabel(text="", font_size=sp(12), color=P["muted"])
        f_ai = field("OpenAI API key (sk-...)", "", password=True)
        mic_choices = {"AUTO (OpenAI if a key is saved)": "AUTO", "ANDROID (phone speech service)": "ANDROID",
                       "OPENAI (record and transcribe)": "OPENAI"}

        def key_text():
            self.store.reload()
            masked = self.store.masked("openai_key")
            return ("OpenAI key: %s." % masked) if masked else "OpenAI key: none saved."

        def save_ai():
            key = clean_secret(f_ai.text)
            if not key:
                lbl_ai.text = "Paste the key first. " + key_text()
                return
            if not key.startswith("sk-") or len(key) < 20:
                lbl_ai.text = "That does not look like an OpenAI key (it starts with sk-)."
                return
            ok = self.store.update(openai_key=key)
            f_ai.text = ""
            lbl_ai.text = (key_text() + " Saved on this phone.") if ok else esc(self.store.last_error or "Could not save.")
            if not ok:
                self.report_error("CONFIG", "OpenAI key not saved: %s" % (self.store.last_error or "unknown reason"))

        def remove_ai():
            ok = self.store.update(openai_key="")
            lbl_ai.text = (key_text() if ok else esc(self.store.last_error or "Could not remove the key."))
            if not ok:
                self.report_error("CONFIG", "OpenAI key not removed: %s" % (self.store.last_error or "unknown reason"))

        def test_ai():
            self.store.reload()
            if not self.openai.ready():
                lbl_ai.text = "Save a key first."
                return
            lbl_ai.text = "Testing OpenAI ..."

            def work():
                try:
                    reply = self.chat_probe()
                    msg = "OpenAI chat works: %s" % reply[:80]
                    audio = self.openai.speech("OpenAI voice check complete.", self.voice.openai_voice())
                    msg += " | voice OK (%d bytes)" % len(audio)
                except OpenAIError as exc:
                    msg = "OpenAI test failed: %s" % exc
                except Exception as exc:
                    msg = "OpenAI test failed: %s" % redact(str(exc))[:100]
                msg = esc(redact(msg))
                if "failed" in msg:
                    self.report_error("OPENAI", msg)
                Clock.schedule_once(lambda dt: setattr(lbl_ai, "text", msg), 0)
            threading.Thread(target=work, daemon=True).start()

        def pick_mic(val):
            choice = mic_choices.get(val, "AUTO")
            self.mic.prefer = choice
            if self.store.update(mic_engine=choice):
                self.status("Microphone engine: %s" % choice)
            else:
                self.report_error("CONFIG", "Microphone engine not saved: %s" % (self.store.last_error or "unknown reason"))

        for text, accent, fn in (("SAVE OPENAI KEY", P["a1"], save_ai), ("TEST OPENAI", P["a2"], test_ai),
                                 ("REMOVE OPENAI KEY", P["danger"], lambda: self.require_pin("remove the OpenAI key", remove_ai))):
            b = HudButton(text=text, accent=accent, height=46)
            b.bind(on_release=lambda inst, f=fn: f())
            body.add_widget(b)
        body.add_widget(lbl_ai)
        current_mic = [k for k, v in mic_choices.items() if v == self.mic.prefer] or ["AUTO (OpenAI if a key is saved)"]
        mic_spin = spinner(tuple(mic_choices), current_mic[0], pick_mic)

        title("VOICE SOURCE (which voice speaks)")
        body.add_widget(AutoLabel(text="AUTO uses ElevenLabs first when its key and voice ID are saved, then OpenAI when "
                                       "its key is saved, and the phone's own voice only as the last choice. If a cloud "
                                       "voice fails, the reason is shown in the red error bar.", font_size=sp(12),
                                  color=P["muted"]))
        src_choices = {"AUTO: ElevenLabs, then OpenAI, then phone": "AUTO", "ELEVENLABS only (then phone)": "ELEVENLABS",
                       "OPENAI only (then phone)": "OPENAI", "PHONE voice only": "PHONE"}
        oa_choices = {"AUTO (the voice of the profile, else onyx)": ""}
        for vname, vdesc in OPENAI_VOICES.items():
            oa_choices["%s - %s" % (vname, vdesc)] = vname
        refreshing = [False]

        def label_of(table, value):
            return ([k for k, v in table.items() if v == value] or [next(iter(table))])[0]

        def pick_source(val):
            if refreshing[0]:
                return
            choice = src_choices.get(val, "AUTO")
            if self.store.update(voice_source=choice):
                self.status("Voice source: %s. Tap TEST VOICE." % choice)
            else:
                self.report_error("CONFIG", "Voice source not saved: %s" % (self.store.last_error or "unknown reason"))

        def pick_oa_voice(val):
            if refreshing[0]:
                return
            choice = oa_choices.get(val, "")
            if self.store.update(openai_voice=choice):
                self.status("OpenAI voice: %s. Tap TEST VOICE." % (choice or "automatic"))
            else:
                self.report_error("CONFIG", "OpenAI voice not saved: %s" % (self.store.last_error or "unknown reason"))
        src_spin = spinner(tuple(src_choices), label_of(src_choices, self.store.get("voice_source")), pick_source)
        body.add_widget(AutoLabel(text="OpenAI voice (the deep ones are listed first):", font_size=sp(12), color=P["muted"]))
        oa_spin = spinner(tuple(oa_choices), label_of(oa_choices, self.store.get("openai_voice")), pick_oa_voice)

        title("ELEVENLABS VOICE (optional, its own key)")
        body.add_widget(AutoLabel(text="Paste your ElevenLabs API key and the voice ID of the voice you want (copy it "
                                       "from the ElevenLabs voice page, for example a deep male voice). Both are saved "
                                       "only inside this app. Never put them in the code or on GitHub.",
                                  font_size=sp(12), color=P["muted"]))
        f_el_key = field("ElevenLabs API key", "", password=True)
        f_el_voice = field("ElevenLabs voice ID (letters and digits)", "")
        lbl_el = AutoLabel(text="", font_size=sp(12), color=P["muted"])

        def el_text():
            self.store.reload()
            key = self.store.masked("eleven_key") or "none saved"
            voice = self.store.get("eleven_voice")
            return "ElevenLabs key: %s. Voice ID: %s." % (key, (voice[:6] + "..." + voice[-3:]) if len(voice) > 10 else (voice or "none saved"))

        def save_el():
            key = clean_secret(f_el_key.text)
            voice = "".join(ch for ch in f_el_voice.text if ch.isascii() and ch.isalnum())
            fields = {}
            if key:
                if len(key) < 16:
                    lbl_el.text = "That does not look like an ElevenLabs key (too short)."
                    return
                fields["eleven_key"] = key
            if voice:
                if len(voice) < 10:
                    lbl_el.text = "A voice ID has at least 10 letters and digits."
                    return
                fields["eleven_voice"] = voice
            if not fields:
                lbl_el.text = "Paste the key and the voice ID first. " + el_text()
                return
            ok = self.store.update(**fields)
            f_el_key.text = ""
            f_el_voice.text = ""
            lbl_el.text = (el_text() + " Saved on this phone.") if ok else esc(self.store.last_error or "Could not save.")
            if not ok:
                self.report_error("CONFIG", "ElevenLabs settings not saved: %s" % (self.store.last_error or "unknown reason"))

        def remove_el():
            ok = self.store.update(eleven_key="", eleven_voice="")
            lbl_el.text = el_text() if ok else esc(self.store.last_error or "Could not remove them.")
            if not ok:
                self.report_error("CONFIG", "ElevenLabs settings not removed: %s" % (self.store.last_error or "unknown reason"))

        def test_sources():
            self.store.reload()
            lbl_el.text = "Testing the saved voices ..."

            def work():
                try:
                    lines = self.voice.check_providers()
                except Exception as exc:
                    lines = ["Voice test failed: %s" % redact(str(exc))[:100]]
                msg = esc(redact(" | ".join(lines)))
                if "FAILED" in msg or "failed" in msg:
                    self.report_error("VOICE", " | ".join(lines))
                Clock.schedule_once(lambda dt: setattr(lbl_el, "text", msg), 0)
            threading.Thread(target=work, daemon=True).start()
        for text, accent, fn in (("SAVE ELEVENLABS", P["a1"], save_el), ("TEST SAVED VOICES (OpenAI + ElevenLabs)", P["a2"], test_sources),
                                 ("REMOVE ELEVENLABS", P["danger"], lambda: self.require_pin("remove the ElevenLabs key", remove_el))):
            b = HudButton(text=text, accent=accent, height=46)
            b.bind(on_release=lambda inst, f=fn: f())
            body.add_widget(b)
        body.add_widget(lbl_el)

        def refresh():
            """Called every time CONFIG opens: shows what is really on disk."""
            lbl_ai.text = key_text()
            lbl_el.text = el_text()
            refreshing[0] = True
            mic_spin.text = ([k for k, v in mic_choices.items() if v == (self.store.get("mic_engine") or "AUTO")] or [mic_spin.text])[0]
            src_spin.text = label_of(src_choices, self.store.get("voice_source"))
            oa_spin.text = label_of(oa_choices, self.store.get("openai_voice"))
            refreshing[0] = False
            self.lbl_voice_status.text = esc(self.voice.status())
        self.config_refresh = refresh
        refresh()
        nb = HudButton(text="NEXT SYMBOL", accent=P["a1"], height=46)
        nb.bind(on_release=lambda inst: self.next_symbol())
        body.add_widget(nb)

        title("ERRORS")
        body.add_widget(AutoLabel(text="Every error shows in a red bar above the menu with its module and reason. Keys "
                                       "and tokens are always hidden in it.", font_size=sp(12), color=P["muted"]))
        spk = HudButton(text="SPEAK ERRORS: %s" % ("ON" if self.ui.get("speak_errors") else "OFF"), accent=P["a2"], height=46)

        def toggle_speak():
            self.ui["speak_errors"] = not self.ui.get("speak_errors")
            self._save_ui()
            spk.text = "SPEAK ERRORS: %s" % ("ON" if self.ui["speak_errors"] else "OFF")
        spk.bind(on_release=lambda inst: toggle_speak())
        body.add_widget(spk)
        elog = HudButton(text="VIEW ERROR LOG", accent=P["danger"], height=46)
        elog.bind(on_release=lambda inst: self.show_error_log())
        body.add_widget(elog)

        title("SAFETY RULES (fixed)")
        body.add_widget(AutoLabel(text="1% maximum risk per trade. At least 9 of 20 agents must agree, reward:risk at "
                                       "least 1:2, no trade on weak volume or traps. Maximum 2% loss per day. Two losses in "
                                       "a row lock the day. Trailing stop only ever tightens. NSE positions are closed at "
                                       "15:15 India time, forex positions before the weekend. Spoken commands only request a "
                                       "trade: the CONFIRM popup is always needed and voice never arms or unlocks. "
                                       "DISARM and the kill switch lock every book until you tap ARM.",
                                  font_size=sp(12), color=P["muted"]))
        return sv

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def status(self, msg, spoken=False):
        text = redact(clean_text(msg, 200))

        def apply(dt):
            lbl = getattr(self, "lbl_status", None)
            if lbl is not None:
                lbl.text = esc(text)
        Clock.schedule_once(apply, 0)
        if looks_like_error(text):                 # an error is never only a thin grey line at the bottom
            self.report_error(guess_module(text), text, spoken=spoken)

    # ------------------------------------------------------------------ #
    # errors: always visible in the app, secrets removed
    # ------------------------------------------------------------------ #
    def report_error(self, module, detail, spoken=False, where=""):
        """Shows an error in the red bar above the menu and keeps it in the error log. Safe from any thread."""
        try:
            detail = redact(clean_text(detail, 300))
            module = clean_text(module, 24).upper() or "APP"
            where = clean_text(where, 80)
            now = time.time()
            if detail == self.last_err[0] and now - self.last_err[1] < 5.0:
                return                              # the same error again within 5 seconds is shown once
            self.last_err = (detail, now)
            entry = (time.strftime("%H:%M:%S"), module, detail + ((" (" + where + ")") if where else ""))
            self.errors.append(entry)
            del self.errors[:-40]
            Clock.schedule_once(lambda dt: self._refresh_err_bar(), 0)
            if self.ui.get("speak_errors") and not spoken and module not in ("VOICE", "MIC"):
                self.voice.say("Error in %s. %s" % (module.lower(), detail[:120]), False)
        except Exception:
            pass                                    # reporting an error must never raise a new one

    def report_exception(self, module, exc, spoken=False):
        """Like report_error for a caught exception: adds the file, line and function where it happened."""
        where = ""
        try:
            tb = traceback.extract_tb(exc.__traceback__)
            if tb:
                last = tb[-1]
                where = "%s:%d %s" % (os.path.basename(last.filename), last.lineno, last.name)
        except Exception:
            pass
        reason = str(exc) or exc.__class__.__name__
        self.report_error(module, "%s: %s" % (exc.__class__.__name__, reason), spoken, where)

    def _refresh_err_bar(self):
        btn = self.err_btn
        if btn is None:
            return
        if not self.errors:
            btn.height, btn.opacity, btn.disabled = 0, 0, True
            btn.text = ""
            return
        stamp, module, detail = self.errors[-1]
        btn.text = "ERROR [%s] %s  |  tap for the error log" % (module, detail[:70])
        btn.height, btn.opacity, btn.disabled = dp(44), 1, False

    def error_log_text(self):
        if not self.errors:
            return "No errors."
        return "\n".join("%s  [%s]  %s" % e for e in reversed(self.errors))

    def show_error_log(self):
        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        box = TextInput(text=self.error_log_text(), readonly=True, multiline=True, font_size=sp(12),
                        background_color=P["panel"], foreground_color=P["text"], cursor_color=P["a1"])
        content.add_widget(box)
        row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(48), spacing=dp(8))
        pop = Popup(title="ERROR LOG (keys and tokens are hidden)", content=content, size_hint=(0.96, 0.8),
                    background="", background_color=P["bg"], separator_color=P["danger"])

        def copy():
            try:
                from kivy.core.clipboard import Clipboard
                Clipboard.copy(self.error_log_text())
                self.status("Error log copied.")
            except Exception as exc:
                self.status("Copy not possible (%s). Touch and hold the text to select it." % str(exc)[:40])

        def clear():
            self.errors[:] = []
            self._refresh_err_bar()
            pop.dismiss()
        for text, fn in (("COPY", copy), ("CLEAR", clear), ("CLOSE", pop.dismiss)):
            b = HudButton(text=text, accent=P["danger"] if text == "CLEAR" else P["a1"], height=48)
            b.bind(on_release=lambda inst, f=fn: f())
            row.add_widget(b)
        content.add_widget(row)
        pop.open()

    def _install_error_hooks(self):
        """Errors in background threads and in Kivy callbacks land in the error log instead of vanishing."""
        app = self
        try:
            def thread_hook(args):
                if args.exc_value is not None and not isinstance(args.exc_value, SystemExit):
                    app.report_exception("THREAD " + str(getattr(args.thread, "name", "?"))[:14], args.exc_value)
            threading.excepthook = thread_hook
        except Exception:
            pass
        try:
            from kivy.base import ExceptionHandler, ExceptionManager

            class _Handler(ExceptionHandler):
                def handle_exception(self, inst):
                    if isinstance(inst, Exception):
                        app.report_exception("UI", inst)
                    return ExceptionManager.PASS
            ExceptionManager.add_handler(_Handler())
        except Exception:
            pass

    def alert(self, text, speak=True, priority=False):
        text = redact(clean_text(text, 400))
        self.status(text, spoken=speak)
        if speak:
            self.voice.say(text, priority)

    def test_voice(self):
        self.voice.test()
        Clock.schedule_once(lambda dt: self._show_voice_status(), 6.0)

    def _show_voice_status(self):
        lab = getattr(self, "lbl_voice_status", None)
        if lab is not None:
            lab.text = esc(self.voice.status())
        self.status(self.voice.status())

    def chat_probe(self):
        return self.openai.chat([{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=8)

    def equity(self):
        eq = safe_float(self.funds.get("equity")) if self.funds else 0.0
        if eq > 0:
            return eq
        if self.mode == "PAPER":
            return safe_float(self.paper_broker.capital, 1.0)
        return FX_REFERENCE_CAPITAL if self.market == "FX" else REFERENCE_CAPITAL

    def cash(self):
        if self.funds and self.funds.get("cash") is not None:
            return safe_float(self.funds["cash"])
        return None

    def tick_info(self):
        try:
            return self.ticks.latest()
        except Exception:
            return {"mode": "OFF", "depth": None, "note": "", "age": None}

    # ------------------------------------------------------------------ #
    # live loop
    # ------------------------------------------------------------------ #
    def _tick(self, dt):
        try:
            candles, price, source, note = self.stream.snapshot()
            now = time.time()
            vt = self.voice_trade
            if vt is not None and now - vt["t"] > 25:
                self.voice_trade = None
                self.alert("The analysis took too long. Trade request cancelled. Nothing was ordered.", True, True)
            if candles:
                self.candles, self.price, self.source, self.note = candles, price, source, note
                if now - self.last_eval >= 3.0 and not self.busy["eval"]:
                    self.busy["eval"] = True
                    self.last_eval = now
                    depth = self.tick_info().get("depth")
                    threading.Thread(target=self._eval_worker,
                                     args=(list(candles), self.equity(), self.cash(), self.symbol, depth),
                                     daemon=True).start()
            else:
                self.source, self.note = source, note
            if self.note != self.last_note:
                self.last_note = self.note
                if self.note and looks_like_error(self.note) or "no data" in str(self.note).lower():
                    self.report_error("MARKET FEED", self.note)
            if now - self.last_exits >= 3.0 and not self.busy["exits"] and any(b.positions() for b, _ in self._books()):
                self.busy["exits"] = True
                self.last_exits = now
                threading.Thread(target=self._exits_worker, daemon=True).start()
            if now - self.last_funds >= 20.0 and not self.busy["funds"] and self.broker.has_session():
                self.busy["funds"] = True
                self.last_funds = now
                threading.Thread(target=self._funds_worker, daemon=True).start()
            for book, _ in self._books():
                for ev in book.pop_events():
                    self.alert(ev, True, False)
            self._refresh_all()
        except Exception as exc:
            self.status("Update error: %s" % str(exc)[:100])

    def _eval_worker(self, candles, equity, cash, symbol, depth):
        try:
            d = self.engine.evaluate(symbol, candles, equity, cash, depth)
            d = self._apply_discipline(d, candles)
            problem = self._data_problem()
            if problem and d.approved:                       # never approve a setup on data that is not live
                d.approved = False
                d.reason = problem
            Clock.schedule_once(lambda dt: self._show_decision(d), 0)
        except Exception as exc:
            self.report_exception("AGENTS", exc)
        finally:
            self.busy["eval"] = False

    def _books(self):
        """All four trade books (NSE and forex, PAPER and LIVE) with their own risk managers. Open positions are
        watched in every book, whatever market or mode is on screen."""
        return [(self.nse_paper, self.risks[("NSE", "PAPER")]), (self.nse_live, self.risks[("NSE", "LIVE")]),
                (self.fx_paper, self.risks[("FX", "PAPER")]), (self.fx_live, self.risks[("FX", "LIVE")])]

    def _paper_price(self, symbol):
        """Live price for the paper desk: only from a live feed, never simulated, never older than MAX_DATA_AGE
        seconds, and never while the market is closed."""
        if (symbol == self.symbol and self.source in LIVE_SOURCES and self.price > 0 and self.candles
                and self.session_open() and time.time() - self.candles[-1].t < MAX_DATA_AGE):
            return self.price
        return 0.0

    def _data_problem(self):
        """Empty text when the data is live and fresh, otherwise the reason. Every order path checks this."""
        if self.source not in LIVE_SOURCES or not self.candles or self.price <= 0:
            return "NO LIVE DATA (feed: %s). Nothing is simulated, so no order." % self.source
        if not self.session_open():
            return "NO LIVE DATA: the %s market is closed. No order." % ("forex" if self.market == "FX" else "NSE")
        age = time.time() - self.candles[-1].t
        if age > MAX_DATA_AGE:
            return "NO LIVE DATA: the last candle is %d minutes old, too stale to trade." % int(age // 60)
        return ""

    def data_live(self):
        return not self._data_problem()

    def _apply_discipline(self, d, candles):
        """Trend/context, reward:risk and invalidation are already in the decision. This adds the market-quality
        check (no trade in a choppy market) and the paper-learned filter. It can only reject, never approve."""
        try:
            d.need = max(int(getattr(d, "need", MIN_AGREE)), MIN_AGREE)
            if d.direction not in (BUY, SELL):
                return d
            choppy = bool(chop_check(candles)["choppy"])
            row = scan_row(d.symbol, candles)
            want = "green" if d.direction == BUY else "red"
            if choppy:
                quality = CHOP
            elif row is not None and row["side"] == want and row["quality"] == GOOD:
                quality = GOOD
            else:
                quality = "WEAK"
            now = ist_now()
            slot = fx_slot() if self.market == "FX" else PaperLearner.slot(now.hour, now.minute)
            extra, note = self.learner.extra_agree(d.direction, quality, slot)
            flow = self._flow_context(d.symbol, d.direction)
            if flow["conflict"]:
                extra += 1                                   # the flow can only make the filter stricter
                note = (note + " " + flow["note"] + " One more agent must agree.").strip()
            d.need = MIN_AGREE + extra
            d.ctx = {"key": PaperLearner.key(d.direction, quality, slot), "quality": quality, "slot": slot,
                     "agree": int(d.agree), "need": int(d.need), "flow": flow["label"], "flow_score": flow["score"],
                     "flow_conflict": bool(flow["conflict"])}
            if d.approved and quality == CHOP:
                d.approved = False
                d.reason = "CHOP / DO NOT TRADE: price is moving sideways, so there is no clear trend. " + d.reason
            elif d.approved and d.agree < d.need:
                d.approved = False
                d.reason = "Filter tightened by paper results: %d agents must agree, only %d do. %s" % (d.need, d.agree, note)
            elif d.approved:
                d.reason = d.reason + " " + note
        except Exception as exc:                  # fail closed
            d.approved = False
            d.reason = "Discipline check failed (%s). No trade." % redact(str(exc))[:60]
        return d

    def _exits_worker(self):
        try:
            closed_all = []
            for book, risk in self._books():
                if not book.positions():
                    continue
                closed = book.check_exits()
                for t in closed:
                    risk.record_result(t["pnl"])
                closed_all += closed
                if not closed and book.last_error:
                    self.report_error("BROKER", "%s position monitor: %s" % (book.name, book.last_error))
            if closed_all:
                Clock.schedule_once(lambda dt: self._after_close(closed_all), 0)
        except Exception as exc:
            self.report_exception("BROKER", exc)
        finally:
            self.busy["exits"] = False

    def _funds_worker(self):
        try:
            self.funds = self.broker.funds()
        except BrokerError as exc:
            self.report_error("BROKER", "Funds: %s" % exc)
        except Exception as exc:
            self.report_exception("BROKER", exc)
        finally:
            self.busy["funds"] = False

    def _after_close(self, closed):
        for t in closed:
            self.last_trade_text = "%s %s closed by %s: %s" % (t["side"], t["symbol"], t["reason"].lower(),
                                                              money_for(t["symbol"], t["pnl"]))
            self.alert("%s closed by %s. %s %s." % (t["symbol"], t["reason"].lower(),
                                                    "Profit" if t["pnl"] >= 0 else "Loss",
                                                    money_for(t["symbol"], abs(t["pnl"]))), True, True)
        self.last_funds = 0.0                      # re-read the broker balance on the next tick
        self._refresh_all()

    # ------------------------------------------------------------------ #
    # rendering
    # ------------------------------------------------------------------ #
    def _levels(self):
        out = []
        d = self.decision
        if d is not None and d.approved and d.symbol == self.symbol:
            out = [(d.entry, P["a1"]), (d.sl, P["danger"]), (d.tp, P["ok"])]
        for p in self.broker.positions():
            if p["symbol"] == self.symbol:
                out = [(p["entry"], P["a1"]), (p["sl"], P["danger"]), (p["tp"], P["ok"])]
        return out

    def _refresh_all(self):
        st = self.risk.status()
        ti = self.tick_info()
        if self.tamper:
            system, scol = "TAMPERED", P["danger"]
        elif st["killed"]:
            system, scol = "DISARMED", P["danger"]
        elif st["locked"]:
            system, scol = "LOCKED TODAY", P["danger"]
        elif self.mode == "PAPER":
            system, scol = "PAPER ARMED", P["a1"]
        elif not self.live_unlocked:
            system, scol = "LIVE LOCKED", P["a2"]
        else:
            system, scol = "LIVE ARMED", P["ok"]
        connected = self.live_broker.has_session()
        mode = ti.get("mode", "OFF")
        age = ti.get("age")
        tick_txt = mode if age is None else "%s %ds" % (mode, int(age))
        if self.source in LIVE_SOURCES:
            self.grid.set("FEED", self.source + (" (may lag)" if self.source == "YAHOO" else ""), P["a1"])
        else:
            self.grid.set("FEED", "NO LIVE DATA", P["danger"])
        if self._data_problem():
            self.grid.set("PRICE", "NO LIVE DATA", P["danger"])
        else:
            self.grid.set("PRICE", "%s %s" % (self.symbol, px(self.price)), P["a2"])
        is_open = self.session_open()
        self.grid.set("MARKET", "%s %s" % (self.market, "OPEN" if is_open else "CLOSED"),
                      P["ok"] if is_open else P["muted"])
        self.grid.set("SYSTEM", system, scol)
        self.grid.set("MODE", self.mode, P["danger"] if self.mode == "LIVE" else P["a1"])
        bname = self.live_broker.name
        if self.market == "FX" and connected:
            bname = "OANDA " + self.fx_live.env
        self.grid.set("BROKER", ("%s ON" % bname) if connected else "%s OFF" % bname,
                      P["danger"] if connected and self.market == "FX" and self.fx_live.env == "LIVE"
                      else P["ok"] if connected else P["muted"])
        self.grid.set("GUARD", "TAMPER" if self.tamper else "OK", P["danger"] if self.tamper else P["ok"])
        self.grid.set("LOCK", "PIN ON" if self.applock.enabled else "PIN OFF", P["ok"] if self.applock.enabled else P["muted"])
        self.grid.set("TICKS", tick_txt, P["a1"] if mode == "WS" else P["a2"] if mode == "REST" else P["muted"])

        killed = st["killed"]
        self.btn_arm.text = "ARM" if killed else "DISARM"
        self.btn_arm.set_accent(P["danger"] if killed else P["ok"], filled=killed)
        self.btn_market.text = "MARKET: %s" % self.market
        self.btn_mode.text = "MODE: %s" % self.mode
        self.btn_mode.set_accent(P["danger"] if self.mode == "LIVE" else P["a1"], filled=self.mode == "LIVE")
        if self.cfg_mode_btn is not None:
            self.cfg_mode_btn.text = "MODE: %s  (TAP TO SWITCH)" % self.mode
        self.btn_live.text = "LIVE: UNLOCKED" if self.live_unlocked else "LIVE: LOCKED"
        self.btn_live.set_accent(P["danger"] if self.live_unlocked else P["muted"], filled=self.live_unlocked)
        self.btn_trail.text = "TRAIL SL LOCK: %s" % ("ON" if self.broker.trailing else "OFF")
        self.btn_trail.set_accent(P["a1"] if self.broker.trailing else P["muted"], filled=self.broker.trailing)

        if self.candles:
            c = self.candles[-1]
            self.chart.update(self.candles, self._levels())
            stale = self._data_problem()
            self.lbl_chart.text = (("[color=%s]%s[/color]  " % (P["hx_danger"], esc(stale)) if stale else "") +
                                   fx("O %.2f  H %.2f  L %.2f  C %.2f  |  %d candles%s") % (
                c.o, c.h, c.l, c.c, len(self.candles), ("  |  " + esc(self.note)) if self.note else ""))
        else:
            self.lbl_chart.text = "[color=%s]NO LIVE DATA[/color]  Waiting for the broker or Yahoo feed. Nothing is simulated.  %s" % (
                P["hx_danger"], esc(self.note))
        f = self.funds
        acct = ("Equity %s  |  cash %s" % (inr(f["equity"]), inr(f["cash"]))) if f else \
            "Equity: waiting for the account (size preview on %s)" % inr(self.equity())
        self.lbl_account.text = "[b]%s[/b]  %s  |  today %s  |  losses in row %d  |  trades today %d" % (
            "PAPER (virtual money)" if self.mode == "PAPER" else "LIVE (real money)", acct, inr(st["daily_pnl"]),
            st["consecutive_losses"], st["trades_today"])
        pos = self.broker.positions()
        if pos:
            p = pos[0]
            sign = 1 if p["side"] == BUY else -1
            live = (self.price - p["entry"]) * p["qty"] * sign if p["symbol"] == self.symbol else 0.0
            stage = TrailingStop.stage(p["side"], p["entry"], p.get("sl0", p["sl"]), p["sl"])
            vpu = self.broker.unit_economics(p["symbol"], self.price or p["entry"])[0] if self.price else 1.0
            self.lbl_position.text = "[b]OPEN[/b] %s %d %s @ %s  SL %s (%s)  TP %s  P&L %s" % (
                p["side"], p["qty"], esc(p["symbol"]), px(p["entry"]), px(p["sl"]), stage, px(p["tp"]), inr(live * vpu))
        else:
            self.lbl_position.text = esc(self.last_trade_text) if self.last_trade_text else "No open position."
        self._refresh_mentor(pos, st)
        self._refresh_ledger(pos)
        self._refresh_depth(ti, st, pos)
        if self.copilot_on:
            self._refresh_copilot()

    def _refresh_ledger(self, pos):
        """Live balance = broker equity + open P&L. Realized numbers come from the broker's own trade book,
        so they survive restarts and update the moment a trade closes."""
        led = self.broker.ledger()
        self.pnl.set_title("PAPER LEDGER (virtual money)" if self.mode == "PAPER" else "REAL-MONEY LEDGER")
        base = self.paper_broker.capital if self.mode == "PAPER" else (
            FX_REFERENCE_CAPITAL if self.market == "FX" else REFERENCE_CAPITAL)
        open_pnl = 0.0
        for p in pos:
            if p["symbol"] == self.symbol and self.price:
                vpu = self.broker.unit_economics(p["symbol"], self.price)[0]
                open_pnl += (self.price - p["entry"]) * p["qty"] * (1 if p["side"] == BUY else -1) * vpu
        if self.funds and self.funds.get("equity") is not None:
            balance, live, note = safe_float(self.funds["equity"]) + open_pnl, True, ""
        else:
            balance, live, note = base + led["net"] + open_pnl, False, "start %s" % inr(base)
        self.pnl.set_values(balance, led["profit"], led["loss"], led["net"], led["day_net"], open_pnl,
                            led["trades"], led["wins"], led["losses"], live, note)

    def _refresh_depth(self, ti, st, pos):
        depth = ti.get("depth")
        if depth and depth.get("bids") and depth.get("asks"):
            lines = ["[b]%s[/b]  last %s  |  %s  |  age %s" % (
                esc(self.symbol), px(ti.get("ltp", self.price)), ti.get("mode"),
                ("%ds" % int(ti["age"])) if ti.get("age") is not None else "-"), ""]
            rows = max(len(depth["bids"]), len(depth["asks"]))
            lines.append("[color=%s][b]BID  qty (orders)[/b][/color]          [color=%s][b]ASK  qty (orders)[/b][/color]"
                         % (P["hx_up"], P["hx_down"]))
            for i in range(min(rows, 5)):
                b = depth["bids"][i] if i < len(depth["bids"]) else None
                a = depth["asks"][i] if i < len(depth["asks"]) else None
                bt = fx("%.2f  %d (%d)") % (b[0], b[1], b[2]) if b else "-"
                at = fx("%.2f  %d (%d)") % (a[0], a[1], a[2]) if a else "-"
                lines.append("[color=%s]%s[/color]      [color=%s]%s[/color]" % (P["hx_up"], bt, P["hx_down"], at))
            bq = sum(q for _p, q, _o in depth["bids"])
            aq = sum(q for _p, q, _o in depth["asks"])
            if bq + aq:
                lines.append("")
                lines.append("Buyers %d vs sellers %d  (%.0f%% buyers)" % (bq, aq, 100.0 * bq / (bq + aq)))
            self.lbl_depth.text = "\n".join(lines)
        else:
            self.lbl_depth.text = ("Forex has no market depth. Live bid and ask are used for prices only." if self.market == "FX"
                                   else "No market depth yet. Mode %s. %s" % (ti.get("mode", "OFF"), esc(ti.get("note", "")) or
                                                                              "Connect a broker in CONFIG."))
        ptxt = []
        for p in pos:
            ptxt.append("OPEN %s %d %s entry %s SL %s TP %s" % (p["side"], p["qty"], esc(p["symbol"]), px(p["entry"]),
                                                              px(p["sl"]), px(p["tp"])))
        for t in reversed(self.broker.trades(6)):
            ptxt.append("%s %s %d  %s  %s" % (t["side"], esc(t["symbol"]), t["qty"], inr(t["pnl"]), esc(t["reason"])))
        self.lbl_positions.text = "\n".join(ptxt) or "No positions or trades yet."
        self.lbl_risk.text = ("State: %s\nKill reason: %s\nDaily P&L %s  |  losses in a row %d  |  trades %d  |  "
                              "wins %d / losses %d") % (
            "DISARMED" if st["killed"] else ("LOCKED" if st["locked"] else "ARMED"), esc(st.get("kill_reason") or "-"),
            inr(st["daily_pnl"]), st["consecutive_losses"], st["trades_today"], st["wins"], st["losses"])

    def _show_decision(self, d, speak=True):
        if d.symbol != self.symbol:
            return
        self.decision = d
        for v in d.votes:
            tile = self.tiles.get(v.agent)
            if tile is None:
                continue
            edge = P["up"] if v.signal == BUY else P["down"] if v.signal == SELL else P["muted"]
            if v.veto:
                edge = P["danger"]
            tag = "  [color=%s][b]VETO[/b][/color]" % P["hx_danger"] if v.veto else ""
            tile.set_content("[b]%s[/b]  [color=%s]%s %d%%[/color]%s\n[color=%s]%s[/color]" % (
                esc(v.agent), hex_of(edge), v.signal, int(v.confidence * 100), tag, P["hx_muted"], esc(v.detail)),
                edge, v.agent == self.selected_agent)
        buys = sum(1 for v in d.votes if v.signal == BUY)
        sells = sum(1 for v in d.votes if v.signal == SELL)
        vetoes = sum(1 for v in d.votes if v.veto)
        self.lbl_tally.text = "[color=%s]BUY %d[/color]  |  [color=%s]SELL %d[/color]  |  neutral %d  |  [color=%s]VETO %d[/color]  |  need %d" % (
            P["hx_up"], buys, P["hx_down"], sells, len(d.votes) - buys - sells, P["hx_danger"], vetoes, d.need)
        if d.approved:
            col = P["hx_up"] if d.direction == BUY else P["hx_down"]
            self.lbl_signal.text = "[b][color=%s]%s %s[/color][/b]  %d/%d agents" % (col, d.direction, esc(d.symbol), d.agree, d.total)
            self.lbl_levels.text = "Entry %s  |  SL %s  |  TP %s  |  RR 1:%.1f  |  Qty %d (risk %s)" % (
                px(d.entry), px(d.sl), px(d.tp), d.rr, d.qty, inr(d.risk_amount))
        else:
            self.lbl_signal.text = "[b][color=%s]NO TRADE[/color][/b]  %s  %d/%d agree" % (P["hx_muted"], esc(d.symbol), d.agree, d.total)
            self.lbl_levels.text = "Waiting for a clean setup."
        self.lbl_reason.text = esc(d.reason)
        if self.selected_agent:
            self._show_agent_log(self.selected_agent, speak=False)
        key = (d.symbol, d.direction, d.approved)
        if speak and d.approved and key != self.last_key and time.time() - self.last_signal_voice > 60:
            self.last_signal_voice = time.time()
            self.voice.say("%s signal on %s. %d of %d agents agree. Tap %s to review the order." % (
                d.direction, d.symbol, d.agree, d.total, d.direction.lower()))
        self.last_key = key
        vt = self.voice_trade
        if vt is not None and d.ts >= vt["t"] and d.symbol == vt["symbol"]:
            self.voice_trade = None
            self._voice_trade_continue(d, vt["side"], vt.get("risk", 1.0), vt.get("fx", False))

    # ------------------------------------------------------------------ #
    # touch-speaking agents
    # ------------------------------------------------------------------ #
    def _agent_meta(self):
        ti = self.tick_info()
        return {"source": self.source, "tick_mode": ti.get("mode") if ti.get("mode") not in (None, "OFF") else "",
                "depth": ti.get("depth"), "now": time.time()}

    def agent_touched(self, name):
        self.selected_agent = name
        for n, tile in self.tiles.items():
            tile.selected = (n == name)
            tile._draw()
        self._show_agent_log(name, speak=True)

    def _show_agent_log(self, name, speak):
        info = self.engine.explain(name, self._agent_meta())
        self.agent_log_lines = info["lines"]
        col = P["hx_up"] if info["signal"] == BUY else P["hx_down"] if info["signal"] == SELL else P["hx_muted"]
        head = "[b][color=%s]%s  %s  %d%%[/color][/b]%s" % (
            col, esc(name), info["signal"], int(info["confidence"] * 100),
            "  [color=%s][b]VETO[/b][/color]" % P["hx_danger"] if info["veto"] else "")
        self.lbl_agent_log.text = head + "\n" + "\n".join(esc(x) for x in info["lines"])
        if speak:
            self.voice.say(info["spoken"], priority=True)
            self.status("%s: speaking live status" % name)

    # ------------------------------------------------------------------ #
    # microphone
    # ------------------------------------------------------------------ #
    def _mic_press(self):
        self.voice.silence()
        self.voice.hold(True)
        self.mic.press()

    def _mic_release(self):
        self.mic.release()

    def _mic_state_cb(self, name):
        if name == "idle":
            self.voice.hold(False)

        def apply(dt):
            self.mic_btn.set_state(name)
            self.lbl_mic.text = {"idle": self.mic_text, "listening": "LISTENING ... speak now",
                                 "processing": "PROCESSING ..."}.get(name, name)
        Clock.schedule_once(apply, 0)

    def _mic_level_cb(self, value):
        Clock.schedule_once(lambda dt: self.mic_btn.set_level(value), 0)

    def _mic_result_cb(self, text):
        Clock.schedule_once(lambda dt: self.handle_command(text), 0)

    def _mic_error_cb(self, message):
        def apply(dt):
            self.voice.hold(False)
            self.mic_btn.set_state("idle")
            self.lbl_mic.text = self.mic_text
            self.report_error("MIC", message)
            self.alert(message, True, False)
        Clock.schedule_once(apply, 0)

    def _send_typed(self):
        text = clean_text(self.cmd_input.text, 400)
        self.cmd_input.text = ""
        if text:
            self.handle_command(text)

    # ------------------------------------------------------------------ #
    # chat panel
    # ------------------------------------------------------------------ #
    def chat_add(self, who, text):
        self.chat_lines.append((who, clean_text(text, 500)))
        self.chat_lines = self.chat_lines[-6:]
        self._render_chat()

    def chat_replace_last(self, text):
        if self.chat_lines:
            who, _old = self.chat_lines[-1]
            self.chat_lines[-1] = (who, clean_text(text, 500))
        self._render_chat()

    def _render_chat(self):
        lab = getattr(self, "lbl_chat", None)
        if lab is None:
            return
        if not self.chat_lines:
            lab.text = ("[color=%s]Ask me anything about the market, your risk or your trades. "
                        "Commands: scan market, status report, buy, sell, trail stop, co pilot.[/color]" % P["hx_muted"])
            return
        rows = []
        for who, text in self.chat_lines:
            col = P["hx_a2"] if who == "YOU" else P["hx_a1"]
            rows.append("[b][color=%s]%s[/color][/b]  %s" % (col, who, esc(text)))
        lab.text = "\n".join(rows)

    def _first_run_checks(self):
        """Asks for the microphone permission once at start, so the first press of the mic already works."""
        try:
            if platform == "android" and not self.mic.has_permission():
                self.mic.ask_permission()
        except Exception as exc:
            self.report_exception("MIC", exc)

    # ------------------------------------------------------------------ #
    # input: commands and AI conversation
    # ------------------------------------------------------------------ #
    def handle_command(self, text):
        """Everything typed or spoken lands here. Short commands run at once; anything else is a conversation."""
        text = clean_text(text, 400)
        if not text:
            return
        if self.app_locked:
            return self.status("The app is locked. Enter your PIN first.")
        self.chat_add("YOU", text)
        if is_flow_request(text):
            return self.flow_report()
        fxc = parse_fx(text, self.market == "FX", self.symbol if self.market == "FX" else "")
        if fxc.action == "REJECT":
            return self.alert(fxc.reason, True, True)
        if fxc.action != "NONE":
            self.status("Forex command: %s" % fxc.action)
            return self.run_fx_command(fxc)
        if is_command(text, AGENT_NAMES):
            self.run_command(text)
        else:
            self.ask_ai(text)

    def run_command(self, text):
        intent, arg = parse_command(text, AGENT_NAMES)
        self.status("Command: %s" % intent)
        if intent == "SCAN":
            self.scan_now(speak_result=True)
        elif intent == "STATUS":
            self.status_report()
        elif intent in ("KILL", "PANIC"):
            self.panic()
        elif intent in ("REARM", "ARM"):
            # Re-arming is never done by voice or text: a mis-heard word must not be able to unlock trading.
            self.reply_local("Trading is locked. To re-arm, tap the ARM button on the screen." if self.risk.is_killed()
                             else "The system is already armed.")
        elif intent == "DISARM":
            self.disarm()
        elif intent == "TRAIL":
            self.toggle_trailing()
        elif intent == "COPILOT":
            self.toggle_copilot(force=True)
        elif intent in ("BUY", "SELL"):
            self.order_request(intent)
        elif intent == "AGENT":
            self.goto("agents")
            self.agent_touched(arg)
        elif intent == "UNLOCK":
            self.ask_unlock()
        elif intent == "LOCK":
            self.lock_live("Live orders locked by your command.")
        elif intent == "PAPER":
            self.set_mode("PAPER")                  # the safe direction may be spoken; LIVE never can
        elif intent == "TRADE":
            self.voice_trade_request(text)
        elif intent == "WATCHLIST":
            self.goto("scan")
            self.scan_watchlist(True)
        elif intent == "MENTOR":
            self.mentor_speak()
        elif intent == "DRAW":
            self.goto("scan")
            self.draw_speak()
        elif intent == "VOICE_TEST":
            self.test_voice()
        elif intent == "MUTE":
            self.voice.say("Voice off.", True)
            self.voice.set_enabled(False)
        elif intent == "UNMUTE":
            self.voice.set_enabled(True)
            self.voice.say("Voice on.", True)
        elif intent == "HELP":
            self.reply_local(HELP_TEXT)
        else:
            self.ask_ai(text)

    def reply_local(self, text):
        """An answer that does not need OpenAI."""
        self.chat_add("TDN", text)
        self.status(text)
        self.voice.say(text, True)

    def _local_chat_reply(self, text):
        words = text.lower().split()
        if any(w in words for w in ("hi", "hello", "hey", "namaste", "hii")):
            return "Hello boss. I am online. Say scan market or status report. Save an OpenAI key in CONFIG for full conversation."
        return ("AI conversation needs an OpenAI key. Open CONFIG, paste the key and tap SAVE OPENAI KEY. "
                "Meanwhile I understand: scan market, status report, buy, sell, trail stop, co pilot, panic square off.")

    def ask_ai(self, text):
        self.store.reload()
        if not self.openai.ready():
            return self.reply_local(self._local_chat_reply(text))
        if self.busy["chat"]:
            self.status("Still thinking about your last question.")
            return
        self.busy["chat"] = True
        self.chat_add("TDN", "thinking ...")
        context = self._ai_context()                 # built on the UI thread: a consistent snapshot
        threading.Thread(target=self._chat_worker, args=(text, context), daemon=True).start()

    def _chat_worker(self, text, context):
        ok = True
        try:
            reply = self.chat.reply(text, context)
        except OpenAIError as exc:
            ok, reply = False, "AI chat problem: %s" % exc
        except Exception as exc:
            ok, reply = False, "AI chat problem: %s" % redact(str(exc))[:100]
        self.busy["chat"] = False
        Clock.schedule_once(lambda dt: self._chat_done(reply, ok), 0)

    def _chat_done(self, reply, ok):
        reply = no_hype(redact(reply))
        self.chat_replace_last(reply)
        self.status(reply if not ok else "TDN replied.")
        self.voice.say(reply, True)

    def _ai_context(self):
        """Live facts for the AI. It never contains keys, tokens or account numbers."""
        d = self.decision
        st = self.risk.status()
        led = self.broker.ledger()
        ti = self.tick_info()
        lines = ["Market %s. Symbol %s, price %s, data source %s, ticks %s, session: %s." % (
            self.market, self.symbol, (fx("%.2f") % self.price) if self.price else "unknown", self.source,
            ti.get("mode", "OFF"), self.session_text())]
        snap = self._flow_snapshot()
        if snap is not None and snap.get("ok"):
            lines.append(flow_report_text(snap)[:420])
        else:
            lines.append("Market flow: NO LIVE DATA yet.")
        if d is not None and d.symbol == self.symbol:
            if d.approved:
                lines.append(fx("Signal: %s approved by %d of %d agents. Entry %.2f, stop %.2f, target %.2f, reward to risk %.1f.")
                             % (d.direction, d.agree, d.total, d.entry, d.sl, d.tp, d.rr))
            else:
                lines.append("Signal: no trade. %d of %d agents agree. Reason: %s" % (d.agree, d.total, d.reason))
            buys = [v.agent for v in d.votes if v.signal == BUY]
            sells = [v.agent for v in d.votes if v.signal == SELL]
            lines.append("Agents buying: %s. Agents selling: %s." % (", ".join(buys) or "none", ", ".join(sells) or "none"))
            veto = [v.agent for v in d.votes if v.veto]
            if veto:
                lines.append("Agents vetoing: %s." % ", ".join(veto))
        state = "DISARMED (kill switch on)" if st["killed"] else "LOCKED for today" if st["locked"] else "ARMED"
        lines.append("Trading mode is %s (%s). Live real-money orders are %s." % (
            self.mode, "virtual money on live prices" if self.mode == "PAPER" else "real money",
            "UNLOCKED" if self.live_unlocked else "LOCKED"))
        lines.append("Data source: %s%s." % (self.source, "" if self.source in LIVE_SOURCES else " (no live data, nothing is simulated)"))
        if self.tamper:
            lines.append("INTEGRITY CHECK FAILED: trading is off.")
        lines.append(DISCIPLINE)
        lines.append("System %s. Daily P and L %.2f. Losses in a row %d. Trades today %d of 4 allowed." % (
            state, st["daily_pnl"], st["consecutive_losses"], st["trades_today"]))
        lines.append("Realized: winnings %.2f, losses %.2f, net %.2f over %d trades (%d wins, %d losses)." % (
            led["profit"], led["loss"], led["net"], led["trades"], led["wins"], led["losses"]))
        if self.funds:
            lines.append("Broker equity %.2f, cash %.2f." % (self.funds.get("equity", 0.0), self.funds.get("cash", 0.0)))
        else:
            lines.append("Broker not connected, so no live balance.")
        positions = self.broker.positions()
        for p in positions:
            lines.append(fx("Open position: %s %d %s entry %.2f stop %.2f target %.2f.") % (
                p["side"], p["qty"], p["symbol"], p["entry"], p["sl"], p["tp"]))
        if not positions:
            lines.append("No open position.")
        return " ".join(lines)

    # ------------------------------------------------------------------ #
    # controls
    # ------------------------------------------------------------------ #
    def scan_now(self, speak_result=False):
        self.last_eval = 0.0
        self.status("Scanning %s with all 20 agents..." % self.symbol)
        if speak_result:
            self.voice.say("Scanning %s." % self.symbol, True)
            Clock.schedule_once(lambda dt: self._speak_scan(), 4.5)

    def _speak_scan(self):
        d = self.decision
        if d is None or d.symbol != self.symbol:
            return self.voice.say("No analysis yet.", True)
        if d.approved:
            self.voice.say(fx("%s setup on %s. %d of %d agents agree. Entry %.2f, stop %.2f, target %.2f.") % (
                d.direction, d.symbol, d.agree, d.total, d.entry, d.sl, d.tp), True)
        else:
            self.voice.say("No trade on %s. %s" % (d.symbol, d.reason), True)

    def status_report(self):
        st = self.risk.status()
        pos = self.broker.positions()
        state = "disarmed" if st["killed"] else "locked for today" if st["locked"] else "armed"
        parts = ["Mode %s." % self.mode, "System %s." % state, fx("%s price %.2f from %s.") % (self.symbol, self.price, self.source),
                 "Live orders are %s." % ("unlocked" if self.live_unlocked else "locked"),
                 "The %s." % self.session_text(),
                 "%s." % (("Broker %s connected" % self.live_broker.name) if self.live_broker.has_session() else "Broker not connected"),
                 "Today's profit and loss %s." % inr(st["daily_pnl"])]
        if pos:
            p = pos[0]
            parts.append(fx("Open %s %d %s, stop %.2f.") % (p["side"], p["qty"], p["symbol"], p["sl"]))
        else:
            parts.append("No open position.")
        self.alert(" ".join(parts), True, True)

    def next_symbol(self):
        wl = self.watchlist
        i = wl.index(self.symbol) if self.symbol in wl else -1
        self.set_symbol(wl[(i + 1) % len(wl)])

    def set_symbol(self, sym):
        if sym not in self.watchlist:
            return
        self.voice_trade = None
        self.fxgate.clear()
        self.symbol = sym
        self.ui["fx_symbol" if self.market == "FX" else "symbol"] = self.symbol
        self._apply_symbol_format(sym)
        self._save_ui()
        self.decision = None
        self.last_key = None
        self.candles = []
        self.ticks.set_symbol(self.symbol)
        self.status("Loading %s ..." % self.symbol)
        threading.Thread(target=self._switch_worker, args=(self.symbol,), daemon=True).start()

    def _switch_worker(self, sym):
        try:
            self.stream.set_symbol(sym)
        except Exception as exc:
            self.report_exception("MARKET FEED", exc)

    def toggle_voice(self):
        self.voice.set_enabled(not self.voice.enabled)
        self.status("Voice %s. %s" % ("ON" if self.voice.enabled else "OFF", self.voice.status()))
        if self.voice.enabled:
            self.voice.say("Voice on.", True)

    def toggle_trailing(self):
        new = not self.broker.trailing
        for book, _ in self._books():
            book.set_trailing(new)
        self.alert("Trailing stop lock %s." % ("on" if self.broker.trailing else "off"), True, False)

    def toggle_copilot(self, force=False):
        self.copilot_on = True if force else not self.copilot_on
        box, col = self.copilot_box, self._copilot_parent
        if self.copilot_on:
            if box.parent is None:
                col.add_widget(box, index=1)          # just above the panic button
            self.goto("hud")
            lines, spoken = self._copilot()
            self.lbl_copilot.text = "\n".join(esc(x) for x in lines)
            self.voice.say(spoken, True)
        elif box.parent is not None:
            col.remove_widget(box)

    def _copilot(self):
        return copilot_report(self.decision, self.tick_info().get("depth"), self.broker.positions(),
                              self.risk.status(), self.price, self.source)

    def _refresh_copilot(self):
        lines, _spoken = self._copilot()
        self.lbl_copilot.text = "\n".join(esc(x) for x in lines)

    # ---- arm / disarm / kill ----
    def toggle_arm(self):
        if self.risk.is_killed():
            self.arm()
        else:
            self.disarm()

    def disarm(self):
        for r in self._all_risks():
            r.kill("manual disarm")
        self.alert("Disarmed. New trades are locked until you arm again. Open positions keep their stops.", True, True)

    def arm(self, confirmed=False):
        if self.app_locked:
            return self.alert("The app is locked. Enter your PIN first.", True, True)
        if self.tamper:
            return self.alert("Integrity check failed, so trading cannot be armed. Reinstall the app you built yourself.", True, True)
        if not confirmed:
            return self.require_pin("arm trading", lambda: self.arm(True))
        for r in self._all_risks():
            r.rearm()
        if self.risk.is_locked():
            self.alert("Armed, but trading stays locked today after the loss limit.", True, True)
        else:
            self.alert("Armed in %s mode, inside the 1 percent rule. Live orders still need UNLOCK LIVE." % self.mode, True, True)

    def kill_switch(self):
        """Lock the backend first, then close everything (same as panic)."""
        self.panic()

    def panic(self):
        if self.busy["panic"]:
            return
        self.busy["panic"] = True
        for r in self._all_risks():
            r.kill("panic square-off")                     # backend lock first, instantly
        self.alert("Panic square off. Trading is locked. Closing every open position now.", True, True)
        threading.Thread(target=self._panic_worker, daemon=True).start()

    def _panic_worker(self):
        try:
            closed_all, errors_all, extra_all = [], [], 0
            for book, risk in self._books():
                real = not isinstance(book, PaperBroker)
                if real and self.mode != "LIVE" and not book.positions():
                    continue                                  # PAPER mode never touches a real account
                if real and not book.positions() and book is not self.live_broker:
                    continue                                  # the other market's real account has nothing to close
                if real and not book.has_session():
                    errors_all.append("no %s broker session" % book.name)
                    continue
                closed, errors, extra = book.square_off_all("PANIC SQUARE-OFF")
                for t in closed:
                    risk.record_result(t["pnl"])
                closed_all += closed
                errors_all += errors
                extra_all += extra
            if closed_all:
                Clock.schedule_once(lambda dt: self._after_close(closed_all), 0)
            if errors_all:
                self.alert("Could not close everything: %s. CHECK YOUR BROKER APP NOW." % "; ".join(errors_all)[:160], True, True)
            else:
                self.alert("Square off complete. %d tracked and %d other positions closed. Tap arm to trade again." % (
                    len(closed_all), extra_all), True, True)
        except Exception as exc:
            self.alert("Square off error: %s. CHECK YOUR BROKER APP." % str(exc)[:100], True, True)
        finally:
            self.busy["panic"] = False

    # ---- live orders ----
    def _block_reason(self, side):
        d = self.decision
        if self.app_locked:
            return "The app is locked. Enter your PIN first."
        if self.tamper:
            return "Integrity check failed (%s). Trading is off." % self.tamper[0][:80]
        if self.risk.is_killed():
            return "System is disarmed. Tap ARM first."
        if self.mode == "LIVE":
            if not self.live_unlocked:
                return "Live orders are locked. Tap the LIVE button or say enable real trading first."
            if not self.live_broker.has_session():
                return "Not logged in to %s today. Open CONFIG and connect." % self.live_broker.name
        why = self._data_problem()
        if why:
            return why
        if not self.session_open():
            if self.market == "FX":
                return "The forex market is closed for the weekend (Friday 21:00 to Sunday 22:00 UTC). No order."
            return "NSE is closed. Orders are possible only 09:15 to 15:30 India time."
        if d is None or not d.approved or d.symbol != self.symbol:
            return "There is no approved setup right now. Patience."
        if d.direction != side:
            return "The agents approve %s, not %s. No order." % (d.direction, side)
        if time.time() - d.ts > 45:
            return "That signal is stale. Scan again."
        if self.broker.positions():
            return "A position is already open. Close it first."
        ok, why = self.risk.can_trade(self.equity())
        if not ok:
            return why
        if self.busy["order"]:
            return "An order is already being sent."
        return ""

    def order_request(self, side, risk_pct=1.0):
        side = str(side).upper()
        if side not in (BUY, SELL):
            return
        try:
            risk_pct = min(1.0, max(0.01, float(risk_pct)))     # a request can ask for less than 1 percent, never more
        except (TypeError, ValueError):
            risk_pct = 1.0
        why = self._block_reason(side)
        if why:
            return self.alert(why)
        d = self.decision
        vpu, notional = self.broker.unit_economics(d.symbol, self.price)
        qty = self.risk.position_size(self.equity() * risk_pct, self.price, d.sl, self.cash(), self.broker.max_units,
                                      vpu, notional)
        if qty < 1:
            return self.alert("Capital is too small for a %g percent risk on this setup." % risk_pct)
        risk_amount = qty * abs(self.price - d.sl) * vpu
        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        live = self.mode == "LIVE"
        unit_word = "units" if self.market == "FX" else "shares"
        text = ("[b][color=%s]%s[/color][/b]\n%s %s  about %d %s at %s\nStop-loss %s   Target %s\n"
                "Invalidation: the idea is wrong beyond %s\n"
                "Money at risk %s (%g%% of the account, 1%% cap)\nBook: %s  |  %s  |  stop placed with the order\n\n"
                "%s This is a probability, not a promise: the trade can lose.") % (
            P["hx_danger"] if live else P["hx_a1"], "REAL MONEY ORDER" if live else "PAPER ORDER (virtual money)",
            d.direction, esc(d.symbol), qty, unit_word, px(self.price), px(d.sl), px(d.tp), px(d.sl),
            inr(risk_amount), risk_pct, self.live_broker.name if live else self.paper_broker.name,
            "Forex" if self.market == "FX" else "Intraday",
            "The order is sent immediately and cannot be undone except by closing the position." if live
            else "Filled at the live price with small slippage. No real money moves.")
        content.add_widget(AutoLabel(text=text, font_size=sp(14)))
        row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(52), spacing=dp(8))
        pop = Popup(title="CONFIRM LIVE ORDER" if live else "CONFIRM PAPER ORDER", content=content, size_hint=(0.94, None), height=dp(500),
                    auto_dismiss=False, background="", background_color=P["bg"], separator_color=P["a2"])
        yes = HudButton(text="CONFIRM " + side, accent=P["a2"], filled=True, height=52)
        yes.bind(on_release=lambda inst: (pop.dismiss(), self._start_order(d, risk_pct)))
        no = HudButton(text="CANCEL", accent=P["a1"], height=52)
        no.bind(on_release=lambda inst: pop.dismiss())
        row.add_widget(yes)
        row.add_widget(no)
        content.add_widget(row)
        pop.open()
        Clock.schedule_once(lambda dt: pop.dismiss(), 45)
        self.voice.say("%s order ready. Confirm on screen." % side.lower(), True)

    def _start_order(self, d, risk_pct=1.0):
        if time.time() - d.ts > 60:
            return self.alert("The signal expired while you were reading. Scan again.")
        now_d = self.decision
        if now_d is None or not now_d.approved or now_d.direction != d.direction or now_d.symbol != d.symbol:
            return self.alert("The signal changed while you were reading. Order cancelled.")
        why = self._block_reason(d.direction)
        if why:
            return self.alert(why)
        self.busy["order"] = True
        self.status("Sending %s order to %s ..." % (self.mode, self.broker.name))
        threading.Thread(target=self._order_worker, args=(d, self.price, risk_pct), daemon=True).start()

    def _order_worker(self, d, price, risk_pct=1.0):
        broker = self.broker
        try:
            live = self.mode == "LIVE"
            if not live:
                self.paper_broker.context = dict(getattr(d, "ctx", {}) or {})
            broker.size_scale = risk_pct
            pos = broker.open_trade(d.symbol, d.direction, price, d.sl, d.tp, self.risk)
            self.last_funds = 0.0
            self.alert("%s order filled. %s %d of %s at %s. Stop %s, target %s. Risk %s." % (
                "Live" if live else "Paper", pos["side"], pos["qty"], pos["symbol"], px(pos["entry"]), px(pos["sl"]),
                px(pos["tp"]), inr(pos["risk"])), True, True)
            self.decision = None
            if live:
                self.live_unlocked = False      # one filled order, then real trading locks again
        except BrokerError as exc:
            self.alert("Order not placed: %s" % exc, True, True)
        except Exception as exc:
            self.alert("Order error: %s" % redact(str(exc))[:120], True, True)
        finally:
            broker.size_scale = 1.0
            self.busy["order"] = False

    # ------------------------------------------------------------------ #
    # mode, PIN and security
    # ------------------------------------------------------------------ #
    def _install_stamp(self):
        """Changes whenever the APK is installed or updated, so a new build gets fresh integrity fingerprints."""
        if platform != "android":
            return "dev"
        try:
            from jnius import autoclass
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            info = activity.getPackageManager().getPackageInfo(activity.getPackageName(), 0)
            return str(int(info.lastUpdateTime))
        except Exception:
            return "unknown"

    def _startup_security(self):
        try:
            if self.tamper:
                self.report_error("INTEGRITY", "Integrity check failed: %s. Trading is off. Reinstall the APK you built yourself."
                                  % "; ".join(self.tamper)[:220])
            if self.guard_warning:
                self.report_error("INTEGRITY", self.guard_warning)
            if self.nse_live.positions() or self.fx_live.positions():
                self.alert("A LIVE position is still open. The app started in LIVE mode so its stop keeps being watched. "
                           "Check your broker app.", True, True)
            if self.app_locked:
                self._show_lock_screen()
        except Exception as exc:
            self.report_exception("APP LOCK", exc)

    def _pin_popup(self, title, text, on_ok, on_cancel=None, dismissable=True):
        content = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(12))
        content.add_widget(AutoLabel(text=text, font_size=sp(14)))
        pin = TextInput(hint_text="PIN", password=True, multiline=False, input_filter="int", font_size=sp(20),
                        size_hint_y=None, height=dp(52), background_color=P["panel"], foreground_color=P["text"],
                        hint_text_color=P["muted"], cursor_color=P["a1"], padding=(dp(10), dp(14)))
        msg = AutoLabel(text="", font_size=sp(12), color=P["danger"])
        content.add_widget(pin)
        content.add_widget(msg)
        row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(52), spacing=dp(8))
        pop = Popup(title=title, content=content, size_hint=(0.94, None), height=dp(330), auto_dismiss=False,
                    background="", background_color=P["bg"], separator_color=P["a2"])

        def submit(*args):
            ok, why = self.applock.verify(pin.text)
            pin.text = ""
            if ok:
                pop.dismiss()
                on_ok()
            else:
                msg.text = esc(why)
        pin.bind(on_text_validate=submit)
        yes = HudButton(text="OK", accent=P["a2"], filled=True, height=52)
        yes.bind(on_release=submit)
        row.add_widget(yes)
        if dismissable:
            no = HudButton(text="CANCEL", accent=P["a1"], height=52)
            no.bind(on_release=lambda inst: (pop.dismiss(), on_cancel() if on_cancel else None))
            row.add_widget(no)
        content.add_widget(row)
        pop.open()
        return pop

    def require_pin(self, action, callback):
        """Runs callback at once when no PIN is set, otherwise only after the right PIN."""
        if self.app_locked:
            return self.alert("The app is locked. Enter your PIN first.", True, True)
        if not self.applock.enabled:
            return callback()
        self._pin_popup("PIN NEEDED", "Enter your PIN to %s." % action, callback)

    def _show_lock_screen(self):
        if not self.applock.enabled or self.lock_popup is not None:
            return
        self.app_locked = True

        def unlocked():
            self.app_locked = False
            self.lock_popup = None
            self._refresh_all()
        self.lock_popup = self._pin_popup("TDN IS LOCKED", "[b]TDN is locked.[/b]\nEnter your PIN. Trading stays off until you do.",
                                          unlocked, dismissable=False)
        if self.applock.damaged:
            self.lock_popup.title = "LOCK FILE DAMAGED"

    def set_mode(self, name, confirmed=False):
        name = "LIVE" if str(name).upper() == "LIVE" else "PAPER"
        if name == self.mode:
            return self.status("Already in %s mode." % name)
        if self.app_locked:
            return self.alert("The app is locked. Enter your PIN first.", True, True)
        if self.paper_broker.positions() or self.live_broker.positions():
            return self.alert("Close the open position before changing mode.", True, True)
        if name == "LIVE":
            if self.tamper:
                return self.alert("Integrity check failed. LIVE mode is not available.", True, True)
            if not confirmed:
                return self.require_pin("switch to LIVE mode", lambda: self.set_mode("LIVE", True))
        self.mode = name
        self._select_books()
        self.live_unlocked = False
        self.voice_trade = None
        self.fxgate.clear()
        self.decision = None
        self.funds = None
        self.last_funds = 0.0
        self.alert("%s mode. %s" % (name, "Real orders need ARM, UNLOCK LIVE and CONFIRM." if name == "LIVE"
                                    else "Virtual money on live prices. Live orders are locked."), True, True)
        self._refresh_all()

    def toggle_mode(self):
        self.set_mode("LIVE" if self.mode == "PAPER" else "PAPER")

    def show_text_popup(self, title, text):
        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        sv = ScrollView(do_scroll_x=False)
        sv.add_widget(AutoLabel(text=esc(text), font_size=sp(12)))
        content.add_widget(sv)
        close = HudButton(text="CLOSE", accent=P["a1"], height=46)
        content.add_widget(close)
        pop = Popup(title=title, content=content, size_hint=(0.94, 0.8), background="", background_color=P["bg"],
                    separator_color=P["a2"])
        close.bind(on_release=lambda inst: pop.dismiss())
        pop.open()

    def paper_report(self):
        led = self.paper_broker.ledger()
        try:
            eq = "%s" % inr(self.paper_broker.funds()["equity"])
        except BrokerError as exc:
            eq = "unknown (%s)" % str(exc)[:60]
        lines = ["PAPER ACCOUNT (virtual money, live prices)",
                 "Starting capital %s  |  equity now %s" % (inr(self.paper_broker.capital), eq),
                 "Closed trades %d: %d wins, %d losses. Winnings %s, losses %s, net %s." % (
                     led["trades"], led["wins"], led["losses"], inr(led["profit"]), inr(led["loss"]), inr(led["net"])),
                 "", "WHAT THE APP LEARNED", self.learner.summary(), "",
                 "The learner only makes the filters stricter. It never changes the 1 percent risk, the 2 percent daily "
                 "limit or the stop-loss rules. Few trades means the numbers are noise, not proof.", "", DISCIPLINE]
        for t in reversed(self.paper_broker.trades(8)):
            lines.append("%s %s %s: %s (%s)" % (t["side"], t["symbol"], t.get("closed_at", "")[11:16], inr(t["pnl"]), t["reason"]))
        self.show_text_popup("PAPER REPORT", "\n".join(lines))

    # ------------------------------------------------------------------ #
    # live lock: real orders stay locked until the user unlocks them on purpose
    # ------------------------------------------------------------------ #
    LIVE_WINDOW = 15 * 60                      # an unlock lasts 15 minutes and ends after one filled order

    def toggle_live(self):
        if self.live_unlocked:
            self.lock_live("Live orders locked.")
        else:
            self.ask_unlock()

    def lock_live(self, message="Live orders locked."):
        self.live_unlocked = False
        self.voice_trade = None
        self.alert(message, True, True)
        self._refresh_all()

    def ask_unlock(self, then=None):
        if self.app_locked:
            return self.alert("The app is locked. Enter your PIN first.", True, True)
        if self.tamper:
            return self.alert("Integrity check failed. Live trading cannot be unlocked.", True, True)
        if self.mode != "LIVE":
            return self.alert("You are in PAPER mode. Tap MODE to switch to LIVE first.", True, True)
        if self.risk.is_killed():
            return self.alert("System is disarmed. Tap ARM first, then unlock live trading.", True, True)
        if self.risk.is_locked():
            return self.alert("Trading is locked for today after the loss limit. Live orders cannot be unlocked.", True, True)
        if not self.live_broker.has_session():
            return self.alert("Not logged in to %s today. Open CONFIG and connect first." % self.live_broker.name, True, True)
        why = self._data_problem()
        if why:
            return self.alert(why + " Real orders stay locked until live prices arrive.", True, True)
        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        content.add_widget(AutoLabel(
            text=("[b][color=%s]UNLOCK REAL-MONEY TRADING?[/color][/b]\nBroker: %s\nFor the next 15 minutes the BUY and SELL "
                  "buttons can send real orders to your account, and only when the agents approve a setup, with a "
                  "stop-loss, inside the 1 percent risk rule and after you press CONFIRM on the order popup.\n"
                  "Live orders lock again after one filled order.") % (P["hx_danger"], self.live_broker.name), font_size=sp(14)))
        row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(52), spacing=dp(8))
        pop = Popup(title="UNLOCK LIVE", content=content, size_hint=(0.94, None), height=dp(380), auto_dismiss=False,
                    background="", background_color=P["bg"], separator_color=P["danger"])

        def finish_unlock():
            self.live_unlocked = True
            self.live_until = time.time() + self.LIVE_WINDOW
            self.alert("Live orders unlocked for 15 minutes. Every order still needs your confirmation.", True, True)
            self._refresh_all()
            if then is not None:
                Clock.schedule_once(lambda dt: then(), 0.3)

        def do_unlock():
            pop.dismiss()
            self.require_pin("unlock live trading", finish_unlock)
        yes = HudButton(text="UNLOCK LIVE", accent=P["danger"], filled=True, height=52)
        yes.bind(on_release=lambda inst: do_unlock())
        no = HudButton(text="CANCEL", accent=P["a1"], height=52)
        no.bind(on_release=lambda inst: pop.dismiss())
        row.add_widget(yes)
        row.add_widget(no)
        content.add_widget(row)
        pop.open()
        Clock.schedule_once(lambda dt: pop.dismiss(), 40)
        self.voice.say("Unlock real money trading? Confirm on screen.", True)

    # ------------------------------------------------------------------ #
    # voice or mic triggered real-market trade: analyse, check every lock, then ask for the confirm tap
    # ------------------------------------------------------------------ #
    def voice_trade_request(self, text, side=None, risk_pct=1.0, from_fx=False):
        words = clean_text(text, 200).lower().split()
        if side is None:
            if any(w in ("buy", "long", "kharid") for w in words):
                side = BUY
            elif any(w in ("sell", "short", "bech", "becho") for w in words):
                side = SELL
        if self.app_locked:
            return self.alert("The app is locked. Enter your PIN first.", True, True)
        if self.mode == "LIVE" and not self.live_unlocked:
            self.voice.say("Real trading is locked. Unlock it first.", True)
            return self.ask_unlock(then=lambda: self.voice_trade_request(text, side, risk_pct, from_fx))
        if self.risk.is_killed():
            return self.alert("System is disarmed. No trade.", True, True)
        if self.mode == "LIVE" and not self.live_broker.has_session():
            return self.alert("Not logged in to %s. No trade." % self.live_broker.name, True, True)
        if self.broker.positions():
            return self.alert("A position is already open. Close it first.", True, True)
        if self.voice_trade is not None:
            return self.status("A trade analysis is already running.")
        self.voice_trade = {"t": time.time(), "side": side, "symbol": self.symbol, "risk": risk_pct, "fx": from_fx}
        self.last_eval = 0.0
        self.alert("Analysing %s with all 20 agents. If the setup is approved I will ask you to confirm on screen."
                   % self.symbol, True, True)

    def _voice_trade_continue(self, d, side, risk_pct=1.0, from_fx=False):
        if from_fx and not (side and self.fxgate.pending_valid(d.symbol, side)):
            return self.alert("The spoken forex request expired or no longer matches. Nothing was ordered.", True, True)
        if not d.approved:
            return self.alert("No trade on %s. %s" % (d.symbol, d.reason), True, True)
        if side and side != d.direction:
            return self.alert("You said %s but the agents approve %s. No order." % (side.lower(), d.direction.lower()), True, True)
        self.order_request(d.direction, risk_pct)

    # ------------------------------------------------------------------ #
    # spoken forex commands: parsed by voice_forex, they only REQUEST. Orders still need the confirm popup.
    # ------------------------------------------------------------------ #
    def run_fx_command(self, cmd):
        """Routes one parsed forex command. Safe by construction: it never arms, unlocks or switches to LIVE."""
        if cmd.action == "MARKET":
            return self.set_market(cmd.market)
        if self.market != "FX":
            if self.paper_broker.positions() or self.live_broker.positions():
                return self.alert("Close the open NSE position before using forex.", True, True)
            self.set_market("FX")
            if cmd.pair and self.market == "FX":
                self.set_symbol(cmd.pair)
            return self.alert("Forex market is on%s. Say the command again." % (
                (" with " + cmd.pair) if cmd.pair else ""), True, True)
        if cmd.action == "SELECT":
            self.set_symbol(cmd.pair)
            return self.voice.say(readback(cmd), True)
        if cmd.action in ("STATUS", "PRICE"):
            if cmd.pair and cmd.pair != self.symbol:
                self.set_symbol(cmd.pair)
                return self.alert("Showing %s. Ask again in a moment." % cmd.pair, True, True)
            if cmd.action == "STATUS":
                return self.status_report()
            if not self.price:
                return self.alert("No live %s price yet. Nothing is simulated." % self.symbol, True, True)
            spread = getattr(self.fx_live, "spread", 0.0) if self.source == "OANDA" else 0.0
            return self.alert("%s %s from %s%s. Forex is %s." % (
                self.symbol, px(self.price), self.source,
                (", spread %s" % px(spread)) if spread else "", "open" if fx_open() else "closed"), True, True)
        if cmd.action == "CLOSE":
            if cmd.pair and cmd.pair != self.symbol and not any(p["symbol"] == cmd.pair for p in self.broker.positions()):
                return self.alert("No open %s position. Nothing was closed." % cmd.pair, True, True)
            return self.close_request()
        if cmd.action == "TRADE":
            return self.fx_trade_request(cmd)

    def fx_trade_request(self, cmd):
        if self.app_locked:
            return self.alert("The app is locked. Enter your PIN first.", True, True)
        if cmd.pair != self.symbol:
            self.set_symbol(cmd.pair)             # pair-match guard: switch, then make the user say it again
            return self.alert("Switched to %s. Say your trade command again so I can check it." % cmd.pair, True, True)
        allowed, why = self.fxgate.begin(cmd)
        if not allowed:
            return self.alert(why, True, False)
        self.voice.say(readback(cmd), True)
        self.voice_trade_request("", cmd.side, cmd.risk_pct, True)
        if self.voice_trade is None:              # refused before the analysis started
            self.fxgate.clear()

    def flow_report(self):
        """Inflow and outflow ranking and correlation from the live collector. Read-only: it never trades."""
        snap = self.collector.latest(self.market)
        text = flow_report_text(snap, self.brain.describe(self.market))
        if snap is None:
            text += " Collector: %s." % self.collector.status()
        self.chat_add("TDN", text)
        self.voice.say(text[:260], True)

    def close_request(self):
        """Confirm popup to close the open position of the active book (a spoken close is only a request)."""
        pos = self.broker.positions()
        if not pos:
            return self.alert("No open position in this book.", True, True)
        p = pos[0]
        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        content.add_widget(AutoLabel(text="[b][color=%s]CLOSE THE POSITION?[/color][/b]\n%s %d %s at entry %s\n"
                                          "It is closed at the market price now." % (
                                              P["hx_danger"], p["side"], p["qty"], esc(p["symbol"]), px(p["entry"])),
                                     font_size=sp(14)))
        row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(52), spacing=dp(8))
        pop = Popup(title="CONFIRM CLOSE", content=content, size_hint=(0.94, None), height=dp(300), auto_dismiss=False,
                    background="", background_color=P["bg"], separator_color=P["a2"])
        yes = HudButton(text="CLOSE NOW", accent=P["danger"], filled=True, height=52)
        yes.bind(on_release=lambda inst: (pop.dismiss(), self._close_worker_start()))
        no = HudButton(text="CANCEL", accent=P["a1"], height=52)
        no.bind(on_release=lambda inst: pop.dismiss())
        row.add_widget(yes)
        row.add_widget(no)
        content.add_widget(row)
        pop.open()
        Clock.schedule_once(lambda dt: pop.dismiss(), 30)
        self.voice.say("Close the position? Confirm on screen.", True)

    def _close_worker_start(self):
        if self.busy["panic"]:
            return
        self.busy["panic"] = True
        book, risk = self.broker, self.risk
        threading.Thread(target=self._close_worker, args=(book, risk), daemon=True).start()

    def _close_worker(self, book, risk):
        try:
            closed, errors, _extra = book.square_off_all("CLOSED BY YOU")
            for t in closed:
                risk.record_result(t["pnl"])
            if closed:
                Clock.schedule_once(lambda dt: self._after_close(closed), 0)
            if errors:
                self.alert("Could not close: %s. CHECK YOUR BROKER APP." % "; ".join(errors)[:160], True, True)
        except Exception as exc:
            self.alert("Close error: %s. CHECK YOUR BROKER APP." % redact(str(exc))[:100], True, True)
        finally:
            self.busy["panic"] = False

    # ------------------------------------------------------------------ #
    # mentor: guidance, tracker, scanner, biodata, drawing guide
    # ------------------------------------------------------------------ #
    def _refresh_mentor(self, pos, st):
        if getattr(self, "live_until", 0) and self.live_unlocked and time.time() > self.live_until:
            self.live_unlocked = False
            self.status("Live orders locked again (15 minutes passed).")
        choppy = bool(self.candles) and chop_check(self.candles)["choppy"]
        d = self.decision if (self.decision is not None and self.decision.symbol == self.symbol) else None
        g = trade_guidance(d, st, choppy)
        col = P["hx_up"] if g["ok"] else P["hx_muted"]
        self.lbl_guidance.text = "[b][color=%s]%s[/color][/b]  %s" % (col, esc(g["verdict"]), esc(g["text"]))
        mine = [p for p in pos if p["symbol"] == self.symbol]
        p0 = mine[0] if mine else (pos[0] if pos else None)
        self.lbl_tracker.text = esc(tracker(p0, self.price if (p0 and p0["symbol"] == self.symbol) else 0.0,
                                            self.candles, st))
        if self.screen_name == "scan" and not self.scan_pick and time.time() - self.last_guide > 5.0 and self.candles:
            self.last_guide = time.time()
            self.lbl_draw.text = esc(drawing_guide(self.symbol, self.candles))

    def mentor_speak(self):
        i = int(time.time() // 60) % len(RULES)
        self.reply_local(RULES[i])

    def draw_speak(self):
        if not self.candles:
            return self.reply_local("No chart data yet.")
        text = drawing_guide(self.symbol, self.candles)
        self.lbl_draw.text = esc(text)
        self.voice.say(text.split("\n")[0], True)

    def _scan_feed(self):
        mode = self.ui["feed"]
        if mode in ("AUTO", "BROKER") and self.live_broker.has_session():
            return BrokerFeed(self.live_broker)
        if mode in ("AUTO", "YAHOO"):
            return YahooFxFeed() if self.market == "FX" else YahooFeed()
        return None

    def scan_watchlist(self, speak=False):
        if self.busy["scan"]:
            return self.status("The scanner is already running.")
        self.busy["scan"] = True
        self.status("Scanning the watchlist ...")
        if hasattr(self, "lbl_scan_status"):
            self.lbl_scan_status.text = "Scanning %d symbols ..." % len(self.watchlist)
        threading.Thread(target=self._scan_worker, args=(speak,), daemon=True).start()

    def _scan_worker(self, speak):
        rows, candles, bad = {}, {}, 0
        self.scan_first_error = ""
        label = "?"
        try:
            feed = self._scan_feed()
            if feed is None:
                label = "NO FEED"
                raise BrokerError("No live data feed is available. Connect a broker or set the feed to AUTO or YAHOO.")
            label = getattr(feed, "label", "FEED")
            for sym in list(self.watchlist):
                try:
                    cs = feed.bootstrap(sym)
                    row = scan_row(sym, cs)
                    if row is not None:
                        rows[sym] = row
                        candles[sym] = cs
                    else:
                        bad += 1
                except Exception as exc:
                    bad += 1
                    if not self.scan_first_error:
                        self.scan_first_error = "%s: %s" % (sym, redact(str(exc))[:80])
                time.sleep(0.35)
        except Exception as exc:
            self.report_exception("SCANNER", exc)
        self.busy["scan"] = False
        Clock.schedule_once(lambda dt: self._scan_done(rows, candles, bad, label, speak), 0)

    def _scan_done(self, rows, candles, bad, label, speak):
        self.scan_rows, self.scan_candles, self.scan_time = rows, candles, time.time()
        self.scan_label = label
        self._render_scan()
        n = len(rows)
        if bad and not n:
            self.report_error("SCANNER", "Scan failed: no stock could be read from %s. %s" % (
                label, ("Check the internet connection. " + (self.scan_first_error or "")).strip()))
        elif bad:
            self.report_error("SCANNER", "%d of %d stocks could not be read from %s. %s" % (
                bad, bad + n, label, self.scan_first_error or ""))
        self.status("Scan finished: %d stocks read from %s, %d without data." % (n, label, bad))
        if speak:
            if not rows:
                return self.voice.say("The scan found no usable data.", True)
            green, red = split_green_red(list(rows.values()))
            bg, br = best_of(green), best_of(red)
            parts = []
            parts.append(("Market achha chal raha hai in %s, rising %.1f percent." % (bg["symbol"], bg["day_pct"]))
                         if bg else "No clean rising market right now.")
            parts.append(("Best falling market is %s, down %.1f percent." % (br["symbol"], abs(br["day_pct"])))
                         if br else "No clean falling market.")
            chop = sum(1 for r in rows.values() if r["quality"] == CHOP)
            if chop:
                parts.append("%d stocks are choppy. Do not trade them." % chop)
            self.voice.say(" ".join(parts), True)

    def _render_scan(self):
        green, red = split_green_red(list(self.scan_rows.values()))
        lists = {"green": green, "red": red}
        for side, rows in lists.items():
            v = self.sv.get(side)
            if v is None:
                continue
            accent = P["up"] if side == "green" else P["down"]
            v["rows"].clear_widgets()
            best = best_of(rows)
            word = "RISING (GREEN CANDLES)" if side == "green" else "FALLING (RED CANDLES)"
            if not self.scan_rows:
                v["best"].text = "Tap SCAN WATCHLIST to read the market."
            elif best is not None:
                v["best"].text = ("[b][color=%s]MARKET ACHHA CHAL RAHA HAI[/color][/b]\nBest %s stock: [b]%s[/b], %+.2f%%, "
                                  "momentum %.0f, volume %.1fx. Clean trend." % (
                                      P["hx_up"], "rising" if side == "green" else "falling", esc(best["symbol"]),
                                      best["day_pct"], best["score"], best["vol_ratio"]))
            elif rows:
                v["best"].text = ("[b][color=%s]NO CLEAN MARKET IN THIS VIEW[/color][/b]\nAll %s stocks are weak or choppy. "
                                  "WAIT, DO NOT TRADE." % (P["hx_danger"], "rising" if side == "green" else "falling"))
            else:
                v["best"].text = "No %s stocks right now." % ("rising" if side == "green" else "falling")
            if not rows:
                v["rows"].add_widget(AutoLabel(text="none", font_size=sp(12), color=P["muted"]))
            for r in rows:
                tag = {"GOOD": "ACHHA CHAL RAHA", "WEAK": "WEAK / WAIT", "CHOP": "CHOP - DO NOT TRADE"}[r["quality"]]
                b = HudButton(text="%s  %+.2f%%  RSI %.0f\n%s" % (r["symbol"], r["day_pct"], r["rsi"], tag),
                              accent=accent if r["quality"] == GOOD else P["muted"], filled=(r["quality"] == GOOD),
                              height=54)
                b.bind(on_release=lambda inst, s=r["symbol"]: self.scan_pick_symbol(s))
                v["rows"].add_widget(b)
            pick = best["symbol"] if best is not None else (rows[0]["symbol"] if rows else "")
            if pick and (self.scan_pick not in [r["symbol"] for r in rows]):
                self._show_pick(side, pick, speak=False)
            elif not rows:
                v["chart"].update([], [])
                v["cap"].text = ""
        age = int(time.time() - self.scan_time) if self.scan_time else 0
        self.lbl_scan_status.text = "Source %s, %d stocks, %ds ago. Tap a stock for its chart and biodata." % (
            getattr(self, "scan_label", "?"), len(self.scan_rows), age)
        self.btn_view_green.text = "GREEN / RISING (%d)" % len(green)
        self.btn_view_red.text = "RED / FALLING (%d)" % len(red)

    def _show_pick(self, side, sym, speak=True):
        cs = self.scan_candles.get(sym)
        row = self.scan_rows.get(sym)
        v = self.sv.get(side)
        if not cs or not row or v is None:
            return self.status("No data for %s." % sym)
        self.scan_pick = sym
        v["chart"].update(cs, [])
        v["cap"].text = esc("%s  |  %s\n%s  |  %d green and %d red candles of the last 20" % (
            sym, QUALITY_TEXT[row["quality"]], row["why"], row["green_candles"], row["red_candles"]))
        d = self.decision if (self.decision is not None and self.decision.symbol == sym) else None
        self.lbl_biodata.text = esc(biodata(sym, cs, d))
        self.lbl_draw.text = esc(drawing_guide(sym, cs))
        if speak:
            self.voice.say("%s. %s" % (spoken_biodata(sym, cs), "Do not trade, it is choppy." if row["quality"] == CHOP
                                        else "Market is good." if row["quality"] == GOOD else "Wait for a cleaner setup."), True)

    def scan_pick_symbol(self, sym):
        row = self.scan_rows.get(sym)
        if row is None:
            return self.status("No data for %s." % sym)
        self._show_pick(row["side"], sym, speak=True)

    def set_scan_view(self, side):
        if side not in ("green", "red") or "green" not in self.sv:
            return
        self.scan_view = side
        self.scan_holder.clear_widgets()
        self.scan_holder.add_widget(self.sv[side]["frame"])
        self.btn_view_green.set_accent(P["up"], filled=(side == "green"))
        self.btn_view_red.set_accent(P["down"], filled=(side == "red"))
        rows = [r for r in self.scan_rows.values() if r["side"] == side]
        if rows and self.scan_pick not in [r["symbol"] for r in rows]:
            rows.sort(key=lambda r: ({"GOOD": 0, "WEAK": 1, "CHOP": 2}[r["quality"]], -r["strength"]))
            self._show_pick(side, rows[0]["symbol"], speak=False)

    def open_picked(self):
        sym = self.scan_pick
        if sym not in self.watchlist:
            return self.status("Tap a symbol in the lists first.")
        self.set_symbol(sym)
        self.goto("hud")

    # ------------------------------------------------------------------ #
    # screens: SCAN and REPLY
    # ------------------------------------------------------------------ #
    def _build_scan_view(self, side):
        """One of the two separate views. Green holds only rising stocks, red only falling stocks."""
        green = side == "green"
        accent = P["up"] if green else P["down"]
        fr = HudFrame(accent=accent, scan=True)
        fr.add_widget(heading("GREEN CANDLE VIEW: RISING MARKETS" if green else "RED CANDLE VIEW: FALLING MARKETS", accent))
        best = AutoLabel(text="Tap SCAN WATCHLIST to read the market.", font_size=sp(13))
        fr.add_widget(best)
        chart = HudChart(height=220)
        fr.add_widget(chart)
        cap = AutoLabel(text="", font_size=sp(12), color=P["muted"])
        fr.add_widget(cap)
        rows = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        rows.bind(minimum_height=rows.setter("height"))
        fr.add_widget(rows)
        self.sv[side] = {"frame": fr, "best": best, "chart": chart, "cap": cap, "rows": rows}

    def _screen_scan(self):
        sv, col = scroll_column(spacing=8)
        self.sv = {}
        top = HudFrame(accent=P["a2"], scan=True)
        top.add_widget(heading("WATCHLIST SCANNER  |  MOMENTUM FILTER", P["a2"]))
        self.lbl_scan_status = AutoLabel(text="Tap SCAN WATCHLIST. Rising and falling markets are shown in two separate views.",
                                         font_size=sp(12), color=P["muted"])
        top.add_widget(self.lbl_scan_status)
        go = HudButton(text="SCAN WATCHLIST", accent=P["a2"], filled=True, height=48)
        go.bind(on_release=lambda inst: self.scan_watchlist(False))
        top.add_widget(go)
        row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(48), spacing=dp(8))
        self.btn_view_green = HudButton(text="GREEN / RISING (0)", accent=P["up"], filled=True, height=48)
        self.btn_view_green.bind(on_release=lambda inst: self.set_scan_view("green"))
        self.btn_view_red = HudButton(text="RED / FALLING (0)", accent=P["down"], height=48)
        self.btn_view_red.bind(on_release=lambda inst: self.set_scan_view("red"))
        row.add_widget(self.btn_view_green)
        row.add_widget(self.btn_view_red)
        top.add_widget(row)
        col.add_widget(top)
        self._build_scan_view("green")
        self._build_scan_view("red")
        self.scan_holder = BoxLayout(orientation="vertical", size_hint_y=None)
        self.scan_holder.bind(minimum_height=self.scan_holder.setter("height"))
        col.add_widget(self.scan_holder)
        bio = HudFrame(accent=P["a1"])
        bio.add_widget(heading("STOCK BIODATA  (tap a stock in the list)"))
        self.lbl_biodata = AutoLabel(text="Nothing selected.", font_size=sp(12))
        bio.add_widget(self.lbl_biodata)
        ob = HudButton(text="OPEN THIS STOCK ON THE HUD", accent=P["a1"], height=44)
        ob.bind(on_release=lambda inst: self.open_picked())
        bio.add_widget(ob)
        col.add_widget(bio)
        dr = HudFrame(accent=P["a2"])
        dr.add_widget(heading("CHART DRAWING GUIDE  |  WHERE TO DRAW AND WHERE NOT TO", P["a2"]))
        self.lbl_draw = AutoLabel(text="Chart data is loading ...", font_size=sp(12))
        dr.add_widget(self.lbl_draw)
        col.add_widget(dr)
        mn = HudFrame(accent=P["a1"])
        mn.add_widget(heading("MENTOR RULES"))
        mn.add_widget(AutoLabel(text="\n".join(esc(x) for x in RULES), font_size=sp(12)))
        col.add_widget(mn)
        self.set_scan_view(self.scan_view)
        return sv

    def _screen_reply(self):
        sv, col = scroll_column(spacing=8)
        box = HudFrame(accent=P["a1"], scan=True)
        box.add_widget(heading("AI MESSAGE AND TELECALLING REPLY AGENT"))
        box.add_widget(AutoLabel(text="Paste a message or question you received. I write the reply or call script. "
                                      "I never send anything: you copy it and send it yourself.",
                                 font_size=sp(12), color=P["muted"]))
        self.in_reply = TextInput(text="", hint_text="paste the incoming message or question here", multiline=True,
                                  size_hint_y=None, height=dp(130), font_size=sp(13), background_color=P["panel"],
                                  foreground_color=P["text"], hint_text_color=P["muted"], cursor_color=P["a1"],
                                  padding=(dp(8), dp(8)))
        box.add_widget(self.in_reply)
        self.in_note = TextInput(text="", hint_text="facts to use (optional): price, date, name ...", multiline=False,
                                 size_hint_y=None, height=dp(44), font_size=sp(13), background_color=P["panel"],
                                 foreground_color=P["text"], hint_text_color=P["muted"], cursor_color=P["a1"],
                                 padding=(dp(8), dp(12)))
        box.add_widget(self.in_note)
        self.sp_mode = Spinner(text=MODES[0], values=MODES, size_hint_y=None, height=dp(44), font_size=sp(13),
                               background_normal="", background_color=P["a1"], color=(0.01, 0.01, 0.02, 1))
        self.sp_tone = Spinner(text=TONES[0], values=TONES, size_hint_y=None, height=dp(44), font_size=sp(13),
                               background_normal="", background_color=P["a2"], color=(0.01, 0.01, 0.02, 1))
        self.sp_lang = Spinner(text=LANGUAGES[0], values=LANGUAGES, size_hint_y=None, height=dp(44), font_size=sp(13),
                               background_normal="", background_color=P["a1"], color=(0.01, 0.01, 0.02, 1))
        for w in (self.sp_mode, self.sp_tone, self.sp_lang):
            box.add_widget(w)
        gen = HudButton(text="WRITE THE REPLY", accent=P["a2"], filled=True, height=50)
        gen.bind(on_release=lambda inst: self.reply_generate())
        box.add_widget(gen)
        col.add_widget(box)
        out = HudFrame(accent=P["a2"])
        out.add_widget(heading("READY-TO-SEND TEXT  (touch and hold the text to select it)", P["a2"]))
        self.out_reply = TextInput(text="", readonly=True, multiline=True, size_hint_y=None, height=dp(240),
                                   font_size=sp(13), background_color=P["panel"], foreground_color=P["text"],
                                   cursor_color=P["a1"], padding=(dp(8), dp(8)))
        out.add_widget(self.out_reply)
        self.lbl_reply_status = AutoLabel(text="", font_size=sp(12), color=P["muted"])
        out.add_widget(self.lbl_reply_status)
        row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(48), spacing=dp(8))
        for text, fn in (("COPY", self.reply_copy), ("SPEAK", self.reply_speak), ("CLEAR", self.reply_clear)):
            b = HudButton(text=text, accent=P["a1"], height=48)
            b.bind(on_release=lambda inst, f=fn: f())
            row.add_widget(b)
        out.add_widget(row)
        col.add_widget(out)
        return sv

    def reply_generate(self):
        if self.busy["reply"]:
            return self.status("Still writing the last reply.")
        incoming = self.in_reply.text
        if not clean_text(incoming, 50):
            return self.alert("Paste the message or question first.", False)
        self.busy["reply"] = True
        self.lbl_reply_status.text = "Writing ..."
        args = (incoming, self.sp_mode.text, self.sp_tone.text, self.sp_lang.text, self.in_note.text)
        threading.Thread(target=self._reply_worker, args=args, daemon=True).start()

    def _reply_worker(self, incoming, mode, tone, lang, note):
        try:
            text = self.reply_agent.generate(incoming, mode, tone, lang, note)
            source = self.reply_agent.last_source
        except ValueError as exc:
            text, source = "", str(exc)
        except Exception as exc:
            text, source = "", "Reply error: %s" % redact(str(exc))[:100]
            self.report_exception("REPLY", exc)
        self.busy["reply"] = False

        def apply(dt):
            self.reply_text = redact(text)
            self.out_reply.text = self.reply_text
            self.lbl_reply_status.text = esc(("Written by %s." % ("OpenAI" if source == "AI" else "a template"))
                                             if text else source)
            if text and source != "AI" and "OpenAI problem" in text:
                self.report_error("OPENAI", text.split("(OpenAI problem:")[-1].strip(" )"))
        Clock.schedule_once(apply, 0)

    def reply_copy(self):
        if not self.reply_text:
            return self.status("Nothing to copy yet.")
        try:
            from kivy.core.clipboard import Clipboard
            Clipboard.copy(self.reply_text)
            self.status("Reply copied. Paste it into WhatsApp, SMS or email.")
        except Exception as exc:
            self.report_error("REPLY", "Copy failed (%s). Touch and hold the text to select it." % str(exc)[:50])

    def reply_speak(self):
        if self.reply_text:
            self.voice.say(self.reply_text[:600], True)

    def reply_clear(self):
        self.in_reply.text = ""
        self.in_note.text = ""
        self.out_reply.text = ""
        self.reply_text = ""
        self.lbl_reply_status.text = ""

    # ------------------------------------------------------------------ #
    # settings actions
    # ------------------------------------------------------------------ #
    def _set_theme(self, name):
        if name == self.ui["theme"] or name not in THEMES:
            return
        self.ui["theme"] = name
        self._save_ui()
        apply_theme(name)
        Window.clearcolor = P["bg"]
        self.copilot_on = False
        Clock.schedule_once(lambda dt: self._rebuild(), 0)

    def _rebuild(self):
        self._build_ui()
        if self.decision is not None:
            self._show_decision(self.decision, speak=False)
        self.status("Theme: %s" % self.ui["theme"])

    def _toggle_anim(self, btn):
        self.ui["anim"] = not self.ui["anim"]
        ANIM.enabled = bool(self.ui["anim"])
        btn.text = "ANIMATIONS: %s" % ("ON" if self.ui["anim"] else "OFF")
        self._save_ui()

    def _set_feed(self, name):
        if name == self.ui["feed"] or name not in FEEDS:
            return
        self.ui["feed"] = name
        self._save_ui()
        self.reload_feed()
        self.status("Data feed: %s" % name)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def on_pause(self):
        self.paused_at = time.time()
        try:
            self.collector.pause()                      # Android may stop background work anyway
        except Exception:
            pass
        return True

    def on_resume(self):
        try:
            self.collector.resume()
        except Exception:
            pass
        try:
            if self.applock.enabled and self.paused_at and time.time() - self.paused_at > 60:
                self.app_locked = True
                self._show_lock_screen()
        except Exception as exc:
            self.report_exception("APP LOCK", exc)

    def on_stop(self):
        for fn in (getattr(self, "stream", None) and self.stream.stop, getattr(self, "ticks", None) and self.ticks.stop,
                   getattr(self, "collector", None) and self.collector.stop,
                   getattr(self, "brain", None) and self.brain.close,
                   ANIM.stop, getattr(self, "mic", None) and self.mic.shutdown):
            try:
                if fn:
                    fn()
            except Exception:
                pass


if __name__ == "__main__":
    TDNApp().run()
