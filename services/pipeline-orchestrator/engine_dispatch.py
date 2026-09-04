"""
services/pipeline-orchestrator/engine_dispatch.py
=================================================
Parallel detection-engine dispatch for the Detection Pipeline Orchestrator
(task 6.2).

Once the :class:`EventCorrelator` (task 6.1) has paired a claim's
``claim.received`` and ``roe.received`` events, the orchestrator must invoke all
six detection engines *concurrently* (never sequentially) and collect their
Indicators within a bounded wall-clock budget. This module implements that
concurrency layer.

Design references
-----------------
Per the design "Time Budget Allocation" table, each engine has its own
per-engine wall-clock budget:

    | Engine                        | Budget |
    |-------------------------------|--------|
    | Repetitive Pattern Engine     | 400 ms |
    | Geographic Anomaly Engine     | 600 ms |
    | Fraud Ring Engine             | 800 ms |
    | History Inconsistency Engine  | 500 ms |
    | Document Forgery Engine       | 700 ms |
    | Regional Analysis Engine      | 500 ms |

Concurrency model (design "Detection Pipeline Orchestrator"):
    "launches all six engines concurrently using asyncio task groups, monitors
     per-engine time budgets, handles PARTIAL_RESULT on timeout, enforces the
     >= 3-engines minimum, publishes INDETERMINATE decision if minimum not met."

Behaviour implemented here
--------------------------
* :class:`EngineDispatcher.dispatch` calls all six engine endpoints in parallel
  using ``asyncio.TaskGroup`` (Python 3.11+), falling back to ``asyncio.gather``
  on older runtimes. Each call is wrapped in ``asyncio.wait_for`` with that
  engine's per-engine timeout budget.
* Engines that respond in time contribute their Indicator list to the result.
* On a per-engine timeout or error, a ``PARTIAL_RESULT`` Indicator is emitted
  carrying the engine id and the elapsed time (ms). PARTIAL_RESULT is
  represented via the Indicator model with ``severity = UNRESOLVED`` and
  ``algorithm = "PARTIAL_RESULT"`` (per the design error-handling contract:
  "Publish PARTIAL_RESULT Indicator with elapsed time").
* If fewer than :data:`MIN_ENGINES_REQUIRED` (3) engines complete, the
  :class:`DispatchResult` is marked ``indeterminate`` with
  ``risk_decision = INDETERMINATE`` and ``payment_halted = True``. Publication of
  the INDETERMINATE ``claim.scored`` event is owned by task 6.3; this module
  optionally publishes via an *injected* producer hook so the two tasks do not
  edit the same code (see ``publish_hook``), otherwise it simply reports the
  flag in the result. A TODO marks the 6.3 hand-off.

Test seam
---------
The network layer is injected: :class:`EngineDispatcher` accepts an
``engine_caller`` coroutine ``(engine, url, claim_payload, roe_payload) -> list``
so unit tests can supply a fake caller and never touch the network. The default
caller uses ``httpx.AsyncClient`` to POST to each engine's ``/engine/analyze``
endpoint.

Requirements: 2.2, 2.3, 2.4, 2.5
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

try:  # OpenTelemetry span helpers (shared source of truth; task 32.1 / Req 21.5)
    from shared.telemetry import engine_span
except ImportError:  # pragma: no cover - shared.telemetry always present in repo
    from contextlib import contextmanager as _contextmanager

    @_contextmanager
    def engine_span(*_args, **_kwargs):  # type: ignore[misc]
        yield None

try:  # canonical domain models live in the shared package
    from shared.models import EngineType, Indicator, RiskDecision, Severity
except ImportError:  # pragma: no cover - defensive fallback when shared is unavailable
    from enum import Enum

    class EngineType(str, Enum):  # type: ignore[no-redef]
        REPETITIVE_PATTERN = "REPETITIVE_PATTERN"
        GEOGRAPHIC_ANOMALY = "GEOGRAPHIC_ANOMALY"
        FRAUD_RING = "FRAUD_RING"
        HISTORY_INCONSISTENCY = "HISTORY_INCONSISTENCY"
        DOCUMENT_FORGERY = "DOCUMENT_FORGERY"
        REGIONAL_ANALYSIS = "REGIONAL_ANALYSIS"

    class Severity(str, Enum):  # type: ignore[no-redef]
        LOW = "LOW"
        MEDIUM = "MEDIUM"
        HIGH = "HIGH"
        CRITICAL = "CRITICAL"
        UNRESOLVED = "UNRESOLVED"

    class RiskDecision(str, Enum):  # type: ignore[no-redef]
        AUTO_APPROVE = "AUTO_APPROVE"
        FLAG_FOR_REVIEW = "FLAG_FOR_REVIEW"
        INVESTIGATE = "INVESTIGATE"
        INDETERMINATE = "INDETERMINATE"

    Indicator = None  # type: ignore[assignment,misc]


logger = logging.getLogger("pipeline-orchestrator.engine_dispatch")


# ---------------------------------------------------------------------------
# Engine configuration: endpoints and per-engine wall-clock budgets
# ---------------------------------------------------------------------------

#: Minimum number of engines that must complete for a determinate decision.
#: Below this the claim is INDETERMINATE and payment is halted (design/Req 2.5).
MIN_ENGINES_REQUIRED: int = 3

#: Algorithm marker used to identify PARTIAL_RESULT Indicators.
PARTIAL_RESULT_ALGORITHM: str = "PARTIAL_RESULT"

#: Per-engine wall-clock timeout budgets in milliseconds (design Time Budget
#: Allocation table). Defining these here (rather than in config.py) keeps this
#: task's edits isolated from the parallel task 6.3.
ENGINE_TIMEOUT_MS: dict[EngineType, int] = {
    EngineType.REPETITIVE_PATTERN: 400,
    EngineType.GEOGRAPHIC_ANOMALY: 600,
    EngineType.FRAUD_RING: 800,
    EngineType.HISTORY_INCONSISTENCY: 500,
    EngineType.DOCUMENT_FORGERY: 700,
    EngineType.REGIONAL_ANALYSIS: 500,
}

#: Default in-cluster service host per engine (Kubernetes service name). Each is
#: overridable via an environment variable of the form
#: ``ORCHESTRATOR_ENGINE_<ENGINE>_URL`` (see :func:`default_engine_endpoints`).
_DEFAULT_ENGINE_HOSTS: dict[EngineType, tuple[str, int]] = {
    EngineType.REPETITIVE_PATTERN: ("repetitive-pattern-engine", 8000),
    EngineType.GEOGRAPHIC_ANOMALY: ("geographic-anomaly-engine", 8000),
    EngineType.FRAUD_RING: ("fraud-ring-engine", 8000),
    EngineType.HISTORY_INCONSISTENCY: ("history-inconsistency-engine", 8000),
    EngineType.DOCUMENT_FORGERY: ("document-forgery-engine", 8000),
    EngineType.REGIONAL_ANALYSIS: ("regional-analysis-engine", 8000),
}

#: The analyze path exposed by every detection engine's FastAPI service.
ENGINE_ANALYZE_PATH: str = "/engine/analyze"

#: The canonical ordered list of the six engines.
ALL_ENGINES: tuple[EngineType, ...] = (
    EngineType.REPETITIVE_PATTERN,
    EngineType.GEOGRAPHIC_ANOMALY,
    EngineType.FRAUD_RING,
    EngineType.HISTORY_INCONSISTENCY,
    EngineType.DOCUMENT_FORGERY,
    EngineType.REGIONAL_ANALYSIS,
)


def _env_var_name(engine: EngineType) -> str:
    """Return the environment-variable name overriding an engine's base URL."""
    return f"ORCHESTRATOR_ENGINE_{engine.value}_URL"


def default_engine_endpoints() -> dict[EngineType, str]:
    """Build the default engine ``/engine/analyze`` URLs.

    Each engine's base URL defaults to ``http://<service>:<port>`` and can be
    overridden via ``ORCHESTRATOR_ENGINE_<ENGINE>_URL`` (base URL without the
    analyze path). The returned mapping holds full analyze URLs.
    """
    endpoints: dict[EngineType, str] = {}
    for engine, (host, port) in _DEFAULT_ENGINE_HOSTS.items():
        base = os.environ.get(_env_var_name(engine), f"http://{host}:{port}")
        endpoints[engine] = base.rstrip("/") + ENGINE_ANALYZE_PATH
    return endpoints


# ---------------------------------------------------------------------------
# Result value objects
# ---------------------------------------------------------------------------


@dataclass
class DispatchResult:
    """Outcome of dispatching a claim to all six detection engines.

    Attributes
    ----------
    claim_id:
        The claim these engines analysed.
    indicators:
        All Indicators collected from completed engines, plus one
        ``PARTIAL_RESULT`` Indicator for each engine that timed out or errored.
    engines_completed:
        Engines that returned within their per-engine budget.
    engines_partial:
        Engines that timed out or errored (a PARTIAL_RESULT Indicator was
        emitted for each).
    indeterminate:
        ``True`` when fewer than :data:`MIN_ENGINES_REQUIRED` engines completed.
    risk_decision:
        ``RiskDecision.INDETERMINATE`` when ``indeterminate`` is ``True``;
        otherwise ``None`` (final scoring is task 6.3's responsibility).
    payment_halted:
        ``True`` when the decision is INDETERMINATE (payment must be halted and
        the claim queued for manual review, per Req 2.5).
    """

    claim_id: str
    indicators: list = field(default_factory=list)
    engines_completed: list = field(default_factory=list)
    engines_partial: list = field(default_factory=list)
    indeterminate: bool = False
    risk_decision: Optional[RiskDecision] = None
    payment_halted: bool = False

    @property
    def completed_count(self) -> int:
        """Number of engines that completed within budget."""
        return len(self.engines_completed)


#: An engine caller coroutine: given the engine, its analyze URL, and the two
#: payloads, return the engine's Indicator list. Raising is treated as failure.
EngineCaller = Callable[
    [EngineType, str, dict[str, Any], dict[str, Any]], Awaitable[list]
]

#: An optional publish hook used only for the INDETERMINATE path when task 6.3's
#: publication is not wired in. It receives the finished :class:`DispatchResult`.
PublishHook = Callable[[DispatchResult], Awaitable[None]]


# ---------------------------------------------------------------------------
# Default network engine caller (httpx)
# ---------------------------------------------------------------------------


def _build_engine_request(
    claim_payload: dict[str, Any], roe_payload: dict[str, Any]
) -> dict[str, Any]:
    """Build a detection engine's flat ``/engine/analyze`` request body.

    Engines expect the flat ``AnalyzeRequest`` shape (``sin_hash`` required, plus
    optional ``claim_id`` / ``employer_bn`` / ``insurable_hours`` /
    ``economic_region_code`` / ``separation_date``), NOT a ``{claim, roe}``
    envelope. We assemble those fields from the paired ``claim.received`` /
    ``roe.received`` payloads, preferring the claim payload and falling back to
    the ROE payload. Fields absent from both are simply omitted — they are
    optional on the engine, which fetches history by ``sin_hash``.
    """
    merged: dict[str, Any] = {}
    for src in (roe_payload, claim_payload):  # claim overrides roe on conflict
        if isinstance(src, dict):
            merged.update(src)

    body: dict[str, Any] = {}
    if merged.get("sin_hash") is not None:
        body["sin_hash"] = merged["sin_hash"]
    for field_name in (
        "claim_id",
        "employer_bn",
        "insurable_hours",
        "economic_region_code",
        "separation_date",
    ):
        value = merged.get(field_name)
        if value is not None:
            body[field_name] = value
    return body


def _service_auth_headers() -> dict[str, str]:
    """Bearer header for internal engine/scorer calls (Req 19.7).

    Every EI-ADS API endpoint validates a GCKey Bearer token, so the
    orchestrator presents its service credential (``ORCHESTRATOR_SERVICE_TOKEN``)
    on internal calls. Absent in local/test environments, in which case no
    header is sent and the callee's own dev/test configuration applies.
    """
    token = os.environ.get("ORCHESTRATOR_SERVICE_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _default_engine_caller(
    engine: EngineType,
    url: str,
    claim_payload: dict[str, Any],
    roe_payload: dict[str, Any],
) -> list:
    """POST a claim to an engine's ``/engine/analyze`` endpoint via httpx.

    Sends the engine's flat ``AnalyzeRequest`` body (built by
    :func:`_build_engine_request`) with the orchestrator's service Bearer token
    (Req 19.7). The engine returns ``{"indicators": [ ... ]}`` (or a bare list);
    each element is decoded to an :class:`Indicator` when the shared model is
    present, otherwise returned unchanged.
    """
    import httpx  # imported lazily so unit tests never require the network layer

    body = _build_engine_request(claim_payload, roe_payload)
    headers = _service_auth_headers()
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=body, headers=headers)
        response.raise_for_status()
        data = response.json()

    raw_indicators = data.get("indicators", data) if isinstance(data, dict) else data
    if not isinstance(raw_indicators, list):
        return []

    if Indicator is None:
        return raw_indicators

    decoded: list = []
    for item in raw_indicators:
        if isinstance(item, Indicator):
            decoded.append(item)
        elif isinstance(item, dict):
            try:
                decoded.append(Indicator.from_dict(item))
            except Exception:  # noqa: BLE001 - tolerate loosely-shaped engine output
                decoded.append(item)
        else:
            decoded.append(item)
    return decoded


# ---------------------------------------------------------------------------
# PARTIAL_RESULT indicator construction
# ---------------------------------------------------------------------------


def make_partial_result_indicator(
    claim_id: str,
    engine: EngineType,
    elapsed_ms: float,
    reason: str,
):
    """Build a PARTIAL_RESULT Indicator for an engine that failed to complete.

    Represents the design's "PARTIAL_RESULT Indicator with elapsed time" using
    the canonical Indicator model: ``severity = UNRESOLVED`` and
    ``algorithm = PARTIAL_RESULT``. The engine id and elapsed time (ms) are
    carried in the ``payload`` so downstream scoring/audit can attribute the gap.

    When the shared :class:`Indicator` model is unavailable (defensive fallback),
    a plain dict with the same fields is returned instead.
    """
    payload = {
        "engine": engine.value,
        "elapsed_ms": round(elapsed_ms, 3),
        "reason": reason,
        "partial_result": True,
    }
    title = f"{engine.value} did not complete within its time budget"
    explanation_en = (
        f"The {engine.value} engine did not return within its "
        f"{ENGINE_TIMEOUT_MS.get(engine, 0)} ms budget "
        f"({elapsed_ms:.0f} ms elapsed); its result is unavailable for scoring."
    )
    explanation_fr = (
        f"Le moteur {engine.value} n'a pas répondu dans son délai de "
        f"{ENGINE_TIMEOUT_MS.get(engine, 0)} ms "
        f"({elapsed_ms:.0f} ms écoulées); son résultat est indisponible."
    )

    if Indicator is None:  # pragma: no cover - only when shared models absent
        return {
            "indicator_id": str(uuid.uuid4()),
            "claim_id": claim_id,
            "engine": engine.value,
            "severity": Severity.UNRESOLVED.value,
            "algorithm": PARTIAL_RESULT_ALGORITHM,
            "title": title,
            "explanation_en": explanation_en,
            "explanation_fr": explanation_fr,
            "payload": payload,
            "produced_at": datetime.now(timezone.utc).isoformat(),
        }

    try:
        claim_uuid = uuid.UUID(str(claim_id))
    except (ValueError, AttributeError, TypeError):
        claim_uuid = uuid.uuid5(uuid.NAMESPACE_URL, str(claim_id))

    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=claim_uuid,
        engine=engine,
        severity=Severity.UNRESOLVED,
        algorithm=PARTIAL_RESULT_ALGORITHM,
        title=title,
        explanation_en=explanation_en,
        explanation_fr=explanation_fr,
        payload=payload,
        produced_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# EngineDispatcher
# ---------------------------------------------------------------------------


@dataclass
class _EngineOutcome:
    """Internal per-engine outcome captured by a dispatch worker."""

    engine: EngineType
    indicators: Optional[list]
    elapsed_ms: float
    error: Optional[str]

    @property
    def completed(self) -> bool:
        return self.error is None and self.indicators is not None


class EngineDispatcher:
    """Dispatch a paired claim to all six detection engines concurrently.

    Parameters
    ----------
    engine_caller:
        Coroutine used to invoke a single engine. Defaults to an httpx-backed
        caller hitting each engine's ``/engine/analyze`` endpoint. Unit tests
        inject a fake caller so they never touch the network.
    endpoints:
        Mapping of engine -> analyze URL. Defaults to
        :func:`default_engine_endpoints` (configurable via env vars).
    timeouts_ms:
        Mapping of engine -> per-engine wall-clock budget in ms. Defaults to
        :data:`ENGINE_TIMEOUT_MS`.
    publish_hook:
        Optional coroutine invoked with the finished :class:`DispatchResult` when
        the decision is INDETERMINATE. This is the isolated seam for the
        INDETERMINATE ``claim.scored`` publication that task 6.3 owns; when
        ``None`` (the default) this module only reports the flag in the result.
    """

    def __init__(
        self,
        engine_caller: Optional[EngineCaller] = None,
        *,
        endpoints: Optional[dict[EngineType, str]] = None,
        timeouts_ms: Optional[dict[EngineType, int]] = None,
        publish_hook: Optional[PublishHook] = None,
    ) -> None:
        self._engine_caller: EngineCaller = engine_caller or _default_engine_caller
        self._endpoints = endpoints or default_engine_endpoints()
        self._timeouts_ms = timeouts_ms or dict(ENGINE_TIMEOUT_MS)
        self._publish_hook = publish_hook

    # ------------------------------------------------------------------
    # Per-engine worker
    # ------------------------------------------------------------------

    async def _call_one(
        self,
        engine: EngineType,
        claim_payload: dict[str, Any],
        roe_payload: dict[str, Any],
    ) -> _EngineOutcome:
        """Invoke a single engine under its per-engine timeout budget.

        Wraps the injected caller in ``asyncio.wait_for`` with the engine's
        budget. A timeout or any exception is captured as a failed outcome so a
        PARTIAL_RESULT Indicator can be emitted; it never propagates out and
        never aborts the sibling engine calls.
        """
        url = self._endpoints.get(engine, "")
        timeout_ms = self._timeouts_ms.get(engine, 0)
        timeout_s = timeout_ms / 1000.0 if timeout_ms > 0 else None
        # Child span per engine call (design trace structure). ``claim_id`` /
        # ``sin_hash`` only — never raw PII. No-op when OTel is unavailable.
        claim_id = str(
            claim_payload.get("claim_id") or roe_payload.get("claim_id") or "unknown"
        )
        start = time.monotonic()
        try:
            with engine_span(engine.value, claim_id, budget_ms=timeout_ms):
                indicators = await asyncio.wait_for(
                    self._engine_caller(engine, url, claim_payload, roe_payload),
                    timeout=timeout_s,
                )
            elapsed_ms = (time.monotonic() - start) * 1000.0
            return _EngineOutcome(engine, list(indicators), elapsed_ms, None)
        except asyncio.TimeoutError:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            logger.warning(
                "engine %s timed out after %.1f ms (budget %d ms)",
                engine.value,
                elapsed_ms,
                timeout_ms,
            )
            return _EngineOutcome(engine, None, elapsed_ms, "timeout")
        except Exception as exc:  # noqa: BLE001 - any engine error -> PARTIAL_RESULT
            elapsed_ms = (time.monotonic() - start) * 1000.0
            logger.warning("engine %s failed after %.1f ms: %s", engine.value, elapsed_ms, exc)
            return _EngineOutcome(engine, None, elapsed_ms, f"error: {exc}")

    # ------------------------------------------------------------------
    # Public dispatch
    # ------------------------------------------------------------------

    async def dispatch(
        self,
        claim_payload: dict[str, Any],
        roe_payload: dict[str, Any],
    ) -> DispatchResult:
        """Call all six engines concurrently and assemble the DispatchResult.

        Uses ``asyncio.TaskGroup`` (Python 3.11+) to run the six per-engine
        workers concurrently; falls back to ``asyncio.gather`` on older
        runtimes. Because every worker captures its own failures internally, the
        TaskGroup never sees an exception and never cancels siblings.

        The result collects Indicators from completed engines and one
        PARTIAL_RESULT Indicator per failed engine, tracks completed vs partial
        engines, and — when fewer than :data:`MIN_ENGINES_REQUIRED` engines
        complete — marks the claim INDETERMINATE with payment halted.
        """
        claim_id = str(
            claim_payload.get("claim_id")
            or roe_payload.get("claim_id")
            or "unknown"
        )

        outcomes = await self._run_workers(claim_payload, roe_payload)

        result = DispatchResult(claim_id=claim_id)
        for outcome in outcomes:
            if outcome.completed:
                result.engines_completed.append(outcome.engine)
                result.indicators.extend(outcome.indicators or [])
            else:
                result.engines_partial.append(outcome.engine)
                result.indicators.append(
                    make_partial_result_indicator(
                        claim_id,
                        outcome.engine,
                        outcome.elapsed_ms,
                        outcome.error or "unknown",
                    )
                )

        if result.completed_count < MIN_ENGINES_REQUIRED:
            result.indeterminate = True
            result.risk_decision = RiskDecision.INDETERMINATE
            result.payment_halted = True
            logger.warning(
                "claim %s INDETERMINATE: only %d/%d engines completed; payment halted",
                claim_id,
                result.completed_count,
                len(ALL_ENGINES),
            )
            # TODO(task 6.3): publication of the INDETERMINATE `claim.scored`
            # event and the ClickHouse audit trail are owned by task 6.3. When a
            # producer is injected via ``publish_hook`` we invoke it here so the
            # halted claim is not dropped; otherwise we only report the flag.
            if self._publish_hook is not None:
                try:
                    await self._publish_hook(result)
                except Exception as exc:  # noqa: BLE001 - never fail dispatch on publish
                    logger.error(
                        "INDETERMINATE publish hook failed for claim %s: %s",
                        claim_id,
                        exc,
                    )

        return result

    async def _run_workers(
        self,
        claim_payload: dict[str, Any],
        roe_payload: dict[str, Any],
    ) -> list:
        """Run the six per-engine workers concurrently.

        Prefers ``asyncio.TaskGroup`` (Python 3.11+); falls back to
        ``asyncio.gather`` on older runtimes. Returns outcomes in a stable order
        matching :data:`ALL_ENGINES`.
        """
        task_group_cls = getattr(asyncio, "TaskGroup", None)

        if task_group_cls is not None:
            tasks: dict[EngineType, Any] = {}
            async with task_group_cls() as tg:
                for engine in ALL_ENGINES:
                    tasks[engine] = tg.create_task(
                        self._call_one(engine, claim_payload, roe_payload)
                    )
            return [tasks[engine].result() for engine in ALL_ENGINES]

        # Fallback for Python < 3.11.
        coros = [
            self._call_one(engine, claim_payload, roe_payload)
            for engine in ALL_ENGINES
        ]
        return list(await asyncio.gather(*coros))


__all__ = [
    "MIN_ENGINES_REQUIRED",
    "PARTIAL_RESULT_ALGORITHM",
    "ENGINE_TIMEOUT_MS",
    "ENGINE_ANALYZE_PATH",
    "ALL_ENGINES",
    "default_engine_endpoints",
    "DispatchResult",
    "EngineCaller",
    "PublishHook",
    "make_partial_result_indicator",
    "EngineDispatcher",
]
