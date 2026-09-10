"""
services/history-inconsistency-engine/llm_client.py
====================================================
Async HTTP client for the LLM Service narrative-contradiction endpoint, used by
the Employment History Inconsistency Engine (Req 6.6 / 6.7).

The engine's :class:`narrative.NarrativeContradictionChecker` takes an async
callable ``(narrative, cra_evidence) -> {"contradicted": bool, "evidence": str}``.
:class:`LLMServiceClient` is that callable, backed by an HTTP POST to the LLM
Service's ``/llm/narrative-contradiction`` endpoint.

Auth (Req 19.7): every EI-ADS endpoint validates a GCKey Bearer, so the client
presents the engine's ``SERVICE_TOKEN`` as ``Authorization: Bearer <token>``
(mirroring the CRA Adapter client). Absent a token (unit tests) no header is
sent and the caller injects a fake instead.

Failure posture: the client raises on any transport / non-2xx / malformed
response. The ``NarrativeContradictionChecker`` wraps the call in a 10-second
``asyncio.wait_for`` and turns a timeout OR any raise into the
``NARRATIVE_ANALYSIS_TIMEOUT`` Indicator (Req 6.7) — so a raising client is the
correct, spec-faithful behaviour (never a false contradiction).

Requirements: 6.6, 6.7, 19.7
"""

from __future__ import annotations

from typing import Any, Optional


class LLMServiceClient:
    """Async callable POSTing to the LLM Service ``/llm/narrative-contradiction``.

    Parameters
    ----------
    base_url:
        LLM Service base URL (e.g. ``http://llm-service:8005``).
    bearer_token:
        Optional GCKey service Bearer (Req 19.7). ``None`` (unit tests) → no
        ``Authorization`` header.
    timeout_seconds:
        Per-request HTTP timeout. The checker additionally enforces the 10 s
        contradiction budget (Req 6.7), so this is a transport-level bound.
    """

    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: Optional[str] = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._headers = (
            {"Authorization": f"Bearer {bearer_token}"} if bearer_token else None
        )

    async def __call__(self, narrative: str, cra_evidence: str) -> dict[str, Any]:
        """Classify contradiction via the LLM Service; return the response dict.

        Raises on any transport / non-2xx / malformed body — the checker turns
        that into the NARRATIVE_ANALYSIS_TIMEOUT Indicator (Req 6.7).
        """
        import httpx  # imported lazily so unit tests never require the network

        url = f"{self._base_url}/llm/narrative-contradiction"
        payload = {"narrative": narrative, "cra_evidence": cra_evidence}
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as http:
            resp = await http.post(url, json=payload, headers=self._headers)
            resp.raise_for_status()
            body = resp.json()
        if not isinstance(body, dict):
            raise ValueError("LLM Service returned a non-object response")
        return {
            "contradicted": bool(body.get("contradicted", False)),
            "evidence": str(body.get("evidence", "")),
        }


__all__ = ["LLMServiceClient"]
