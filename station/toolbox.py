"""Tools the thinking model may call while it works out an answer (xAI tool calling), instead of being handed the
library and the path analyses as context (owner 2026-11-12: "do them all", item 3).

* search_library(query)  -> the best chunks of the station's library for that query (station/knowledge.py)
* list_sites()           -> the operator's sites (name | lat, lon | height | note), kept on this PC only
* path_analysis(site_a, site_b, mhz?) -> the computed terrain path between two named sites (hamkit/pathprofile.py)

Every result is bounded, every failure is returned as text ("not computed ...") so the model says so rather than
guesses, and the number of tool rounds is capped (agent.call_xai_tools). A tool only reads: nothing here can key a
radio or change a setting.
"""
from __future__ import annotations

import json

LIB_CHARS = 4500
RESULT_CHARS = 6000


def _spec(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required, "additionalProperties": False}}}


class Toolbox:
    def __init__(self, agent, rid: str | None = None):
        self.agent, self.rid = agent, rid
        self.calls: list[str] = []
        self.specs = [
            _spec("search_library", "Search this station's library (the operator's own documents and this station's "
                  "manual) for passages on a topic. Use it before answering anything about this station, its "
                  "procedures, the net, or the operator's documents.", {"query": {"type": "string"}}, ["query"]),
            _spec("list_sites", "List the operator's named sites (EOCs, shelters, repeaters) that path_analysis "
                  "accepts.", {}, []),
            _spec("path_analysis", "Terrain path analysis between two of the operator's named sites at a frequency, "
                  "computed from elevation data. Use it for any question about whether two places can talk; never "
                  "estimate a path yourself.", {"site_a": {"type": "string"}, "site_b": {"type": "string"},
                                                 "mhz": {"type": "number"}}, ["site_a", "site_b"])]

    def run(self, name: str, arguments) -> str:
        self.calls.append(str(name))
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
            fn = {"search_library": self._library, "list_sites": self._sites, "path_analysis": self._path}.get(name)
            if not fn:
                return f"unknown tool {name!r}"
            return str(fn(**args))[:RESULT_CHARS]
        except Exception as e:  # noqa: BLE001 - said to the model, never raised
            return f"tool failed ({type(e).__name__}: {str(e)[:120]}): say it could not be looked up"

    def _library(self, query: str = "") -> str:
        out = self.agent.library().context_for(str(query)[:300], LIB_CHARS)
        return out or "nothing in the library matches: say you have nothing on that"

    def _sites(self) -> str:
        from .topics import sites_text
        s = self.agent.topics.sites
        return sites_text(s) if s else "no sites have been entered (Chat & prepared… → Sites)"

    def _path(self, site_a: str = "", site_b: str = "", mhz: float | None = None) -> str:
        from .topics import path_facts
        sites = self.agent.topics.sites
        pick = lambda n: next((s for s in sites if s["name"].lower() == str(n).strip().lower()), None)  # noqa: E731
        a, b = pick(site_a), pick(site_b)
        if not a or not b:
            return "unknown site: use list_sites for the exact names"
        r = (self.agent.st.radios.get(self.rid) or {}) if self.rid else {}
        f = float(mhz or r.get("freq_mhz") or 146.0)
        return "\n".join(path_facts([a, b], f if 30 <= f <= 1300 else 146.0))
