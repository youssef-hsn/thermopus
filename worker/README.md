# Thermopus Worker
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

## Setup & Run (Raspberry Pi 5)

### 1) Enable 1-Wire (optionally on multiple GPIO pins)
The worker relies on the Linux 1-Wire kernel drivers (`w1-gpio`, `w1-therm`). On Raspberry Pi OS Bookworm,
edit `/boot/firmware/config.txt` and add one `dtoverlay=w1-gpio` line per GPIO you want to use:

#### Single bus (default on GPIO 4)

```
dtoverlay=w1-gpio,gpiopin=4,pullup=1
```

#### Multiple buses (example)

```
dtoverlay=w1-gpio,gpiopin=17,pullup=1
dtoverlay=w1-gpio,gpiopin=22,pullup=1
```

#### Reboot, then verify:

```bash
ls -1 /sys/bus/w1/devices/
```

> You should see entries like w1_bus_master1 and 28-xxxxxxxxxxxx ROM devices when sensors are attached

### 2) Configure the worker
Copy the example env and adjust to your wiring and intervals:
```bash
cp worker/.env.example worker/.env
```

#### Edit worker/.env
**Key variables**:
- `WORKER_PINS=4,17,22` — BCM GPIOs hosting your 1-Wire buses
- `WORKER_METRICS_PORT=8000` — Prometheus scrape port
- `WORKER_SCAN_INTERVAL`, `WORKER_READ_INTERVAL`, `WORKER_READ_TIMEOUT` — support `ms/s/m/h` units
- `WORKER_LABEL_MAP` — JSON map of ROM → human-friendly name

### 3) Build the Docker image
From the repository root (folder containing `worker/`):

```bash
docker build -f worker/dockerfile -t thermopus-worker:latest .
```

### 4) Run the container
Mount the host’s 1-Wire sysfs into the container and expose the metrics port.

#### Run with host networking
```bash
docker run –rm -it 
–name thermopus-worker 
–network host 
–env-file worker/.env 
-v /sys/bus/w1:/sys/bus/w1:ro 
thermopus-worker:latest
```

#### Health Check
```bash
curl http://localhost:8000/metrics
```

### 5) Prometheus scrape config
Add a job to your Prometheus configuration:

```yaml
scrape_configs:
  - job_name: 'thermopus-worker'
    static_configs:
      - targets: ['pi-hostname-or-ip:8000']
```

### 6) Hot-plug visibility

The worker scans /sys/bus/w1/devices/ periodically (or via inotify if WORKER_USE_INOTIFY=true)
to log connect/disconnect events. Ensure sensors appear under that directory when plugged in.

