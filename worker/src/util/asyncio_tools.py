from __future__ import annotations

import asyncio
import math
import random
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Optional, TypeVar


__all__ = [
    "cancellable_sleep",
    "jittered",
    "backoff_delay",
    "run_periodic",
    "gather_limited",
    "BoundedWorkerPool",
    "with_timeout",
    "shielded",
]

T = TypeVar("T")


# ---- Sleep & jitter ----------------------------------------------------------

async def cancellable_sleep(seconds: float) -> None:
    """
    A simple sleep that is cooperative with cancellation.
    """
    await asyncio.sleep(max(0.0, float(seconds)))


def jittered(base: float, *, jitter: float = 0.0, minimum: float = 0.0) -> float:
    """
    Return base +/- jitter% (0..1) with a floor at `minimum`.
    jitter=0.1 -> ±10%. If jitter=0, returns base.
    """
    base = float(base)
    if jitter <= 0:
        return max(base, minimum)
    span = base * float(jitter)
    return max(base + random.uniform(-span, span), minimum)


def backoff_delay(attempt: int, *, base: float = 0.5, factor: float = 2.0, cap: float = 8.0, jitter: float = 0.1) -> float:
    """
    Exponential backoff delay in seconds (with jitter).
    - attempt: 1-based attempt count (1,2,3,...)
    - base:    starting delay
    - factor:  exponential growth per attempt
    - cap:     max delay
    - jitter:  ±percentage noise for desynchronization
    """
    attempt = max(1, int(attempt))
    delay = base * (factor ** (attempt - 1))
    delay = min(delay, cap)
    return jittered(delay, jitter=jitter, minimum=0.0)


# ---- Periodic runner ---------------------------------------------------------

async def run_periodic(
    func: Callable[[], Awaitable[None]],
    interval_seconds: float,
    *,
    start_immediately: bool = True,
    jitter: float = 0.0,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """
    Run `func()` forever at ~interval_seconds (with optional jitter) until cancelled or stop_event is set.
    """
    try:
        if start_immediately:
            await func()
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            await cancellable_sleep(jittered(interval_seconds, jitter=jitter, minimum=0.0))
            if stop_event is not None and stop_event.is_set():
                return
            await func()
    except asyncio.CancelledError:
        # cooperative shutdown
        return


# ---- Concurrency helpers -----------------------------------------------------

async def gather_limited(
    n: int,
    coros: Iterable[Callable[[], Awaitable[T]]],
    *,
    return_exceptions: bool = False,
) -> list[T]:
    """
    Run a list/generator of callables that return coroutines with concurrency limit n.
    """
    sem = asyncio.Semaphore(max(1, int(n)))
    results: list[T] = []

    async def _runner(coro_factory: Callable[[], Awaitable[T]]) -> T:
        async with sem:
            return await coro_factory()

    tasks = [asyncio.create_task(_runner(cf)) for cf in coros]
    try:
        for t in asyncio.as_completed(tasks):
            res = await t
            results.append(res)
    except Exception:
        if not return_exceptions:
            for t in tasks:
                t.cancel()
            raise
        # collect with exceptions
        results = [await _safe_result(t) for t in tasks]
    return results


async def _safe_result(task: "asyncio.Task[T]") -> T:
    try:
        return await task
    except Exception as e:  # type: ignore[return-value]
        return e  # type: ignore[return-value]


class BoundedWorkerPool:
    """
    A tiny concurrency-limited worker pool (queue + fixed workers).
    Use for steady flows like sensor reads.

    Example:
        pool = BoundedWorkerPool(workers=8)
        await pool.start()

        await pool.submit(read_sensor, arg1, arg2)
        ...
        await pool.stop()
    """
    def __init__(self, *, workers: int = 4, max_queue: int = 0) -> None:
        self._workers = max(1, int(workers))
        self._queue: "asyncio.Queue[tuple[Callable[..., Awaitable[None]], tuple[Any, ...], dict[str, Any]]]" = asyncio.Queue(maxsize=max_queue)
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        self._tasks = [asyncio.create_task(self._worker_loop(i)) for i in range(self._workers)]

    async def stop(self) -> None:
        self._stopping.set()
        # Enqueue poison pills for workers to exit promptly
        for _ in self._tasks:
            await self._queue.put((_noop_async, (), {}))
        for t in self._tasks:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    async def submit(self, fn: Callable[..., Awaitable[None]], *args: Any, **kwargs: Any) -> None:
        await self._queue.put((fn, args, kwargs))

    async def _worker_loop(self, idx: int) -> None:
        try:
            while not self._stopping.is_set():
                fn, args, kwargs = await self._queue.get()
                if fn is _noop_async:
                    # shutdown sentinel
                    return
                try:
                    await fn(*args, **kwargs)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            return


async def _noop_async(*_: Any, **__: Any) -> None:
    return


# ---- Timeout / shielding -----------------------------------------------------

async def with_timeout(aw: Awaitable[T], timeout: float) -> T:
    """
    Await with a timeout (raises asyncio.TimeoutError on expiry).
    """
    return await asyncio.wait_for(aw, timeout=timeout)


async def shielded(aw: Awaitable[T]) -> T:
    """
    Run a coroutine under asyncio.shield() so outer cancellation doesn't kill it.
    Useful for final flush / metrics write.
    """
    return await asyncio.shield(aw)


# Optional: async context manager to temporarily ignore CancelledError
@asynccontextmanager
async def ignore_cancellation() -> AsyncIterator[None]:
    try:
        yield
    except asyncio.CancelledError:
        # swallow cancellation inside the context
        pass
