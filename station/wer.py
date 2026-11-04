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


def align(a: list[str], b: list[str]) -> list[tuple]:
    """A minimal word alignment of `a` to `b` as opcodes (op, i0, i1, j0, j1), op in equal / replace / insert /
    delete, a run of differences between two matches merged into one opcode (so "k seven a b c" -> "K7ABC" is one
    replace). Used to learn fix-ups from the operator's corrections (station/corrections.py)."""
    n, m = len(a), len(b)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
    steps, i, j = [], n, m
    while i or j:                                          # back from the end: matches first, then substitutions
        if i and j and a[i - 1] == b[j - 1] and d[i][j] == d[i - 1][j - 1]:
            steps.append("=")
            i -= 1; j -= 1
        elif i and j and d[i][j] == d[i - 1][j - 1] + 1:
            steps.append("~"); i -= 1; j -= 1
        elif i and d[i][j] == d[i - 1][j] + 1:
            steps.append("-"); i -= 1
        else:
            steps.append("+"); j -= 1
    ops, i, j = [], 0, 0
    for s in reversed(steps):
        di, dj = (0 if s == "+" else 1), (0 if s == "-" else 1)
        kind = "equal" if s == "=" else "diff"
        if ops and ops[-1][0] == kind:
            ops[-1][2] += di; ops[-1][4] += dj
        else:
            ops.append([kind, i, i + di, j, j + dj])
        i += di; j += dj
    out = []
    for k, i0, i1, j0, j1 in ops:
        if k == "diff":
            k = "replace" if i1 > i0 and j1 > j0 else "delete" if i1 > i0 else "insert"
        out.append((k, i0, i1, j0, j1))
    return out
