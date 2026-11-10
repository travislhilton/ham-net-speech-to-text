"""The station's secrets: config/secrets.env (KEY=value lines, never committed, never overwritten by the installer).

Each station owner supplies their own keys:
  EMCOMM_STT_KEY  the home server's upload key (station/accurate.py)
  XAI_API_KEY     the station agent's xAI key (console.x.ai), set in Settings -> Station agent
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
            raise ValueError(f"that doesn't look like an {KEYS[name]['label']} (it starts with "
                             f"{KEYS[name]['shape'][:4]!r}, no spaces)")
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


def _lock(p: Path):
    """A new secrets file: readable only by SYSTEM, Administrators and this user (Windows)."""
    if sys.platform != "win32":
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "") if os.environ.get("USERNAME") else None
    args = ["icacls", str(p), "/inheritance:r", "/grant:r", "*S-1-5-18:F", "/grant:r", "*S-1-5-32-544:F"]
    if user:
        args += ["/grant:r", f"{user}:F"]
    try:
        subprocess.run(args, capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass


def test(root, name: str, opener=urllib.request.urlopen) -> dict:
    """Ask the provider whether the key works (a read-only call: the list of models)."""
    v = read(root, name)
    if not v:
        return {"ok": False, "say": "No key set."}
    k = KEYS[name]
    req = urllib.request.Request(k["test"], headers=k["headers"](v) if k.get("headers") else {"Authorization": f"Bearer {v}"})
    try:
        with opener(req, timeout=10) as r:
            n = len((json.loads(r.read() or b"{}").get("data") or []))
        return {"ok": True, "say": f"The key works ({n} models available)."}
    except urllib.error.HTTPError as e:
        return {"ok": False, "say": f"{k['who']} refused the key (check it was copied whole, and that the account has "
                "credit)." if e.code in (401, 403) else f"{k['who']} answered {e.code}; try again later."}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "say": f"Couldn't reach {k['who']} ({type(e).__name__}): is this PC online?"}
