"""Moved to hamkit/callsigns.py (shared with the Windows station). This alias keeps imports working."""
import sys

from hamkit import callsigns as _callsigns

sys.modules[__name__] = _callsigns
