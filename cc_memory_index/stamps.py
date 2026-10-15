"""UTC timestamps naming the backups of build --force and the migration snapshots (DESIGN §6, §8).

A stamp is YYYYmmddTHHMMSSZ, optionally followed by -N (1 to 6 digits) when that second is taken.
"""

from __future__ import annotations

import re
import time
from typing import Optional

_STAMP = re.compile(r"([0-9]{8}T[0-9]{6}Z)(?:-([0-9]{1,6}))?\Z")


def utc_stamp(now: Optional[float] = None) -> str:
    """The stamp of now (default: the current time), without a suffix."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))


def with_suffix(stamp: str, n: int) -> str:
    """stamp, or stamp-n when n is not 0."""
    return f"{stamp}-{n}" if n else stamp


def parse_stamp(text: str) -> Optional[tuple[str, int]]:
    """(stamp, suffix) when text is exactly a stamp (suffix 0 when it has none), else None."""
    m = _STAMP.match(text)
    return (m.group(1), int(m.group(2) or 0)) if m else None
