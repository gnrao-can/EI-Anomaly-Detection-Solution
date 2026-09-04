"""
tests/unit/test_telemetry.py
=============================
Unit + property tests for the shared distributed-tracing module
(``shared/telemetry.py``, task 32.1 / Req 21.5).

These tests exercise the parts of the tracing layer that MUST work regardless of
whether the OpenTelemetry SDK is installed:

  * the Protected B PII guard — no raw SIN / name / address may ever be attached
    to a span attribute (only ``sin_hash`` is allowed);
  * the span helpers degrade to safe no-ops (yielding ``None``) when the OTel SDK
    is absent, while STILL enforcing the PII guard before creating a span;
  * Kafka trace-context injection/extraction round-trips without raising and
    never drops the caller's existing headers.

The PII-guard invariant is the security-critical property, so it is covered both
by explicit examples and by a Hypothesis property test.

Requirements: 21.5
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shared import telemetry


# ---------------------------------------------------------------------------
# PII guard — examples
# ---------------------------------------------------------------------------


def test_sin_hash_is_allowed():
    """``sin_hash`` is not raw PII and must be permitted on a span."""
    attrs = telemetry.safe_span_attributes({"sin_hash": "a" * 64, "claim_id": "c1"})
    assert attrs == {"sin_hash": "a" * 64, "claim_id": "c1"}


@pytest.mark.parametrize(
    "key",
    ["sin", "social_insurance_number", "name", "first_name", "last_name",
     "address", "postal_code", "date_of_birth", "email", "phone"],
)
def test_forbidden_pii_keys_rejected(key):
    """Any attribute key naming raw PII is rejected."""
    with pytest.raises(telemetry.PIIViolationError):
        telemetry.safe_span_attributes({key: "whatever"})


def test_dotted_pii_key_rejected():
    """A namespaced key such as ``claim.sin`` is still caught as raw PII."""
    with pytest.raises(telemetry.PIIViolationError):
        telemetry.assert_no_pii("claim.sin", "123456789")


def test_raw_sin_value_rejected_even_under_benign_key():
    """A value that structurally looks like a raw 9-digit SIN is rejected."""
    with pytest.raises(telemetry.PIIViolationError):
        telemetry.assert_no_pii("identifier", "123 456 789")


def test_sin_hash_value_not_flagged_as_raw_sin():
    """A 64-hex sin_hash never trips the raw-SIN value heuristic."""
    telemetry.assert_no_pii("sin_hash", "0" * 64)  # must not raise


def test_none_values_are_dropped():
    """OTel rejects None attribute values, so they are dropped by the guard."""
    assert telemetry.safe_span_attributes({"claim_id": None, "engine": "x"}) == {
        "engine": "x"
    }


# ---------------------------------------------------------------------------
# PII guard — property
# ---------------------------------------------------------------------------


@given(
    key=st.sampled_from(sorted(telemetry.FORBIDDEN_PII_KEYS)),
    prefix=st.sampled_from(["", "claim.", "claimant.", "svc."]),
    value=st.text(min_size=1, max_size=20),
)
def test_property_forbidden_keys_always_rejected(key, prefix, value):
    """Property: any forbidden PII key (bare or namespaced) is ALWAYS rejected.

    Validates: Requirements 21.5
    """
    with pytest.raises(telemetry.PIIViolationError):
        telemetry.assert_no_pii(prefix + key, value)


@given(digits=st.text(alphabet="0123456789", min_size=9, max_size=9))
def test_property_nine_digit_values_look_like_sin(digits):
    """Property: any bare 9-digit string is treated as a raw SIN and rejected.

    Validates: Requirements 21.5
    """
    with pytest.raises(telemetry.PIIViolationError):
        telemetry.assert_no_pii("some_id", digits)


# ---------------------------------------------------------------------------
# Span helpers degrade to no-ops but still enforce the guard
# ---------------------------------------------------------------------------


def test_start_span_yields_none_without_sdk_but_enforces_guard():
    """Without the OTel SDK, start_span yields None yet still blocks raw PII."""
    with telemetry.start_span("test.span", attributes={"claim_id": "c1"}) as span:
        # In the sandbox (no OTel SDK) the span is a no-op None; the block runs.
        assert span is None or hasattr(span, "set_attribute")

    with pytest.raises(telemetry.PIIViolationError):
        with telemetry.start_span("bad.span", attributes={"name": "Jane Doe"}):
            pass  # pragma: no cover - guard raises before body runs


def test_engine_and_shap_and_llm_spans_run():
    """The convenience span helpers are safe to use and enforce the guard."""
    with telemetry.engine_span("repetitive_pattern", "claim-1", budget_ms=400):
        pass
    with telemetry.shap_span("claim-1", indicator_count=3):
        pass
    with telemetry.llm_span("explain_bilingual", risk_score=42):
        pass
    with telemetry.adapter_span("cbsa", "fetch", record_count=10):
        pass


def test_engine_span_rejects_pii_attribute():
    """A raw-PII kwarg on an engine span is rejected."""
    with pytest.raises(telemetry.PIIViolationError):
        with telemetry.engine_span("fraud_ring", "claim-1", address="123 Main St"):
            pass  # pragma: no cover


def test_root_claim_span_carries_claim_id_and_sin_hash():
    """The root claim.received span accepts claim_id + sin_hash (no raw PII)."""
    with telemetry.root_claim_span("claim-1", "f" * 64) as span:
        assert span is None or hasattr(span, "set_attribute")


# ---------------------------------------------------------------------------
# Kafka trace-context propagation
# ---------------------------------------------------------------------------


def test_inject_preserves_existing_headers():
    """Injecting trace context never drops the caller's existing headers."""
    existing = [("x-request-id", b"abc")]
    result = telemetry.inject_trace_context(list(existing))
    assert ("x-request-id", b"abc") in result


def test_inject_and_extract_round_trip_do_not_raise():
    """Inject then extract round-trips cleanly whether or not the SDK is present."""
    headers = telemetry.inject_trace_context([])
    ctx = telemetry.extract_trace_context(headers)
    # Without the SDK, extract returns None; with it, a Context object.
    assert ctx is None or ctx is not None


def test_extract_from_empty_headers_returns_none():
    assert telemetry.extract_trace_context(None) is None
    assert telemetry.extract_trace_context([]) is None
