from __future__ import annotations

import asyncio
from typing import Optional, Set, Tuple

from ..config.settings import Settings
from ..core.events import (
    Event,
    BusDiscovered,
    BusRemoved,
    SensorConnected,
    SensorDisconnected,
)
from ..io.w1_sysfs import enumerate_addresses, get_buses, DEVICES_DIR
from ..io.watcher import SysfsWatcher, Change
from ..metrics.metrics import on_sensor_connected, mark_unavailable
from ..sensors.sensor_set import SensorSet
from ..util.logging import get_logger


BusKey = Tuple[int, int]  # (bus_id, gpio or -1)


async def scanner_loop(
    settings: Settings,
    sensors: SensorSet,
    events: "asyncio.Queue[Event]",
    *,
    poll_interval_override: float | None = None,
) -> None:
    """
    Reconciles DS18B20 devices from sysfs into SensorSet and emits topology events.

    - Uses SysfsWatcher (inotify if WORKER_USE_INOTIFY=true, else polling)
    - On every change (or periodic tick), it:
      * Detects bus add/remove -> BusDiscovered/BusRemoved events
      * Reconciles sensor addresses -> SensorConnected/SensorDisconnected events
      * Updates metrics for connected sensors and marks unavailable ones for gaps
    """
    log = get_logger("scanner")
    poll_interval = (
        poll_interval_override
        if poll_interval_override is not None
        else float(settings.scan_interval.total_seconds())
    )
    watcher = SysfsWatcher(
        _make_on_change_callback(events),  # enqueue coarse change signals
        devices_dir=DEVICES_DIR,
        poll_interval=poll_interval,
        force_polling=not settings.use_inotify,
    )

    # Internal queue for change signals from the watcher
    change_q: "asyncio.Queue[Change]" = asyncio.Queue()

    # Bridge: the watcher expects an async callback; we push into change_q
    async def _bridge(ch: Change) -> None:
        change_q.put_nowait(ch)

    # Rebuild watcher with our bridge callback
    watcher = SysfsWatcher(
        on_change=_bridge,
        devices_dir=DEVICES_DIR,
        poll_interval=poll_interval,
        force_polling=not settings.use_inotify,
    )

    # Track known buses to emit BusDiscovered/BusRemoved
    known_buses: Set[BusKey] = set()

    await watcher.start()
    log.info(
        "scanner_started",
        inotify=not watcher._force_polling,  # type: ignore[attr-defined]
        poll_interval=poll_interval,
        devices_dir=str(DEVICES_DIR),
    )

    # Initial reconcile on startup
    await _reconcile_once(settings, sensors, events, known_buses, log)

    try:
        while True:
            # Wait for a file system change (or polling tick)
            _ = await change_q.get()
            await _reconcile_once(settings, sensors, events, known_buses, log)
    except asyncio.CancelledError:
        log.info("scanner_stopping")
    finally:
        await watcher.stop()
        log.info("scanner_stopped")


def _make_on_change_callback(_events: "asyncio.Queue[Event]"):
    """
    (Kept for clarity; currently we push changes to an internal queue in scanner_loop.)
    """
    async def _noop(_ch: Change) -> None:  # pragma: no cover - placeholder
        return
    return _noop


async def _reconcile_once(
    settings: Settings,
    sensors: SensorSet,
    events: "asyncio.Queue[Event]",
    known_buses: set[BusKey],
    log,
) -> None:
    """
    One reconciliation pass:
      - Detect bus add/remove
      - Reconcile sensors
      - Emit events + metrics
    """
    # --- Buses: add/remove detection -------------------------------------------------
    current_buses = {(b.bus_id, (b.gpio if b.gpio is not None else -1)) for b in get_buses()}
    added_buses = current_buses - known_buses
    removed_buses = known_buses - current_buses

    for bus_id, gpio in sorted(added_buses):
        await events.put(BusDiscovered(bus_id=bus_id, gpio=gpio))
        log.info("bus_discovered", bus_id=bus_id, gpio=gpio)

    for bus_id, gpio in sorted(removed_buses):
        await events.put(BusRemoved(bus_id=bus_id, gpio=gpio))
        log.warning("bus_removed", bus_id=bus_id, gpio=gpio)

    known_buses.clear()
    known_buses.update(current_buses)

    # --- Sensors: reconcile addresses ------------------------------------------------
    addrs = enumerate_addresses()
    connected, missing = await sensors.reconcile_addresses(addrs)

    # Newly connected sensors
    for s in connected:
        await events.put(SensorConnected(addr=s.address, family=getattr(s.meta or None, "family", None),
                                         resolution_bits=getattr(s.meta or None, "resolution_bits", None)))
        on_sensor_connected(s.address, label=s.label)
        log.info(
            "sensor_connected",
            rom=s.rom, bus=s.address.bus_id, gpio=s.address.gpio, path=s.address.path, label=s.label
        )

    # Became missing
    for s in missing:
        await events.put(SensorDisconnected(addr=s.address))
        # Ensure temperature series vanish -> gaps in graphs
        mark_unavailable(s, strategy="remove")
        log.warning(
            "sensor_disconnected",
            rom=s.rom, bus=s.address.bus_id, gpio=s.address.gpio, path=s.address.path, label=s.label
        )
