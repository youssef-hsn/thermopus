from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

from ..core.types import Address, Reading, Timestamp, utc_now, Celsius

# ---- Constants & helpers ----------------------------------------------------

DEVICES_DIR = Path("/sys/bus/w1/devices")
BUS_PREFIX = "w1_bus_master"          # e.g. w1_bus_master1
ROM_PREFIXES: Tuple[str, ...] = ("28-",)  # DS18B20 family directory prefix
W1_SLAVE_FILENAME = "w1_slave"        # file to read temperature and CRC

_ROM_RE = re.compile(r"^(?:%s)[0-9a-fA-F]+$" % "|".join(map(re.escape, ROM_PREFIXES)))
_BUS_RE = re.compile(rf"^{re.escape(BUS_PREFIX)}(\d+)$")


@dataclass(frozen=True, slots=True)
class BusInfo:
    """Kernel 1-Wire master bus."""
    bus_id: int
    path: Path
    gpio: Optional[int] = None  # Not always available; best-effort


# ---- Discovery --------------------------------------------------------------

def list_bus_dirs(devices_dir: Path = DEVICES_DIR) -> Dict[int, Path]:
    """Return all w1 master directories as {bus_id: path}."""
    result: Dict[int, Path] = {}
    if not devices_dir.is_dir():
        return result
    for child in devices_dir.iterdir():
        m = _BUS_RE.match(child.name)
        if m and child.is_dir():
            result[int(m.group(1))] = child
    return result


def _read_int_file(p: Path) -> Optional[int]:
    try:
        txt = p.read_text().strip()
        return int(txt)
    except Exception:
        return None


def get_buses(devices_dir: Path = DEVICES_DIR) -> List[BusInfo]:
    """
    Enumerate w1 masters. Attempts to read GPIO mapping if kernel exposes it
    (some kernels export 'w1_master_gpio' under each bus master).
    """
    buses: List[BusInfo] = []
    for bus_id, path in list_bus_dirs(devices_dir).items():
        gpio: Optional[int] = None
        gpio_file = path / "w1_master_gpio"         # present on some builds
        if gpio_file.exists():
            gpio = _read_int_file(gpio_file)
        buses.append(BusInfo(bus_id=bus_id, path=path, gpio=gpio))
    return sorted(buses, key=lambda b: b.bus_id)


def list_roms_on_bus(bus: BusInfo) -> Set[str]:
    """
    Parse ROM list from w1 master file if available; otherwise fall back to
    scanning device directories.
    """
    slaves_file = bus.path / "w1_master_slaves"
    roms: Set[str] = set()
    if slaves_file.exists():
        try:
            for line in slaves_file.read_text().splitlines():
                s = line.strip()
                if s and _ROM_RE.match(s):
                    roms.add(s)
        except Exception:
            pass

    if not roms:
        # Fallback: scan device dirs; not filtered by bus, but works on kernels that
        # mount a per-bus subdir under the master path. If not, we still return empty
        # here and rely on enumerate_addresses() which cross-checks.
        for child in bus.path.iterdir():
            if child.is_dir() and _ROM_RE.match(child.name):
                roms.add(child.name)
    return roms


def enumerate_addresses(devices_dir: Path = DEVICES_DIR) -> List[Address]:
    """
    Enumerate all DS18B20 sensors as Address objects across all buses.
    Strategy:
      - Prefer parsing each bus master 'w1_master_slaves'
      - As a safety net, also scan DEVICES_DIR for 28-XXXX dirs and associate
        via 'w1_master_slaves' membership (fast and robust)
    """
    buses = get_buses(devices_dir)
    by_bus: Dict[int, Set[str]] = {b.bus_id: list_roms_on_bus(b) for b in buses}

    # Build address list with path to 'w1_slave'
    addrs: List[Address] = []
    for bus in buses:
        for rom in sorted(by_bus.get(bus.bus_id, ())):
            slave_path = devices_dir / rom / W1_SLAVE_FILENAME
            addrs.append(Address(rom=rom, bus_id=bus.bus_id, gpio=bus.gpio or -1, path=slave_path))

    # Safety net: ensure any 28-XXXX dirs that somehow weren't listed get included
    # (bus_id unknown -> -1)
    for child in devices_dir.iterdir():
        if child.is_dir() and _ROM_RE.match(child.name):
            rom = child.name
            if not any(a.rom == rom for a in addrs):
                slave_path = child / W1_SLAVE_FILENAME
                addrs.append(Address(rom=rom, bus_id=-1, gpio=-1, path=slave_path))

    return addrs


# ---- Reading ----------------------------------------------------------------

class CrcError(Exception): ...
class ParseError(Exception): ...
class NoDeviceError(Exception): ...
class IoReadError(Exception): ...


def _parse_w1_slave(content: str) -> Tuple[int, bool]:
    """
    Parse w1_slave file content.
    Returns: (millideg_celsius, crc_ok)
    Raises ParseError if format is unexpected.
    """
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ParseError("w1_slave: expected at least 2 lines")

    # First line contains CRC verdict: "... YES" / "... NO"
    crc_ok = lines[0].endswith("YES")

    # Second line includes "t=<millideg>"
    # Example: "t=24125"
    idx = lines[1].find("t=")
    if idx == -1:
        raise ParseError("w1_slave: missing 't=' value")
    try:
        milli = int(lines[1][idx + 2 :])
    except ValueError as e:
        raise ParseError("w1_slave: invalid temperature integer") from e

    return milli, crc_ok


def read_temperature(addr: Address) -> Reading:
    """
    Read a single DS18B20 via its w1_slave file.
    - Raises NoDeviceError if the file is missing
    - Raises CrcError if CRC indicates 'NO'
    - Raises ParseError on unexpected format
    - Raises IoReadError on generic I/O failures
    """
    slave = Path(addr.path) if addr.path else (DEVICES_DIR / addr.rom / W1_SLAVE_FILENAME)
    t0 = time.perf_counter()
    try:
        content = slave.read_text()
    except FileNotFoundError as e:
        raise NoDeviceError(f"device file not found: {slave}") from e
    except PermissionError as e:
        raise IoReadError(f"permission denied: {slave}") from e
    except OSError as e:
        raise IoReadError(f"i/o error reading {slave}: {e}") from e

    milli, crc_ok = _parse_w1_slave(content)
    if not crc_ok:
        raise CrcError("CRC check failed")

    latency_ms = (time.perf_counter() - t0) * 1000.0
    return Reading(
        value_c=milli / 1000.0,
        raw_millideg=milli,
        at=utc_now(),
        path=str(slave),
        latency_ms=latency_ms,
    )


# ---- Cheap change detection --------------------------------------------------

def snapshot_devices(devices_dir: Path = DEVICES_DIR) -> Tuple[Set[str], Set[str]]:
    """
    Return a snapshot of current bus and rom names:
    (bus_names, rom_names)
    """
    bus_names: Set[str] = set()
    rom_names: Set[str] = set()
    if not devices_dir.exists():
        return bus_names, rom_names

    for child in devices_dir.iterdir():
        n = child.name
        if _BUS_RE.match(n):
            bus_names.add(n)
        elif _ROM_RE.match(n):
            rom_names.add(n)
    return bus_names, rom_names
