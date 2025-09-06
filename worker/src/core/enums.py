from __future__ import annotations
from enum import Enum


class SensorState(str, Enum):
    """
    Lifecycle/state for a DS18B20 sensor in the registry.

    NEW:       Discovered but not yet successfully read.
    ACTIVE:    Last read succeeded within freshness window.
    STALE:     Last read is too old (reader lag or temporary issue).
    MISSING:   Sensor disappeared from the bus (hot-unplug or bus down).
    ERROR:     Consecutive read/CRC errors; backoff may be applied.
    DISABLED:  Intentionally disabled (config or admin action).
    """
    NEW = "new"
    ACTIVE = "active"
    STALE = "stale"
    MISSING = "missing"
    ERROR = "error"
    DISABLED = "disabled"
