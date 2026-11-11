"""Is a cloud service reachable right now? One small tracker per service (the station agent's model, Grok
speech-to-text), so a station that loses its internet keeps working at once instead of waiting on timeouts.

Nothing is queued while offline (owner 2026-11-10: "The conversation must be interactive and not queued"): a call
made while the service is down is simply not made. After a network failure the service is not tried again for a
back-off (30 s, doubling to 5 minutes); the first success clears it. An answer from the service that is an error
(401, 400) is NOT "offline": the service is reachable, the request was wrong.
"""
from __future__ import annotations

import socket
import threading
import time
import urllib.error

BACKOFF_S = (30, 60, 120, 300)


def is_network_error(e: BaseException) -> bool:
    """True when the service could not be reached (no route, DNS, timeout, reset), False for an HTTP answer."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (502, 503, 504)
    if isinstance(e, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError)):
        return True
    name = type(e).__name__                       # the anthropic SDK's APIConnectionError / APITimeoutError
    return name in ("APIConnectionError", "APITimeoutError", "ConnectError", "ReadTimeout", "ConnectTimeout")


class Online:
    def __init__(self, name: str, clock=time.monotonic, wall=time.time):
        self.name, self.clock, self.wall = name, clock, wall
        self.lock = threading.Lock()
        self.online: bool | None = None           # None = not tried yet
        self.fails = 0
        self.retry_at = 0.0
        self.since: float | None = None           # wall time of the last change
        self.last_error = ""

    def should_try(self) -> bool:
        with self.lock:
            return self.online is not False or self.clock() >= self.retry_at

    def ok(self):
        with self.lock:
            if self.online is not True:
                self.since = self.wall()
            self.online, self.fails, self.last_error = True, 0, ""

    def failed(self, e: BaseException | str):
        with self.lock:
            if self.online is not False:
                self.since = self.wall()
            self.online = False
            self.retry_at = self.clock() + BACKOFF_S[min(self.fails, len(BACKOFF_S) - 1)]
            self.fails += 1
            self.last_error = e if isinstance(e, str) else f"{type(e).__name__}: {str(e)[:120]}"

    def view(self) -> dict:
        with self.lock:
            wait = max(0.0, self.retry_at - self.clock()) if self.online is False else 0.0
            return {"name": self.name, "online": self.online, "since": self.since, "last_error": self.last_error,
                    "retry_in_s": round(wait)}
