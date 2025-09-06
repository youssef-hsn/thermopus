from datetime import timedelta
from typing import Union


def parse_duration(value: Union[str, int, float, timedelta]) -> timedelta:
    """
    Accepts durations like:
      - "500ms", "2s", "1m", "15m", "1h", "1h30m", "90s"
      - plain seconds as int/float
      - timedelta
    Returns a timedelta.
    """
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)):
        return timedelta(seconds=float(value))
    if not isinstance(value, str):
        raise ValueError(f"Unsupported duration type: {type(value)}")

    s = value.strip().lower()
    try:
        return timedelta(seconds=float(s))
    except ValueError:
        pass

    total = 0.0
    num = ""
    i = 0
    units = {"ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}
    while i < len(s):
        ch = s[i]
        if ch.isdigit() or ch == ".":
            num += ch
            i += 1
            continue
        # read unit
        if not num:
            raise ValueError(f"Invalid duration segment at position {i}: {s!r}")
        # support 1h30m style (single-letter units or 'ms')
        if s.startswith("ms", i):
            unit = "ms"
            i += 2
        else:
            unit = s[i]
            i += 1
        if unit not in units:
            raise ValueError(f"Unknown duration unit {unit!r} in {s!r}")
        total += float(num) * units[unit]
        num = ""

    if num:
        # trailing number without unit => seconds
        total += float(num)

    return timedelta(seconds=total)