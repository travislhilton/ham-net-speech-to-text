"""This PC's speed: measured properly, its power settings checked, and what the station changes said plainly.

Owner 2026-11-12: "When a user installs this we need to properly detect and manage performance setting and
communicate with the user so they are aware of any settings that will be modified while running."

Why: a station PC (a 2026 Core Ultra laptop) installed while nobody was at it benchmarked with its screen off — Modern
Standby held the processor at 25% of its speed — and chose a slower accurate pass (live text 0.53 s per second of
audio; with the screen held on 0.03 s). So:

* MEASURE (`python -m station.pccheck measure`, the installer and Settings → This PC → Measure this PC again): hold
  the screen and system awake and switch to the HamNet Station power plan (station/awake.py, as during a net), wait for
  the processor to come up to speed, run tools/pick_engine.py and tools/pick_accurate.py, put the operator's plan back.
  The processor's speed before and while holding is kept with the result; a measurement taken slowed is marked.
* CHECK (`check`): on battery, the Windows power mode, hibernate on mains power, Modern Standby (the screen-off slow
  down), an old graphics driver, the processor held back even at full power (a maker's quiet / fan profile). Each says
  what it means and how to fix it. Kept in data/pc_check.json; Settings → This PC shows it.
* SAY WHAT CHANGES (`explain`, the installer, Settings, the dashboard's one-time notice): the station's "Full speed
  and screen on" (keep_awake) choice, asked once (`consent`).
* WATCH (SlowWatch): a live transcription several times slower than measured is said on the dashboard.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FULL_SPEED_PCT = 70           # "% Processor Performance" this high = not held back (it reads 100-200 when boosting)
WAIT_S = 45                   # how long a measurement waits for the processor to come up to speed
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

BEST_EFFICIENCY = "961cc777-2547-4f9d-8174-7d86181b8a7a"
BEST_PERFORMANCE = "ded574b5-45a0-4f42-8737-46345c09c238"
MODE_NAMES = {BEST_EFFICIENCY: "Best power efficiency", "00000000-0000-0000-0000-000000000000": "Balanced",
              BEST_PERFORMANCE: "Best performance"}

MODES = {"traffic": "During nets and traffic (recommended)", "listening": "The whole time a radio listens",
         "always": "Always, while the station runs", "off": "Never (my power settings only)"}


def explain(mode: str = "traffic") -> list[str]:
    """What the station changes on this PC, in the operator's words (installer, Settings, the one-time notice)."""
    when = {"traffic": "during a scheduled net, while a transmission is received or transcribed, and for 10 minutes "
                       "after the last one",
            "listening": "the whole time a radio is listening",
            "always": "the whole time the station runs",
            "off": None}.get(mode, "during nets and traffic")
    if not when:
        return ["Full speed and screen on: Never. The station changes no power settings; with the screen off a "
                "laptop may transcribe several times slower."]
    return [f"Full speed and screen on: {when}, the station",
            "  - keeps the screen on and the PC awake (the same request a video player makes),",
            "  - switches Windows to its 'HamNet Station' power plan (no sleep, lid does nothing, USB / Wi-Fi / PCIe "
            "power saving off, graphics at maximum) and the power mode to Best performance,",
            "  - and then puts your own power plan and power mode back. Your plan can favour the battery.",
            "Change it any time: Settings -> Full speed and screen on."]


# ---------- facts about this PC (Windows) ----------

def perf_pct(samples: int = 2, run=subprocess.run) -> float | None:
    """The processor's current speed as % of its rated speed (Windows' "% Processor Performance"; above 100 while
    boosting). None when it cannot be read."""
    try:
        p = run(["typeperf", r"\Processor Information(_Total)\% Processor Performance", "-sc", str(samples)],
                capture_output=True, text=True, timeout=20 + samples, creationflags=NO_WINDOW)
        vals = [float(v) for v in re.findall(r'^"[^"]*","([\d.]+)"', p.stdout or "", re.M)]
        return round(max(vals[1:] or vals), 0) if vals else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


_FACTS_PS = r"""
$o = @{}
$cs = Get-CimInstance Win32_ComputerSystem; $o.maker = "$($cs.Manufacturer)"; $o.model = "$($cs.Model)"
$o.battery = @(Get-CimInstance Win32_Battery).Count -gt 0
$o.gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object { @{ name = "$($_.Name)"; version = "$($_.DriverVersion)";
  date = $(if ($_.DriverDate) { $_.DriverDate.ToString('yyyy-MM-dd') } else { '' }) } })
$a = (powercfg /a) -join "`n"; $o.modern_standby = $a -match 'S0 Low Power Idle'
$q = (powercfg /query SCHEME_CURRENT SUB_SLEEP HIBERNATEIDLE) -join "`n"
if ($q -match 'Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)') { $o.hibernate_ac_s = [Convert]::ToInt64($Matches[1], 16) }
$k = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes' -ErrorAction SilentlyContinue
$o.mode_ac = "$($k.ActiveOverlayAcPowerScheme)"
$o | ConvertTo-Json -Depth 4 -Compress
"""


def gather(run=subprocess.run) -> dict:
    """What can be read about this PC's power and graphics. {} off Windows."""
    if sys.platform != "win32":
        return {}
    out: dict = {}
    try:
        p = run(["powershell", "-NoProfile", "-Command", _FACTS_PS], capture_output=True, text=True, timeout=60,
                creationflags=NO_WINDOW)
        out = json.loads((p.stdout or "").strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    try:
        from .awake import PowerPlan
        out["on_battery"] = PowerPlan(ROOT / "data" / "power_snapshot.json").on_battery()
    except Exception:  # noqa: BLE001
        pass
    return out


def checks(f: dict, measured: dict | None = None, today=None) -> list[dict]:
    """[{id, ok, say, fix}] from gather()'s facts and the last measurement. Pure (the tests feed it facts)."""
    today = today or datetime.now().date()
    out = []

    def add(i, ok, say, fix=""):
        out.append({"id": i, "ok": ok, "say": say, "fix": fix})
    if f.get("on_battery"):
        add("battery", False, "Running on battery.", "Plug the PC in for nets: on battery Windows slows it down, "
            "and a flat battery stops the station.")
    elif f.get("battery"):
        add("battery", True, "Plugged in.")
    mode = (f.get("mode_ac") or "").lower()
    if mode == BEST_EFFICIENCY:
        add("mode", False, "Windows power mode (plugged in): Best power efficiency.",
            "Settings -> System -> Power & battery -> Power mode: Balanced or Best performance. (The station "
            "switches to Best performance itself while it transcribes.)")
    elif mode:
        add("mode", True, f"Windows power mode (plugged in): {MODE_NAMES.get(mode, 'set')}.")
    h = f.get("hibernate_ac_s")
    if h:
        add("hibernate", False, f"Hibernates after {round(h / 60)} minutes on mains power.",
            "A hibernated PC cannot start the station for a net: Control Panel -> Power Options -> your plan -> "
            "Change advanced power settings -> Sleep -> Hibernate after: Never (Plugged in).")
    if f.get("modern_standby"):
        add("standby", True, "This PC uses Modern Standby: with the screen off it runs several times slower.",
            "Nothing to do: the station keeps the screen on while it transcribes (Full speed and screen on).")
    for g in f.get("gpus") or []:
        try:
            age = (today - datetime.strptime(g.get("date") or "", "%Y-%m-%d").date()).days
        except ValueError:
            continue
        if age > 540 and re.search(r"intel|nvidia", g.get("name") or "", re.I):
            add("driver", False, f"Graphics driver for {g['name']} is from {g['date']}.",
                "Update it (Windows Update -> Advanced options -> Optional updates, or the maker's site), then "
                "Settings -> This PC -> Measure this PC again.")
    m = measured or {}
    held = m.get("perf_held")
    if held is not None and held < FULL_SPEED_PCT and not f.get("on_battery"):
        maker = (f.get("maker") or "").lower()
        hint = (" ASUS: MyASUS -> Fan Profile (or Armoury Crate) -> Performance." if "asus" in maker else
                " Lenovo: Lenovo Vantage -> Power -> Intelligent Cooling -> Performance." if "lenovo" in maker else
                " Dell: Dell Optimizer / Power Manager -> Thermal -> Ultra Performance." if "dell" in maker else
                " HP: HP Command Center -> Performance.")
        add("held_back", False, f"The processor stayed at {held:.0f}% of its speed even with the screen on and full "
            "power: something is holding it back.",
            "Set the maker's fan / performance profile to Performance." + hint +
            " Then Settings -> This PC -> Measure this PC again.")
    if m.get("slowed"):
        add("measured_slowed", False, "The last measurement ran while the PC was slowed down, so the station may "
            "have chosen a slower engine than this PC can run.",
            "Settings -> This PC -> Measure this PC again (with the PC plugged in and nobody using it).")
    return out


# ---------- measuring ----------

def measure(data: Path, models: Path, out: Path, py: str = sys.executable, run=subprocess.run, hold=None,
            pct=perf_pct, sleep=time.sleep, say=print) -> dict:
    """Hold the PC at full speed, run both benchmarks, put everything back. The result goes to data/pc_check.json."""
    data = Path(data)
    before = pct()
    keeper = hold if hold is not None else _keeper(data)
    keeper.set(True)
    try:
        held = pct()
        t0 = time.monotonic()
        while held is not None and held < FULL_SPEED_PCT and time.monotonic() - t0 < WAIT_S:
            sleep(5)
            held = pct()
        say(f"  processor speed: {before if before is not None else '?'}% before, "
            f"{held if held is not None else '?'}% with the screen held on")
        if held is not None and held < FULL_SPEED_PCT:
            say("  !! the processor is held back: the results below may be too slow (see 'This PC' at the end)")
        import os
        env = dict(os.environ, HAMNET_PLAN_HELD="1")      # the benchmarks leave the plan to us (tools/pick_*.py)
        for tool in ("pick_engine.py", "pick_accurate.py"):
            run([py, str(ROOT / "tools" / tool), "--models", str(models), "--out", str(out)], check=False, env=env)
        after = pct(1)
    finally:
        keeper.set(False)
    res = {"measured_at": datetime.now().isoformat(timespec="seconds"), "perf_before": before, "perf_held": held,
           "perf_after": after, "slowed": held is not None and held < FULL_SPEED_PCT}
    res |= _rtfs(out)
    save(data, measured=res)
    return res


def _keeper(data: Path):
    """The screen + system power request, and the HamNet plan unless the station already has it on (then the
    station owns the plan: switching back here would undo its hold mid-net)."""
    from .awake import HAMNET_PLAN, KeepAwake, PowerPlan
    if not station_running():
        # The installer stopped it: share its file, so a plan it left on (killed while holding) is put back first
        # and the operator's own plan is the one restored afterwards.
        return KeepAwake(reason="HamNet Station is measuring this PC", snapshot=data / "power_snapshot.json")
    snap = data / "power_snapshot_measure.json"
    try:
        if sys.platform == "win32" and PowerPlan(snap).active() == HAMNET_PLAN and not snap.exists():
            snap = None
    except Exception:  # noqa: BLE001
        snap = None
    return KeepAwake(reason="HamNet Station is measuring this PC", snapshot=snap)


def station_running(port: int = 8091) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _rtfs(engine_yaml: Path) -> dict:
    try:
        import yaml
        e = yaml.safe_load(Path(engine_yaml).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    return {"final": {k: (e.get("final") or {}).get(k) for k in ("device", "model", "rtf")},
            "accurate": {k: (e.get("accurate") or {}).get(k) for k in ("mode", "device", "model", "rtf")}}


def load(data: Path) -> dict:
    try:
        return json.loads((Path(data) / "pc_check.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(data: Path, **parts) -> dict:
    d = load(data) | parts
    p = Path(data) / "pc_check.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    tmp.replace(p)
    return d


def check(data: Path, gather_fn=gather) -> dict:
    facts = gather_fn()
    d = load(data)
    res = checks(facts, d.get("measured"))
    return save(data, facts=facts, checks=res, checked_at=datetime.now().isoformat(timespec="seconds"))


# ---------- the operator's choice ----------

def consent(data: Path, mode: str) -> str:
    """Save the 'Full speed and screen on' choice (data/operator.json, as Settings does)."""
    if mode not in MODES:
        raise ValueError("mode must be one of " + ", ".join(MODES))
    p = Path(data) / "operator.json"
    try:
        saved = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    saved.setdefault("operator", {})["keep_awake"] = mode
    saved.setdefault("pc", {})["asked_at"] = datetime.now().isoformat(timespec="seconds")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    return mode


def asked(data: Path) -> bool:
    try:
        s = json.loads((Path(data) / "operator.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool((s.get("pc") or {}).get("asked_at") or (s.get("operator") or {}).get("keep_awake"))


# ---------- while running: a transcription much slower than measured ----------

class SlowWatch:
    """Live transcriptions several times slower than this PC measured: the dashboard says so (screen off, on
    battery, a power-saving mode). Fed by Station.finish (took_s and the audio's length)."""
    FACTOR = 4.0          # this many times the measured time per second of audio
    FLOOR = 0.5           # and at least half a second per second of audio (a quick PC's 4x is still fine)
    MIN_AUDIO_S = 3.0
    N = 3                 # the median of the last 3
    HOLD = timedelta(minutes=10)

    def __init__(self, measured_rtf: float | None, clock=datetime.now):
        self.ref = measured_rtf
        self.clock = clock
        self.recent: deque = deque(maxlen=self.N)
        self.slow: dict | None = None

    def add(self, took_s: float, audio_s: float, radio: str | None = None) -> dict | None:
        if not self.ref or not took_s or not audio_s or audio_s < self.MIN_AUDIO_S:
            return self.state()
        self.recent.append(took_s / audio_s)
        if len(self.recent) == self.N:
            med = statistics.median(self.recent)
            if med > max(self.FLOOR, self.FACTOR * self.ref):
                self.slow = {"since": (self.slow or {}).get("since") or self.clock().isoformat(timespec="seconds"),
                             "at": self.clock().isoformat(timespec="seconds"), "rtf": round(med, 2),
                             "measured": self.ref, "radio": radio,
                             "say": f"This PC is transcribing slowly: {med:.2f} s per second of audio "
                                    f"(it measured {self.ref:.2f}). Is its screen off, is it on battery, or is a "
                                    "power-saving or quiet fan profile on? Settings -> This PC."}
            elif med <= max(self.FLOOR, self.FACTOR * self.ref) / 2:
                self.slow = None
        return self.state()

    def state(self) -> dict | None:
        if self.slow and self.clock() - datetime.fromisoformat(self.slow["at"]) > self.HOLD:
            self.slow = None
        return self.slow


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("measure", help="hold full speed and run both benchmarks")
    m.add_argument("--models", default=str(ROOT / "models"))
    m.add_argument("--out", default=str(ROOT / "config" / "engine.yaml"))
    sub.add_parser("check", help="check this PC's power settings and print what to fix")
    c = sub.add_parser("consent", help="save the Full speed and screen on choice")
    c.add_argument("--mode", required=True, choices=list(MODES))
    sub.add_parser("explain", help="what the station changes on this PC")
    sub.add_parser("asked", help="exit 0 when the choice was already made")
    for p in sub.choices.values():
        p.add_argument("--data", default=str(ROOT / "data"))
    a = ap.parse_args(argv)
    data = Path(a.data)
    if a.cmd == "measure":
        r = measure(data, Path(a.models), Path(a.out))
        print(f"  measured: live text {(r.get('final') or {}).get('rtf')} s per s of audio; accurate pass "
              f"{(r.get('accurate') or {}).get('mode')} {(r.get('accurate') or {}).get('model') or ''}")
        return 0
    if a.cmd == "check":
        d = check(data)
        bad = [c for c in d["checks"] if not c["ok"]]
        for c in d["checks"]:
            print(f"  {'OK ' if c['ok'] else '!! '} {c['say']}")
            if not c["ok"] and c["fix"]:
                print(f"       -> {c['fix']}")
        if not d["checks"]:
            print("  (nothing to check on this system)")
        print(f"  {len(bad)} thing(s) to fix" if bad else "  nothing to fix")
        return 0
    if a.cmd == "consent":
        print("  saved: " + MODES[consent(data, a.mode)])
        return 0
    if a.cmd == "asked":
        return 0 if asked(data) else 1
    mode = "traffic"
    try:
        mode = (json.loads((data / "operator.json").read_text(encoding="utf-8")).get("operator") or {}).get(
            "keep_awake") or mode
    except (OSError, ValueError):
        pass
    print("\n".join(explain(mode)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
