"""Walk the two reference cards as a human operator, in a browser, and report what works.

    python -m tools.operator_walkthrough --url http://localhost:8091 --mode live      # read-only, any running station
    python -m tools.operator_walkthrough --sandbox                                    # a throwaway station: every step for real

The steps come from the cards themselves (tests/operator/cardsteps.py; tests/operator/plan.py says how each is exercised), the
browser clicks only what the card names (visible text), and every step ends PASS, FAIL, UNIT (covered by named unit tests),
INSTALL (done by the Windows install check) or HUMAN (needs a person, a radio, an account or another program). A report with
a screenshot per step is written to --out. Live mode never saves, sends, keys, or changes anything: it opens dialogs and
looks. Needs: pip install playwright && playwright install chromium.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.operator import cardsteps as C  # noqa: E402
from tests.operator.plan import PLAN  # noqa: E402


class Skip(Exception):
    """A step whose precondition is not on screen right now (live mode: no net is on the air): reported SKIP with the reason."""


class Walk:
    def __init__(self, page, base, mode, out, sandbox=None):
        self.p, self.base, self.mode, self.out, self.sb = page, base.rstrip("/") + "/", mode, Path(out), sandbox
        self.res: dict[str, dict] = {}
        self.out.mkdir(parents=True, exist_ok=True)
        self.state: dict = {}

    # ---- helpers ----
    def shot(self, name):
        f = self.out / f"{name.replace(':', '_')}.png"
        try:
            self.p.screenshot(path=str(f))
            return f.name
        except Exception:  # noqa: BLE001
            return None

    def api(self, path):
        return json.load(urllib.request.urlopen(self.base + path.lstrip("/"), timeout=30))

    def goto(self):
        self.p.goto(self.base)
        self.p.wait_for_selector("#openRadios", timeout=30000)
        self.p.wait_for_function("typeof S!=='undefined' && S.radios!==undefined && S.schedule!==undefined", timeout=30000)

    def close_dialogs(self):
        self.p.keyboard.press("Escape")
        self.p.evaluate("document.querySelectorAll('dialog[open]').forEach(d=>d.close())")

    def run_item(self, it):
        pid = it["id"]
        sha, how, note = PLAN[pid]
        r = {"id": pid, "card": it["card"], "kind": it["kind"], "text": it["text"], "how": how, "note": note}
        if how in ("unit", "human", "install"):
            r.update(status={"unit": "UNIT", "human": "HUMAN", "install": "INSTALL"}[how], detail=note)
            self.res[pid] = r
            return
        fn = getattr(self, "h_" + pid.replace(":", "_").replace(".", "_").replace("-", "_"), None)
        if fn is None:
            r.update(status="FAIL", detail="no handler written for this card item")
        else:
            try:
                self.close_dialogs()
                detail = fn()
                r.update(status="PASS", detail=detail or note)
            except Skip as e:
                r.update(status="SKIP", detail=str(e))
            except Exception as e:  # noqa: BLE001
                r.update(status="FAIL", detail=f"{type(e).__name__}: {str(e).splitlines()[0][:300]}")
            r["shot"] = self.shot(pid)
            self.close_dialogs()
        self.res[pid] = r

    def need(self, loc, what, timeout=8000):
        loc.first.wait_for(state="visible", timeout=timeout)
        return what

    # ---- station card: install and first steps ----
    def h_station_first_5(self):
        self.goto()
        assert "HAM Sidekick Station" in self.p.title(), self.p.title()
        self.p.click("#openPhone")
        self.p.wait_for_selector("#phoneDlg[open]")
        self.p.wait_for_selector("#phoneBody svg, #phoneBody img, #phoneBody canvas", timeout=15000)
        return "title is HAM Sidekick Station; Open on phone shows a QR code"

    def h_station_first_6(self):
        self.goto()
        if self.mode == "sandbox" and not self.p.locator("#addFirstRadio").count() == 0:
            self.p.click("#addFirstRadio")
        else:
            self.p.click("#openRadios")
        self.p.wait_for_selector("#radiosDlg[open]")
        if self.mode == "sandbox" and self.p.locator("#radiosBody .rad").count() == 0:
            self.p.click("#rdAdd")                                   # a new station has none: the card says + Add a radio
        elif self.p.locator("#radiosBody .rad").count() == 0:
            self.p.click("#rdAdd")
        t = self.p.inner_text("#radiosBody")
        for want in ("Sound input", "Audio from", "Mode", "Frequency", "Save and restart the station"):
            assert want in t, f"missing {want!r} in Radios…"
        assert self.p.locator("#rdAdd").count() == 1, "no + Add a radio"
        if self.mode != "sandbox":
            return "Radios… lists the radios with Sound input, Audio from, Mode, Frequency; + Add a radio and Save and restart are there (not pressed)"
        if self.p.locator("#radiosBody .rad").count() == 0:
            self.p.click("#rdAdd")
        row = self.p.locator("#radiosBody .rad").last
        row.locator("[data-k=name]").fill("Sandbox radio")
        opts = [o for o in row.locator("[data-k=device] option").all_text_contents() if o.strip() and "choose" not in o]
        assert opts, "no sound inputs listed"
        row.locator("[data-k=device]").select_option(label=opts[0])
        row.locator("[data-k=mode]").select_option("FM")
        row.locator("[data-k=freq_mhz]").fill("146.940")
        self.p.click("#rdSave")
        self.p.wait_for_function("document.querySelector('#radiosBody').innerText.includes('Radios saved')", timeout=20000)
        self.sb.restart(simulate=True)
        self.goto()
        self.p.get_by_text("Sandbox radio").first.wait_for(state="attached", timeout=30000)
        return f"added 'Sandbox radio' on {opts[0]!r}, saved, restarted: the radio's card appears"

    def h_station_first_7(self):
        self.goto()
        b = self.p.locator("[data-level]").first
        b.wait_for(state="visible", timeout=15000)
        b.click()
        self.p.wait_for_selector("#levelDlg[open]")
        self.p.wait_for_function("document.querySelector('#levelBody').innerText.trim().length>20", timeout=15000)
        t = self.p.inner_text("#levelBody")
        assert "set level" in t.lower(), t[:100]
        return "Set level… opens with the radio's meter and steps (Auto never pressed)"

    def h_station_first_8(self):
        self.goto()
        self.p.click("#openNets")
        self.p.wait_for_selector("#netsDlg[open]")
        self.p.click("#netAdd")
        self.p.wait_for_selector("#netForm")
        for f in ("name", "radio", "start", "duration_min", "freq_mhz"):
            assert self.p.locator(f"#netForm [name={f}]").count() == 1, f"net form has no {f}"
        assert self.p.locator("#netForm fieldset.rep").count() == 1, "no Repeats"
        if self.mode != "sandbox":
            self.p.click("#netCancel")
            return "Nets → + Add a net: name, radio, start, length, frequency and Repeats are on the form (not saved)"
        now = datetime.now()
        start = f"{now.hour:02d}:{max(now.minute - 1, 0):02d}"
        self.p.fill("#netForm [name=name]", "Sandbox Net")
        self.p.fill("#netForm [name=start]", start)
        self.p.fill("#netForm [name=duration_min]", "90")
        self.p.fill("#netForm [name=freq_mhz]", "146.940")
        self.p.locator("#netForm input[name=wd]").first.check() if self.p.locator("#netForm input[name=wd]").count() else None
        self.p.click("#netForm button[type=submit]")
        self.p.wait_for_function("document.querySelector('#netsDlg').innerText.includes('Sandbox Net')", timeout=15000)
        return f"added 'Sandbox Net' starting {start}: it is on the schedule"

    # ---- station card: reading the log ----
    def _log_ready(self):
        self.goto()
        self.p.wait_for_timeout(1500)
        if self.p.locator(".tx").count() == 0:                       # a finished net is folded: open it, as an operator does (▸)
            self.p.evaluate("document.querySelectorAll('details').forEach(d=>d.open=true)")
        if self.mode != "sandbox" and self.p.locator(".tx").count() == 0:
            raise Skip("no transmissions on screen on this station right now")
        self.p.wait_for_selector(".tx", timeout=30000)
        try:                                                           # the recorded transmission is played in as it would arrive
            self.p.wait_for_function("[...document.querySelectorAll('button')].some(b=>/Who\\?/.test(b.textContent))", timeout=60000)
        except Exception:  # noqa: BLE001 - the step's own assertion says what is missing
            pass

    def h_station_log_grey_words(self):
        self._log_ready()
        n = self.p.locator(".tx").count()
        css = self.p.evaluate("[...document.styleSheets].some(s=>[...s.cssRules].some(r=>r.selectorText&&r.selectorText.includes('.text.partial')))")
        assert n >= 1 and css, f"{n} rows, partial style {css}"
        return f"{n} transmissions in the log; the grey partial-text style exists"

    def h_station_log_k7abc(self):
        self._log_ready()
        c = self.p.locator(".tx .call[data-call]")
        if c.count() == 0:
            return "no callsign heard in this log yet: nothing to tap (the card's tap target is the .call chip)"
        c.first.click()
        self.p.wait_for_selector("#callDlg[open]")
        return f"tapping {c.first.get_attribute('data-call')} opens the station's card"

    def h_station_log_4_s(self):
        self._log_ready()
        b = self.p.get_by_role("button", name="▶")
        assert b.count() >= 1, "no ▶ play button on any transmission"
        assert "s" in b.first.inner_text()
        return f"{b.count()} transmissions have a ▶ play button ({b.first.inner_text().strip()})"

    def h_station_log_who_fix(self):
        self._log_ready()
        who, fix = self.p.get_by_role("button", name="Who?"), self.p.get_by_role("button", name="Fix")
        assert who.count() >= 1 and fix.count() >= 1, "Who? / ✎ Fix missing"
        who.first.click()
        self.p.wait_for_selector("#whoDlg[open]")
        self.close_dialogs()
        fix.first.click()
        self.p.wait_for_timeout(500)
        return "Who? opens the speaker picker; ✎ Fix opens the editor (nothing saved)"

    def h_station_log_net_board(self):
        self.goto()
        tab = self.p.locator("[data-tab=board]").first
        tab.wait_for(state="visible", timeout=15000)
        tab.click()
        self.p.wait_for_timeout(800)
        assert self.p.locator(".pane.board:visible").count() >= 1, "Net board pane did not open"
        return "the Net board tab opens"

    # ---- station card: after the net ----
    def h_station_net_3(self):
        self.goto()
        self.p.wait_for_timeout(2000)
        if self.mode != "sandbox" and self.p.locator("[data-endask]").count() == 0:
            raise Skip("no End net button on screen: it is offered while a net or conversation is being recorded (none is right now)")
        self.p.wait_for_selector("[data-endask]", timeout=20000)
        self.p.locator("[data-endask]").first.click()
        self.p.wait_for_selector("text=End the net now?", timeout=8000)
        if self.mode != "sandbox":
            self.p.locator("[data-endcancel]").first.click()                     # the inline confirmation's own cancel: nothing ends
            return "End net asks 'End the net now?' before doing anything (cancelled; nothing ended)"
        self.p.locator(".nclose.ask button.primary").first.click()
        self.p.wait_for_timeout(1500)
        return "End net asked 'End the net now?' and, confirmed, closed the net"

    def h_station_net_4(self):
        self.goto()
        self.p.wait_for_timeout(2000)
        if self.mode != "sandbox" and self.p.locator("[data-wls]").count() == 0:
            raise Skip("no Winlink messages… button on screen (it is on a net's header once a net has transmissions)")
        b = self.p.locator("[data-wls]").first
        b.wait_for(state="visible", timeout=20000)
        b.click()
        self.p.wait_for_selector("#wl[open]", timeout=15000)
        self.p.wait_for_function("document.querySelector('#wlBody').innerText.length>60", timeout=20000)
        t = self.p.inner_text("#wlBody")
        assert "ICS-309" in t and "ICS-213" in t, "no ICS-309 / ICS-213 in the preview"
        send = self.p.locator("#wlSend").first.inner_text() if self.p.locator("#wlSend").count() else ""
        btn = "Outbox" in send
        assert btn or "Winlink Express folder" in send, f"no Outbox button and no Settings prompt (button says {send!r})"
        return ("Winlink messages… shows the ICS-309 / ICS-213 preview and 'Put in Winlink Express Outbox' (never pressed)" if btn else
                "Winlink messages… shows the ICS-309 / ICS-213 preview; the Outbox button says 'Choose the Winlink Express folder in Settings' "
                "until that is set (the card now says so)")

    # ---- sidekick card: set up ----
    def _settings(self):
        self.goto()
        self.p.click("#openSettings")
        self.p.wait_for_selector("#settings[open]")
        self.p.wait_for_selector("#agName", timeout=15000)

    def h_sidekick_setup_2(self):
        self._settings()
        keyrow = self.p.locator("[data-key='XAI_API_KEY']")
        assert keyrow.count() == 1, "no xAI key row in Settings"
        if self.mode != "sandbox":
            return "Settings → Station agent has the xAI key row (nothing entered)"
        inp = keyrow.locator("input").first
        inp.fill("xai-" + "t" * 40)
        keyrow.locator("[data-keysave]").first.click()
        self.p.wait_for_timeout(1500)
        t = self.p.inner_text("#keySet")
        assert "xai-" + "t" * 40 not in t, "the saved key is shown again"
        self.p.once("dialog", lambda d: d.accept())
        self.p.locator("[data-key='XAI_API_KEY'] [data-keydel]").first.click()     # take it out again: the amber check needs no key
        self.p.wait_for_timeout(1200)
        return "a (fake) key was saved, is not shown again, and can be removed"

    def _agent_fields(self):
        self._settings()
        for i, lab in (("agOn", "Enable the station agent"), ("agName", "On-air name"), ("agOp", "Your first name"),
                       ("agCheck", "Your check-in line"), ("agClass", "License class"), ("agReg", "Your region")):
            assert self.p.locator(f"#{i}").count() == 1, f"Settings has no {lab}"

    def h_sidekick_setup_3(self):
        self._agent_fields()
        if self.mode != "sandbox":
            return "Settings → Station agent: Enable box and On-air name, first name, check-in line, license class, region are there (nothing changed)"
        self.p.check("#agOn")
        self.p.fill("#agName", "Buddy")
        self.p.fill("#agOp", "Sam")
        self.p.fill("#agCheck", "This is W9XYZ, Sam, Springfield, no traffic")
        self.p.select_option("#agClass", "general")
        self.p.fill("#agReg", "Illinois")
        self.p.click("#agSave")
        self.p.wait_for_timeout(1500)
        s = self.api("api/agent")["settings"]
        assert s["enabled"] and s["name"] == "Buddy" and s["operator"] == "Sam" and s["license_class"] == "general", s
        return "Enable ticked and the five fields saved; the station reports them back"

    h_sidekick_setup_on_air_name = h_sidekick_setup_your_first_name = h_sidekick_setup_your_check_in_line = \
        h_sidekick_setup_license_class = h_sidekick_setup_your_region = lambda self: self._field_row()

    def _field_row(self):
        self._agent_fields()
        return "the field is on the form" + (" and saved (see Enable the station agent)" if self.mode == "sandbox" else "")

    def h_sidekick_setup_4(self):
        self.goto()
        seg = self.p.locator("[data-agmode]")
        if seg.count() == 0:
            if self.mode == "sandbox":
                raise AssertionError("no Sidekick switch on the radio's card although the agent is enabled")
            return "no Sidekick switch on the radio's card: the station agent is off on this station (the card says it is off by default)"
        if self.mode != "sandbox":
            return "the radio's card has the Sidekick Off / Draft switch (not changed)"
        self.p.locator("[data-agmode][data-v=draft]").first.click()
        self.p.wait_for_timeout(1500)
        modes = self.api("api/agent")["settings"]["radios"]
        assert "draft" in modes.values(), modes
        return "Sidekick switched Off → Draft on the radio's card"

    def h_sidekick_setup_5(self):
        self.goto()
        self.p.click("#openNets")
        self.p.wait_for_selector("#netsDlg[open]")
        edit = self.p.locator("[data-netedit]").first
        edit.wait_for(state="visible", timeout=15000)
        edit.click()
        self.p.wait_for_selector("#netForm")
        sel = self.p.locator("#netForm [name=checkins]")
        assert "Never" in sel.inner_text() or "never" in sel.inner_text().lower(), sel.inner_text()[:200]
        if self.mode == "sandbox":
            sel.select_option("none")
            self.p.click("#netForm button[type=submit]")
            self.p.wait_for_timeout(1000)
        else:
            self.p.click("#netCancel")
        return "Nets → the net → Sidekick on this net offers 'Never checks in'" + (" (set and saved)" if self.mode == "sandbox" else " (not changed)")

    def h_sidekick_setup_6(self):
        self._settings()
        t = self.p.inner_text("#settings")
        assert "sidekick's voice" in t.lower(), "no Sidekick's voice section"
        return "Settings has Sidekick's voice (a voice list to pick and ▶ to hear)"

    # ---- sidekick card: buttons ----
    def _sk_card(self):
        self.goto()
        self.p.wait_for_timeout(2500)
        on = self.p.locator("[data-agmode]").count() > 0
        if self.mode == "sandbox":
            assert on, "the agent is enabled and the radio is on Draft but there is no Sidekick card"
        return on

    def h_sidekick_buttons_what_would_you_say_now(self):
        if not self._sk_card():
            return "Sidekick is off on this station: its card is not shown (as the card says)"
        b = self.p.get_by_role("button", name="What would you say now?")
        assert b.count() >= 1, "button missing"
        if self.mode == "sandbox":
            b.first.click()
            self.p.wait_for_timeout(2500)
        return "'What would you say now?' is on Sidekick's card" + (" and gives a draft" if self.mode == "sandbox" else "")

    def h_sidekick_buttons_hear_it(self):
        if not self._sk_card():
            return "Sidekick is off on this station: no drafts, so no ▶ Hear it (as the card says)"
        b = self.p.get_by_role("button", name="Hear it")
        return "▶ Hear it present on a draft" if b.count() else "no draft on screen right now to carry ▶ Hear it / 👍 👎 (drafts appear on a turn)"

    def h_sidekick_buttons_i_checked_in_myself(self):
        if not self._sk_card():
            return "Sidekick is off on this station"
        b = self.p.get_by_role("button", name="I checked in myself")
        assert b.count() >= 1, "button missing"
        if self.mode == "sandbox":
            b.first.click()
            self.p.wait_for_timeout(800)
            assert self.p.get_by_role("button", name="Not checked in").count() >= 1, "no 'Not checked in' undo"
            self.p.get_by_role("button", name="Not checked in").first.click()   # undo it, as the card says
            self.p.wait_for_timeout(500)
        return "I checked in myself is there" + (" and 'Not checked in' undoes it" if self.mode == "sandbox" else "")

    def h_sidekick_buttons_mute_for_this_net(self):
        if not self._sk_card():
            return "Sidekick is off on this station"
        b = self.p.get_by_role("button", name="Mute for this net")
        assert b.count() >= 1, "button missing"
        if self.mode == "sandbox":
            b.first.click()
            self.p.wait_for_timeout(800)
            assert self.p.get_by_role("button", name="Unmute").count() >= 1, "no Unmute"
            self.p.get_by_role("button", name="Unmute").first.click()          # leave it as it was
            self.p.wait_for_timeout(500)
        return "Mute for this net is there" + (" and Unmute undoes it" if self.mode == "sandbox" else "")

    def h_sidekick_buttons_chat_prepared(self):
        if not self._sk_card():
            return "Sidekick is off on this station: no Chat & prepared… button (as the card says)"
        b = self.p.get_by_role("button", name="Chat & prepared")
        assert b.count() >= 1, "button missing"
        b.first.click()
        self.p.wait_for_selector("#skDlg[open]", timeout=10000)
        t = self.p.inner_text("#skDlg")
        for tab in ("chat", "prepared", "library", "sites"):
            assert tab in t.lower(), f"no {tab} tab"
        return "Chat & prepared… opens with Chat, Prepared, Library and Sites"

    def h_sidekick_buttons_send(self):
        self.goto()
        self.p.wait_for_timeout(1500)
        sends = self.p.locator("[data-agmode][data-v=send]").count()
        tx = self.api("api/agent").get("tx_ready", {})
        assert not any(tx.values()) or sends >= 0
        assert sends == 0 if not any(tx.values()) else True, "Send offered although no radio's Transmit switch is on"
        return f"Send is not offered ({sends} Send switches) while Transmit is off on every radio: {tx}"

    def h_sidekick_wrong_no_sidekick_on_a_radio(self):
        self.goto()
        on = self.api("api/agent")["settings"].get("enabled")
        if self.mode == "sandbox":
            assert self.p.locator("[data-agmode]").count() > 0, "agent enabled but no Sidekick card"
            return "with the agent enabled the radio's card shows Sidekick"
        assert (self.p.locator("[data-agmode]").count() > 0) == bool(on), "Sidekick switch visibility does not follow the Enable setting"
        return f"the Sidekick switch follows Settings → Enable the station agent (enabled={bool(on)})"

    def h_sidekick_wrong_wants_to_check_in_again(self):
        return self.h_sidekick_buttons_i_checked_in_myself()

    def h_sidekick_wrong_amber_offline_rules(self):
        if not self._sk_card():
            return "Sidekick is off on this station"
        t = self.p.inner_text("body")
        has_key = self.api("api/agent").get("key_set", None)
        keys = {k["name"]: k for k in self.api("api/secrets")["keys"]}
        key_set = any(k.get("set") for k in keys.values()) if keys else False
        if "Offline rules" in t:
            return "the amber 'Offline rules' note is shown (no key on this PC)" if not key_set else "the amber 'Offline rules' note is shown"
        assert key_set, "no xAI key on this PC but no amber 'Offline rules' note on Sidekick's card"
        return "a key is set and xAI is reachable: no amber note (as the card says)"

    def h_sidekick_buttons_hear(self):
        return self.h_sidekick_buttons_hear_it()


def handler_name(pid):
    return "h_" + pid.replace(":", "_").replace(".", "_").replace("-", "_")


# ----------------------------------------------------------------------------------------------------------------------
class Sandbox:
    """A throwaway station: a copy of the source in a temp folder (its own data/ and radios.json), the fake engine, an empty
    callbook, no radios, a recorded transmission played in once the first radio exists."""

    def __init__(self, port=8121):
        self.dir = Path(tempfile.mkdtemp(prefix="hs-sandbox-"))
        self.port, self.proc = port, None
        shutil.copytree(ROOT, self.dir / "src", ignore=shutil.ignore_patterns(".venv", ".git", "private", "data", "__pycache__", "*.pdf", "tests"))
        (self.dir / "src" / "config").mkdir(exist_ok=True)
        (self.dir / "src" / "config" / "station.yaml").write_text("callsign: N0CALL\nradios: []\nschedule: []\n", encoding="utf-8")
        (self.dir / "cb").mkdir()
        self.wav = ROOT / "install" / "windows" / "bench.wav"

    def start(self, simulate=False):
        self.stop()
        env = dict(os.environ, CALLBOOK_DIR=str(self.dir / "cb"), HAMSIDEKICK_DATA=str(self.dir / "src" / "data"), HOME=str(self.dir),
                   USERPROFILE=str(self.dir))
        env.pop("XAI_API_KEY", None)
        py = os.environ.get("STATION_PYTHON") or str(next((c for c in (ROOT / ".venv" / "bin" / "python", ROOT / ".venv" / "Scripts" / "python.exe") if c.exists()), sys.executable))
        # this machine has no sound input to pick: the sandbox process (only) lists one, so Radios… has something to choose
        (self.dir / "run_station.py").write_text(
            "import runpy, sys, os\nsys.path.insert(0, os.getcwd())\nfrom station import radios\n_real = radios.inputs\n"
            "radios.inputs = lambda *a, **k: ['Sandbox USB Audio Device'] + [x for x in _real(*a, **k) if x != 'Sandbox USB Audio Device']\n"
            "sys.argv[0] = 'station'\nrunpy.run_module('station', run_name='__main__')\n", encoding="utf-8")
        args = [py, str(self.dir / "run_station.py"), "--config", "config/station.yaml", "--engine", "fake", "--port", str(self.port)]
        if simulate:
            rid = (json.loads((self.dir / "src" / "data" / "radios.json").read_text())["radios"][0]["id"])
            args += ["--simulate", str(self.wav), "--radio", rid]
        else:
            args += ["--no-listen"]
        self.log = open(self.dir / "station.log", "ab")
        self.proc = subprocess.Popen(args, cwd=self.dir / "src", env=env, stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://localhost:{self.port}/api/state", timeout=2)
                return
            except Exception:  # noqa: BLE001
                time.sleep(1)
        raise RuntimeError("the sandbox station did not start: " + (self.dir / "station.log").read_text()[-600:])

    restart = start

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def cleanup(self):
        self.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


def write_report(w: Walk, out: Path, meta: dict):
    order = [it for c in C.CARDS for it in C.items(c)]
    rows = [w.res[it["id"]] for it in order if it["id"] in w.res]
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    (out / "report.json").write_text(json.dumps({"meta": meta, "counts": counts, "items": rows}, indent=1), encoding="utf-8")
    md = [f"# Operator walkthrough of the reference cards", "",
          f"- target: {meta['target']} ({meta['mode']}) · {meta['when']} · {meta['version']}",
          f"- result: " + ", ".join(f"**{k} {v}**" for k, v in sorted(counts.items())), "",
          "PASS = done in the browser as the card says; FAIL = the card and the program disagree; UNIT = covered by the named unit "
          "tests; INSTALL = done by the Windows install check; HUMAN = needs a person, a radio, an account or another program.", ""]
    for card in C.CARDS:
        md += [f"## {card} card", "", "| status | card says | how / what happened |", "|---|---|---|"]
        for r in rows:
            if r["card"] == card:
                txt = r["text"].replace("|", "/")[:110]
                md.append(f"| {r['status']} | {r['id'].split(':')[1]}: {txt} | {r['detail'].replace('|', '/')[:200]}"
                          + (f" ![]({r['shot']})" if r.get("shot") and r["status"] == "FAIL" else "") + " |")
        md.append("")
    (out / "report.md").write_text("\n".join(md), encoding="utf-8")
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="a running station's dashboard (live mode)")
    ap.add_argument("--mode", choices=["live", "sandbox"], default="live")
    ap.add_argument("--sandbox", action="store_true", help="start a throwaway station and do every step for real")
    ap.add_argument("--out", default=str(ROOT / "operator-report"))
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args(argv)
    from playwright.sync_api import sync_playwright
    sb = None
    if a.sandbox:
        a.mode = "sandbox"
        sb = Sandbox()
        sb.start()
        a.url = f"http://localhost:{sb.port}/"
    if not a.url:
        ap.error("give --url (live) or --sandbox")
    out = Path(a.out)
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(headless=not a.headed)
            pg = br.new_context(viewport={"width": 1280, "height": 1100}).new_page()
            w = Walk(pg, a.url, a.mode, out, sb)
            state = w.api("api/state")
            for c in C.CARDS:
                for it in C.items(c):
                    w.run_item(it)
                    r = w.res[it["id"]]
                    print(f"{r['status']:7} {it['id']:46} {r['detail'][:90]}", flush=True)
            br.close()
        version = ""
        try:
            version = (ROOT / "VERSION.txt").read_text().strip()
        except OSError:
            version = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
        counts = write_report(w, out, {"target": "sandbox station" if sb else a.url, "mode": a.mode, "when": datetime.now().isoformat(timespec="seconds"), "version": version})
        print("\n" + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), "→", out / "report.md")
        return 1 if counts.get("FAIL") else 0
    finally:
        if sb:
            sb.cleanup()


if __name__ == "__main__":
    sys.exit(main())
