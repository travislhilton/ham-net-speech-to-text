"""Which net does a transmission belong to? Pure logic, no database — tested directly.

A net is open from started_utc until ended_utc (None = still running). A transmission belongs to
the net whose window contains its start time. When several nets overlap (two frequencies at once),
the frequency decides: a net with a frequency only takes transmissions on that frequency (±0.5 kHz);
a net without one takes any. Among the remaining candidates the most recently started wins."""
from datetime import datetime

FREQ_TOL_MHZ = 0.0005


def freq_matches(net_freq, tx_freq) -> bool:
    if net_freq is None or tx_freq is None:
        return True
    return abs(float(net_freq) - float(tx_freq)) <= FREQ_TOL_MHZ


def contains(net, at: datetime) -> bool:
    return net.started_utc <= at and (net.ended_utc is None or at <= net.ended_utc)


def pick_net(nets, at: datetime, freq=None):
    """nets: objects with id, started_utc, ended_utc, frequency_mhz. Returns the chosen net or None."""
    cands = [n for n in nets if contains(n, at) and freq_matches(n.frequency_mhz, freq)]
    if not cands:
        return None
    # A net that names this exact frequency beats a catch-all net with none.
    exact = [n for n in cands if n.frequency_mhz is not None and freq is not None]
    return max(exact or cands, key=lambda n: n.started_utc)
