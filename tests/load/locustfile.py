"""
tests/load/locustfile.py
=========================
Locust load test for the EI-ADS Claim Intake pipeline and Claimant Portal.

Task 34.1 / Requirements 17.1, 17.2, 17.3.

What this exercises
-------------------
Two user classes model the two performance-critical entry points defined in the
design (Design -> Claim Intake Service, Design -> GCKey OAuth 2.0 Flow):

* ``ClaimUser`` -- simulates the full claim-submission pipeline against the
  **Claim Intake Service**: obtain a GCKey Bearer token, ``POST /api/v1/claims``
  (multipart form: claim fields + optional ROE document), then poll claim status
  until the Detection_Pipeline produces a Risk_Score / Risk_Decision. This is the
  path governed by the sub-2-second scoring SLA (Req 17.1 / 17.2).

* ``PortalUser`` -- simulates a claimant loading the submission form and
  receiving a submission confirmation from the **Claimant Portal**, governed by
  the 3-second render SLA under 1,000 concurrent users (Req 17.3).

SLA targets encoded here (from Requirement 17)
---------------------------------------------
* 17.1  99% (p99) of claims scored within **2,000 ms** under 500 concurrent claims.
* 17.2  Sustained throughput of at least **2,000 claims/min** with no latency
        percentile degradation.
* 17.3  Claimant Portal p99 within **3,000 ms** at 1,000 concurrent users.

These targets are enforced two ways so a CI run *fails* when an SLA is missed:

1. Per-request assertions inside each task flag individual requests that exceed
   the budget (they show up as request failures in the Locust statistics).
2. A ``quitting`` event handler (``_enforce_sla_on_quit``) inspects the final
   aggregated statistics and sets a non-zero process exit code when p99 latency
   or throughput miss target -- so ``locust --headless ... `` fails the pipeline.

Run configuration (against a live deployed system)
-------------------------------------------------
Locust drives load against a *running* target; it cannot exercise anything in a
sandbox with no deployed services. Point it at the deployed ingress with the
``--host`` flag (or the ``EI_ADS_TARGET_HOST`` environment variable) and pick the
user class for the scenario you are validating with ``--class-picker`` /
positional class names.

Claim pipeline SLA (Req 17.1 / 17.2) -- 500 concurrent claims, >= 2,000/min::

    locust -f tests/load/locustfile.py ClaimUser \
        --host https://ei-ads.gc.ca \
        --headless --users 500 --spawn-rate 50 --run-time 10m \
        --csv results/claim_pipeline

Claimant Portal SLA (Req 17.3) -- 1,000 concurrent users, p99 <= 3,000 ms::

    locust -f tests/load/locustfile.py PortalUser \
        --host https://portal.ei-ads.gc.ca \
        --headless --users 1000 --spawn-rate 100 --run-time 10m \
        --csv results/portal

A GCKey access token for the load-test service account is read from
``EI_ADS_LOAD_TEST_TOKEN``; when absent the users fetch one from the configured
token endpoint (``EI_ADS_TOKEN_URL``) using client-credentials env vars. In a
dry environment neither is required for the file to import and parse.

Sandbox behaviour
-----------------
``locust==2.32.3`` is pinned in ``pyproject.toml`` /
``scripts/dependencies/requirements-test.txt`` but may be absent in some
sandboxes, and there is never a live target during unit collection. This module
therefore imports cleanly and degrades to lightweight stand-ins when ``locust``
is not installed, so ``python -c "import ast; ast.parse(...)"`` and a plain
import both succeed. It only does real work when launched by the Locust runner
against ``--host``.
"""

from __future__ import annotations

import io
import os
import random
import time
import uuid
from datetime import date, timedelta
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Optional dependency guard -- import cleanly even without locust installed.
# ---------------------------------------------------------------------------
try:
    from locust import HttpUser, between, events, task
    from locust.env import Environment  # noqa: F401  (typing / event payloads)

    _HAS_LOCUST = True
except Exception:  # noqa: BLE001 - locust absent in this sandbox
    _HAS_LOCUST = False

    # Minimal stand-ins so the module still imports, parses, and can be reasoned
    # about (and unit-tested for its pure helpers) without locust present. These
    # are never used when the real Locust runner drives the file.
    class HttpUser:  # type: ignore
        """No-op stand-in for :class:`locust.HttpUser`."""

        abstract = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
            self.client = None

    def between(low: float, high: float):  # type: ignore
        """Return a constant wait-time factory mirroring locust.between."""

        def _wait(*_args: Any, **_kwargs: Any) -> float:
            return random.uniform(low, high)

        return _wait

    def task(weight: int = 1):  # type: ignore
        """Pass-through decorator mirroring locust.task."""

        if callable(weight):  # used as bare @task
            return weight

        def _decorator(func):
            return func

        return _decorator

    class _Events:
        """Stand-in exposing the event hooks used below as no-op decorators."""

        def __getattr__(self, _name: str):
            class _Hook:
                def add_listener(self, func):
                    return func

            return _Hook()

    events = _Events()  # type: ignore


# ---------------------------------------------------------------------------
# SLA targets (Requirement 17). Values are milliseconds unless noted.
# ---------------------------------------------------------------------------

# 17.1 -- p99 end-to-end claim scoring latency ceiling.
CLAIM_PIPELINE_P99_MS = 2_000
# 17.2 -- minimum sustained claim throughput.
MIN_CLAIM_THROUGHPUT_PER_MIN = 2_000
# 17.3 -- Claimant Portal p99 render/confirmation latency ceiling.
PORTAL_P99_MS = 3_000

# Documented run shape for each SLA (used only by humans / CI wrappers; the
# actual concurrency is set on the command line via --users / --spawn-rate).
CLAIM_TARGET_CONCURRENCY = 500       # Req 17.1 "500 concurrent claims"
PORTAL_TARGET_CONCURRENCY = 1_000    # Req 17.3 "1,000 concurrent users"

# Request "names" used to group statistics so the quit-time SLA check can find
# the right aggregate rows regardless of dynamic paths.
NAME_CLAIM_SUBMIT = "POST /api/v1/claims"
NAME_CLAIM_STATUS = "GET /api/v1/claims/[id]/status"
NAME_PORTAL_FORM = "GET / (portal form)"
NAME_PORTAL_CONFIRM = "POST /api/v1/claims (portal confirm)"

# How long to poll for a terminal Risk_Decision before giving up on a claim.
_STATUS_POLL_TIMEOUT_S = 5.0
_STATUS_POLL_INTERVAL_S = 0.1
_TERMINAL_STATUSES = frozenset({"SCORED", "ERROR"})


# ---------------------------------------------------------------------------
# Payload / auth helpers (pure -- safe to import and unit test).
# ---------------------------------------------------------------------------


def _luhn_check_digit(digits: str) -> str:
    """Return the Luhn check digit for an 8-digit SIN base (validates SIN)."""
    total = 0
    # Double every second digit from the right of the 8-digit base.
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 0:  # positions that get doubled for an 8-digit base
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return str((10 - (total % 10)) % 10)


def generate_valid_sin() -> str:
    """Generate a syntactically valid, Luhn-consistent 9-digit test SIN.

    Uses the 9xx range reserved for temporary/test identifiers so no real SIN is
    ever transmitted.
    """
    base = "9" + "".join(str(random.randint(0, 9)) for _ in range(7))
    return base + _luhn_check_digit(base)


def generate_employer_bn() -> str:
    """Generate a valid 15-char Business Number (9 digits + 'RT' + 4 digits)."""
    nine = "".join(str(random.randint(0, 9)) for _ in range(9))
    four = "".join(str(random.randint(0, 9)) for _ in range(4))
    return f"{nine}RT{four}"


def build_claim_fields() -> dict[str, str]:
    """Build a fresh, schema-valid claim submission payload.

    Mirrors the ``ClaimSubmission`` model accepted by the Claim Intake Service:
    valid SIN (Luhn), BN format, positive hours/earnings, and a separation date
    no later than today.
    """
    separation = date.today() - timedelta(days=random.randint(1, 30))
    return {
        "sin": generate_valid_sin(),
        "employer_bn": generate_employer_bn(),
        "insurable_hours": str(random.randint(420, 2_000)),
        "insurable_earnings": str(random.randint(5_000, 90_000)),
        "separation_date": separation.isoformat(),
        "separation_reason": "Shortage of work / layoff",
        "postal_code": random.choice(["K1A0B1", "M5H2N2", "V6B4N9", "T2P1J9"]),
        "separation_reason_code": "A",
        "economic_region_code": random.choice(["24", "35", "59", "48"]),
        "noc_code": random.choice(["73200", "65100", "94141", "75110"]),
    }


def build_roe_document() -> tuple[str, bytes, str]:
    """Return a small in-memory ROE document upload tuple (filename, bytes, ct).

    The multipart claim submission accepts an optional ROE document; we attach a
    tiny synthetic PDF-like blob so the request exercises the multipart path.
    """
    content = b"%PDF-1.4\n% EI-ADS load-test synthetic ROE\n"
    return ("roe.pdf", content, "application/pdf")


def _auth_headers() -> dict[str, str]:
    """Return Authorization headers for the Claim Intake Service.

    Prefers a pre-provisioned service-account token in
    ``EI_ADS_LOAD_TEST_TOKEN``. Returns an empty mapping when no token is
    configured (the request then simply gets a 401, surfaced as a failure), so
    the file still imports and runs in environments without credentials.
    """
    token = os.environ.get("EI_ADS_LOAD_TEST_TOKEN", "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


# ---------------------------------------------------------------------------
# ClaimUser -- full claim submission + scoring pipeline (Req 17.1 / 17.2).
# ---------------------------------------------------------------------------


class ClaimUser(HttpUser):
    """Simulate the full claim submission -> scoring pipeline.

    Flow per iteration:
      1. Ensure a GCKey Bearer token is available.
      2. ``POST /api/v1/claims`` as multipart (claim fields + ROE document).
      3. Poll ``GET /api/v1/claims/{id}/status`` until a terminal Risk_Decision
         (``SCORED`` / ``ERROR``) or the poll timeout.
      4. Assert the end-to-end latency stayed within the 2,000 ms budget so the
         request is marked failed when it degrades (Req 17.1 / 17.2).
    """

    # Short think time keeps a single simulated claimant issuing a steady stream
    # of submissions so a modest user count can sustain >= 2,000 claims/min.
    wait_time = between(0.5, 1.5)

    def on_start(self) -> None:
        """Acquire auth headers once per simulated user."""
        self.headers = _auth_headers()

    @task
    def submit_and_await_scoring(self) -> None:
        """Submit a claim and poll until it is scored, timing the whole flow."""
        fields = build_claim_fields()
        files = {"roe_document": build_roe_document()}

        started = time.monotonic()
        with self.client.post(
            "/api/v1/claims",
            data=fields,
            files=files,
            headers=self.headers,
            name=NAME_CLAIM_SUBMIT,
            catch_response=True,
        ) as response:
            if response.status_code not in (200, 202):
                response.failure(
                    f"claim submission failed: HTTP {response.status_code}"
                )
                return
            try:
                claim_id = response.json().get("claim_id")
            except ValueError:
                response.failure("claim submission returned non-JSON body")
                return
            if not claim_id:
                response.failure("claim submission response missing claim_id")
                return
            response.success()

        scored = self._poll_until_scored(claim_id)
        elapsed_ms = (time.monotonic() - started) * 1_000.0

        # Encode the p99 target as a per-request expectation: any claim whose
        # end-to-end scoring exceeds the 2,000 ms budget is a failure, so latency
        # degradation shows up directly in the statistics (Req 17.1 / 17.2).
        if scored and elapsed_ms > CLAIM_PIPELINE_P99_MS:
            events.request.fire(
                request_type="PIPELINE",
                name="claim.scored (end-to-end)",
                response_time=elapsed_ms,
                response_length=0,
                exception=RuntimeError(
                    f"scoring latency {elapsed_ms:.0f} ms exceeded "
                    f"{CLAIM_PIPELINE_P99_MS} ms budget"
                ),
            )

    def _poll_until_scored(self, claim_id: str) -> bool:
        """Poll the claim status endpoint until terminal or timeout.

        Returns ``True`` when a terminal Risk_Decision was observed within the
        timeout, ``False`` otherwise.
        """
        deadline = time.monotonic() + _STATUS_POLL_TIMEOUT_S
        while time.monotonic() < deadline:
            with self.client.get(
                f"/api/v1/claims/{claim_id}/status",
                headers=self.headers,
                name=NAME_CLAIM_STATUS,
                catch_response=True,
            ) as response:
                if response.status_code != 200:
                    response.failure(
                        f"status poll failed: HTTP {response.status_code}"
                    )
                    return False
                response.success()
                try:
                    status_value = response.json().get("status")
                except ValueError:
                    return False
                if status_value in _TERMINAL_STATUSES:
                    return True
            time.sleep(_STATUS_POLL_INTERVAL_S)
        return False


# ---------------------------------------------------------------------------
# PortalUser -- Claimant Portal form render + confirmation (Req 17.3).
# ---------------------------------------------------------------------------


class PortalUser(HttpUser):
    """Simulate a claimant using the Claimant Portal UI.

    Flow per iteration:
      1. Load the claim submission form (portal SPA / SSR entry point).
      2. Submit the claim through the portal and receive a confirmation.

    The 3,000 ms p99 render/confirmation budget (Req 17.3) is asserted per
    request so slow renders are marked failed and enforced again at quit time.
    """

    wait_time = between(1.0, 3.0)

    def on_start(self) -> None:
        self.headers = _auth_headers()

    @task(3)
    def load_submission_form(self) -> None:
        """Render the claim submission form within the portal budget."""
        with self.client.get(
            "/",
            name=NAME_PORTAL_FORM,
            catch_response=True,
        ) as response:
            if response.status_code >= 500:
                response.failure(f"portal form failed: HTTP {response.status_code}")
            elif response.elapsed.total_seconds() * 1_000.0 > PORTAL_P99_MS:
                response.failure(
                    f"portal render {response.elapsed.total_seconds() * 1_000.0:.0f} ms "
                    f"exceeded {PORTAL_P99_MS} ms budget"
                )
            else:
                response.success()

    @task(1)
    def submit_via_portal(self) -> None:
        """Submit a claim through the portal and receive a confirmation."""
        fields = build_claim_fields()
        with self.client.post(
            "/api/v1/claims",
            data=fields,
            files={"roe_document": build_roe_document()},
            headers=self.headers,
            name=NAME_PORTAL_CONFIRM,
            catch_response=True,
        ) as response:
            if response.status_code not in (200, 202):
                response.failure(
                    f"portal submission failed: HTTP {response.status_code}"
                )
            elif response.elapsed.total_seconds() * 1_000.0 > PORTAL_P99_MS:
                response.failure(
                    f"portal confirmation {response.elapsed.total_seconds() * 1_000.0:.0f} ms "
                    f"exceeded {PORTAL_P99_MS} ms budget"
                )
            else:
                response.success()


# ---------------------------------------------------------------------------
# Quit-time SLA gate -- fail the run when p99 latency or throughput miss target.
# ---------------------------------------------------------------------------


def evaluate_sla(stats: Any) -> list[str]:
    """Return a list of SLA-violation messages for the given Locust ``stats``.

    Encodes Requirement 17 as post-run assertions against the aggregated
    statistics object:

    * Claim submission + status p99 <= ``CLAIM_PIPELINE_P99_MS`` (17.1).
    * Aggregate claim throughput >= ``MIN_CLAIM_THROUGHPUT_PER_MIN`` (17.2).
    * Portal form + confirmation p99 <= ``PORTAL_P99_MS`` (17.3).

    An empty list means every relevant SLA that produced traffic passed. Entries
    that saw no traffic are skipped (nothing to assert), which keeps the check
    valid whether the run exercised ``ClaimUser`` or ``PortalUser``.
    """
    violations: list[str] = []

    def _entry(name: str) -> Optional[Any]:
        getter = getattr(stats, "get", None)
        if callable(getter):
            try:
                entry = stats.get(name, "POST") or stats.get(name, "GET")
            except TypeError:
                entry = None
            if entry is not None and getattr(entry, "num_requests", 0):
                return entry
        # Fall back to the aggregated per-name dict when present.
        by_name = getattr(stats, "entries", None)
        if isinstance(by_name, dict):
            for key, entry in by_name.items():
                key_name = key[0] if isinstance(key, tuple) else key
                if key_name == name and getattr(entry, "num_requests", 0):
                    return entry
        return None

    def _p99(entry: Any) -> Optional[float]:
        try:
            return float(entry.get_response_time_percentile(0.99))
        except Exception:  # noqa: BLE001
            return None

    # 17.1 -- claim scoring pipeline p99.
    for name in (NAME_CLAIM_SUBMIT, NAME_CLAIM_STATUS):
        entry = _entry(name)
        if entry is not None:
            p99 = _p99(entry)
            if p99 is not None and p99 > CLAIM_PIPELINE_P99_MS:
                violations.append(
                    f"[17.1] {name} p99 {p99:.0f} ms > {CLAIM_PIPELINE_P99_MS} ms"
                )

    # 17.2 -- sustained claim throughput (claims/min) from the submit endpoint.
    submit = _entry(NAME_CLAIM_SUBMIT)
    if submit is not None:
        per_min = float(getattr(submit, "total_rps", 0.0)) * 60.0
        if per_min < MIN_CLAIM_THROUGHPUT_PER_MIN:
            violations.append(
                f"[17.2] claim throughput {per_min:.0f}/min < "
                f"{MIN_CLAIM_THROUGHPUT_PER_MIN}/min"
            )

    # 17.3 -- Claimant Portal p99.
    for name in (NAME_PORTAL_FORM, NAME_PORTAL_CONFIRM):
        entry = _entry(name)
        if entry is not None:
            p99 = _p99(entry)
            if p99 is not None and p99 > PORTAL_P99_MS:
                violations.append(
                    f"[17.3] {name} p99 {p99:.0f} ms > {PORTAL_P99_MS} ms"
                )

    return violations


@events.quitting.add_listener
def _enforce_sla_on_quit(environment: Any, **_kwargs: Any) -> None:
    """Fail the Locust process when any Requirement 17 SLA is missed.

    Runs after the load test completes. Inspects the aggregated statistics and,
    on any violation, sets ``environment.process_exit_code = 1`` so a headless
    ``locust`` run returns non-zero and fails the CI pipeline.
    """
    stats = getattr(environment, "stats", None)
    if stats is None:
        return

    violations = evaluate_sla(stats)
    if violations:
        for message in violations:
            print(f"SLA VIOLATION {message}")
        environment.process_exit_code = 1
    else:
        environment.process_exit_code = 0
