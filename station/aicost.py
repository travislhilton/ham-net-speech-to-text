"""What the AI services cost (owner 2026-11-14: "track the costs of using the xAI API so that the user knows what it is
costing them" ... "can we see how many credits remain on the user's xAI account ... a link pointing to where they
buy/up the credits").

1. The ledger: every paid call the station makes (Sidekick's drafts and chat, the net summary, Find nets, Grok
   speech-to-text, Sidekick's voice, the optional Anthropic comparison) is written to data/ai_cost/<YYYY-MM>.jsonl:
   when, which feature, which model, the tokens / searches / audio seconds / characters, and the cost. The cost is
   xAI's own (`usage.cost_in_usd_ticks`, 1e10 ticks to the dollar) when the answer carries it, else estimated from
   PRICES (xAI's published prices, 2026-11-14) and marked "est". A call with no known price is counted, not priced.
2. A monthly budget (optional, this station's own spend): a warning at WARN_AT of it; at 100% the background features
   (net summaries, Grok speech-to-text, the voice's pre-made clips) stop until the month turns. What the operator
   starts (a Sidekick reply, chat, Find nets) still runs.
3. The account (optional): with an xAI *management* key (Settings -> AI costs; console -> Settings -> Management
   Keys), every REFRESH_S the station reads the team's prepaid credit and this month's spend for the whole account
   (all stations) from xAI's Management API, and warns below the low-credit line. Only these read calls are ever
   made: never a top-up (credit is bought on xAI's own billing page, BUY_URL).
4. "Out of credit": an xAI refusal that says so is remembered and shown until a call works again.

Each station keeps its own ledger; the account numbers (and the bill on the xAI console) are the true total.
"""
from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BUY_URL = "https://console.x.ai/team/default/billing"
USAGE_URL = "https://console.x.ai/team/default/usage"
MGMT = "https://management-api.x.ai"
MGMT_KEY = "XAI_MANAGEMENT_KEY"
TICKS_PER_USD = 1e10
WARN_AT = 0.8
REFRESH_S = 30 * 60
LOW_CREDIT_USD = 5.0

# USD per 1M tokens: input, cached input, output (xAI models page, 2026-11-14; at 200k+ prompt tokens all double).
PRICES = {
    "grok-4.20": (1.25, 0.20, 2.50),
    "grok-4.7": (2.00, 0.50, 6.00),
    "grok-4.6": (2.00, 0.50, 6.00),
    "grok-4.5": (2.00, 0.30, 6.00),
    "grok-4.3": (1.25, 0.20, 2.50),
}
LONG_PROMPT = 200_000
WEB_SEARCH_USD = 5.0 / 1000          # per web_search call
STT_USD_PER_HOUR = 0.10              # Speech to Text (REST)
TTS_USD_PER_MCHAR = 15.0             # Text to Speech, per 1M characters

FEATURES = {"draft": "Sidekick drafts", "answer": "Sidekick (thinking model)", "chat": "Sidekick chat",
            "topic": "Sidekick prepared topics", "net_summary": "Net summaries", "find_nets": "Find nets",
            "speech_to_text": "Grok speech-to-text", "text_to_speech": "Sidekick's voice"}
BACKGROUND = ("net summaries", "Grok speech-to-text", "the voice's pre-made clips")

_current: "Costs | None" = None


def use(c: "Costs | None"):
    global _current
    _current = c


def record(feature: str, model: str = "", usage: dict | None = None, **units):
    """Write one paid call to the ledger of the running station (a no-op when none is)."""
    c = _current
    if c is not None:
        try:
            c.record(feature, model, usage, **units)
        except Exception as e:  # noqa: BLE001 - the ledger never breaks the call it counts
            print(f"aicost: {type(e).__name__}: {e}", flush=True)


def refused(e: urllib.error.HTTPError, body: str):
    c = _current
    if c is not None:
        c.refused(e.code, body)


def xai_json(req: urllib.request.Request, timeout: float, feature: str | None, model: str = "") -> dict:
    """urlopen + json for one xAI inference call: the answer's usage goes in the ledger; a refusal for want of
    credit is remembered (and re-raised as the same HTTPError, its body still readable)."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read() or b""
        except Exception:  # noqa: BLE001
            pass
        refused(e, body.decode("utf-8", "replace"))
        raise urllib.error.HTTPError(e.url, e.code, e.msg, e.hdrs, io.BytesIO(body)) from None
    if feature:                                                  # None: the caller records (tool rounds)
        record(feature, model or str(d.get("model") or ""), d.get("usage") or {})
    return d


def _price(model: str):
    m = str(model or "").lower()
    for k in sorted(PRICES, key=len, reverse=True):
        if m.startswith(k):
            return PRICES[k]
    return None


def cost(model: str, usage: dict | None, **units) -> tuple[float | None, str]:
    """(USD, "xai" | "est" | "none") for one call."""
    u = usage or {}
    ticks = u.get("cost_in_usd_ticks")
    if isinstance(ticks, (int, float)) and ticks >= 0 and (ticks or not _tokens(u)[0]):
        return ticks / TICKS_PER_USD, "xai"
    if units.get("audio_s") is not None:
        return float(units["audio_s"]) / 3600 * STT_USD_PER_HOUR, "est"
    if units.get("chars") is not None:
        return float(units["chars"]) / 1e6 * TTS_USD_PER_MCHAR, "est"
    p = _price(model)
    if not p:
        return None, "none"
    tin, cached, tout = _tokens(u)
    k = 2 if tin >= LONG_PROMPT else 1
    usd = k * ((tin - cached) * p[0] + cached * p[1] + tout * p[2]) / 1e6
    usd += _searches(u) * WEB_SEARCH_USD
    return usd, "est"


def _tokens(u: dict) -> tuple[int, int, int]:
    """(prompt, cached prompt, output incl. reasoning) from a chat-completions or responses usage."""
    tin = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    det = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
    cached = min(tin, int(det.get("cached_tokens") or 0))
    out = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    odet = u.get("completion_tokens_details") or u.get("output_tokens_details") or {}
    reasoning = int(odet.get("reasoning_tokens") or 0)
    total = int(u.get("total_tokens") or 0)
    if reasoning and total and total >= tin + out + reasoning:     # xAI counts reasoning apart from completion
        out += reasoning
    return tin, cached, out


def _searches(u: dict) -> int:
    d = u.get("server_side_tool_usage_details") or {}
    return int(d.get("web_search_calls") or 0)


class Costs:
    def __init__(self, st, opener=None, clock=None):
        self.st = st
        self.dir = Path(st.data) / "ai_cost"
        self.cfg_path = Path(st.data) / "ai_cost.json"
        self.lock = threading.Lock()
        self.opener = opener or urllib.request.urlopen
        self.clock = clock or (lambda: datetime.now())
        self.cfg = {"budget_usd": 0.0, "low_credit_usd": LOW_CREDIT_USD}
        self.account: dict = {}
        self.out_of_credit: dict | None = None
        try:
            saved = json.loads(self.cfg_path.read_text(encoding="utf-8"))
            self.cfg.update({k: saved[k] for k in self.cfg if k in saved})
            self.account = saved.get("account") or {}
        except (OSError, ValueError):
            pass
        self._last_refresh = 0.0
        self._month: tuple[str, float] | None = None         # (YYYY-MM, USD) so the snapshot never re-reads the file

    # ----- the ledger -----
    def record(self, feature: str, model: str = "", usage: dict | None = None, **units):
        usd, how = cost(model, usage, **units)
        u = usage or {}
        tin, cached, tout = _tokens(u)
        now = self.clock()
        row = {"at": now.isoformat(timespec="seconds"), "feature": feature, "model": model, "usd": usd, "how": how}
        if tin or tout:
            row |= {"in": tin, "cached": cached, "out": tout}
        if _searches(u):
            row["searches"] = _searches(u)
        row |= {k: v for k, v in units.items() if v is not None}
        with self.lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / f"{now:%Y-%m}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            if self._month and self._month[0] == f"{now:%Y-%m}":
                self._month = (self._month[0], self._month[1] + (usd or 0))
            self.out_of_credit = None                   # a call that went through: there is credit again
        return row

    def refused(self, code: int, body: str):
        b = (body or "").lower()
        if code in (402, 403, 429) and any(w in b for w in ("credit", "spending limit", "billing", "balance",
                                                             "exhausted", "insufficient")):
            self.out_of_credit = {"at": self.clock().isoformat(timespec="seconds"), "code": code,
                                  "say": (body or "")[:200]}

    def rows(self, month: str) -> list[dict]:
        try:
            lines = (self.dir / f"{month}.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def month_usd(self, now: datetime | None = None) -> float:
        m = f"{(now or self.clock()):%Y-%m}"
        with self.lock:
            if not self._month or self._month[0] != m:
                self._month = (m, sum(r.get("usd") or 0 for r in self.rows(m)))
            return self._month[1]

    # ----- the budget -----
    def over_budget(self, now: datetime | None = None) -> bool:
        b = float(self.cfg.get("budget_usd") or 0)
        return bool(b) and self.month_usd(now) >= b

    def allow_background(self) -> bool:
        return not self.over_budget()

    def settings(self, body: dict) -> dict:
        with self.lock:
            for k in ("budget_usd", "low_credit_usd"):
                if k in body:
                    v = float(body[k] or 0)
                    if not 0 <= v <= 100000:
                        raise ValueError(f"{k}: 0 to 100000")
                    self.cfg[k] = round(v, 2)
            self._save()
        return self.view()

    def _save(self):
        self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cfg | {"account": self.account}, indent=2), encoding="utf-8")
        tmp.replace(self.cfg_path)

    # ----- the account (management key) -----
    def _mgmt(self, method: str, path: str, key: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(MGMT + path, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with self.opener(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")

    def refresh(self) -> dict:
        """Read the account's prepaid credit and this month's spend (read-only calls; never a top-up)."""
        from . import secrets
        key = secrets.read(self.st.root, MGMT_KEY)
        if not key:
            self.account = {}
            return self.account
        a = {"at": self.clock().isoformat(timespec="seconds")}
        try:
            v = self._mgmt("GET", "/auth/management-keys/validation", key)
            team = v.get("scopeId") if v.get("scope") == "SCOPE_TEAM" and v.get("scopeId") else v.get("teamId")
            if not team:
                raise ValueError("the management key names no team (make it for a team, not the organization)")
            a["team"] = team
            b = self._mgmt("GET", f"/v1/billing/teams/{team}/prepaid/balance", key)
            cents = float(((b.get("total") or {}).get("val")) or 0)
            a["credit_usd"] = round(max(0.0, -cents) / 100, 2)       # purchases are negative: what is left
        except urllib.error.HTTPError as e:
            a["error"] = f"xAI answered {e.code}" + (" (the management key was refused or lacks billing access)"
                                                     if e.code in (401, 403) else "")
        except Exception as e:  # noqa: BLE001
            a["error"] = f"{type(e).__name__}: {e}"
        if a.get("team"):
            try:
                now = datetime.now(timezone.utc)
                start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                q = {"analyticsRequest": {
                    "timeRange": {"startTime": f"{start:%Y-%m-%d %H:%M:%S}",
                                  "endTime": f"{now + timedelta(hours=1):%Y-%m-%d %H:00:00}", "timezone": "Etc/GMT"},
                    "timeUnit": "TIME_UNIT_NONE", "values": [{"name": "usd", "aggregation": "AGGREGATION_SUM"}],
                    "groupBy": ["description"], "filters": []}}
                u = self._mgmt("POST", f"/v1/billing/teams/{a['team']}/usage", key, q)
                by = []
                for s in u.get("timeSeries") or []:
                    usd = sum(float((p.get("values") or [0])[0] or 0) for p in s.get("dataPoints") or [])
                    if usd:
                        by.append({"what": " ".join(s.get("groupLabels") or s.get("group") or ["?"]),
                                   "usd": round(usd, 4)})
                a["month_usd"] = round(sum(x["usd"] for x in by), 4)
                a["by"] = sorted(by, key=lambda x: -x["usd"])[:12]
                a["partial"] = bool(u.get("limitReached"))
            except urllib.error.HTTPError as e:
                a["usage_error"] = f"xAI answered {e.code} for the account's usage"
            except Exception as e:  # noqa: BLE001
                a["usage_error"] = f"{type(e).__name__}: {e}"
        with self.lock:
            self.account = a
            self._save()
        if a.get("error"):
            print(f"aicost: account: {a['error']}", flush=True)
        return a

    def tick(self):
        if time.monotonic() - self._last_refresh >= REFRESH_S:
            self._last_refresh = time.monotonic()
            self.refresh()

    def run(self, stop: threading.Event):
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                print(f"aicost: {type(e).__name__}: {e}", flush=True)
            if stop.wait(60):
                return

    # ----- what the dashboard shows -----
    def warnings(self, now: datetime | None = None) -> list[dict]:
        now = now or self.clock()
        out = []
        b = float(self.cfg.get("budget_usd") or 0)
        spent = self.month_usd(now)
        if b and spent >= b:
            out.append({"kind": "budget", "level": "bad",
                        "say": f"This station's AI spend this month is ${spent:.2f}, at its ${b:.2f} budget: "
                               f"{', '.join(BACKGROUND)} are paused until the 1st."})
        elif b and spent >= WARN_AT * b:
            out.append({"kind": "budget", "level": "warn",
                        "say": f"This station's AI spend this month is ${spent:.2f} of its ${b:.2f} budget."})
        if self.out_of_credit:
            out.append({"kind": "credit", "level": "bad", "buy": BUY_URL,
                        "say": "xAI refused a request: the account looks out of credit. Summaries, Sidekick and Find "
                               "nets need credit to work."})
        low = float(self.cfg.get("low_credit_usd") or 0)
        cr = self.account.get("credit_usd")
        if cr is not None and low and cr < low and not self.out_of_credit:
            out.append({"kind": "low", "level": "warn", "buy": BUY_URL,
                        "say": f"xAI credit is down to ${cr:.2f} (your warning line is ${low:.2f})."})
        return out

    def view(self, now: datetime | None = None) -> dict:
        now = now or self.clock()
        rows = self.rows(f"{now:%Y-%m}")
        prev = now.replace(day=1) - timedelta(days=1)
        week_rows = rows + (self.rows(f"{prev:%Y-%m}") if now.day < 7 else [])
        day, week = f"{now:%Y-%m-%d}", (now - timedelta(days=7)).isoformat(timespec="seconds")

        def tot(rs):
            return round(sum(r.get("usd") or 0 for r in rs), 4)
        by: dict[str, dict] = {}
        for r in rows:
            f = FEATURES.get(r.get("feature"), r.get("feature") or "?")
            x = by.setdefault(f, {"what": f, "calls": 0, "usd": 0.0, "unpriced": 0, "est": 0})
            x["calls"] += 1
            x["usd"] += r.get("usd") or 0
            x["unpriced"] += r.get("usd") is None
            x["est"] += r.get("how") == "est"
        for x in by.values():
            x["usd"] = round(x["usd"], 4)
        recent = [r | {"what": FEATURES.get(r.get("feature"), r.get("feature"))}
                  for r in sorted(week_rows[::-1], key=lambda r: r.get("at") or "", reverse=True)[:15]]   # newest first
        from . import secrets
        return {"today": tot([r for r in rows if (r.get("at") or "").startswith(day)]),
                "week": tot([r for r in week_rows if (r.get("at") or "") >= week]),
                "month": tot(rows), "month_name": f"{now:%B}", "calls": len(rows),
                "by": sorted(by.values(), key=lambda x: -x["usd"]), "recent": recent,
                "budget_usd": float(self.cfg.get("budget_usd") or 0),
                "low_credit_usd": float(self.cfg.get("low_credit_usd") or 0),
                "over_budget": self.over_budget(now), "background": list(BACKGROUND),
                "mgmt_key": secrets.status(self.st.root, MGMT_KEY)["set"], "account": self.account,
                "out_of_credit": self.out_of_credit, "warnings": self.warnings(now),
                "buy_url": BUY_URL, "usage_url": USAGE_URL}
