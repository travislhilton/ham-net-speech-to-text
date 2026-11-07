"""A radio that went silent: switched off, asleep (a handheld's auto power-off), a flat battery, the plug out.

Silence is normal on a speaker / earphone jack (voice gate) whenever the squelch is closed, so "nothing heard" alone
cannot tell a radio that is off from a quiet frequency. What can: a radio that is ON adds its own small noise under a
closed squelch; a radio that is OFF leaves only the sound card's own floor, lower and dead flat. Measured on a laptop
station (handheld through a Digirig, 2026-11-06): squelch closed -79 to -85 dBFS per second, wandering a few dB; the
radio asleep, -87 / -88 dead flat for 7 minutes while the other station heard traffic.

  off     voice gate: for `off_min` (2) minutes the input sat >= `off_drop_db` (3) below the radio's own learned
          squelch-closed floor and flat (per-second spread <= `flat_db`, 0.8 dB). The floor is learned from the last
          hour's seconds with no transmission (needs 10 minutes of them).
  dead    carrier gate (a data jack always carries hiss): rms below `dead_dbfs` (-60) for `dead_min` (2) minutes.
  quiet   any gate, only while a scheduled net is on this radio: no transmission for `quiet_min` (10) minutes (a
          softer note; outside nets a quiet repeater is normal and would make the note noise).

station.yaml per radio: `silence: {off_min, off_drop_db, flat_db, dead_min, dead_dbfs, quiet_min}` (0 turns one off).
"""
from __future__ import annotations

import collections
import statistics

DEFAULTS = {"off_min": 2.0, "off_drop_db": 3.0, "flat_db": 0.8, "dead_min": 2.0, "dead_dbfs": -60.0,
            "quiet_min": 10.0, "learn_min_s": 600}

SAY = {"off": "The radio seems to be OFF or asleep: its input is quieter than its own squelch-closed hiss. "
              "Check the radio's power (battery, auto power-off), its volume and the cable.",
       "dead": "No audio from the radio: check the cable, the radio's power and its data/volume level.",
       "quiet": "A net is scheduled, but nothing heard for {m:.0f} minutes. Is the radio on, on the net's frequency?"}


class SilenceWatch:
    def __init__(self, gate: str = "carrier", cfg: dict | None = None):
        self.c = DEFAULTS | (cfg or {})
        self.gate = gate
        self.sec = collections.deque(maxlen=3600)          # (second, mean dB, receiving)
        self._acc, self._n, self._rx, self._t = 0.0, 0, False, None
        self.last_rx = None
        self.started = None

    def feed(self, t: float, rms_db: float, receiving: bool):
        """One meter reading (~10 a second)."""
        if self.started is None:
            self.started = t
        if receiving:
            self.last_rx = t
        s = int(t)
        if self._t is not None and s != self._t and self._n:
            import math
            self.sec.append((self._t, 10 * math.log10(self._acc / self._n + 1e-15), self._rx))
            self._acc, self._n, self._rx = 0.0, 0, False
        self._t = s
        self._acc += 10 ** (rms_db / 10); self._n += 1; self._rx = self._rx or receiving

    def state(self, now: float, net_on: bool = False) -> dict | None:
        """None, or {"kind": off|dead|quiet, "since": epoch s, "say"}."""
        c = self.c
        if self.started is None:
            return None
        if self.gate == "carrier" and c["dead_min"] > 0:
            win = [(t, d) for t, d, _ in self.sec if t >= now - c["dead_min"] * 60]
            if len(win) >= c["dead_min"] * 60 * 0.9 and all(d < c["dead_dbfs"] for _, d in win):
                return {"kind": "dead", "since": win[0][0], "say": SAY["dead"]}
        if self.gate != "carrier" and c["off_min"] > 0:
            n = int(c["off_min"] * 60)
            recent = [(t, d, rx) for t, d, rx in list(self.sec)[-n:]]
            fl = self.floor_before(now - n)
            if fl is not None and len(recent) >= n * 0.9 and not any(rx for *_, rx in recent):
                ds = [d for _, d, _ in recent]
                if statistics.mean(ds) <= fl - c["off_drop_db"] and statistics.pstdev(ds) <= c["flat_db"]:
                    return {"kind": "off", "since": recent[0][0], "say": SAY["off"]}
        if c["quiet_min"] > 0 and net_on:
            ref = self.last_rx if self.last_rx is not None else self.started
            if now - ref >= c["quiet_min"] * 60:
                return {"kind": "quiet", "since": ref, "say": SAY["quiet"].format(m=(now - ref) / 60)}
        return None

    def floor_before(self, t: float) -> float | None:
        """The learned squelch-closed floor from seconds before `t` (the stretch being judged is not its own yardstick)."""
        quiet = [d for s, d, rx in self.sec if not rx and s < t]
        if len(quiet) < self.c["learn_min_s"]:
            return None
        return statistics.median(quiet)
