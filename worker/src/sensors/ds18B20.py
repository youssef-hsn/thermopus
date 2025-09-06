from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from ..core.enums import SensorState
from ..core.types import Address, Reading, Timestamp, utc_now, RomCode
from ..core.types import SensorMeta  # optional metadata
from ..core.events import ReadErrorKind  # for bookkeeping only


@dataclass(slots=True)
class Sensor:
    """
    In-memory model of a DS18B20 sensor.
    One instance per ROM code, tracked in SensorSet.
    """
    rom: RomCode
    address: Address                      # includes bus_id, gpio, sysfs path
    label: Optional[str] = None
    meta: Optional[SensorMeta] = None

    state: SensorState = SensorState.NEW
    last_reading: Optional[Reading] = None
    last_read_at: Optional[Timestamp] = None

    read_ok_count: int = 0
    read_error_count: int = 0
    consecutive_failures: int = 0
    last_error_kind: Optional[ReadErrorKind] = None
    last_error_message: Optional[str] = None

    disabled: bool = False

    # --- convenience ---

    @property
    def value_c(self) -> Optional[float]:
        return self.last_reading.value_c if self.last_reading else None

    @property
    def is_active(self) -> bool:
        return self.state is SensorState.ACTIVE

    # --- mutators (return old_state, new_state when state changes) ---

    def apply_reading(self, reading: Reading) -> tuple[SensorState, SensorState]:
        old = self.state
        self.last_reading = reading
        self.last_read_at = reading.at
        self.read_ok_count += 1
        self.consecutive_failures = 0
        self.last_error_kind = None
        self.last_error_message = None

        if self.disabled:
            # keep DISABLED if set explicitly
            self.state = SensorState.DISABLED
        else:
            self.state = SensorState.ACTIVE
        return old, self.state

    def note_error(
        self,
        kind: ReadErrorKind,
        message: Optional[str] = None,
        *,
        error_threshold: int = 3,
    ) -> tuple[SensorState, SensorState]:
        old = self.state
        self.read_error_count += 1
        self.consecutive_failures += 1
        self.last_error_kind = kind
        self.last_error_message = message

        if self.disabled:
            self.state = SensorState.DISABLED
        elif self.consecutive_failures >= error_threshold:
            self.state = SensorState.ERROR
        # else: keep whatever state we were (ACTIVE/NEW/STALE) until threshold reached
        return old, self.state

    def mark_stale(self) -> tuple[SensorState, SensorState]:
        if self.disabled:
            return self.state, SensorState.DISABLED
        old = self.state
        if self.state not in (SensorState.MISSING, SensorState.ERROR, SensorState.DISABLED):
            self.state = SensorState.STALE
        return old, self.state

    def mark_missing(self) -> tuple[SensorState, SensorState]:
        old = self.state
        self.state = SensorState.MISSING if not self.disabled else SensorState.DISABLED
        return old, self.state

    def set_disabled(self, disabled: bool) -> tuple[SensorState, SensorState]:
        old = self.state
        self.disabled = disabled
        if disabled:
            self.state = SensorState.DISABLED
        else:
            # on re-enable, consider as NEW until next successful read
            self.state = SensorState.NEW
        return old, self.state

    def set_label(self, label: Optional[str]) -> None:
        self.label = label

    def update_address(self, address: Address) -> None:
        """
        Update bus/gpio/path if the kernel assigned a new bus number or path changed.
        """
        self.address = address

    # --- helpers ---

    def stale_due(self, freshness_window: timedelta, now: Optional[Timestamp] = None) -> bool:
        if self.disabled:
            return False
        if self.last_read_at is None:
            return False
        now = now or utc_now()
        return (now - self.last_read_at) > freshness_window

    def __repr__(self) -> str:
        label = f" label={self.label!r}" if self.label else ""
        return (
            f"Sensor(rom={self.rom}, bus={self.address.bus_id}, gpio={self.address.gpio},"
            f" state={self.state.value},{label} value={self.value_c})"
        )