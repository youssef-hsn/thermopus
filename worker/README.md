## Thermopus Worker
Thermopus worker is a python worker that works with prometheus to log data from DS18B20 sensors

## Project Structure
To make it easier to debug and understand the code a clean custom structure was built for this project, the following is an example of how things are structured
```
worker/
├─ pyproject.toml                # poetry/pip build + deps (prometheus-client, pydantic, watchdog)
├─ README.md                     # how to enable w1 on Pi 5, run as systemd, config notes
├─ .env.example                  # PORT=9108, SCAN_INTERVAL=5s, READ_INTERVAL=2s, PINS=4,17,22,...
├─ .gitignore
├─ scripts/
│  ├─ enable-w1-multi-bus.sh     # adds dtoverlay w1-gpio for each pin; reboots if needed
│  ├─ dev-run.sh                 # runs app with local .env
│  └─ list-buses.sh              # prints /sys/bus/w1/devices/w1_bus_master* map → gpio pin
└─  src/
   ├─ __init__.py
   ├─ main.py                 # wires up asyncio tasks: scanner, reader, metrics
   ├─ config/
   │  ├─ __init__.py
   │  └─ settings.py          # Pydantic: pins list, intervals, labels, http port, log level
   ├─ core/
   │  ├─ __init__.py
   │  ├─ enums.py             # SensorState(Enum): NEW, ACTIVE, STALE, MISSING, ERROR, DISABLED
   │  ├─ events.py            # dataclasses: Connected, Disconnected, ReadingError, etc.
   │  └─ types.py             # type aliases (SensorId, BusId, GpioPin, Timestamp)
   ├─ io/
   │  ├─ __init__.py
   │  ├─ w1_sysfs.py          # read/sysfs helpers; parse ROM codes; crc check; bus discovery
   │  └─ watcher.py           # (optional) inotify/watchdog dir watcher for hot-plug events
   ├─ sensors/
   │  ├─ __init__.py
   │  ├─ ds18b20.py           # Sensor class: metadata, last_reading, state machine, read()
   │  └─ sensor_set.py        # SensorSet: registry keyed by ROM; add/remove/update; thread-safe
   ├─ loops/
   │  ├─ __init__.py
   │  ├─ scanner.py           # scan loop: reconcile sysfs vs SensorSet; emit connect/disconnect
   │  ├─ reader.py            # read loop: concurrent reads with backoff; updates SensorSet
   │  └─ metrics.py           # metrics loop: updates Gauges from SensorSet; serves HTTP
   ├─ metrics/
   │  ├─ __init__.py
   │  └─ registry.py          # prometheus_client Gauge/Counter/Info definitions + labels
   └─ utils/
      ├─ __init__.py
      ├─ logging.py           # structlog/logging config; JSON logs optional
      └─ asyncio_tools.py     # cancellation shields, jittered sleeps, bounded semaphore
``` 