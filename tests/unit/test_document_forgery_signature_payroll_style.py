"""
tests/unit/test_document_forgery_signature_payroll_style.py
===========================================================
Unit + property tests for the Document Forgery Engine task-15.3 slice:
digital signature verification, CRA payroll cross-reference, async LLM style
analysis, Kafka publication, and the 700 ms processing budget.

Coverage (design "5. Document Forgery Engine", steps 5-8):

  * Digital signature verification (Requirements 7.5, 7.6):
      - absent signature                 -> CRITICAL (7.6)
      - signer cert hash != registered   -> CRITICAL (7.5)
      - signer cert hash == registered   -> NO Indicator
      - cryptography unavailable / cannot verify -> skipped (no false flag)
      - real PKCS#7 signature round-trip via cryptography (when available)
  * CRA payroll cross-reference (Requirement 7.7):
      - SIN confirmed NOT in payroll     -> CRITICAL (7.7)
      - SIN found                        -> NO Indicator
      - CRA Adapter UNAVAILABLE          -> NO CRITICAL (graceful degradation)
      - CRA Adapter mocked (no live network)
  * LLM style analysis (Requirements 7.8, 7.9):
      - >= 5 prior authenticated ROEs + LLM YES -> MEDIUM (7.8)
      - >= 5 prior ROEs + LLM NO                -> NO Indicator
      - < 5 prior authenticated ROEs            -> skipped (7.9)
      - LLM timeout / error                     -> NO Indicator (best-effort)
  * Kafka publication to indicators.document-forgery + 700 ms budget
    (Requirements 7.10, 7.11):
      - all Indicators published to the correct topic
      - PROCESSING_TIMEOUT Indicator emitted when core analysis > 700 ms (7.11)

The service directory is hyphenated (``services/document-forgery-engine``) and
its modules import each other by bare name, so we load them under unique
namespaced keys via the shared ``load_service_module`` helper. The CRA Adapter
and LLM Service HTTP calls are mocked — no live network is used.

Requirements: 7.5, 7.6, 7.7, 7.8, 7.9, 7.10, 7.11
"""

from __future__ import annotations

import asyncio
import datetime as _dt

import pytest

from shared.models import EngineType, Severity
from tests.conftest import load_service_module

# ---------------------------------------------------------------------------
# Load the hyphenated document-forgery-engine service modules under unique
# namespaced keys.
# ---------------------------------------------------------------------------

_signature = load_service_module(
    "document-forgery-engine", "signature_verification",
    siblings=["signature_verification"],
)
_payroll = load_service_module(
    "document-forgery-engine", "payroll_cross_reference",
    siblings=["payroll_cross_reference"],
)
_style = load_service_module(
    "document-forgery-engine", "style_analysis", siblings=["style_analysis"]
)
_kafka = load_service_module(
    "document-forgery-engine", "kafka_publisher", siblings=["kafka_publisher"]
)

# Signature verification surface
SignatureExtraction = _signature.SignatureExtraction
extract_signature_certificate_hash = _signature.extract_signature_certificate_hash
check_digital_signature = _signature.check_digital_signature
sha256_certificate_hash = _signature.sha256_certificate_hash
REASON_NO_SIGNATURE = _signature.REASON_NO_SIGNATURE
REASON_HASH_MISMATCH = _signature.REASON_HASH_MISMATCH

# Payroll surface
PayrollLookup = _payroll.PayrollLookup
CRAPayrollClient = _payroll.CRAPayrollClient
check_payroll_cross_reference = _payroll.check_payroll_cross_reference
STATUS_FOUND = _payroll.STATUS_FOUND
STATUS_NOT_FOUND = _payroll.STATUS_NOT_FOUND
STATUS_UNAVAILABLE = _payroll.STATUS_UNAVAILABLE

# Style surface
StyleAnalysisChecker = _style.StyleAnalysisChecker
MIN_STYLE_ROES = _style.MIN_AUTHENTICATED_ROES_FOR_STYLE_CHECK

# Kafka / budget surface
INDICATORS_TOPIC = _kafka.INDICATORS_TOPIC
ANALYSIS_BUDGET_MS = _kafka.ANALYSIS_BUDGET_MS
PROCESSING_TIMEOUT = _kafka.PROCESSING_TIMEOUT
IndicatorPublisher = _kafka.IndicatorPublisher
make_processing_timeout_indicator = _kafka.make_processing_timeout_indicator


# ===========================================================================
# Digital signature verification (Requirements 7.5, 7.6)
# ===========================================================================


def test_absent_signature_flags_critical():
    extraction = extract_signature_certificate_hash({"signature_present": False})
    indicators = check_digital_signature(extraction, "abc123", claim_id="c1")
    assert len(indicators) == 1
    ind = indicators[0]
    assert ind.severity == Severity.CRITICAL
    assert ind.engine == EngineType.DOCUMENT_FORGERY
    assert ind.algorithm == "digital_signature_verification"
    assert ind.payload["flag"] == REASON_NO_SIGNATURE


def test_no_signature_material_at_all_flags_absent():
    # A document with no signature keys at all is treated as unsigned (7.6).
    extraction = extract_signature_certificate_hash({})
    assert extraction.present is False
    indicators = check_digital_signature(extraction, "abc123")
    assert len(indicators) == 1
    assert indicators[0].payload["flag"] == REASON_NO_SIGNATURE


def test_hash_mismatch_flags_critical():
    extraction = extract_signature_certificate_hash(
        {"signature_cert_hash": "AA:BB:CC"}
    )
    indicators = check_digital_signature(extraction, "ddeeff", claim_id="c2")
    assert len(indicators) == 1
    ind = indicators[0]
    assert ind.severity == Severity.CRITICAL
    assert ind.payload["flag"] == REASON_HASH_MISMATCH
    assert ind.payload["observed_cert_hash"] == "aabbcc"
    assert ind.payload["registered_cert_hash"] == "ddeeff"


def test_matching_hash_no_flag():
    extraction = extract_signature_certificate_hash(
        {"signature_cert_hash": "abc123"}
    )
    assert check_digital_signature(extraction, "ABC123") == []


def test_hash_normalisation_is_separator_and_case_insensitive():
    extraction = extract_signature_certificate_hash(
        {"signature_cert_hash": "sha256:AB-CD-EF"}
    )
    assert check_digital_signature(extraction, "ab:cd:ef") == []


def test_unavailable_verification_skips_no_flag():
    # cryptography missing + only raw bytes -> available=False -> skip.
    extraction = SignatureExtraction(present=True, cert_hash=None, available=False)
    assert check_digital_signature(extraction, "abc123") == []


def test_no_registered_hash_skips_mismatch_check():
    extraction = extract_signature_certificate_hash(
        {"signature_cert_hash": "abc123"}
    )
    # No registered hash to compare against -> cannot assert a mismatch.
    assert check_digital_signature(extraction, None) == []


def test_real_pkcs7_signature_round_trip():
    """A real PKCS#7 signature is parsed and its signer cert hash verified."""
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs7, Encoding

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Employer")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    signature = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(b"ROE content")
        .add_signer(cert, key, hashes.SHA256())
        .sign(Encoding.DER, [])
    )

    registered = sha256_certificate_hash(cert.public_bytes(Encoding.DER))

    extraction = extract_signature_certificate_hash({"signature_pkcs7": signature})
    assert extraction.present is True
    assert extraction.available is True
    # Authentic signature -> no Indicator.
    assert check_digital_signature(extraction, registered) == []
    # A different registered hash -> mismatch CRITICAL.
    mismatch = check_digital_signature(extraction, "00" * 32)
    assert len(mismatch) == 1
    assert mismatch[0].payload["flag"] == REASON_HASH_MISMATCH


# ===========================================================================
# CRA payroll cross-reference (Requirement 7.7)
# ===========================================================================


def test_sin_not_in_payroll_flags_critical():
    indicators = check_payroll_cross_reference(
        PayrollLookup(status=STATUS_NOT_FOUND),
        sin_hash="hash1",
        employer_bn="123456789RP0001",
        claim_id="c3",
    )
    assert len(indicators) == 1
    ind = indicators[0]
    assert ind.severity == Severity.CRITICAL
    assert ind.engine == EngineType.DOCUMENT_FORGERY
    assert ind.algorithm == "payroll_cross_reference"
    assert ind.payload["flag"] == "SIN_NOT_IN_PAYROLL"
    assert ind.payload["employer_bn"] == "123456789RP0001"


def test_sin_found_no_flag():
    assert check_payroll_cross_reference(PayrollLookup(status=STATUS_FOUND)) == []


def test_cra_unavailable_no_critical():
    # An unavailable adapter is not evidence of absence -> no CRITICAL flag.
    assert check_payroll_cross_reference(PayrollLookup(status=STATUS_UNAVAILABLE)) == []


@pytest.mark.asyncio
async def test_cra_client_maps_found_via_mock_transport():
    async def fake_transport(url, timeout):
        assert "/cra/payroll/" in url
        return {"status": "OK", "sin_in_payroll": True}

    client = CRAPayrollClient("http://cra", transport=fake_transport)
    lookup = await client.check_sin_in_payroll("h", "BN", "2024-01-01", "2024-06-01")
    assert lookup.found


@pytest.mark.asyncio
async def test_cra_client_maps_not_found_via_mock_transport():
    async def fake_transport(url, timeout):
        return {"status": "OK", "sin_in_payroll": False}

    client = CRAPayrollClient("http://cra", transport=fake_transport)
    lookup = await client.check_sin_in_payroll("h", "BN", "2024-01-01", "2024-06-01")
    assert lookup.not_found
    # And that maps to a CRITICAL Indicator.
    assert len(check_payroll_cross_reference(lookup)) == 1


@pytest.mark.asyncio
async def test_cra_client_transport_error_maps_unavailable():
    async def failing_transport(url, timeout):
        raise RuntimeError("connection refused")

    client = CRAPayrollClient("http://cra", transport=failing_transport)
    lookup = await client.check_sin_in_payroll("h", "BN", "2024-01-01", "2024-06-01")
    assert lookup.unavailable
    assert check_payroll_cross_reference(lookup) == []


@pytest.mark.asyncio
async def test_cra_client_adapter_reported_unavailable():
    async def fake_transport(url, timeout):
        return {"status": "UNAVAILABLE"}

    client = CRAPayrollClient("http://cra", transport=fake_transport)
    lookup = await client.check_sin_in_payroll("h", "BN", "2024-01-01", "2024-06-01")
    assert lookup.unavailable


# ===========================================================================
# LLM style analysis (Requirements 7.8, 7.9)
# ===========================================================================

_CORPUS_5 = [f"narrative {i}" for i in range(5)]


@pytest.mark.asyncio
async def test_style_yes_with_five_roes_flags_medium():
    async def llm(narrative, corpus):
        assert len(corpus) == 5
        return {"inconsistent": True, "score": 0.87}

    checker = StyleAnalysisChecker(llm)
    indicators = await checker.check(
        "You are terminated.", _CORPUS_5, claim_id="c4"
    )
    assert len(indicators) == 1
    ind = indicators[0]
    assert ind.severity == Severity.MEDIUM
    assert ind.engine == EngineType.DOCUMENT_FORGERY
    assert ind.algorithm == "llm_style_analysis"
    assert ind.payload["flag"] == "STYLE_INCONSISTENT"
    assert ind.payload["score"] == 0.87
    assert ind.payload["corpus_size"] == 5


@pytest.mark.asyncio
async def test_style_no_with_five_roes_no_flag():
    async def llm(narrative, corpus):
        return {"inconsistent": False, "score": 0.10}

    checker = StyleAnalysisChecker(llm)
    assert await checker.check("n", _CORPUS_5) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 2, 3, 4])
async def test_style_skipped_below_five_prior_roes(count):
    called = False

    async def llm(narrative, corpus):
        nonlocal called
        called = True
        return {"inconsistent": True, "score": 0.99}

    checker = StyleAnalysisChecker(llm)
    corpus = [f"n{i}" for i in range(count)]
    indicators = await checker.check("n", corpus)
    assert indicators == []
    assert called is False  # LLM must not even be invoked (Requirement 7.9)


@pytest.mark.asyncio
async def test_style_runs_at_exactly_five_prior_roes():
    async def llm(narrative, corpus):
        return {"inconsistent": True, "score": 0.9}

    checker = StyleAnalysisChecker(llm)
    indicators = await checker.check(
        "n", [], prior_authenticated_roe_count=MIN_STYLE_ROES
    )
    assert len(indicators) == 1


@pytest.mark.asyncio
async def test_style_llm_timeout_no_flag():
    async def slow_llm(narrative, corpus):
        await asyncio.sleep(1.0)
        return {"inconsistent": True, "score": 1.0}

    checker = StyleAnalysisChecker(slow_llm, timeout_seconds=0.01)
    assert await checker.check("n", _CORPUS_5) == []


@pytest.mark.asyncio
async def test_style_llm_error_no_flag():
    async def bad_llm(narrative, corpus):
        raise RuntimeError("llm exploded")

    checker = StyleAnalysisChecker(bad_llm)
    assert await checker.check("n", _CORPUS_5) == []


# ===========================================================================
# Kafka publication + 700 ms budget (Requirements 7.10, 7.11)
# ===========================================================================


class _FakeProducer:
    """Records every published event so tests can assert topic + payload."""

    def __init__(self):
        self.events = []

    async def send_and_wait(self, topic, value, key=None):
        self.events.append((topic, value, key))
        return True


@pytest.mark.asyncio
async def test_publisher_sends_all_indicators_to_topic():
    producer = _FakeProducer()
    publisher = IndicatorPublisher(producer)
    indicators = check_payroll_cross_reference(
        PayrollLookup(status=STATUS_NOT_FOUND), claim_id="c5"
    )
    result = await publisher.publish("c5", indicators)
    assert result.published_count == 1
    assert result.topic == INDICATORS_TOPIC
    topic, value, key = producer.events[0]
    assert topic == INDICATORS_TOPIC
    assert key == b"c5"
    import json

    assert json.loads(value)["algorithm"] == "payroll_cross_reference"


@pytest.mark.asyncio
async def test_publisher_none_producer_is_noop():
    publisher = IndicatorPublisher(None)
    result = await publisher.publish("c6", [])
    assert result.published_count == 0


@pytest.mark.asyncio
async def test_publisher_swallows_broker_errors():
    class _Broken:
        async def send_and_wait(self, topic, value, key=None):
            raise RuntimeError("broker down")

    publisher = IndicatorPublisher(_Broken())
    indicators = check_payroll_cross_reference(
        PayrollLookup(status=STATUS_NOT_FOUND)
    )
    # A publish failure must not raise.
    result = await publisher.publish("c7", indicators)
    assert result.published_count == 0


def test_processing_timeout_indicator_shape():
    ind = make_processing_timeout_indicator(812.5, claim_id="c8")
    assert ind.engine == EngineType.DOCUMENT_FORGERY
    assert ind.severity == Severity.UNRESOLVED
    assert ind.payload["flag"] == PROCESSING_TIMEOUT
    assert ind.payload["elapsed_ms"] == 812.5
    assert ind.payload["budget_ms"] == ANALYSIS_BUDGET_MS


# ===========================================================================
# Engine -> CRA Adapter authentication (Req 19.7)
#
# The CRA Adapter's payroll endpoint is auth-gated (require_role INVESTIGATOR).
# CRAPayrollClient's DEFAULT transport must attach the service Bearer when a
# token is configured, and send NO Authorization header when it is absent.
# ===========================================================================


@pytest.mark.asyncio
async def test_cra_default_transport_sends_bearer_when_token_set(monkeypatch):
    httpx = pytest.importorskip("httpx")
    seen = {}

    def handler(request):
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "OK", "sin_in_payroll": True})

    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    client = CRAPayrollClient("http://cra", bearer_token="tok-doc-123")
    lookup = await client.check_sin_in_payroll("h", "BN", "2024-01-01", "2024-06-01")
    assert lookup.found
    assert seen["authorization"] == "Bearer tok-doc-123"


@pytest.mark.asyncio
async def test_cra_default_transport_no_header_when_token_absent(monkeypatch):
    httpx = pytest.importorskip("httpx")
    seen = {}

    def handler(request):
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "OK", "sin_in_payroll": False})

    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    client = CRAPayrollClient("http://cra")  # no token
    lookup = await client.check_sin_in_payroll("h", "BN", "2024-01-01", "2024-06-01")
    assert lookup.not_found
    assert seen["authorization"] is None


# ===========================================================================
# LLMStyleServiceClient — style-analysis HTTP seam + auth (Req 7.8 / 19.7)
# ===========================================================================

_llm_style = load_service_module(
    "document-forgery-engine", "llm_style_client", siblings=["llm_style_client"]
)
LLMStyleServiceClient = _llm_style.LLMStyleServiceClient


def _run_async(coro):
    import asyncio

    return asyncio.get_event_loop().run_until_complete(coro)


def _patch_httpx_style(monkeypatch, body, capture):
    import httpx

    def handler(request: "httpx.Request") -> "httpx.Response":
        capture["url"] = str(request.url)
        capture["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )


@pytest.mark.asyncio
async def test_style_client_posts_and_attaches_bearer(monkeypatch):
    pytest.importorskip("httpx")
    cap: dict = {}
    _patch_httpx_style(monkeypatch, {"inconsistent": True, "score": 0.83}, cap)
    client = LLMStyleServiceClient("http://llm", bearer_token="tok-s")
    result = await client("narrative", ["a", "b", "c"])
    assert result["inconsistent"] is True
    assert result["score"] == 0.83
    assert cap["url"].endswith("/llm/style-analysis")
    assert cap["authorization"] == "Bearer tok-s"


@pytest.mark.asyncio
async def test_style_client_no_bearer_when_token_absent(monkeypatch):
    pytest.importorskip("httpx")
    cap: dict = {}
    _patch_httpx_style(monkeypatch, {"inconsistent": False, "score": 0.1}, cap)
    client = LLMStyleServiceClient("http://llm")  # no token
    result = await client("narrative", [])
    assert result["inconsistent"] is False
    assert cap["authorization"] is None


@pytest.mark.asyncio
async def test_style_checker_with_http_client_yields_medium(monkeypatch):
    """StyleAnalysisChecker + the real HTTP client -> MEDIUM Indicator (Req 7.8)."""
    pytest.importorskip("httpx")
    cap: dict = {}
    _patch_httpx_style(monkeypatch, {"inconsistent": True, "score": 0.9}, cap)
    client = LLMStyleServiceClient("http://llm", bearer_token="tok")
    checker = StyleAnalysisChecker(client, timeout_seconds=5.0)
    # >= 5 prior ROEs so the check runs (Req 7.9 gate satisfied).
    indicators = await checker.check(
        "odd narrative",
        ["p1", "p2", "p3", "p4", "p5"],
        prior_authenticated_roe_count=5,
        claim_id="c1",
    )
    assert len(indicators) == 1
    assert indicators[0].severity == Severity.MEDIUM
    assert indicators[0].algorithm == "llm_style_analysis"
