from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .enums import SensorState
from .types import Address, Reading, Timestamp, utc_now


# ---- Error/Reason enums kept local to events (can be moved to core/enums.py if preferred) ----

class ReadErrorKind(str, Enum):
    """Categories of read failure to simplify counters & alerts."""
    TIMEOUT = "timeout"
    CRC_FAIL = "crc_fail"
    PARSE_ERROR = "parse_error"
    IO_ERROR = "io_error"
    NO_DEVICE = "no_device"        # file disappeared during read
    PERMISSION = "permission"
    BUS_DOWN = "bus_down"
    UNKNOWN = "unknown"


class DisconnectReason(str, Enum):
    """Why a sensor is considered disconnected/missing."""
    HOT_UNPLUG = "hot_unplug"
    BUS_REMOVED = "bus_removed"
    NO_LONGER_LISTED = "no_longer_listed"
    ERROR_THRESHOLD = "error_threshold"
    UNKNOWN = "unknown"


# ---- Base event ----

@dataclass(slots=True, frozen=True)
class Event:
    """Base event: everything has a timestamp."""
    at: Timestamp = field(default_factory=utc_now)


# ---- Topology events ----

@dataclass(slots=True, frozen=True)
class BusDiscovered(Event):
    bus_id: int
    gpio: int


@dataclass(slots=True, frozen=True)
class BusRemoved(Event):
    bus_id: int
    gpio: int


@dataclass(slots=True, frozen=True)
class SensorConnected(Event):
    """A sensor appeared on a bus."""
    addr: Address
    # Optional snapshot of metadata when first seen (family/resolution)
    # Can be updated later by a dedicated metadata refresh.
    # Using Optional to keep event lightweight.
    family: Optional[str] = None
    resolution_bits: Optional[int] = None


@dataclass(slots=True, frozen=True)
class SensorDisconnected(Event):
    """A sensor disappeared or is considered missing."""
    addr: Address
    reason: DisconnectReason = DisconnectReason.NO_LONGER_LISTED


# ---- Reading events ----

@dataclass(slots=True, frozen=True)
class ReadingSucceeded(Event):
    """A successful read of a sensor."""
    addr: Address
    reading: Reading


@dataclass(slots=True, frozen=True)
class ReadingFailed(Event):
    """A failed attempt to read a sensor."""
    addr: Address
    kind: ReadErrorKind
    message: Optional[str] = None       # human/debug info
    attempts: int = 1                   # consecutive attempt count


# ---- Lifecycle / metadata events ----

@dataclass(slots=True, frozen=True)
class StateTransition(Event):
    """Sensor state machine transition."""
    addr: Address
    old: SensorState
    new: SensorState


@dataclass(slots=True, frozen=True)
class LabelUpdated(Event):
    """Human label changed for a ROM (useful for audit trails)."""
    rom: str
    old: Optional[str]
    new: Optional[str]
