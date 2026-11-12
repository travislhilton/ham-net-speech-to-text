"""US state names, for hearing a roll call by place ("Stations <state>, check in now").

The names come from the US Census list of states (public domain, ~2 KB), downloaded into the callbook folder like the
ZIP centres (hamkit/zipgeo.py); none are written into this repository. Without the file a roll call by place is
still recognised by its shape (station/agent.py), only less often.

  python -m hamkit.places update      download the list (once; the installer and the station do it)
  python -m hamkit.places list        print the names
"""
from __future__ import annotations

import argparse
import sys
import threading
import urllib.request
from pathlib import Path

from .fcc import DEFAULT_DB

URL = "https://www2.census.gov/geo/docs/reference/state.txt"
DEFAULT_PATH = DEFAULT_DB.parent / "us_states.txt"
_cache: dict[str, tuple[float, list[str]]] = {}
_lock = threading.Lock()


def parse(text: str) -> list[str]:
    """The Census file (STATE|STUSAB|STATE_NAME|STATENS) -> state names."""
    out = []
    for line in text.splitlines()[1:]:
        parts = line.split("|")
        if len(parts) >= 3 and parts[2].strip():
            out.append(parts[2].strip())
    return out


def update(path: Path = DEFAULT_PATH, url: str = URL, log=print, timeout: float = 30) -> int:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        names = parse(r.read().decode("utf-8", "replace"))
    if len(names) < 50:
        raise ValueError(f"the Census list had {len(names)} states")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(names) + "\n", encoding="utf-8")
    tmp.replace(path)
    log(f"US state names: {len(names)} -> {path}")
    return len(names)


def names(path: Path = DEFAULT_PATH) -> list[str]:
    """The state names, or [] when the list has not been downloaded."""
    path = Path(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    with _lock:
        hit = _cache.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1]
        got = [x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        _cache[str(path)] = (mtime, got)
        return got


def ensure(path: Path = DEFAULT_PATH) -> None:
    """Download the list in the background when it is missing (a station installed without the callbook step)."""
    if Path(path).exists():
        return

    def run():
        try:
            update(path, log=lambda *_: None)
        except Exception as e:  # noqa: BLE001 - offline: roll calls by place are recognised by their shape only
            print(f"places: US state names not downloaded ({type(e).__name__})", flush=True)
    threading.Thread(target=run, daemon=True, name="places").start()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["update", "list"])
    ap.add_argument("--path", default=str(DEFAULT_PATH))
    a = ap.parse_args(argv)
    if a.what == "update":
        update(Path(a.path))
    else:
        print("\n".join(names(Path(a.path))) or "(not downloaded: python -m hamkit.places update)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
