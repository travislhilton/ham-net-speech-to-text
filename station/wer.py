"""The over-the-air test: the operator reads a fixed text and the station scores what it heard (word error rate),
for the live text and the accurate pass. Numbers and callsigns are compared digit by digit and letter by letter, so
"145.9", "one four five point nine" and "W9XYZ" / "W 9 X Y Z" all compare the same."""
import re

TEMPLATE = ("This is {call}, {phonetic}, testing speech to text on one four five point nine. "
            "The quick brown fox jumps over the lazy dog. {call} clear.")


def test_text(call: str | None) -> str:
    """The text the operator reads, with their own callsign (from config/station.yaml)."""
    from hamkit.hints import phonetic
    call = (call or "N0CALL").upper()
    return TEMPLATE.format(call=call, phonetic=phonetic(call))


TEST_TEXT = test_text("W9XYZ")   # an example; the station uses test_text(its callsign)
DIGITS = "zero one two three four five six seven eight nine".split()
ITU = {w: w[0] for w in ("alfa alpha bravo charlie delta echo foxtrot golf hotel india juliett juliet kilo lima mike "
                         "november oscar papa quebec romeo sierra tango uniform victor whiskey xray yankee zulu").split()}
ITU.update(niner="nine")
CALL = re.compile(r"^[a-z]{1,2}\d[a-z]{1,4}$|^\d[a-z]\d[a-z]{1,3}$")


def words(text: str, me: str | None = None) -> list[str]:
    t = (text or "").lower().replace(me.lower() if me else "\0", " {me} ")
    t = re.sub(r"(?<=\d)\.(?=\d)", " point ", t)
    t = re.sub(r"[^a-z0-9{} ]+", " ", t)
    out = []
    for w in t.split():
        if w == "{me}":
            out += [DIGITS[int(c)] if c.isdigit() else c for c in me.lower()]
        elif any(c.isdigit() for c in w) or CALL.match(w):
            out += [DIGITS[int(c)] if c.isdigit() else c for c in w]
        else:
            out.append(ITU.get(w, w))                           # "Whiskey Nine" and "W9" compare the same
    return out


def wer(ref: str, hyp: str, me: str | None = None) -> dict:
    r, h = words(ref, me), words(hyp, me)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return {"wer": round(d[len(h)] / max(len(r), 1), 3), "errors": d[len(h)], "words": len(r)}
