# -*- coding: utf-8 -*-
"""custom_voice.py - your own voice clips for Tactical Neural Matrix (TDN). No paid voice credits needed.

What this does (and does not)
  * You pick your own .wav or .mp3 files and assign each one to an EVENT (cue): STARTUP, ORDER FILLED, TARGET HIT,
    STOP HIT, DISARMED, PANIC, NO LIVE DATA, CONFIRM NEEDED, ALERT. When the app is about to say a sentence of that
    kind, your clip plays. Depending on the mode the clip is followed by the spoken sentence (so the numbers are still
    read out) or plays alone.
  * A recorded file cannot read new numbers aloud. Every sentence without a clip is spoken by the phone voice
    (Android TTS) with your custom voice profile (language, pitch, speed, installed voice), see voice_engine.py.
  * If a clip is missing, damaged or cannot play, the app silently falls back to the phone voice. Nothing crashes and
    no order, risk or safety rule depends on sound.

Storage and safety
  * Files are copied into the app's private folder  <data_dir>/voices/  (mode 600 where the phone allows it) under
    names the app chooses, so nothing outside the app is ever read again and the original file can be deleted.
  * The format is checked by content, not by name: standard PCM WAV (at most 20 seconds, 3 MB) or MP3 (at most 600 KB).
    At most 12 MB are kept in total, at most one clip per cue. Writes are atomic (temporary file, then rename).
  * The clips never reach the network and are not part of the Agent Brain.

The Android system file picker (pick_audio_android) needs no storage permission: Android hands over only the file you
choose. On other systems, or when the picker is not available, import_file() takes a path instead.
"""
import io
import json
import os
import struct
import threading
import time
import wave
from typing import Callable, Dict, List, Optional, Tuple

try:
    from kivy.utils import platform as _platform
except Exception:                                   # plain Python (tests, desktop)
    _platform = "other"

MODES = ("OFF", "CLIPS_AND_SPEECH", "CLIPS_ONLY")
MODE_LABELS = {"OFF": "OFF (phone voice only)", "CLIPS_AND_SPEECH": "CLIP, THEN THE SENTENCE",
               "CLIPS_ONLY": "CLIP ONLY (sentence only when there is no clip)"}
MAX_WAV_BYTES = 3 * 1024 * 1024
MAX_MP3_BYTES = 600 * 1024
MAX_TOTAL_BYTES = 12 * 1024 * 1024
MAX_WAV_SECONDS = 20.0
PLAY_SECONDS = 20.0

# (cue id, label shown in CONFIG)
CUES: Tuple[Tuple[str, str], ...] = (
    ("startup", "STARTUP"), ("order_filled", "ORDER FILLED"), ("target_hit", "TARGET HIT"), ("stop_hit", "STOP HIT"),
    ("disarmed", "DISARMED"), ("panic", "PANIC"), ("no_live_data", "NO LIVE DATA"), ("confirm", "CONFIRM NEEDED"),
    ("alert", "ALERT"))
CUE_IDS = tuple(c for c, _l in CUES)
CUE_LABELS = dict(CUES)


def cue_for(message: str) -> str:
    """Which event a spoken sentence belongs to ('' when none). The first matching rule wins."""
    t = " ".join(str(message or "").lower().split())
    if not t:
        return ""
    if "panic" in t or "square-off" in t or "square off" in t:
        return "panic"
    if "no live data" in t:
        return "no_live_data"
    if "disarmed" in t or "kill switch" in t:
        return "disarmed"
    if "closed by stop" in t or "closed by trailing" in t:
        return "stop_hit"
    if "closed by target" in t:
        return "target_hit"
    if "order filled" in t:
        return "order_filled"
    if "confirm on screen" in t or "i heard:" in t:
        return "confirm"
    if "online" in t and "tactical neural matrix" in t:
        return "startup"
    for word in ("error", "could not", "cannot", "refus", "blocked", "locked", "too stale", "not tradeable", "no order"):
        if word in t:
            return "alert"
    return ""


def detect_kind(head: bytes) -> str:
    """'wav', 'mp3' or '' from the first bytes of a file."""
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if len(head) >= 3 and head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    return ""


def check_wav(data: bytes) -> Tuple[bool, str]:
    """Standard PCM WAV of at most MAX_WAV_SECONDS."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            rate, frames, width, chans = w.getframerate(), w.getnframes(), w.getsampwidth(), w.getnchannels()
    except (wave.Error, EOFError, struct.error, ValueError) as exc:
        return False, "That WAV file cannot be read (%s). Use a standard PCM WAV." % str(exc)[:50]
    if rate <= 0 or frames <= 0 or width not in (1, 2, 3, 4) or chans not in (1, 2):
        return False, "That WAV file is empty or in an unusual format."
    seconds = frames / float(rate)
    if seconds > MAX_WAV_SECONDS:
        return False, "That clip is %.0f seconds long. Keep it under %d seconds." % (seconds, int(MAX_WAV_SECONDS))
    return True, ""


class CustomVoice:
    """The private clip store and its settings (mode and which file belongs to which cue)."""

    def __init__(self, data_dir: str, log: Optional[Callable[[str], None]] = None):
        self._log = log or (lambda msg: None)
        self.dir = os.path.join(data_dir, "voices")
        self._manifest = os.path.join(self.dir, "voices.json")
        self._lock = threading.RLock()
        self.mode = "OFF"
        self.clips: Dict[str, Dict] = {}
        self.problem = ""
        try:
            os.makedirs(self.dir, exist_ok=True)
        except OSError as exc:
            self.problem = "voice folder: %s" % str(exc)[:60]
        self._load()

    # ---- settings file ----
    def _load(self) -> None:
        try:
            with open(self._manifest, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if data.get("mode") in MODES:
            self.mode = data["mode"]
        clips = data.get("clips")
        if isinstance(clips, dict):
            for cue, info in clips.items():
                if cue in CUE_IDS and isinstance(info, dict) and info.get("file") and info.get("kind") in ("wav", "mp3"):
                    name = os.path.basename(str(info["file"]))               # never a path: only a name in our folder
                    self.clips[cue] = {"file": name, "kind": info["kind"], "bytes": int(info.get("bytes", 0) or 0)}

    def _save(self) -> bool:
        tmp = self._manifest + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"mode": self.mode, "clips": self.clips}, fh)
            os.replace(tmp, self._manifest)
            self._private(self._manifest)
            return True
        except OSError as exc:
            self.problem = "could not save voice settings: %s" % str(exc)[:60]
            return False

    @staticmethod
    def _private(path: str) -> None:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def set_mode(self, mode: str) -> bool:
        if mode not in MODES:
            return False
        with self._lock:
            self.mode = mode
            return self._save()

    # ---- reading ----
    def path_for(self, cue: str) -> Optional[str]:
        """Full path of the clip of a cue, or None when there is none or the file is missing or changed."""
        with self._lock:
            info = self.clips.get(cue)
            if not info:
                return None
            path = os.path.join(self.dir, os.path.basename(info["file"]))
            try:
                if not os.path.isfile(path) or os.path.getsize(path) != info.get("bytes", -1):
                    return None
            except OSError:
                return None
            return path

    def clip_for(self, message: str) -> Tuple[str, Optional[str]]:
        """(cue, path) for a sentence. path is None when the cue has no usable clip."""
        cue = cue_for(message)
        if not cue:
            return "", None
        return cue, self.path_for(cue)

    def total_bytes(self) -> int:
        with self._lock:
            return sum(int(i.get("bytes", 0)) for i in self.clips.values())

    def status_lines(self) -> List[str]:
        lines = ["Clip mode: %s." % MODE_LABELS[self.mode]]
        for cue, label in CUES:
            info = self.clips.get(cue)
            if not info:
                lines.append("%s: no clip" % label)
            elif self.path_for(cue) is None:
                lines.append("%s: FILE MISSING (the phone voice is used)" % label)
            else:
                lines.append("%s: %s clip, %d KB" % (label, info["kind"].upper(), max(1, int(info["bytes"]) // 1024)))
        lines.append("Stored: %.1f of %d MB, private to this app." % (self.total_bytes() / 1048576.0,
                                                                     MAX_TOTAL_BYTES // 1048576))
        if self.problem:
            lines.append(self.problem)
        return lines

    # ---- writing ----
    def import_bytes(self, cue: str, data: bytes) -> Tuple[bool, str]:
        """Checks the content and stores it as the clip of a cue. Never raises."""
        try:
            if cue not in CUE_IDS:
                return False, "Unknown event."
            if not data:
                return False, "The file is empty."
            kind = detect_kind(data[:16])
            if not kind:
                return False, "That is not a WAV or MP3 file."
            limit = MAX_WAV_BYTES if kind == "wav" else MAX_MP3_BYTES
            if len(data) > limit:
                return False, "That %s file is %d KB. The limit is %d KB." % (kind.upper(), len(data) // 1024, limit // 1024)
            if kind == "wav":
                good, why = check_wav(data)
                if not good:
                    return False, why
            with self._lock:
                old = self.clips.get(cue)
                others = sum(int(i.get("bytes", 0)) for c, i in self.clips.items() if c != cue)
                if others + len(data) > MAX_TOTAL_BYTES:
                    return False, "Not enough room: all clips together may use %d MB. Remove one first." % (
                        MAX_TOTAL_BYTES // 1048576)
                name = "clip_%s_%d.%s" % (cue, int(time.time()), kind)
                path = os.path.join(self.dir, name)
                tmp = path + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, path)
                self._private(path)
                self.clips[cue] = {"file": name, "kind": kind, "bytes": len(data)}
                if not self._save():
                    self.clips.pop(cue, None)
                    self._remove_file(name)
                    return False, self.problem
                if old and old.get("file") and old["file"] != name:
                    self._remove_file(old["file"])
            return True, "%s clip saved for %s (%d KB)." % (kind.upper(), CUE_LABELS[cue], max(1, len(data) // 1024))
        except Exception as exc:
            return False, "Could not store the clip: %s" % str(exc)[:70]

    def import_file(self, cue: str, path: str) -> Tuple[bool, str]:
        """Copies a file the user pointed to. The size is checked before reading so a huge file is never loaded."""
        try:
            path = os.path.expanduser(str(path).strip().strip('"').strip("'"))
            if not path or not os.path.isfile(path):
                return False, "No file at that path."
            size = os.path.getsize(path)
            if size <= 0:
                return False, "The file is empty."
            if size > MAX_WAV_BYTES:
                return False, "That file is %d KB. The limit is %d KB." % (size // 1024, MAX_WAV_BYTES // 1024)
            with open(path, "rb") as fh:
                data = fh.read(MAX_WAV_BYTES + 1)
        except OSError as exc:
            return False, "Could not read the file: %s" % str(exc)[:60]
        return self.import_bytes(cue, data)

    def _remove_file(self, name: str) -> None:
        try:
            os.remove(os.path.join(self.dir, os.path.basename(name)))
        except OSError:
            pass

    def remove(self, cue: str) -> str:
        with self._lock:
            info = self.clips.pop(cue, None)
            if info:
                self._remove_file(info["file"])
                self._save()
                return "Clip for %s removed. The phone voice is used." % CUE_LABELS.get(cue, cue)
        return "There was no clip for %s." % CUE_LABELS.get(cue, cue)

    def clear(self) -> None:
        with self._lock:
            for info in list(self.clips.values()):
                self._remove_file(info["file"])
            self.clips.clear()
            self._save()
            try:                                                   # leftovers of interrupted writes
                for name in os.listdir(self.dir):
                    if name.startswith("clip_"):
                        self._remove_file(name)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Android system file picker (no storage permission needed)
# --------------------------------------------------------------------------- #
_PICK_REQUEST = 7211


def pick_audio_android(on_result: Callable[[Optional[bytes], str], None]) -> Tuple[bool, str]:
    """Opens the Android file picker for audio. on_result(data, message) is called from a background thread: data is
    the chosen file (at most MAX_WAV_BYTES + 1 bytes) or None, message says why. Returns (started, message)."""
    if _platform != "android":
        return False, "The system file picker exists only on Android. Type a file path instead."
    try:
        from android import activity as android_activity
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        Intent = autoclass("android.content.Intent")
        activity = PythonActivity.mActivity
        intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
        intent.addCategory(Intent.CATEGORY_OPENABLE)
        intent.setType("audio/*")

        def read_uri(uri) -> None:
            try:
                Array = autoclass("java.lang.reflect.Array")
                Byte = autoclass("java.lang.Byte")
                stream = activity.getContentResolver().openInputStream(uri)
                if stream is None:
                    on_result(None, "The phone could not open that file.")
                    return
                buf = Array.newInstance(Byte.TYPE, 65536)
                out = bytearray()
                try:
                    while len(out) <= MAX_WAV_BYTES:
                        n = int(stream.read(buf))
                        if n <= 0:
                            break
                        out.extend(bytes(x & 0xFF for x in buf[:n]))
                finally:
                    stream.close()
                on_result(bytes(out), "")
            except Exception as exc:
                on_result(None, "Could not read the chosen file: %s" % str(exc)[:70])

        def result_cb(request_code, result_code, data) -> None:
            if request_code != _PICK_REQUEST:
                return
            try:
                android_activity.unbind(on_activity_result=result_cb)
            except Exception:
                pass
            if result_code != -1 or data is None or data.getData() is None:
                on_result(None, "No file chosen.")
                return
            threading.Thread(target=read_uri, args=(data.getData(),), daemon=True).start()

        android_activity.bind(on_activity_result=result_cb)
        activity.startActivityForResult(intent, _PICK_REQUEST)
        return True, "Choose an audio file (WAV or MP3)."
    except Exception as exc:
        return False, "The file picker could not open: %s" % str(exc)[:70]
