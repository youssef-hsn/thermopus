from __future__ import annotations

import asyncio
import signal
from typing import Optional

from config.settings import Settings
from core.events import Event
from loops.scanner import scanner_loop
from loops.reader import reader_loop
from loops.reporter import reporter_loop
from sensors.sensor_set import SensorSet
from util.logging import init_logging, get_logger
import logging


async def main_async() -> None:
    settings = Settings()
    # Switch json=True if you prefer JSON logs in Docker
    init_logging(settings.log_level, json=False)
    log = get_logger("main")

    # Shared state
    events: asyncio.Queue[Event] = asyncio.Queue()
    sensors = SensorSet(
        freshness_window=settings.freshness_window,
        label_map=settings.label_map,
        disabled_roms=settings.disabled_roms,
        error_threshold=3,  # tune if needed
    )

    # Start loops
    tasks = [
        asyncio.create_task(scanner_loop(settings, sensors, events), name="scanner"),
        asyncio.create_task(reader_loop(settings, sensors, events, concurrency=8), name="reader"),
        asyncio.create_task(reporter_loop(settings, sensors, events), name="metrics"),
    ]

    # Graceful shutdown on SIGINT/SIGTERM
    stop_event = asyncio.Event()

    def _handle_signal(sig: int) -> None:
        log.warning("shutdown_signal_received", signal=sig)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except NotImplementedError:
            # add_signal_handler may be unavailable on some platforms
            pass

    log.info(
        "worker_started",
        pins=settings.pins,
        port=settings.metrics_port,
        scan_interval_s=settings.scan_interval.total_seconds(),
        read_interval_s=settings.read_interval.total_seconds(),
    )

    # Wait for a shutdown signal
    await stop_event.wait()

    # Cancel tasks and wait for them to exit
    for t in tasks:
        t.cancel()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for name, res in zip(["scanner", "reader", "metrics"], results):
        if isinstance(res, Exception):
            log.error("task_ended_with_error", task=name, error=repr(res))
        else:
            log.info("task_stopped", task=name)

    log.info("worker_stopped")


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logging.getLogger("main").warning("shutdown_signal_received", signal="KeyboardInterrupt")


if __name__ == "__main__":
    main()