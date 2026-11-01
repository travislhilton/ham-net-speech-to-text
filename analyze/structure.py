"""Per-transmission structuring: who spoke, what kind of traffic, extracted fields."""
import json
from .llm import chat, LIGHT

SYSTEM = """You structure amateur-radio EMCOMM net transmissions. Return ONLY JSON:
{"from_callsign": str|null, "to_callsign": str|null, "tactical": str|null,
 "kind": "net_open|check_in|check_out|traffic|relay|ack|net_control|chatter|unknown",
 "precedence": "routine|welfare|priority|emergency|null",
 "traffic": {"summary": str|null, "location": str|null, "needs": [str], "ics213_message": str|null},
 "confidence": 0.0-1.0}
Callsigns are US amateur format (K7ABC, W9XYZ). Phonetics may be partially mangled; infer carefully."""


def structure(text: str, callsigns: list[str], prior_context: str = "") -> dict:
    user = f"Known callsigns in this transmission: {callsigns}\nRecent net context:\n{prior_context}\n\nTransmission:\n{text}"
    return json.loads(chat(LIGHT, SYSTEM, user, json_mode=True, max_tokens=600))
