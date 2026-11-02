"""Frequency -> amateur band (US allocations, ITU Region 2) and a few facts an operator wants shown."""
BANDS = [  # (name, low MHz, high MHz, range)
    ("160 m", 1.8, 2.0, "MF"), ("80 m", 3.5, 4.0, "HF"), ("60 m", 5.3305, 5.4065, "HF"),
    ("40 m", 7.0, 7.3, "HF"), ("30 m", 10.1, 10.15, "HF"), ("20 m", 14.0, 14.35, "HF"),
    ("17 m", 18.068, 18.168, "HF"), ("15 m", 21.0, 21.45, "HF"), ("12 m", 24.89, 24.99, "HF"),
    ("10 m", 28.0, 29.7, "HF"), ("6 m", 50.0, 54.0, "VHF"), ("2 m", 144.0, 148.0, "VHF"),
    ("1.25 m", 222.0, 225.0, "VHF"), ("70 cm", 420.0, 450.0, "UHF"), ("33 cm", 902.0, 928.0, "UHF"),
    ("23 cm", 1240.0, 1300.0, "UHF"),
]


def band(freq_mhz: float | None) -> dict | None:
    if freq_mhz is None:
        return None
    f = float(freq_mhz)
    for name, lo, hi, rng in BANDS:
        if lo <= f <= hi:
            d = {"band": name, "range": rng, "freq_mhz": f, "label": f"{f:.3f} MHz ({name})"}
            if name == "2 m" and (145.1 <= f < 145.5 or 146.61 <= f < 147.4):
                d["repeater_output"] = True
                d["usual_offset_mhz"] = -0.6 if f < 147.0 else +0.6
            elif name == "70 cm" and 442.0 <= f < 445.0:
                d["repeater_output"] = True
                d["usual_offset_mhz"] = +5.0
            return d
    return {"band": None, "range": None, "freq_mhz": f, "label": f"{f:.3f} MHz (outside amateur bands)"}
