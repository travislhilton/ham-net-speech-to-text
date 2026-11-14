"""The station's secrets: config/secrets.env (KEY=value lines, never committed, never overwritten by the installer).

Each station owner supplies their own keys:
  EMCOMM_STT_KEY  the home server's upload key (station/accurate.py)
  XAI_API_KEY     the station agent's xAI key (console.x.ai), set in Settings -> Station agent
  XAI_MANAGEMENT_KEY optional: reads the xAI account's credit and spend (station/aicost.py), Settings -> AI costs
  ANTHROPIC_API_KEY  optional: Claude as a comparison model for the station agent (console.anthropic.com)

An environment variable of the same name wins over the file. The dashboard never receives a key: only whether one
is set and its last 4 characters. Writing keeps the file's own permissions (it is rewritten in place); a new file is
locked to SYSTEM, Administrators and the station's user on Windows.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

KEYS = {
    "XAI_API_KEY": {"label": "xAI API key", "shape": r"xai-[A-Za-z0-9_-]{20,200}",
                    "test": "https://api.x.ai/v1/models", "who": "xAI"},
    "XAI_MANAGEMENT_KEY": {"label": "xAI management key (optional: shows the account's credit and spend)",
                           "shape": r"[A-Za-z0-9_.:+/=-]{20,400}", "who": "xAI",
                           "looks": "copy the whole key, no spaces",
                           "test": "https://management-api.x.ai/auth/management-keys/validation",
                           "say": lambda d: "The key works (" + ("team " + str(d.get("scopeId") or d.get("teamId") or "?")[:8]
                                                                 + "…)." if d.get("scope") != "SCOPE_ORGANIZATION" else
                                                                 "an organization key: make one for your team instead).")},
    "ANTHROPIC_API_KEY": {"label": "Anthropic API key (optional: Claude for comparison)",
                          "shape": r"sk-ant-[A-Za-z0-9_-]{20,300}", "test": "https://api.anthropic.com/v1/models",
                          "who": "Anthropic", "headers": lambda v: {"x-api-key": v, "anthropic-version": "2023-06-01"}},
}


def path(root: Path) -> Path:
    return Path(root) / "config" / "secrets.env"


def _lines(root) -> list[str]:
    try:
        return path(root).read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return []


def read(root, name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    for line in _lines(root):
        k, _, v = line.partition("=")
        if k.strip() == name and v.strip():
            return v.strip()
    return None


def status(root, name: str) -> dict:
    v = read(root, name)
    return {"name": name, "label": KEYS.get(name, {}).get("label", name), "set": bool(v),
            "hint": ("…" + v[-4:]) if v and len(v) > 8 else None,
            "from": "environment" if os.environ.get(name) else "file" if v else None}


def write(root, name: str, value: str | None) -> dict:
    """Set (or with None, remove) one key. Only names in KEYS; the value must have the key's shape."""
    if name not in KEYS:
        raise ValueError("unknown key")
    if value is not None:
        value = str(value).strip()
        if value.startswith(name + "="):              # pasted the whole line
            value = value[len(name) + 1:].strip()
        if not re.fullmatch(KEYS[name]["shape"], value):
            raise ValueError(f"that doesn't look like an {KEYS[name]['label']} ("
                             + KEYS[name].get("looks", f"it starts with {KEYS[name]['shape'][:4]!r}, no spaces") + ")")
    p = path(root)
    lines, out, done = _lines(root), [], False
    for line in lines:
        if line.partition("=")[0].strip() == name:
            if value is not None and not done:
                out.append(f"{name}={value}")
                done = True
            continue
        out.append(line)
    if value is not None and not done:
        out += [f"# {KEYS[name]['label']}, set in Settings", f"{name}={value}"]
    new = not p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\r\n" if sys.platform == "win32" else "\n") as fh:   # in place: keeps
        fh.write("\n".join(out) + "\n")                                                              # its permissions
    if new:
        _lock(p)
    print(f"secrets: {name} {'set' if value is not None else 'removed'}", flush=True)
    return status(root, name)


def _lock(p: Path, run=None) -> bool:
    """A new secrets file: readable only by SYSTEM, Administrators and this user (Windows). True when locked.
    The user is named by its SID (whoami /user): in an SSH session USERDOMAIN is "WORKGROUP", "WORKGROUP\\user" maps
    to no account, and icacls then changed nothing at all (2026-11-12: a key written over SSH stayed readable by
    every user on the PC)."""
    if sys.platform != "win32" and run is None:
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return True
    run = run or (lambda a: subprocess.run(a, capture_output=True, text=True, timeout=15,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    me = None
    try:
        out = run(["whoami", "/user", "/fo", "csv", "/nh"]).stdout or ""
        m = re.search(r"(S-1-5-[\d-]+)", out)
        me = "*" + m.group(1) if m else None
    except (OSError, subprocess.SubprocessError):
        pass
    base = ["icacls", str(p), "/inheritance:r", "/grant:r", "*S-1-5-18:F", "/grant:r", "*S-1-5-32-544:F"]
    for who in dict.fromkeys(x for x in (me, os.environ.get("USERNAME")) if x):   # never without this user: the
        args = [*base, "/grant:r", f"{who}:F"]                                     # station runs as it
        try:
            if run(args).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    print(f"secrets: could not restrict {p} to this user (check its permissions)", flush=True)
    return False


def test(root, name: str, opener=urllib.request.urlopen) -> dict:
    """Ask the provider whether the key works (a read-only call: the list of models)."""
    v = read(root, name)
    if not v:
        return {"ok": False, "say": "No key set."}
    k = KEYS[name]
    req = urllib.request.Request(k["test"], headers=k["headers"](v) if k.get("headers") else {"Authorization": f"Bearer {v}"})
    try:
        with opener(req, timeout=10) as r:
            d = json.loads(r.read() or b"{}")
        if k.get("say"):
            return {"ok": True, "say": k["say"](d)}
        return {"ok": True, "say": f"The key works ({len(d.get('data') or [])} models available)."}
    except urllib.error.HTTPError as e:
        return {"ok": False, "say": f"{k['who']} refused the key (check it was copied whole, and that the account has "
                "credit)." if e.code in (401, 403) else f"{k['who']} answered {e.code}; try again later."}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "say": f"Couldn't reach {k['who']} ({type(e).__name__}): is this PC online?"}
