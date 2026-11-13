"""Sidekick's voice: xAI text-to-speech (POST /v1/tts), for the OPERATOR to hear — the voice audition in Settings and
"Hear it" on a draft. The sound plays in the dashboard's browser, never on a radio: nothing here can reach a radio's
audio or PTT (station/cat.py refuses PTT).

Owner 2026-11-10: "build up an audition with each of the Grok voices so I can choose one", the choice kept in
Settings (Settings → Station agent → Voice; data/agent.json "voice" and "speed").

Each clip is kept by its words, voice and speed (data/tts/<hash>.mp3) so an audition or a draft is paid for once
(xAI bills by characters sent). The voice list comes from GET /v1/tts/voices (kept a day in data/tts/voices.json);
offline, the list below is used and only clips already made can play.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import secrets
from .online import Online, is_network_error

URL = "https://api.x.ai/v1/tts"
VOICES_URL = "https://api.x.ai/v1/tts/voices"
KEY = "XAI_API_KEY"
DEFAULT_VOICE = "eve"
# GET /v1/tts/voices as documented 2026-11-10 (the live list wins when it can be fetched)
VOICES = ["eve", "ara", "leo", "rex", "sal", "carina", "zagan", "helix", "orion", "luna", "iris", "altair", "zenith",
          "perseus", "helios", "lux", "kepler", "rigel", "cosmo", "celeste", "ursa", "sirius", "lumen", "castor",
          "naksh", "atlas"]
MAX_CHARS = 1200          # a draft or an audition, never a document


def synth(key: str, text: str, voice: str, speed: float = 1.0, replace: dict | None = None, timeout=30) -> bytes:
    body = {"text": text, "voice_id": voice, "language": "en", "speed": speed,
            "output_format": {"codec": "mp3", "sample_rate": 24000, "bit_rate": 128000}}
    if replace:
        body["replace"] = replace
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_voices(key: str, timeout=10) -> list[dict]:
    req = urllib.request.Request(VOICES_URL, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()).get("voices") or []


def spoken_call(call: str) -> str:
    """How a call should sound: letter by letter ("W 9 X Y Z"), never as a word."""
    return " ".join(c for c in (call or "").upper() if c.isalnum())


def audition_text(call: str, name: str, op: str) -> str:
    return (f"This is {call}, {name}, an automated assistant; {op} is control operator. Checking in, no traffic. "
            f"[pause] Copy your question on the sixty meter band plan. Working on it, I'll report back when "
            f"there's an opening. [pause] This is {call} {name}, I have a response ready for the earlier inquiry. "
            f"[pause] Sixty meters is five channels, upper sideband, one hundred watts. {call}.")


class Voice:
    def __init__(self, station, synth_fn=synth, voices_fn=fetch_voices):
        self.st = station
        self.synth_fn, self.voices_fn = synth_fn, voices_fn
        self.dir = station.data / "tts"
        self.online = Online("xai-tts")

    def _settings(self) -> dict:
        a = getattr(self.st, "agent", None)
        return (a.cfg if a else {}) or {}

    def voices(self) -> dict:
        cache = self.dir / "voices.json"
        try:
            d = json.loads(cache.read_text(encoding="utf-8"))
            if time.time() - d.get("at", 0) < 86400 and d.get("voices"):
                return d | {"source": "xai (kept)"}
        except (OSError, ValueError):
            pass
        key = secrets.read(self.st.root, KEY)
        if key and self.online.should_try():
            try:
                v = [x for x in self.voices_fn(key) if x.get("voice_id")]
                self.online.ok()
                if v:
                    self.dir.mkdir(parents=True, exist_ok=True)
                    d = {"at": time.time(), "voices": v}
                    cache.write_text(json.dumps(d), encoding="utf-8")
                    return d | {"source": "xai"}
            except Exception as e:  # noqa: BLE001
                if is_network_error(e):
                    self.online.failed(e)
        return {"voices": [{"voice_id": v, "name": v.title(), "language": "en"} for v in VOICES], "source": "built in"}

    def view(self) -> dict:
        s = self._settings()
        made = {p.stem for p in self.dir.glob("*.mp3")} if self.dir.exists() else set()
        auds = {v["voice_id"]: self._key(self._audition_words(), v["voice_id"], 1.0) in made
                for v in self.voices()["voices"]}
        return {"voice": s.get("voice") or "", "default": DEFAULT_VOICE, "speed": s.get("speed") or 1.0,
                "key_set": bool(secrets.read(self.st.root, KEY)), "online": self.online.view(),
                "voices": self.voices()["voices"], "auditioned": auds}

    def _audition_words(self) -> str:
        s = self._settings()
        name = s.get("name") or "Sidekick"
        return audition_text(self.st.me or "N0CALL", name, s.get("operator") or "the operator")

    @staticmethod
    def _key(text, voice, speed) -> str:
        return hashlib.sha1(f"{voice}|{speed:.2f}|{text}".encode()).hexdigest()[:20]

    def clip(self, text: str, voice: str | None = None, speed: float | None = None) -> bytes:
        """The mp3 for these words in this voice: kept, or made now. Offline = LookupError (nothing waits)."""
        s = self._settings()
        voice = (voice or s.get("voice") or DEFAULT_VOICE).lower()
        if not re.fullmatch(r"[a-z0-9_-]{2,40}", voice):
            raise ValueError("unknown voice")
        speed = float(speed or s.get("speed") or 1.0)
        text = re.sub(r"\s+", " ", str(text or "")).strip()[:MAX_CHARS]
        if not text:
            raise ValueError("nothing to say")
        p = self.dir / f"{self._key(text, voice, speed)}.mp3"
        if p.exists():
            return p.read_bytes()
        key = secrets.read(self.st.root, KEY)
        if not key:
            raise LookupError("set the xAI key first (Settings → Station agent)")
        if not self.online.should_try():
            raise LookupError(f"xAI is offline ({self.online.last_error}); only clips already made can play")
        call = self.st.me or ""
        replace = {call: spoken_call(call)} if re.fullmatch(r"[A-Z0-9]{3,8}", call) else None
        try:
            audio = self.synth_fn(key, text, voice, speed, replace)
        except urllib.error.HTTPError as e:
            self.online.ok()
            raise ValueError(f"xAI answered {e.code}" + (" (unknown voice)" if e.code == 404 else "")) from e
        except Exception as e:  # noqa: BLE001
            if is_network_error(e):
                self.online.failed(e)
                raise LookupError(f"xAI unreachable: {type(e).__name__}") from e
            raise
        self.online.ok()
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(audio)
        tmp.replace(p)
        return audio

    def prerender(self, wait: bool = False):
        """Make (and keep) the clips for the rules' fixed lines and the transmitter test, in the chosen voice, so
        the first check-in on the air does not wait on the voice service. Quiet when there is no key or no
        internet; at most one run at a time; an unchanged line is never paid for twice (clips are kept by words)."""
        import threading
        from .agent import fixed_lines
        if getattr(self, "_pre", None) and self._pre.is_alive():
            return
        s = self._settings()
        a = getattr(self.st, "agent", None)
        call = self.st.me or ""
        if not call or not secrets.read(self.st.root, KEY) or not self.online.should_try():
            return
        name, op = (a.name if a else "Sidekick"), s.get("operator") or "the operator"
        lines = list(fixed_lines(call, name, op).values()) + [
            f"This is {call}, a transmitter test into a dummy load. {call}."]

        def run():
            for text in lines:
                try:
                    self.clip(text)
                except Exception:  # noqa: BLE001 - offline / no key: the next change or restart tries again
                    return
        self._pre = threading.Thread(target=run, daemon=True, name="tts-prerender")
        self._pre.start()
        if wait:
            self._pre.join(60)

    def audition(self, voice: str) -> bytes:
        return self.clip(self._audition_words(), voice, 1.0)

    def draft(self, draft_id: str) -> bytes:
        a = getattr(self.st, "agent", None)
        d = next((x for x in (a.drafts if a else []) if x.get("id") == draft_id), None)
        if not d or not d.get("say"):
            raise ValueError("no such draft with words")
        return self.clip(d["say"])
