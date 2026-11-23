# -*- coding: utf-8 -*-
"""voice_mic.py - push-to-talk / tap-to-talk voice commands for Tactical Neural Matrix.

Two ways to turn speech into text, chosen by VoiceMic.prefer ("AUTO", "ANDROID" or "OPENAI"):

  OPENAI  - the sentence is recorded with MediaRecorder and sent to OpenAI for transcription. It needs no Android
            speech service, so it works on every phone. AUTO uses it whenever an OpenAI key is saved.
  ANDROID - the phone's own SpeechRecognizer, driven through pyjnius. It must be created and used on the Android
            UI thread, so every call goes through android.runnable.run_on_ui_thread.

How the button works
  * HOLD the mic, speak, release: the sentence is sent when you let go.
  * TAP the mic once (short press): it keeps listening, stops by itself after you finish speaking, or tap again.
  Releasing early never throws the sentence away: a recording is always kept for at least 1.2 seconds.

All callbacks (state, level, result, error) may arrive on a background thread. The caller must move them onto the
Kivy thread (main.py does this with Clock.schedule_once).

parse_command() turns text into an intent. It never raises and works with typed text too.
Intents: SCAN, STATUS, KILL, REARM, ARM, DISARM, PANIC, COPILOT, TRAIL, BUY, SELL, AGENT, VOICE_TEST, MUTE, UNMUTE,
HELP, UNKNOWN.
"""
import os
import re
import tempfile
import threading
import time

try:
    from kivy.utils import platform as _platform
except Exception:
    _platform = "linux"

# (intent, phrases). The first group that matches wins, so order matters: most specific first.
_INTENTS = [
    ("PANIC", ["panic", "square off", "square-off", "squareoff", "close all", "exit all", "sab band", "sab bech"]),
    ("KILL", ["kill switch", "activate kill", "kill the", "stop trading", "trading band", "kill"]),
    ("REARM", ["re arm", "rearm", "re-arm", "release kill", "resume trading"]),
    ("DISARM", ["disarm", "dis arm", "safe mode", "weapons down"]),
    ("UNLOCK", ["enable real trading", "unlock live", "unlock trading", "real money mode", "go live", "live mode on",
                "enable live trading", "real trading on"]),
    ("LOCK", ["lock live", "lock trading", "live mode off", "disable live trading", "real trading off", "paper mode"]),
    ("ARM", ["arm the", "arm system", "arm trading", "arm"]),
    ("TRAIL", ["trail", "trailing", "lock stop", "stop loss lock", "lock profit", "tsl"]),
    ("COPILOT", ["co pilot", "copilot", "co-pilot", "ai pilot", "advice", "what should i do"]),
    ("STATUS", ["status", "report", "how are we", "kaisa chal", "summary", "position"]),
    ("VOICE_TEST", ["voice test", "test voice", "test the voice", "mic check"]),
    ("MUTE", ["mute", "be quiet", "silence", "chup"]),
    ("UNMUTE", ["unmute", "speak again", "voice on"]),
    ("HELP", ["help", "commands", "what can you do"]),
    ("WATCHLIST", ["scan all", "scan watchlist", "scan the watchlist", "scanner", "top stocks", "gainers", "losers",
                   "green and red"]),
    ("MENTOR", ["teach me", "mentor", "trading rules", "lesson", "rules"]),
    ("DRAW", ["where to draw", "drawing guide", "draw lines", "support and resistance", "where not to draw"]),
    ("TRADE", ["place a real trade", "place real trade", "real market trade", "place a trade", "place trade",
               "take a trade", "take trade", "trade now", "real trade", "execute trade", "place a bet", "bet now",
               "trade karo", "trade lagao", "trade with real money", "trade"]),
    ("SCAN", ["scan", "analyse", "analyze", "market check", "check market", "check the market", "dekho", "market dekho"]),
    ("BUY", ["buy", "go long", "long", "kharid"]),
    ("SELL", ["sell", "go short", "short", "becho", "bech"]),
]

# spoken agent names -> agent display names (resolved by the caller through its own list too)
_AGENT_WORDS = {
    "structure": "Market Structure", "sweep": "Liquidity Sweep", "liquidity": "Liquidity Sweep",
    "choch": "Choch", "change of character": "Choch", "trend": "Trend EMA", "ema": "Trend EMA",
    "rsi": "Momentum RSI", "momentum": "Momentum RSI", "volume": "Volume Flow", "flow": "Volume Flow",
    "support": "Support/Resistance", "resistance": "Support/Resistance", "volatility": "Volatility Guard",
    "fakeout": "Fakeout Shield", "trap": "Fakeout Shield",
}

# a sentence that starts like this is a question for the AI chat, not a command
_QUESTION_WORDS = ("should", "what", "why", "how", "when", "where", "who", "which", "can", "could", "would", "is",
                   "are", "do", "does", "did", "tell", "explain", "kya", "kyun", "kaise", "kab", "kitna", "batao",
                   "samjhao", "please explain")

TRADING_PROMPT = ("Trading voice commands: scan market, status report, activate kill switch, panic square off, buy, "
                  "sell, trail stop, co pilot. NSE stocks: SBIN, Reliance, TCS, Infosys, HDFC Bank, ICICI Bank.")

HELP_TEXT = ("Say: scan market, scan all stocks, status report, activate kill switch, disarm, buy, sell, trail stop, "
             "co pilot, panic square off, enable real trading, place a real trade, where to draw, trading rules, or ask "
             "about an agent, for example volume flow agent. You can also just chat with me.")


def _clean(text, limit=400):
    out = []
    for ch in str(text or ""):
        if ch in "\r\n\t":
            out.append(" ")
        elif ch.isprintable() and ch not in "\u200b\u200c\u200d\u2060\ufeff\u00ad\u202e\u202d":
            out.append(ch)
    return " ".join("".join(out).split())[:limit]


def parse_command(text, agent_names=None):
    """Return (intent, argument). Never raises."""
    t = " ".join(re.sub(r"[^a-z0-9\u0900-\u097f/\- ]", " ", _clean(text).lower()).split())
    if not t:
        return "UNKNOWN", ""
    # An agent query: "what is the RSI agent saying", "explain volume flow", "agent trend"
    if "agent" in t or t.startswith(("explain", "tell me about", "what is", "what does", "inspect")):
        names = list(agent_names or [])
        for name in names:
            if name.lower() in t:
                return "AGENT", name
        for word, name in _AGENT_WORDS.items():
            if word in t and (not names or name in names):
                return "AGENT", name
    for intent, phrases in _INTENTS:
        for p in phrases:
            if re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", t):
                return intent, t
    return "UNKNOWN", t


def is_command(text, agent_names=None):
    """True when the text is meant as a command for the app. Questions and long sentences go to the AI chat, so
    'should I buy now?' starts a conversation and never an order."""
    intent, arg = parse_command(text, agent_names)
    if intent == "UNKNOWN":
        return False
    words = _clean(text).lower().split()
    if intent in ("AGENT", "DRAW", "MENTOR"):
        return True
    limit = 9 if intent in ("TRADE", "UNLOCK", "LOCK", "WATCHLIST", "DRAW") else 6
    if not words or len(words) > limit:
        return False
    if words[0] in _QUESTION_WORDS or words[0].rstrip("?") in _QUESTION_WORDS:
        return False
    return not _clean(text).endswith("?")


def _jstr(obj):
    """Text of a Java string that pyjnius may hand over as a Python str or as a Java object."""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    try:
        return str(obj.toString())
    except Exception:
        return str(obj)


class VoiceMic:
    """Speech to text.

    on_state(name)      name in 'idle', 'listening', 'processing'
    on_level(0..1)      loudness while the user is speaking
    on_result(text)     final recognised sentence
    on_error(message)   short human readable problem
    """
    MIN_RECORD = 1.2          # seconds a recording is kept at least
    MAX_RECORD = 20.0
    TAP_SECONDS = 0.45        # a press shorter than this is a "tap"
    SPEECH_LEVEL = 2200       # MediaRecorder amplitude (0..32767) that counts as speech
    QUIET_LEVEL = 1300
    SILENCE_END = 1.4         # seconds of quiet after speech that end a tap-recording
    NO_SPEECH_END = 8.0

    def __init__(self, on_state=None, on_level=None, on_result=None, on_error=None, language="en-IN",
                 openai=None, data_dir=None):
        self.on_state = on_state or (lambda s: None)
        self.on_level = on_level or (lambda v: None)
        self.on_result = on_result or (lambda t: None)
        self.on_error = on_error or (lambda m: None)
        self.language = language
        self.openai = openai                  # voice_engine.OpenAIClient or None
        self.prefer = "AUTO"                  # "AUTO", "ANDROID" or "OPENAI"
        self.available = False                # the phone's SpeechRecognizer is created
        self.listening = False
        self._data_dir = data_dir or tempfile.gettempdir()
        self._lock = threading.RLock()
        self._engine = ""                     # "openai" or "android" while listening
        self._press_t = 0.0
        self._tap_mode = False
        self._swallow_release = False
        # Android recognizer
        self._rec = None
        self._listener = None
        self._intent = None
        self._native = None
        self._speech_began = False
        self._peak_rms = -2.0                 # loudest level the phone recognizer measured in this try
        self._partial = ""
        # recorder
        self._recorder = None
        self._rec_started = 0.0
        self._rec_path = os.path.join(self._data_dir, "command.m4a")
        self._max_amp = 0
        if _platform == "android":
            try:
                self._build_native()
            except Exception as exc:
                self._native = None
                self.available = False
                self.on_error("Phone speech service setup failed: %s" % str(exc)[:80])

    # ------------------------------------------------------------------ #
    # Android SpeechRecognizer (runs on the UI thread)
    # ------------------------------------------------------------------ #
    def _build_native(self):
        from jnius import PythonJavaClass, java_method, autoclass
        from android.runnable import run_on_ui_thread

        outer = self

        class Listener(PythonJavaClass):
            __javainterfaces__ = ["android/speech/RecognitionListener"]
            __javacontext__ = "app"

            @java_method("(Landroid/os/Bundle;)V")
            def onReadyForSpeech(self, params):
                outer.on_state("listening")

            @java_method("()V")
            def onBeginningOfSpeech(self):
                outer._speech_began = True

            @java_method("(F)V")
            def onRmsChanged(self, rms):
                try:
                    outer._peak_rms = max(outer._peak_rms, float(rms))
                    outer.on_level(max(0.0, min(1.0, (float(rms) + 2.0) / 12.0)))
                except Exception:
                    pass

            @java_method("([B)V")
            def onBufferReceived(self, buf):
                pass

            @java_method("()V")
            def onEndOfSpeech(self):
                outer.on_state("processing")

            @java_method("(I)V")
            def onError(self, code):
                code = int(code)
                partial = outer._partial
                heard = outer._speech_began or outer._peak_rms >= 2.0
                outer._finish()
                if partial and code in (6, 7):
                    outer.on_result(partial)             # the phone heard something before it gave up
                else:
                    outer.on_error(outer._error_text(code, heard))

            @java_method("(Landroid/os/Bundle;)V")
            def onResults(self, results):
                text = ""
                try:
                    key = autoclass("android.speech.SpeechRecognizer").RESULTS_RECOGNITION
                    arr = results.getStringArrayList(key)
                    if arr is not None and arr.size() > 0:
                        text = _clean(_jstr(arr.get(0)))
                except Exception:
                    text = ""
                text = text or outer._partial
                heard = outer._speech_began or outer._peak_rms >= 2.0
                outer._finish()
                if text:
                    outer.on_result(text)
                else:
                    outer.on_error(outer._error_text(7, heard))

            @java_method("(Landroid/os/Bundle;)V")
            def onPartialResults(self, partial):
                try:
                    key = autoclass("android.speech.SpeechRecognizer").RESULTS_RECOGNITION
                    arr = partial.getStringArrayList(key)
                    if arr is not None and arr.size() > 0:
                        outer._partial = _clean(_jstr(arr.get(0)))
                except Exception:
                    pass

            @java_method("(ILandroid/os/Bundle;)V")
            def onEvent(self, event_type, params):
                pass

        self._Listener = Listener

        @run_on_ui_thread
        def _create():
            try:
                activity = autoclass("org.kivy.android.PythonActivity").mActivity
                SR = autoclass("android.speech.SpeechRecognizer")
                # On Android 11+ isRecognitionAvailable() can say False for apps without package-visibility
                # entries even though recognition works, so it is only a hint and never blocks the attempt.
                try:
                    hint_ok = bool(SR.isRecognitionAvailable(activity))
                except Exception:
                    hint_ok = True
                self._rec = SR.createSpeechRecognizer(activity)
                self._listener = Listener()
                self._rec.setRecognitionListener(self._listener)
                Intent = autoclass("android.content.Intent")
                RS = autoclass("android.speech.RecognizerIntent")
                intent = Intent(RS.ACTION_RECOGNIZE_SPEECH)
                intent.putExtra(RS.EXTRA_LANGUAGE_MODEL, RS.LANGUAGE_MODEL_FREE_FORM)
                intent.putExtra(RS.EXTRA_LANGUAGE, self.language)
                intent.putExtra(RS.EXTRA_PARTIAL_RESULTS, True)
                intent.putExtra(RS.EXTRA_MAX_RESULTS, 3)
                intent.putExtra("android.speech.extra.SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS", 1500)
                intent.putExtra("android.speech.extra.SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS", 1500)
                self._intent = intent
                self.available = self._rec is not None
                if not hint_ok:
                    self._hint_missing = True
            except Exception as exc:
                self.available = False
                self.on_error("Phone speech service error: %s" % str(exc)[:80])

        @run_on_ui_thread
        def _start():
            try:
                self._rec.startListening(self._intent)
            except Exception as exc:
                self._finish()
                self.on_error("Could not start listening: %s" % str(exc)[:80])

        @run_on_ui_thread
        def _stop():
            try:
                self._rec.stopListening()
            except Exception:
                pass

        @run_on_ui_thread
        def _cancel():
            try:
                self._rec.cancel()
            except Exception:
                pass

        self._native = {"create": _create, "start": _start, "stop": _stop, "cancel": _cancel}
        _create()

    @staticmethod
    def _error_text(code, heard=True):
        """Plain words for a phone recognizer error. heard tells whether any sound reached the recognizer."""
        if code in (6, 7):
            if heard:
                return ("Sound reached the phone, but it could not turn it into words (phone code %d). "
                        "Say one short, clear command, for example: scan market." % code)
            return ("The microphone heard only silence (phone code %d). Check that no other app is using the "
                    "microphone and that it is allowed for this app in Android settings." % code)
        return {
            1: "Network timeout. Phone voice recognition needs internet.",
            2: "Network error. Check your connection.",
            3: "Audio recording problem. Another app may be using the microphone.",
            4: "The phone speech service had an error.",
            5: "Voice client error. Install or enable the Google app, or save an OpenAI key in CONFIG.",
            8: "The recognizer is busy. Wait a second and try again.",
            9: "Microphone permission is missing. Allow it in Android settings.",
            10: "Too many speech requests. Wait a moment.",
            11: "The phone speech server is not available.",
            12: "This phone does not support the speech language. Save an OpenAI key in CONFIG.",
            13: "The speech language pack is not installed on this phone.",
        }.get(code, "Voice recognition error %d." % code)

    def _finish(self):
        with self._lock:
            self.listening = False
            self._engine = ""
            self._tap_mode = False
            self._speech_began = False
        self.on_state("idle")
        self.on_level(0.0)

    # ------------------------------------------------------------------ #
    # permission
    # ------------------------------------------------------------------ #
    def has_permission(self):
        if _platform != "android":
            return True
        try:
            from android.permissions import check_permission, Permission
            return bool(check_permission(Permission.RECORD_AUDIO))
        except Exception:
            return False

    def ask_permission(self, then=None):
        if _platform != "android":
            if then:
                then()
            return
        try:
            from android.permissions import request_permissions, Permission

            def _cb(perms, grants):
                if all(grants):
                    if then:
                        then()
                else:
                    self.on_error("Microphone permission was denied. Allow it in Android settings.")

            request_permissions([Permission.RECORD_AUDIO], _cb)
        except Exception as exc:
            self.on_error("Permission request failed: %s" % str(exc)[:80])

    # ------------------------------------------------------------------ #
    # button handling
    # ------------------------------------------------------------------ #
    def _ai_ready(self):
        return self.openai is not None and self.openai.ready()

    def _pick_engine(self):
        ai = self._ai_ready()
        if self.prefer == "OPENAI":
            return "openai" if ai else ("android" if self._native is not None else "")
        if self.prefer == "ANDROID":
            if self._native is not None and self.available:
                return "android"
            return "openai" if ai else ("android" if self._native is not None else "")
        if ai:
            return "openai"
        return "android" if self._native is not None else ""

    def press(self):
        """The button went down."""
        if _platform != "android":
            self.on_error("Voice input works on the Android phone. Type in the box instead.")
            return
        with self._lock:
            if self.listening:                       # second tap while a tap-recording is running: finish it
                self._swallow_release = True
                engine = self._engine
                self._tap_mode = False
            else:
                engine = None
        if engine:
            self._stop_engine(engine)
            return
        if not self.has_permission():
            self.ask_permission(then=None)
            self.on_error("Allow the microphone, then hold the button again.")
            return
        engine = self._pick_engine()
        if not engine:
            self.on_error("No speech service. Save an OpenAI key in CONFIG or type in the box.")
            return
        self._press_t = time.time()
        self._swallow_release = False
        if engine == "openai":
            self._record_start()
        else:
            self._android_start()

    def release(self):
        """The button was released."""
        if _platform != "android":
            return
        if self._swallow_release:
            self._swallow_release = False
            return
        with self._lock:
            if not self.listening:
                return
            engine = self._engine
        held = time.time() - self._press_t
        if held < self.TAP_SECONDS:
            with self._lock:
                self._tap_mode = True                # keep listening, finish by silence or a second tap
            return
        self._stop_engine(engine)

    def _stop_engine(self, engine):
        if engine == "openai":
            wait = self.MIN_RECORD - (time.time() - self._rec_started)
            if wait > 0:
                threading.Timer(wait, self._record_stop).start()
            else:
                self._record_stop()
        elif engine == "android" and self._native is not None:
            if self._speech_began:
                self.on_state("processing")
                self._native["stop"]()
            # no speech yet: the phone's own timeout ends the listening, nothing is thrown away

    # ------------------------------------------------------------------ #
    # phone recognizer path
    # ------------------------------------------------------------------ #
    def _android_start(self):
        if self._native is None:
            self.on_error("The phone has no speech service. Type in the box instead.")
            return
        if not self.available:
            self._native["create"]()
            self.on_error("Voice recognizer is starting. Hold the button again.")
            return
        with self._lock:
            self.listening = True
            self._engine = "android"
            self._speech_began = False
            self._peak_rms = -2.0
            self._partial = ""
        self.on_state("listening")
        self._native["cancel"]()                      # clears a half-finished earlier session (avoids "busy")
        self._native["start"]()

    # ------------------------------------------------------------------ #
    # OpenAI path: record with MediaRecorder, then transcribe
    # ------------------------------------------------------------------ #
    def _new_recorder(self, source):
        from jnius import autoclass
        MediaRecorder = autoclass("android.media.MediaRecorder")
        try:
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            rec = MediaRecorder(activity)            # Android 12+ wants a context
        except Exception:
            rec = MediaRecorder()
        rec.setAudioSource(source)                   # 6 = VOICE_RECOGNITION, 1 = MIC
        rec.setOutputFormat(2)                       # MPEG_4
        rec.setAudioEncoder(3)                       # AAC
        rec.setAudioChannels(1)
        rec.setAudioSamplingRate(16000)
        rec.setAudioEncodingBitRate(64000)
        rec.setOutputFile(self._rec_path)
        rec.prepare()
        rec.start()
        return rec

    def _record_start(self):
        with self._lock:
            if self.listening:
                return
            self.listening = True
            self._engine = "openai"
        try:
            os.remove(self._rec_path)
        except OSError:
            pass
        rec, last = None, ""
        for source in (6, 1):
            try:
                rec = self._new_recorder(source)
                break
            except Exception as exc:
                last = str(exc)[:80]
                rec = None
        if rec is None:
            self._finish()
            self.on_error("Could not open the microphone (another app may be using it): %s" % last)
            return
        self._recorder = rec
        self._rec_started = time.time()
        self._max_amp = 0
        self.on_state("listening")
        threading.Thread(target=self._level_loop, args=(rec,), daemon=True).start()

    def _level_loop(self, rec):
        """Shows the loudness while recording, ends a tap-recording after you stop speaking, and caps the length."""
        speech_seen = False
        quiet_since = 0.0
        while self._recorder is rec:
            try:
                amp = int(rec.getMaxAmplitude())
            except Exception:
                amp = 0
            self._max_amp = max(self._max_amp, amp)
            self.on_level(min(1.0, amp / 12000.0))
            now = time.time()
            elapsed = now - self._rec_started
            if amp >= self.SPEECH_LEVEL:
                speech_seen, quiet_since = True, 0.0
            elif speech_seen and amp < self.QUIET_LEVEL:
                quiet_since = quiet_since or now
            else:
                quiet_since = 0.0 if amp >= self.QUIET_LEVEL else quiet_since
            if self._tap_mode:
                if speech_seen and quiet_since and now - quiet_since >= self.SILENCE_END:
                    break
                if not speech_seen and elapsed >= self.NO_SPEECH_END:
                    break
            if elapsed >= self.MAX_RECORD:
                break
            time.sleep(0.08)
        if self._recorder is rec:
            self._record_stop()

    def _record_stop(self):
        with self._lock:
            rec, self._recorder = self._recorder, None
        if rec is None:
            return
        self.on_state("processing")
        ok = True
        try:
            rec.stop()
        except Exception:
            ok = False                                # nothing usable was captured
        try:
            rec.release()
        except Exception:
            pass
        if not ok:
            self._finish()
            self.on_error("Nothing was recorded. Hold the button while you speak.")
            return
        if self._max_amp < 700:
            self._finish()
            self.on_error("The microphone heard only silence. Check that no other app is using it, "
                          "and that the microphone permission is allowed.")
            return
        threading.Thread(target=self._transcribe, args=(self._rec_path,), daemon=True).start()

    def _transcribe(self, path):
        try:
            text = _clean(self.openai.transcribe(path, "", TRADING_PROMPT))
        except Exception as exc:
            self._finish()
            self.on_error(str(exc)[:140])
            return
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        self._finish()
        if text and text.lower().strip(" .!") not in ("you", "thank you", "thanks for watching"):
            self.on_result(text)
        else:
            self.on_error("The recording was understood as no words. Speak one short, clear command, for example: "
                          "scan market.")

    # ------------------------------------------------------------------ #
    def cancel(self):
        with self._lock:
            rec, self._recorder = self._recorder, None
        if rec is not None:
            try:
                rec.stop()
            except Exception:
                pass
            try:
                rec.release()
            except Exception:
                pass
        if self._native is not None:
            self._native["cancel"]()
        self._finish()

    def shutdown(self):
        self.cancel()
        if self._native is not None and self._rec is not None:
            try:
                from android.runnable import run_on_ui_thread

                @run_on_ui_thread
                def _destroy():
                    try:
                        self._rec.destroy()
                    except Exception:
                        pass
                _destroy()
            except Exception:
                pass
