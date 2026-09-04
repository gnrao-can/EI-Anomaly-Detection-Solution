"""
shared/cache.py
===============
Async Redis cache wrapper for the EI Anomaly Detection Solution.

Provides a single ``RedisCache`` class used by every external-data adapter and
by the pipeline orchestrator. It wraps ``redis-py`` (``redis==5.2.1``) using the
async client (``redis.asyncio``) and transparently selects between:

  - ``redis.asyncio.RedisCluster`` when cluster mode is enabled (production,
    3 primary + 2 replica topology — see scripts/redis/redis-cluster.conf), and
  - ``redis.asyncio.Redis`` for single-node local development.

Design goals:
  - Never crash the calling engine/adapter on a cache read failure. ``get``
    (and its JSON variant) log the error and return ``None`` so the caller can
    fall through to the source of truth.
  - Emit structured logs via ``structlog`` for cache hits, misses, and errors.

Requirements: 17.4, 23.5
"""

from __future__ import annotations

import json
from typing import Any, Sequence

import structlog

try:  # pragma: no cover - import guard exercised only when redis is absent
    import redis.asyncio as redis_async
    from redis.exceptions import RedisError

    _REDIS_AVAILABLE = True
except ImportError:  # pragma: no cover
    redis_async = None  # type: ignore[assignment]
    RedisError = Exception  # type: ignore[assignment,misc]
    _REDIS_AVAILABLE = False


logger = structlog.get_logger(__name__)


class RedisCache:
    """Async wrapper around redis-py supporting cluster and single-node modes.

    The client is created lazily on first use (or explicitly via
    :meth:`connect`) so the object can be constructed cheaply at import/startup
    time before an event loop is running.

    Example:
        cache = RedisCache(url="redis://localhost:6379", cluster_mode=False)
        await cache.set_with_ttl("k", "v", ttl_seconds=60)
        value = await cache.get("k")
        await cache.invalidate("k")
        await cache.close()
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        startup_nodes: Sequence[dict[str, Any]] | None = None,
        password: str | None = None,
        cluster_mode: bool = False,
        decode_responses: bool = True,
    ) -> None:
        """Initialise the cache configuration (no connection is opened yet).

        Args:
            url: A redis connection URL (e.g. ``redis://host:6379``). Used for
                single-node mode, or as a single seed node for cluster mode.
            startup_nodes: Optional list of ``{"host": ..., "port": ...}`` dicts
                used to seed a cluster client. Takes precedence over ``url`` in
                cluster mode when provided.
            password: Optional auth password (maps to Redis ``requirepass``).
            cluster_mode: When ``True``, use ``RedisCluster``; otherwise a
                single-node ``Redis`` client.
            decode_responses: When ``True`` (default), values are returned as
                ``str`` rather than ``bytes``.

        Raises:
            RuntimeError: If the ``redis`` package is not importable.
            ValueError: If neither ``url`` nor ``startup_nodes`` is provided.
        """
        if not _REDIS_AVAILABLE:  # pragma: no cover
            raise RuntimeError(
                "The 'redis' package (redis==5.2.1) is required to use RedisCache "
                "but is not installed."
            )
        if url is None and not startup_nodes:
            raise ValueError("Provide either 'url' or 'startup_nodes'.")

        self._url = url
        self._startup_nodes = list(startup_nodes) if startup_nodes else None
        self._password = password
        self._cluster_mode = cluster_mode
        self._decode_responses = decode_responses
        self._client: Any | None = None
        self._log = logger.bind(component="RedisCache", cluster_mode=cluster_mode)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Create the underlying redis client if it does not already exist."""
        if self._client is not None:
            return

        if self._cluster_mode:
            if self._startup_nodes:
                nodes = [
                    redis_async.cluster.ClusterNode(n["host"], int(n["port"]))
                    for n in self._startup_nodes
                ]
                self._client = redis_async.RedisCluster(
                    startup_nodes=nodes,
                    password=self._password,
                    decode_responses=self._decode_responses,
                )
            else:
                self._client = redis_async.RedisCluster.from_url(
                    self._url,
                    password=self._password,
                    decode_responses=self._decode_responses,
                )
            self._log.info("redis_cluster_client_created")
        else:
            self._client = redis_async.Redis.from_url(
                self._url,
                password=self._password,
                decode_responses=self._decode_responses,
            )
            self._log.info("redis_client_created")

    async def _get_client(self) -> Any:
        """Return the live client, connecting on first use."""
        if self._client is None:
            await self.connect()
        return self._client

    async def close(self) -> None:
        """Close the connection and release resources."""
        if self._client is None:
            return
        try:
            await self._client.aclose()
        except (RedisError, AttributeError) as exc:  # pragma: no cover
            self._log.warning("redis_close_failed", error=str(exc))
        finally:
            self._client = None
            self._log.info("redis_client_closed")

    # ------------------------------------------------------------------
    # Core string operations
    # ------------------------------------------------------------------

    async def get(self, key: str) -> str | None:
        """Return the cached value for ``key`` or ``None``.

        Connection or read errors are logged and swallowed (returns ``None``)
        so a cache outage degrades to a cache miss rather than failing the
        caller.

        Args:
            key: The cache key.

        Returns:
            The cached string value, or ``None`` on miss or error.
        """
        try:
            client = await self._get_client()
            value = await client.get(key)
        except RedisError as exc:
            self._log.warning("cache_get_failed", key=key, error=str(exc))
            return None
        if value is None:
            self._log.debug("cache_miss", key=key)
            return None
        self._log.debug("cache_hit", key=key)
        return value if isinstance(value, str) else value.decode("utf-8")

    async def set_with_ttl(self, key: str, value: str, ttl_seconds: int) -> None:
        """Set ``key`` to ``value`` with an expiry of ``ttl_seconds``.

        Args:
            key: The cache key.
            value: The string value to store.
            ttl_seconds: Expiry in seconds. Must be a positive integer.

        Raises:
            ValueError: If ``ttl_seconds`` is not positive.
        """
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer.")
        try:
            client = await self._get_client()
            await client.set(key, value, ex=ttl_seconds)
            self._log.debug("cache_set", key=key, ttl_seconds=ttl_seconds)
        except RedisError as exc:
            # Writes are best-effort: a failed cache write should not break the
            # caller, but it is worth logging at a higher level than a read miss.
            self._log.warning(
                "cache_set_failed", key=key, ttl_seconds=ttl_seconds, error=str(exc)
            )

    async def invalidate(self, key: str) -> None:
        """Delete ``key`` from the cache.

        Args:
            key: The cache key to remove.
        """
        try:
            client = await self._get_client()
            await client.delete(key)
            self._log.debug("cache_invalidated", key=key)
        except RedisError as exc:
            self._log.warning("cache_invalidate_failed", key=key, error=str(exc))

    # ------------------------------------------------------------------
    # JSON convenience helpers
    # ------------------------------------------------------------------

    async def get_json(self, key: str) -> Any | None:
        """Return the cached JSON-decoded value for ``key`` or ``None``.

        A miss, a connection error, or a JSON decode error all yield ``None``.

        Args:
            key: The cache key.

        Returns:
            The decoded object, or ``None``.
        """
        raw = await self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            self._log.warning("cache_json_decode_failed", key=key, error=str(exc))
            return None

    async def set_json_with_ttl(
        self, key: str, obj: Any, ttl_seconds: int
    ) -> None:
        """JSON-serialise ``obj`` and store it under ``key`` with a TTL.

        Args:
            key: The cache key.
            obj: A JSON-serialisable object.
            ttl_seconds: Expiry in seconds. Must be a positive integer.
        """
        payload = json.dumps(obj, separators=(",", ":"), sort_keys=True)
        await self.set_with_ttl(key, payload, ttl_seconds)

    # ------------------------------------------------------------------
    # Async context-manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "RedisCache":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


__all__ = ["RedisCache"]
