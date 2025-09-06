from __future__ import annotations

import asyncio
from typing import Optional

from ..config.settings import Settings
from ..core.events import Event
from ..metrics.metrics import serve_http, export_full_snapshot
from ..sensors.sensor_set import SensorSet
from ..util.asyncio_tools import run_periodic
from ..util.logging import get_logger


async def reporter_loop(
    settings: Settings,
    sensors: SensorSet,
    events: Optional["asyncio.Queue[Event]"] = None,  # reserved for future use
    *,
    snapshot_every: float | None = None,
    start_immediately: bool = True,
) -> None:
    """
    Metrics loop:
      - Starts the Prometheus HTTP server on WORKER_METRICS_PORT.
      - Periodically exports a full snapshot of all sensors to keep gauges in sync.
        (temperature series for missing/error sensors are handled by scanner/reader
         via mark_unavailable(); snapshot won't re-create removed series.)
    """
    log = get_logger("metrics-loop")

    # Start Prometheus exposition (idempotent within this process)
    serve_http(settings.metrics_port)

    # Pick a sensible default snapshot cadence
    interval = (
        float(settings.read_interval.total_seconds())
        if snapshot_every is None
        else float(snapshot_every)
    )
    interval = max(5.0, interval)  # avoid overly chatty snapshots

    log.info("metrics_loop_started", port=settings.metrics_port, snapshot_interval_s=interval)

    async def _tick() -> None:
        export_full_snapshot(await sensors.values())

    try:
        await run_periodic(
            _tick,
            interval_seconds=interval,
            start_immediately=start_immediately,
            jitter=0.05,
        )
    except asyncio.CancelledError:
        log.info("metrics_loop_stopping")
        return
