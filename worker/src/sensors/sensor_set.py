from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Dict, Iterable, Iterator, Optional

from ..core.enums import SensorState
from ..core.types import Address, RomCode, LabelMap, Timestamp, utc_now
from ..core.events import ReadErrorKind
from .ds18b20 import Sensor


@dataclass(slots=True)
class Transition:
    rom: RomCode
    old: SensorState
    new: SensorState


SensorsByRom = Dict[RomCode, Sensor]


class SensorSet:
    """
    Registry of all sensors keyed by ROM code.
    Designed for use from asyncio tasks; operations are protected by a single lock.
    """

    def __init__(
        self,
        *,
        freshness_window: timedelta,
        label_map: Optional[LabelMap] = None,
        disabled_roms: Iterable[RomCode] = (),
        error_threshold: int = 3,
    ) -> None:
        self._sensors: SensorsByRom = {}
        self._lock = asyncio.Lock()
        self._freshness_window = freshness_window
        self._error_threshold = int(error_threshold)
        self._label_map: dict[str, str] = dict(label_map or {})
        self._disabled = set(disabled_roms)

    # ----- basic lookups -----

    async def get(self, rom: RomCode) -> Optional[Sensor]:
        async with self._lock:
            return self._sensors.get(rom)

    async def all(self) -> list[Sensor]:
        async with self._lock:
            return list(self._sensors.values())

    # ----- upsert & reconcile -----

    async def upsert(self, addr: Address, meta: object | None = None) -> tuple[Sensor, bool]:
        """
        Ensure a Sensor exists for this ROM; update address and apply label/disabled flags.
        Returns (sensor, created).
        """
        rom = addr.rom
        created = False
        async with self._lock:
            s = self._sensors.get(rom)
            if s is None:
                s = Sensor(rom=rom, address=addr, meta=meta)  # type: ignore[arg-type]
                s.set_label(self._label_map.get(rom))
                if rom in self._disabled:
                    s.set_disabled(True)
                self._sensors[rom] = s
                created = True
            else:
                s.update_address(addr)
        return s, created

    async def reconcile_addresses(self, addrs: list[Address]) -> tuple[list[Sensor], list[Sensor]]:
        """
        Reconcile the in-memory set against a fresh enumeration from sysfs.
        Returns (connected, became_missing).
        """
        now_roms = {a.rom for a in addrs}
        connected: list[Sensor] = []
        missing: list[Sensor] = []

        async with self._lock:
            # upsert all present
            for a in addrs:
                s = self._sensors.get(a.rom)
                if s is None:
                    s = Sensor(rom=a.rom, address=a)
                    s.set_label(self._label_map.get(a.rom))
                    if a.rom in self._disabled:
                        s.set_disabled(True)
                    self._sensors[a.rom] = s
                    connected.append(s)
                else:
                    # refresh address (bus/gpio/path may change)
                    s.update_address(a)

            # mark absent as MISSING
            for rom, s in self._sensors.items():
                if rom not in now_roms and s.state is not SensorState.MISSING:
                    s.mark_missing()
                    missing.append(s)

        return connected, missing

    # ----- readings & errors -----

    async def apply_reading(self, rom: RomCode, reading) -> Optional[Transition]:
        """
        Apply a successful reading and update state to ACTIVE (unless disabled).
        Returns a Transition if state changed.
        """
        async with self._lock:
            s = self._sensors.get(rom)
            if s is None:
                return None
            old, new = s.apply_reading(reading)
            if old != new:
                return Transition(rom=rom, old=old, new=new)
            return None

    async def note_error(
        self,
        rom: RomCode,
        kind: ReadErrorKind,
        message: str | None = None,
    ) -> Optional[Transition]:
        async with self._lock:
            s = self._sensors.get(rom)
            if s is None:
                return None
            old, new = s.note_error(kind, message, error_threshold=self._error_threshold)
            if old != new:
                return Transition(rom=rom, old=old, new=new)
            return None

    async def mark_missing(self, rom: RomCode) -> Optional[Transition]:
        async with self._lock:
            s = self._sensors.get(rom)
            if s is None:
                return None
            old, new = s.mark_missing()
            if old != new:
                return Transition(rom=rom, old=old, new=new)
            return None

    # ----- maintenance -----

    async def sweep_staleness(self, now: Timestamp | None = None) -> list[Transition]:
        """
        Mark sensors STALE when last_read_at exceeds freshness_window.
        Returns transitions for changed sensors.
        """
        now = now or utc_now()
        changes: list[Transition] = []
        async with self._lock:
            for rom, s in self._sensors.items():
                if s.stale_due(self._freshness_window, now):
                    old, new = s.mark_stale()
                    if old != new:
                        changes.append(Transition(rom=rom, old=old, new=new))
        return changes

    async def update_labels(self, label_map: LabelMap) -> None:
        """
        Update labels from a new map; does not emit transitions.
        """
        async with self._lock:
            self._label_map = dict(label_map or {})
            for rom, s in self._sensors.items():
                s.set_label(self._label_map.get(rom))

    async def set_disabled(self, rom: RomCode, disabled: bool) -> Optional[Transition]:
        async with self._lock:
            s = self._sensors.get(rom)
            if s is None:
                return None
            old, new = s.set_disabled(disabled)
            if disabled:
                self._disabled.add(rom)
            else:
                self._disabled.discard(rom)
            if old != new:
                return Transition(rom=rom, old=old, new=new)
            return None

    # ----- iteration (read-only) -----

    def __len__(self) -> int:
        return len(self._sensors)

    async def items(self) -> list[tuple[RomCode, Sensor]]:
        async with self._lock:
            return list(self._sensors.items())

    async def values(self) -> list[Sensor]:
        async with self._lock:
            return list(self._sensors.values())
