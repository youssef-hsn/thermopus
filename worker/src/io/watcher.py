from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple

from .w1_sysfs import DEVICES_DIR, snapshot_devices

# We use watchdog if available; otherwise we fall back to a polling watcher.
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_AVAILABLE = True
except Exception:
    _WATCHDOG_AVAILABLE = False


@dataclass(slots=True)
class Change:
    """A minimal change signal for the scanner loop to react to."""
    buses_changed: bool
    roms_changed: bool
    # You can extend this later with specific names if needed.


class _DirHandler(FileSystemEventHandler):
    """Watchdog handler that triggers an asyncio callback when sysfs changes."""

    def __init__(self, on_change: Callable[[Change], Awaitable[None]], devices_dir: Path):
        super().__init__()
        self._on_change = on_change
        self._devices_dir = devices_dir

    def _queue_change(self, *_):
        # We don't try to classify deeply here—scanner will reconcile truth.
        asyncio.get_event_loop().create_task(self._on_change(Change(True, True)))

    # Directory and file add/remove/modify events
    on_created = _queue_change
    on_deleted = _queue_change
    on_moved = _queue_change
    on_modified = _queue_change


class SysfsWatcher:
    """
    Cross-platform sysfs watcher that emits coarse 'Change' signals.
    - If watchdog is present, uses inotify.
    - Otherwise, periodically polls directory listings and compares hashes.
    Typical usage:
        q: asyncio.Queue[Change] = asyncio.Queue()
        watcher = SysfsWatcher(lambda ch: q.put_nowait(ch))
        await watcher.start()
        ...
        await watcher.stop()
    """

    def __init__(
        self,
        on_change: Callable[[Change], Awaitable[None]],
        *,
        devices_dir: Path = DEVICES_DIR,
        poll_interval: float = 2.0,
        force_polling: bool = False,
    ) -> None:
        self._on_change = on_change
        self._devices_dir = devices_dir
        self._poll_interval = poll_interval
        self._force_polling = force_polling or not _WATCHDOG_AVAILABLE

        # watchdog fields
        self._observer: Optional[Observer] = None

        # polling fields
        self._polling_task: Optional[asyncio.Task[None]] = None
        self._last_sig: Optional[Tuple[str, str]] = None  # (bus_sig, rom_sig)

    async def start(self) -> None:
        if not self._force_polling:
            # Start watchdog observer in a background thread
            self._observer = Observer()
            handler = _DirHandler(self._on_change, self._devices_dir)
            self._observer.schedule(handler, str(self._devices_dir), recursive=False)
            self._observer.start()
        else:
            # Start polling task
            self._polling_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2.0)
            self._observer = None
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None

    async def _poll_loop(self) -> None:
        # Initialize signature
        self._last_sig = self._signature()
        # Emit an initial 'changed' to force a scan on start
        await self._on_change(Change(True, True))

        try:
            while True:
                await asyncio.sleep(self._poll_interval)
                sig = self._signature()
                buses_changed = sig[0] != self._last_sig[0]
                roms_changed = sig[1] != self._last_sig[1]
                if buses_changed or roms_changed:
                    self._last_sig = sig
                    await self._on_change(Change(buses_changed, roms_changed))
        except asyncio.CancelledError:
            return

    def _signature(self) -> Tuple[str, str]:
        buses, roms = snapshot_devices(self._devices_dir)
        bus_sig = _hash_names(buses)
        rom_sig = _hash_names(roms)
        return bus_sig, rom_sig


def _hash_names(names: set[str]) -> str:
    h = hashlib.sha256()
    for n in sorted(names):
        h.update(n.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()
