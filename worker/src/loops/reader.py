# worker/src/loops/reader.py
from __future__ import annotations

import asyncio
import time
from typing import Optional

from ..config.settings import Settings
from ..core.events import (
    Event,
    ReadingSucceeded,
    ReadingFailed,
    StateTransition,
    ReadErrorKind,
)
from ..core.enums import SensorState
from ..io.w1_sysfs import (
    read_temperature,
    CrcError,
    ParseError,
    NoDeviceError,
    IoReadError,
)
from ..metrics.metrics import observe_read_success, observe_read_error, mark_unavailable
from ..sensors.sensor_set import SensorSet
from ..util.asyncio_tools import with_timeout, gather_limited, backoff_delay, run_periodic
from ..util.logging import get_logger


async def reader_loop(
    settings: Settings,
    sensors: SensorSet,
    events: "asyncio.Queue[Event]",
    *,
    concurrency: int = 8,
    start_immediately: bool = True,
) -> None:
    """
    Periodically reads all eligible sensors with limited concurrency and per-sensor backoff.
    - Success: applies reading, emits ReadingSucceeded (+ optional StateTransition), updates metrics.
    - Failure/timeout: bumps error counters, emits ReadingFailed (+ optional StateTransition),
      applies exponential backoff, and on ERROR transition removes the temperature series
      to produce gaps in graphs.
    """
    log = get_logger("reader")

    # Next time (monotonic seconds) we are allowed to read a sensor (per ROM).
    next_allowed: dict[str, float] = {}

    interval = float(settings.read_interval.total_seconds())
    timeout = float(settings.read_timeout.total_seconds())

    async def _tick() -> None:
        nonlocal next_allowed
        now_mono = time.monotonic()

        # Snapshot current sensors
        all_sensors = await sensors.values()
        if not all_sensors:
            return

        # Filter eligibility:
        # - Skip DISABLED and MISSING (scanner will re-add when present)
        # - Apply per-sensor backoff (next_allowed)
        to_probe = []
        for s in all_sensors:
            if s.state in (SensorState.DISABLED, SensorState.MISSING):
                continue
            # Respect per-sensor backoff
            if next_allowed.get(s.rom, 0.0) > now_mono:
                continue
            to_probe.append(s)

        if not to_probe:
            # Still run staleness sweep below
            await _sweep_staleness()
            return

        # Prefers not to oversubscribe tiny Pi CPUs
        eff_concurrency = max(1, min(concurrency, len(to_probe)))

        async def _probe_one(s):
            nonlocal next_allowed
            # Run file I/O in thread to avoid blocking the event loop
            try:
                reading = await with_timeout(
                    asyncio.to_thread(read_temperature, s.address),
                    timeout=timeout,
                )

                # Apply to model
                transition = await sensors.apply_reading(s.rom, reading)
                # Update metrics
                observe_read_success(s)
                # Events
                await events.put(ReadingSucceeded(addr=s.address, reading=reading))
                if transition:
                    await events.put(
                        StateTransition(addr=s.address, old=transition.old, new=transition.new)
                    )
                # Schedule next probe at the normal read interval
                next_allowed[s.rom] = time.monotonic() + interval

            except asyncio.TimeoutError as e:
                await _note_failure(s, ReadErrorKind.TIMEOUT, "read timeout")
            except CrcError as e:
                await _note_failure(s, ReadErrorKind.CRC_FAIL, str(e))
            except ParseError as e:
                await _note_failure(s, ReadErrorKind.PARSE_ERROR, str(e))
            except NoDeviceError as e:
                await _note_failure(s, ReadErrorKind.NO_DEVICE, str(e))
            except IoReadError as e:
                await _note_failure(s, ReadErrorKind.IO_ERROR, str(e))
            except Exception as e:  # unknown/unexpected
                await _note_failure(s, ReadErrorKind.UNKNOWN, f"{type(e).__name__}: {e}")

        async def _note_failure(s, kind: ReadErrorKind, msg: Optional[str]) -> None:
            nonlocal next_allowed
            transition = await sensors.note_error(s.rom, kind, msg)
            observe_read_error(s, reason=kind.value)
            await events.put(
                ReadingFailed(
                    addr=s.address,
                    kind=kind,
                    message=msg,
                    attempts=s.consecutive_failures,
                )
            )
            # If we just crossed the error threshold -> mark unavailable for gaps
            if transition and transition.new is SensorState.ERROR:
                mark_unavailable(s, strategy="remove")
                await events.put(
                    StateTransition(addr=s.address, old=transition.old, new=transition.new)
                )

            # Backoff proportional to consecutive failures (capped)
            delay = backoff_delay(
                s.consecutive_failures,
                base=0.5,   # seconds
                factor=2.0,
                cap=8.0,
                jitter=0.1,
            )
            # Don't probe faster than the steady read interval
            next_allowed[s.rom] = time.monotonic() + max(interval, delay)

        # Run with limited concurrency
        await gather_limited(eff_concurrency, [lambda s=s: _probe_one(s) for s in to_probe])

        # After probing, sweep for stale sensors (no recent successful read)
        await _sweep_staleness()

    async def _sweep_staleness() -> None:
        changes = await sensors.sweep_staleness()
        for t in changes:
            s = await sensors.get(t.rom)
            if s is None:
                continue
            await events.put(StateTransition(addr=s.address, old=t.old, new=t.new))

    log.info(
        "reader_started",
        interval_s=interval,
        timeout_s=timeout,
        concurrency=concurrency,
    )

    try:
        await run_periodic(
            _tick,
            interval_seconds=interval,
            start_immediately=start_immediately,
            jitter=0.05,  # small jitter to avoid synchronized sampling across sensors
        )
    except asyncio.CancelledError:
        log.info("reader_stopping")
        return
