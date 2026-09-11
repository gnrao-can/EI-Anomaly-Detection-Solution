"""
shared/retry.py
===============
Small async retry-with-backoff helper for startup connections.

On a local ``podman-compose`` bring-up the app services frequently start before
their infrastructure dependency (Kafka in particular) is ready, because
``depends_on`` was intentionally removed from the compose (it caused
out-of-order-removal deadlocks + netns coupling under podman-compose — see
docs/regeneration/STATUS.md "Task 30 — compose gotchas"). Without a retry, a
service's single connect attempt fails, it degrades gracefully, and it stays
inert until someone restarts it manually.

:func:`connect_with_retry` awaits an async "connect" factory, retrying on any
exception with exponential backoff up to a bounded number of attempts. It keeps
the graceful-degradation contract: after the final failed attempt it returns
``None`` (rather than raising) so the caller can degrade exactly as before — but
in the common case where the broker comes up a few seconds later, the service
attaches itself with no manual restart.

The helper is intentionally dependency-free (stdlib ``asyncio`` + ``logging``)
so every service can use it without new packages.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional, TypeVar

logger = logging.getLogger("shared.retry")

T = TypeVar("T")

#: Default startup-connect retry parameters. ~10 attempts with backoff
#: (1s, 2s, 4s, 8s, 10s, 10s, ...) comfortably covers a broker's start_period
#: without blocking service startup indefinitely.
DEFAULT_MAX_ATTEMPTS: int = 10
DEFAULT_INITIAL_DELAY_S: float = 1.0
DEFAULT_MAX_DELAY_S: float = 10.0


async def connect_with_retry(
    factory: Callable[[], Awaitable[T]],
    *,
    description: str = "dependency",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    initial_delay_s: float = DEFAULT_INITIAL_DELAY_S,
    max_delay_s: float = DEFAULT_MAX_DELAY_S,
    on_failed_attempt: Optional[Callable[[T], Awaitable[None]]] = None,
) -> Optional[T]:
    """Await ``factory()`` with exponential backoff; return its result or ``None``.

    Parameters
    ----------
    factory:
        An async callable that establishes and returns the connection/resource
        (e.g. builds an ``AIOKafkaProducer`` and ``await``\\s its ``.start()``).
        It is retried on ANY exception.
    description:
        Human-readable name of the dependency, used in log messages.
    max_attempts:
        Maximum number of attempts before giving up (and returning ``None``).
    initial_delay_s / max_delay_s:
        Exponential-backoff bounds between attempts.
    on_failed_attempt:
        Optional async cleanup invoked with the partially-built result before a
        retry (rarely needed; ``factory`` should normally clean up its own
        half-built state).

    Returns
    -------
    The value returned by ``factory()`` on success, or ``None`` when every
    attempt failed (the caller then degrades gracefully, exactly as a
    single-attempt connect would have).
    """
    delay = initial_delay_s
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await factory()
        except Exception as exc:  # noqa: BLE001 - retry on any connection failure
            last_exc = exc
            if on_failed_attempt is not None:
                try:
                    await on_failed_attempt(None)  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001 - cleanup is best-effort
                    pass
            if attempt < max_attempts:
                logger.warning(
                    "%s connect attempt %d/%d failed (%s); retrying in %.1fs",
                    description,
                    attempt,
                    max_attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay_s)

    logger.warning(
        "%s unavailable after %d attempts, degrading gracefully: %s",
        description,
        max_attempts,
        last_exc,
    )
    return None


__all__ = [
    "connect_with_retry",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_INITIAL_DELAY_S",
    "DEFAULT_MAX_DELAY_S",
]
