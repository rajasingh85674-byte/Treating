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
