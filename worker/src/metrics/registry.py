from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

from prometheus_client import Counter, Gauge, start_http_server

from ..core.enums import SensorState
from ..core.types import Address
from ..sensors import Sensor

# --------------------------------
# Metric definitions (singleton)
# --------------------------------

_LABELS = ("rom", "bus", "gpio", "label")
_STATE_LABELS = tuple(s.value for s in SensorState)  # ("new","active","stale","missing","error","disabled")


@dataclass(frozen=True)
class Metrics:
    # Value metrics
    temperature_c: Gauge
    last_read_ts: Gauge

    # State gauge (one-hot)
    sensor_state: Gauge

    # Counters
    read_errors_total: Counter
    sensors_discovered_total: Counter


_METRICS_SINGLETON: Optional[Metrics] = None


def get_metrics() -> Metrics:
    """Create (once) and return the metrics registry for this process."""
    global _METRICS_SINGLETON
    if _METRICS_SINGLETON is not None:
        return _METRICS_SINGLETON

    temperature_c = Gauge(
        "ds18b20_temperature_celsius",
        "DS18B20 temperature in Celsius.",
        labelnames=_LABELS,
    )
    last_read_ts = Gauge(
        "ds18b20_last_read_timestamp_seconds",
        "Unix timestamp (seconds) of last successful read.",
        labelnames=_LABELS,
    )
    sensor_state = Gauge(
        "ds18b20_sensor_state",
        "One-hot sensor state; 1 for current state, 0 otherwise.",
        labelnames=(*_LABELS, "state"),
    )
    read_errors_total = Counter(
        "ds18b20_read_errors_total",
        "Count of read failures by reason.",
        labelnames=(*_LABELS, "reason"),
    )
    sensors_discovered_total = Counter(
        "ds18b20_sensors_discovered_total",
        "Total sensors seen on a (bus,gpio) over process lifetime.",
        labelnames=("bus", "gpio"),
    )

    _METRICS_SINGLETON = Metrics(
        temperature_c=temperature_c,
        last_read_ts=last_read_ts,
        sensor_state=sensor_state,
        read_errors_total=read_errors_total,
        sensors_discovered_total=sensors_discovered_total,
    )
    return _METRICS_SINGLETON


# --------------------------------
# Label helpers
# --------------------------------

def _labels_for_sensor(sensor: Sensor) -> Dict[str, str]:
    return {
        "rom": sensor.rom,
        "bus": str(sensor.address.bus_id),
        "gpio": str(sensor.address.gpio),
        "label": sensor.label or "",
    }


def _label_tuple_for_sensor(sensor: Sensor) -> Tuple[str, str, str, str]:
    d = _labels_for_sensor(sensor)
    return (d["rom"], d["bus"], d["gpio"], d["label"])


def _labels_for_addr(addr: Address, label: Optional[str]) -> Dict[str, str]:
    return {
        "rom": addr.rom,
        "bus": str(addr.bus_id),
        "gpio": str(addr.gpio),
        "label": label or "",
    }


# --------------------------------
# Public API (call from loops)
# --------------------------------

def serve_http(port: int, addr: str = "0.0.0.0") -> None:
    """Start the Prometheus exposition server on given port."""
    start_http_server(port, addr=addr)


def on_sensor_connected(addr: Address, label: Optional[str] = None) -> None:
    """
    Call when a sensor first appears. Increments discovery counter and
    pre-initializes state rows to 0 (optional).
    """
    m = get_metrics()
    m.sensors_discovered_total.labels(bus=str(addr.bus_id), gpio=str(addr.gpio)).inc()
    for state in _STATE_LABELS:
        m.sensor_state.labels(**_labels_for_addr(addr, label), state=state).set(0.0)


def observe_read_success(sensor: Sensor) -> None:
    """
    After a successful reading is applied to the Sensor object.
    Sets temperature/ts and updates one-hot state.
    """
    if sensor.last_reading is None:
        return

    m = get_metrics()
    base = _labels_for_sensor(sensor)

    # value + timestamp
    m.temperature_c.labels(**base).set(sensor.last_reading.value_c)
    m.last_read_ts.labels(**base).set(sensor.last_reading.at.timestamp())

    # state one-hot
    for state in _STATE_LABELS:
        m.sensor_state.labels(**base, state=state).set(1.0 if sensor.state.value == state else 0.0)


def observe_read_error(sensor: Sensor, *, reason: str, message: Optional[str] = None) -> None:
    """
    After a failed read. Bumps error counter and updates state one-hot
    (your model sets state based on error threshold).
    """
    m = get_metrics()
    base = _labels_for_sensor(sensor)
    m.read_errors_total.labels(**base, reason=reason).inc()

    for state in _STATE_LABELS:
        m.sensor_state.labels(**base, state=state).set(1.0 if sensor.state.value == state else 0.0)


# ---------- Unavailability handling for GAPs ----------

def mark_unavailable(sensor: Sensor, *, strategy: str = "remove") -> None:
    """
    Call when the sensor is MISSING/ERROR and you want temperature graphs to show gaps.

    strategy = "remove" (default): delete the temperature/ts series entirely so
        Prometheus sees them disappear -> staleness marker -> visible gap.
    strategy = "nan": keep the series but set temperature to NaN (Grafana renders a gap).
    """
    m = get_metrics()
    base = _labels_for_sensor(sensor)

    if strategy == "remove":
        # Remove labeled series so they vanish from exposition
        label_tuple = _label_tuple_for_sensor(sensor)  # order must match _LABELS
        try:
            m.temperature_c.remove(*label_tuple)
        except KeyError:
            pass
        try:
            m.last_read_ts.remove(*label_tuple)
        except KeyError:
            pass
    elif strategy == "nan":
        # Emit NaN for gaps, keep last_read_ts untouched (or remove it too if you prefer)
        m.temperature_c.labels(**base).set(float("nan"))
    else:
        raise ValueError("strategy must be 'remove' or 'nan'")

    # Keep state gauge published so dashboards can still show MISSING/ERROR
    for state in _STATE_LABELS:
        m.sensor_state.labels(**base, state=state).set(1.0 if sensor.state.value == state else 0.0)


def export_full_snapshot(sensors: Iterable[Sensor]) -> None:
    """Idempotently set all gauges for a full snapshot (useful on startup or periodic sync)."""
    m = get_metrics()
    for s in sensors:
        base = _labels_for_sensor(s)
        if s.last_reading is not None:
            m.temperature_c.labels(**base).set(s.last_reading.value_c)
            m.last_read_ts.labels(**base).set(s.last_reading.at.timestamp())
        # State one-hot
        for state in _STATE_LABELS:
            m.sensor_state.labels(**base, state=state).set(1.0 if s.state.value == state else 0.0)
