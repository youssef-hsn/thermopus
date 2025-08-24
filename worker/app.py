#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DS18B20 Prometheus exporter (multi-bus, per-pin aware when provided a mapping).

Assumptions
- You are using the Linux 1-Wire kernel driver (w1-gpio + w1-therm).
- You may have multiple 1-Wire buses (one per GPIO). Each will appear as
  /sys/bus/w1/devices/w1_bus_master<N> with an associated 'w1_master_slaves' file.

Pin <-> Bus mapping (non-automatic by design)
- By default, metrics are labeled by 'bus' only.
- If you provide a mapping BUS_PIN_MAP (env JSON) or /etc/ds18b20_bus_map.json
  of the form {"w1_bus_master1": 4, "w1_bus_master2": 17, ...}, the exporter
  will also label metrics with the GPIO 'pin' (int). No guesses are made.

Logging
- Logs when a bus has no slaves (no communication), when reads fail, and when
  previously-seen sensors disappear (communication lost).

Metrics
- ds18b20_temperature_c{sensor_id, bus, pin?}
- ds18b20_connected{bus, pin?}                  # 1 if ≥1 sensor present on bus
- ds18b20_scrape_ok{sensor_id, bus, pin?}       # 1 on last read ok, else 0
- ds18b20_read_errors_total{sensor_id, bus, pin?}

Config via environment:
- EXPORTER_PORT          : Prometheus HTTP port (default 9101)
- SCRAPE_INTERVAL_SEC    : Seconds between scans (default 5)
- BUS_PIN_MAP            : JSON string mapping bus->pin (see above)
- BUS_WHITELIST          : Comma-separated list of bus names to include (optional)
"""

import os
import time
import json
import glob
import logging
from typing import Dict, List, Tuple, Optional

from prometheus_client import start_http_server, Gauge, Counter

# ---------- Configuration ----------
EXPORTER_PORT = int(os.getenv("EXPORTER_PORT", "9101"))
SCRAPE_INTERVAL_SEC = float(os.getenv("SCRAPE_INTERVAL_SEC", "5"))

SYSFS_W1_DEVICES = "/sys/bus/w1/devices"
BUS_GLOB = os.path.join(SYSFS_W1_DEVICES, "w1_bus_master*")
SLAVES_FILE = "w1_master_slaves"      # lists 28-xxxx...
TEMP_FILE = "w1_slave"                # under each device dir

# Optional bus whitelist (if you want to restrict which buses are scraped)
BUS_WHITELIST = set(
    [b.strip() for b in os.getenv("BUS_WHITELIST", "").split(",") if b.strip()]
)

# Load optional bus->pin map from env or file
def _load_bus_pin_map() -> Dict[str, int]:
    env_map = os.getenv("BUS_PIN_MAP")
    if env_map:
        try:
            parsed = json.loads(env_map)
            return {str(k): int(v) for k, v in parsed.items()}
        except Exception as e:
            logging.error("BUS_PIN_MAP is invalid JSON: %s", e)

    try:
        with open("/etc/ds18b20_bus_map.json", "r", encoding="utf-8") as f:
            parsed = json.load(f)
            return {str(k): int(v) for k, v in parsed.items()}
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.error("Failed reading /etc/ds18b20_bus_map.json: %s", e)

    return {}

BUS_PIN_MAP: Dict[str, int] = _load_bus_pin_map()

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

if not BUS_PIN_MAP:
    logging.info(
        "No BUS_PIN_MAP provided. Metrics will be labeled by 'bus' only. "
        "To add per-pin labels, set BUS_PIN_MAP env (JSON) or /etc/ds18b20_bus_map.json. "
        "Example: {\"w1_bus_master1\": 4, \"w1_bus_master2\": 17}"
    )

if BUS_WHITELIST:
    logging.info("BUS_WHITELIST active: %s", sorted(BUS_WHITELIST))

# ---------- Prometheus metrics ----------
g_temp_c = Gauge(
    "ds18b20_temperature_c",
    "DS18B20 temperature in Celsius",
    ["sensor_id", "bus", "pin"],
)

g_connected = Gauge(
    "ds18b20_connected",
    "Whether at least one DS18B20 is present on the bus (1 yes, 0 no)",
    ["bus", "pin"],
)

g_scrape_ok = Gauge(
    "ds18b20_scrape_ok",
    "1 if last read success for sensor, else 0",
    ["sensor_id", "bus", "pin"],
)

c_read_errors = Counter(
    "ds18b20_read_errors_total",
    "Total number of read errors per sensor",
    ["sensor_id", "bus", "pin"],
)

# Track seen sensors to log disappearance
_seen_sensors_per_bus: Dict[str, set] = {}

def list_buses() -> List[str]:
    buses = []
    for path in glob.glob(BUS_GLOB):
        bus = os.path.basename(path)
        if BUS_WHITELIST and bus not in BUS_WHITELIST:
            continue
        buses.append(bus)
    return sorted(buses)

def bus_dir(bus: str) -> str:
    return os.path.join(SYSFS_W1_DEVICES, bus)

def list_bus_sensors(bus: str) -> List[str]:
    slaves_path = os.path.join(bus_dir(bus), SLAVES_FILE)
    try:
        with open(slaves_path, "r", encoding="utf-8") as f:
            ids = [ln.strip() for ln in f.readlines()]
        # Filter typical DS18B20 family code '28-'
        return [sid for sid in ids if sid.startswith("28-")]
    except FileNotFoundError:
        # Bus present but file missing => treat as no communication
        logging.warning("No '%s' file for %s (no communication?)", SLAVES_FILE, bus)
        return []
    except Exception as e:
        logging.error("Error reading %s: %s", slaves_path, e)
        return []

def read_sensor_celsius(sensor_id: str) -> Tuple[bool, Optional[float], str]:
    """
    Returns (ok, value_c, err_msg).
    """
    dev_path = os.path.join(SYSFS_W1_DEVICES, sensor_id, TEMP_FILE)
    try:
        with open(dev_path, "r", encoding="utf-8") as f:
            lines = f.read().strip().splitlines()
        if len(lines) < 2 or "YES" not in lines[0]:
            return (False, None, "CRC not OK or malformed read")
        # line 2 example: "t=23123"
        if "t=" not in lines[1]:
            return (False, None, "Missing t= field")
        t_str = lines[1].split("t=")[-1].strip()
        milli_c = int(t_str)
        return (True, milli_c / 1000.0, "")
    except FileNotFoundError:
        return (False, None, "w1_slave not found (sensor removed?)")
    except ValueError:
        return (False, None, "Parse error for temperature")
    except Exception as e:
        return (False, None, f"Unhandled read error: {e}")

def pin_label_for_bus(bus: str) -> str:
    """
    Returns the pin label (string) for metric labeling.
    If unknown, return empty string "" (Prometheus label present but empty).
    """
    pin = BUS_PIN_MAP.get(bus)
    return str(pin) if pin is not None else ""

def scrape_once() -> None:
    global _seen_sensors_per_bus

    buses = list_buses()
    # Log if you expected specific buses but they don't exist: intentionally omitted;
    # we never assume which buses should exist.

    for bus in buses:
        pin_label = pin_label_for_bus(bus)
        sensors = list_bus_sensors(bus)

        if not sensors:
            # No communication on this bus/pin
            g_connected.labels(bus=bus, pin=pin_label).set(0)
            # Log only if previously had sensors or intermittently (avoid log spam)
            if _seen_sensors_per_bus.get(bus):
                logging.warning("Communication lost: %s (no sensors found now)", bus)
                _seen_sensors_per_bus[bus] = set()
            else:
                logging.info("No sensors on %s (pin %s)", bus, pin_label or "unknown")
            continue

        g_connected.labels(bus=bus, pin=pin_label).set(1)

        seen_now = set()
        for sid in sensors:
            seen_now.add(sid)
            ok, val_c, err = read_sensor_celsius(sid)
            if ok and val_c is not None:
                g_temp_c.labels(sensor_id=sid, bus=bus, pin=pin_label).set(val_c)
                g_scrape_ok.labels(sensor_id=sid, bus=bus, pin=pin_label).set(1)
            else:
                g_scrape_ok.labels(sensor_id=sid, bus=bus, pin=pin_label).set(0)
                c_read_errors.labels(sensor_id=sid, bus=bus, pin=pin_label).inc()
                logging.warning("Read error for %s on %s (pin %s): %s",
                                sid, bus, pin_label or "unknown", err)

        # Log disappearance of previously-seen sensors on this bus
        prev = _seen_sensors_per_bus.get(bus, set())
        disappeared = prev - seen_now
        for sid in disappeared:
            logging.warning("Sensor disappeared from %s (pin %s): %s",
                            bus, pin_label or "unknown", sid)

        _seen_sensors_per_bus[bus] = seen_now

def main():
    logging.info("Starting DS18B20 exporter on port %d, interval %.1fs",
                 EXPORTER_PORT, SCRAPE_INTERVAL_SEC)
    if BUS_PIN_MAP:
        logging.info("BUS_PIN_MAP in use: %s", BUS_PIN_MAP)

    start_http_server(EXPORTER_PORT)

    # Initialize seen set to avoid spurious "lost" logs on first pass
    for bus in list_buses():
        _seen_sensors_per_bus[bus] = set(list_bus_sensors(bus))

    while True:
        try:
            scrape_once()
        except Exception as e:
            logging.exception("Unexpected error during scrape: %s", e)
        time.sleep(SCRAPE_INTERVAL_SEC)

if __name__ == "__main__":
    main()