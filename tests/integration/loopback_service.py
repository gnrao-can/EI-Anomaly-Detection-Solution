"""
tests/integration/loopback_service.py
======================================
Run a FastAPI ``app`` on a real loopback HTTP socket for integration tests
(Slice 1, Option 1 topology).

The Slice 1 integration test wires the pipeline over REAL transports without
containerising every service: the Repetitive Pattern Engine and the Ensemble
Scorer run as genuine HTTP servers bound to ``127.0.0.1:<ephemeral>`` inside the
test process (a ``uvicorn.Server`` on a background thread), so the orchestrator's
``httpx`` calls cross a real socket and the real ``shared.auth`` Bearer/JWKS
validation runs on the receiving side. Only Kafka is an external container.

``LoopbackService`` wraps that lifecycle:

    svc = LoopbackService(app).start()
    try:
        base_url = svc.base_url          # e.g. "http://127.0.0.1:54123"
        ...
    finally:
        svc.stop()

It waits for the port to accept connections (and, when a ``health_path`` is
given, for that endpoint to answer) before ``start()`` returns, so callers never
race the server's startup. This is test-only infrastructure; nothing here is
imported by production code.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Optional

import httpx
import uvicorn


def _free_port() -> int:
    """Bind an ephemeral port, close it, and return the number for reuse."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LoopbackService:
    """A FastAPI ``app`` served over real loopback HTTP on a background thread.

    Parameters
    ----------
    app:
        The ASGI application (a service's module-level ``app``).
    host / port:
        Bind address. ``port=0`` (default) picks a free ephemeral port.
    health_path:
        Optional path (e.g. ``"/healthz"``) polled during ``start()`` to confirm
        readiness beyond a bare TCP accept. When ``None``, only TCP readiness is
        awaited.
    startup_timeout_s:
        Maximum time to wait for readiness before raising ``TimeoutError``.
    """

    def __init__(
        self,
        app,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        health_path: Optional[str] = None,
        startup_timeout_s: float = 20.0,
    ) -> None:
        self._app = app
        self._host = host
        self._port = port or _free_port()
        self._health_path = health_path
        self._startup_timeout_s = startup_timeout_s
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> "LoopbackService":
        """Start the server thread and block until it is ready."""
        config = uvicorn.Config(
            self._app,
            host=self._host,
            port=self._port,
            log_level="warning",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        # uvicorn installs signal handlers only on the main thread; disable so it
        # runs cleanly on a worker thread.
        self._server.install_signal_handlers = lambda: None  # type: ignore[assignment]
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        self._await_ready()
        return self

    def _await_ready(self) -> None:
        deadline = time.monotonic() + self._startup_timeout_s
        # 1) Wait for uvicorn to report started AND the port to accept TCP.
        while time.monotonic() < deadline:
            if self._server is not None and getattr(self._server, "started", False):
                if self._tcp_ok():
                    break
            time.sleep(0.05)
        else:
            raise TimeoutError(
                f"LoopbackService did not start within {self._startup_timeout_s}s"
            )

        # 2) Optionally wait for a health endpoint to ANSWER. Any HTTP status
        #    (including 503) proves the app is accepting and routing requests —
        #    which is what "ready" means here. Some services intentionally report
        #    503/"degraded" when an optional dependency (e.g. a DB) is absent,
        #    which is the expected thin-path state for Slice 1; that must NOT be
        #    treated as "not ready".
        if self._health_path is not None:
            url = f"{self.base_url}{self._health_path}"
            while time.monotonic() < deadline:
                try:
                    httpx.get(url, timeout=1.0)
                    return  # a response of any status => the app is serving
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            raise TimeoutError(
                f"LoopbackService health {url} not ready within "
                f"{self._startup_timeout_s}s"
            )

    def _tcp_ok(self) -> bool:
        try:
            with socket.create_connection((self._host, self._port), timeout=1.0):
                return True
        except OSError:
            return False

    def stop(self) -> None:
        """Signal the server to exit and join its thread."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10.0)
        self._server = None
        self._thread = None

    def __enter__(self) -> "LoopbackService":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


__all__ = ["LoopbackService"]
