from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, TypeAlias

# ---- Primitive aliases (semantic clarity) ----
RomCode: TypeAlias = str        # e.g. "28-00000abcdef0"
BusId: TypeAlias = int          # kernel w1 bus number (w1_bus_masterX)
GpioPin: TypeAlias = int        # BCM GPIO pin number
Celsius: TypeAlias = float
Millis: TypeAlias = float
FilePath: TypeAlias = str
Timestamp: TypeAlias = datetime
LabelMap: TypeAlias = Mapping[RomCode, str]


def utc_now() -> Timestamp:
    """Timezone-aware UTC now (consistent timestamps in logs/metrics)."""
    return datetime.now(timezone.utc)


# ---- Rich types used across modules ----

@dataclass(slots=True, frozen=True)
class Address:
    """Physical/logical address of a DS18B20 sensor on the system."""
    rom: RomCode
    bus_id: BusId
    gpio: GpioPin
    path: FilePath | None = None  # e.g. /sys/bus/w1/devices/28-xxxx/w1_slave


@dataclass(slots=True, frozen=True)
class SensorMeta:
    """Optional hardware metadata when known (populated by io/w1_sysfs)."""
    family: str | None = None          # e.g. "28" for DS18B20 family
    resolution_bits: int | None = None # 9..12 if configured/readable


@dataclass(slots=True, frozen=True)
class Reading:
    """A single temperature reading."""
    value_c: Celsius                   # normalized degrees Celsius
    raw_millideg: int                  # raw *1000 °C, from sysfs if available
    at: Timestamp
    path: FilePath | None = None       # where it was read from
    latency_ms: Millis | None = None   # time to read/parse, if measured
    