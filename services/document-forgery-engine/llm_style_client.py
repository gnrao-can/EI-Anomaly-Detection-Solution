"""
services/document-forgery-engine/llm_style_client.py
=====================================================
Async HTTP client for the LLM Service style-analysis endpoint, used by the
Document Forgery Engine (Req 7.8 / 7.9).

The engine's :class:`style_analysis.StyleAnalysisChecker` takes an async callable
``(narrative, corpus) -> {"inconsistent": bool, "score": float}``.
:class:`LLMStyleServiceClient` is that callable, backed by an HTTP POST to the
LLM Service's ``/llm/style-analysis`` endpoint.

Auth (Req 19.7): the client presents the engine's ``SERVICE_TOKEN`` as
``Authorization: Bearer <token>`` (mirroring the CRA payroll client). Absent a
token (unit tests) no header is sent and the caller injects a fake instead.

Failure posture: the client raises on any transport / non-2xx / malformed
response. The ``StyleAnalysisChecker`` wraps the call in a bounded
``asyncio.wait_for`` and turns a timeout OR any raise into "no style Indicator"
(the style signal is best-effort, off the 700 ms critical path) — so a raising
client is the correct, spec-faithful behaviour.

Requirements: 7.8, 7.9, 19.7
"""

from __future__ import annotations

from typing import Any, Optional


class LLMStyleServiceClient:
    """Async callable POSTing to the LLM Service ``/llm/style-analysis``.

    Parameters
    ----------
    base_url:
        LLM Service base URL (e.g. ``http://llm-service:8005``).
    bearer_token:
        Optional GCKey service Bearer (Req 19.7). ``None`` → no header.
    timeout_seconds:
        Per-request HTTP timeout (the checker also enforces its own budget).
    """

    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: Optional[str] = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._headers = (
            {"Authorization": f"Bearer {bearer_token}"} if bearer_token else None
        )

    async def __call__(
        self, narrative: str, corpus: list[str]
    ) -> dict[str, Any]:
        """Classify style inconsistency via the LLM Service; return the dict.

        Raises on any transport / non-2xx / malformed body — the checker turns
        that into "no style Indicator" (best-effort, Req 7.8/7.9).
        """
        import httpx  # imported lazily so unit tests never require the network

        url = f"{self._base_url}/llm/style-analysis"
        payload = {"narrative": narrative, "corpus": list(corpus or [])}
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as http:
            resp = await http.post(url, json=payload, headers=self._headers)
            resp.raise_for_status()
            body = resp.json()
        if not isinstance(body, dict):
            raise ValueError("LLM Service returned a non-object response")
        return {
            "inconsistent": bool(body.get("inconsistent", False)),
            "score": body.get("score"),
        }


__all__ = ["LLMStyleServiceClient"]
