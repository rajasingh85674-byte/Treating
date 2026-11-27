# TDN v5.6.0 - itemized list of everything asked for, and where it lives

## A. Trading modes, data, safety
1. Only PAPER and LIVE modes; no DEMO / simulated feed (DemoFeed deleted; old "DEMO" setting becomes AUTO) - broker.py, main.py
2. Live data: broker feed first, then Yahoo; if neither works the screen says NO LIVE DATA and refuses orders - main.py (_data_problem)
3. PAPER: PaperBroker on live prices, virtual capital (default 100000, settable), stops, trailing, target, EOD exit, P&L ledger, PAPER REPORT - broker.py
4. PaperLearner: learns from every closed paper trade, can only make filters stricter (+0 to +3 agents) - risk_manager.py
5. Start DISARMED (kill switch ON) at every launch, LIVE: LOCKED at every launch, app opens in PAPER - main.py
6. LIVE needs MODE=LIVE + ARM + UNLOCK LIVE (15 min, one order) + CONFIRM popup; ARM is never voice-controlled - main.py
7. 1% risk per trade, 2% daily loss, 2 losses in a row lock the day, max 4 trades - risk_manager.py (separate state for paper and live)
8. Panic square-off, persistent kill switch, stop-loss mandatory, signal stale after 45 s, one position at a time
9. Voice may switch to PAPER ("paper mode"), never to LIVE

## B. Keys, PIN, tamper
10. No key in source; OpenAI / ElevenLabs key + Voice ID in private storage (mode 600), shown as "saved, ends with ####", redacted in logs, errors, prompts - broker.py
11. Optional PIN (AppLock): salted PBKDF2, 5 wrong tries then growing wait, survives restart, damaged file fails closed; guards app screen, ARM, UNLOCK LIVE, switch to LIVE, key removal; locks again after 60 s in background
12. IntegrityGuard: fingerprints of every module per install, safety limits re-checked, hard-coded key scan; a failure keeps trading off and refuses ARM

## C. Discipline
13. Trend/context, reward:risk >= 2, invalidation level, honest uncertainty in agents.py reasons, mentor.py guidance, reply_agent.py, AI chat prompt, HUD note
14. CHOP market = no trade; no_hype() strips "guaranteed / risk free" style claims from AI text

## D. Voice, mic, UI, build (kept from earlier rounds)
15. AUTO voice order ElevenLabs -> OpenAI -> phone (en-IN, pitch 0.80, rate 0.95); ELEVENLABS only / OPENAI only / PHONE only; deep voices onyx, ash, echo first
16. Unmuted startup; no speaking while mic is active; TEST SAVED VOICES and TEST VOICE show OK / FAILED with reason
17. Mic messages: silence (code 7), sound without words, network, speech service, busy, language pack; clean session reset
18. Android TTS speak() overload fix (4 call forms)
19. Single-line bottom tabs: HUD, SCAN, AGENTS, REPLY, DEPTH, CONFIG
20. SCAN: separate green and red views, GOOD / WEAK / CHOP ratings, momentum, biodata, drawing guide, 4-point tracker
21. REPLY: message / telecall / follow-up writer (text only)
22. Red error bar + log with secret redaction
23. buildozer.spec (INTERNET, RECORD_AUDIO, arm64-v8a, exact filename) and .github/workflows/build.yml (locate files, key scan, compile, SDK/NDK, ARM64 check)

## v5.7.0 - Forex (new)
- Markets: NSE shares and forex (EURUSD, GBPUSD, USDJPY, AUDUSD, NZDUSD, USDCAD, USDCHF). HUD button MARKET switches; a switch always lands in PAPER, live locked.
- Four separate books (NSE paper/live, forex paper/live), each with its own risk counters, state file and paper learner.
- Live forex prices: OANDA candles when a token is saved, otherwise Yahoo Finance (public, may lag). No data = NO LIVE DATA and no order. Nothing simulated.
- FX PAPER: virtual 10,000 USD on live prices, same stop/trailing/target/weekend-exit engine.
- LIVE forex: OANDA v20. Stop-loss and target are sent with the order; if the stop is not confirmed the trade is closed again. USD accounts only. Environment PRACTICE (default) or LIVE (PIN needed to switch).
- Risk in USD: 1% per trade (size from stop distance, quote currency converted), 2% daily loss, 2 losses lock, one position at a time, weekend close (Friday 20:30 UTC), market closed Friday 21:00 to Sunday 22:00 UTC.
- Voice (voice_forex.py): pair, buy/sell, risk, status, price, close, forex mode, stock market. Voice only requests: pair-match check, read-back, risk clamped to 1%, size words ignored, ambiguous/negated/two-pair phrases refused, 25 s expiry, cooldown, CONFIRM popup always. Voice never arms, unlocks or goes LIVE.
- CONFIG: OANDA token (masked), account id, PRACTICE/LIVE, save/test/remove (PIN guarded).
- Honest limits: OANDA does not accept Indian residents; NSE currency futures through Kite/Upstox are not implemented; nothing was tested on a real phone or broker.

## v5.7.0 final additions
- Strict NO LIVE DATA: no source, no candles, market closed, or last candle older than 300 s shows NO LIVE DATA (header, chart line) and blocks every order and every approval. Nothing is simulated anywhere (no DEMO feed exists).
- market_flow.py: inflow/outflow ranking and correlation (together / opposite) from real 1-minute candles; FlowCollector polls the active market while the app and the market are open. Voice: "market flow", "correlation". HUD button MARKET FLOW.
- agent_brain.py: bounded SQLite memory (row caps, 7-day age limit, 6 MB limit with compaction, in-memory fallback if SQLite is missing). Stores live snapshots, the shared flow context and closed paper trades with their flow context. CONFIG shows status and CLEAR.
- Flow context is shared with every agent (x.flow) and with the AI chat. It can only make the filter stricter (+1 agent) when a trade goes against a strong flow; it never approves.
- Voice: the saved AI key (ElevenLabs or OpenAI) is the main voice (mode AUTO). The basic phone voice is only the fallback and the app says so when no AI key is saved.
- Android limits: the collector pauses in the background; collection is not continuous 24 hours and the Agent Brain reports real coverage.

## Repository layout (exact paths)
  buildozer.spec
  .github/workflows/build.yml
  main.py  ui_hud.py  voice_mic.py  voice_engine.py  agents.py  broker.py  risk_manager.py  mentor.py  reply_agent.py
  forex.py  voice_forex.py  market_flow.py  agent_brain.py

## Custom voice (v5.7.0, no paid voice credits)
- New module custom_voice.py (14 modules now; build.yml and IntegrityGuard list it).
- CONFIG > MY OWN VOICE: choose your own .wav / .mp3 per event (startup, order filled, target hit, stop hit, disarmed,
  panic, no live data, confirm needed, alert) with the Android file picker (no storage permission) or by file path.
- Files are copied into the private app folder (voices/, mode 600, atomic writes), checked by content: WAV up to 20 s /
  3 MB, MP3 up to 600 KB, 12 MB total. Manifest names are reduced to plain file names (no path tricks).
- Clip mode: OFF, CLIP THEN THE SENTENCE, or CLIP ONLY. A clip cannot read new numbers, so other sentences use the
  phone voice (Android TTS).
- Custom phone-voice profile: language code, pitch, speed (clamped 0.5 to 2.0), saved in voice.json.
- Voice source CUSTOM: skips OpenAI and ElevenLabs completely.
- Missing, damaged or unplayable clip: the phone voice speaks, nothing crashes. Sound never affects any trading rule.
- Unchanged: DISARMED start, LIVE LOCKED, 1% / 2% limits, CONFIRM popup, NO LIVE DATA, no demo data, bounded Agent Brain.

## Red / green candle bias and side choice
- HUD shows the last 10 real candles: GREEN count, RED count, current streak and bias (GREEN / RED / MIXED).
  A stale or missing feed shows "CANDLES: NO LIVE DATA" (no demo numbers).
- SIDE button cycles AUTO, GREEN (BUY only), RED (SELL only), saved in ui.json. It only restricts: it never creates an
  order, and every order still needs the agents' approval, the CONFIRM popup, stop-loss, 1% risk and the 2% daily limit.
