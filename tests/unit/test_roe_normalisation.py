"""
tests/unit/test_roe_normalisation.py
====================================
Supplementary unit tests for the ROE Adapter (services/roe-adapter) that
strengthen the coverage established by ``test_roe_normalizer.py``.

This file deliberately complements — rather than duplicates — the existing
suite. It adds:

  * Extended ROE Block-style alias coverage (block8, block10, block11, block15A,
    block15B, block16, block18).
  * Additional encoding edge cases (mixed accented characters, UTF-8 BOM, pure
    ASCII) exercised through the full parse path.
  * Cross-transport equivalence (JSON == XML == SOAP) for MULTIPLE distinct
    claims, parametrised.
  * Structured failure modes — invalid SIN (bad Luhn), invalid BN, negative and
    zero hours, negative and zero earnings, unparseable separation date, and a
    non-dict payload — each asserting a ``NormalizationError`` with the correct
    field and that Kafka publish is NEVER invoked.
  * A success-path assertion that ``roe.received`` is published exactly once,
    keyed by ``claim_id``.

Requirements: 1.6, 1.7, 1.8
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the roe-adapter modules from the hyphenated service directory.
# ---------------------------------------------------------------------------

_SERVICE_DIR = Path(__file__).resolve().parents[2] / "services" / "roe-adapter"


def _load(module_name: str):
    spec = importlib.util.spec_from_file_location(
        f"roe_adapter_{module_name}", _SERVICE_DIR / f"{module_name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


normalizer = _load("normalizer")
normalize_roe = normalizer.normalize_roe
detect_and_decode = normalizer.detect_and_decode
xml_to_dict = normalizer.xml_to_dict
NormalizationError = normalizer.NormalizationError


# ---------------------------------------------------------------------------
# Distinct valid ROE fixtures (SINs are Luhn-valid; BNs are format-valid).
# Each renders identically into JSON, plain XML, and namespaced SOAP.
# ---------------------------------------------------------------------------

_CLAIM_A = {
    "sin": "130692544",
    "employer_bn": "123456789RT0001",
    "insurable_hours": "1820.50",
    "insurable_earnings": "54600.00",
    "separation_date": "2024-03-15",
    "claim_start_date": "2024-03-16",
    "separation_reason": "Shortage of work / end of contract",
    "separation_reason_code": "A",
    "economic_region_code": "35",
    "noc_code": "12010",
    "residential_postal_code": "K1A0A9",
    "employer_work_location": "100 Wellington St, Ottawa ON",
}

_CLAIM_B = {
    "sin": "193456787",
    "employer_bn": "987654321RP0002",
    "insurable_hours": "900",
    "insurable_earnings": "27000.75",
    "separation_date": "2023-11-30",
    "claim_start_date": "2023-12-01",
    "separation_reason": "Seasonal layoff",
    "separation_reason_code": "D",
    "economic_region_code": "24",
    "noc_code": "65100",
    "residential_postal_code": "H2X 1Y4",
    "employer_work_location": "500 Rue Sainte-Catherine, Montreal QC",
}

_CLAIM_C = {
    "sin": "371956988",
    "employer_bn": "111222333RC0009",
    "insurable_hours": "2040.25",
    "insurable_earnings": "81610.10",
    "separation_date": "2024-06-01",
    "claim_start_date": "2024-06-02",
    "separation_reason": "End of term / dismissal",
    "separation_reason_code": "M",
    "economic_region_code": "59",
    "noc_code": "21231",
    "residential_postal_code": "V6B4Y8",
    "employer_work_location": "999 Canada Pl, Vancouver BC",
}

_DISTINCT_CLAIMS = [_CLAIM_A, _CLAIM_B, _CLAIM_C]


def _claim_signature(claim):
    """Fields that must be identical across transports (ignore generated ids/ts)."""
    return (
        claim.sin_hash,
        claim.employer_bn,
        claim.insurable_hours,
        claim.insurable_earnings,
        claim.separation_date,
        claim.claim_start_date,
        claim.separation_reason,
        claim.separation_reason_code,
        claim.economic_region_code,
        claim.noc_code,
        claim.residential_postal_code,
        claim.employer_work_location,
        claim.status,
    )


def _to_plain_xml(fields: dict) -> str:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
    return f"<ROE>{body}</ROE>"


def _to_soap_envelope(fields: dict) -> str:
    inner = "".join(f"<roe:{k}>{v}</roe:{k}>" for k, v in fields.items())
    return (
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:roe="urn:esdc:ei-ads:roe">'
        f"<soap:Body><roe:SubmitROE>{inner}</roe:SubmitROE></soap:Body>"
        "</soap:Envelope>"
    )


# ===========================================================================
# Requirement 1.6/1.7 — Cross-transport equivalence for MULTIPLE claims
# ===========================================================================


class TestCrossTransportEquivalenceParametrized:
    """JSON, REST-XML, and SOAP payloads normalise to identical Claim objects
    for several distinct claims (not just one canonical example)."""

    @pytest.mark.parametrize("fields", _DISTINCT_CLAIMS, ids=["claimA", "claimB", "claimC"])
    def test_json_xml_soap_all_equivalent(self, fields):
        json_claim = normalize_roe(dict(fields))
        xml_claim = normalize_roe(xml_to_dict(_to_plain_xml(fields)))
        soap_claim = normalize_roe(xml_to_dict(_to_soap_envelope(fields)))

        assert _claim_signature(json_claim) == _claim_signature(xml_claim)
        assert _claim_signature(json_claim) == _claim_signature(soap_claim)

    @pytest.mark.parametrize("fields", _DISTINCT_CLAIMS, ids=["claimA", "claimB", "claimC"])
    def test_distinct_claims_produce_distinct_signatures(self, fields):
        """Sanity check that the fixtures are genuinely different from each other,
        so the equivalence assertions above are non-trivial."""
        others = [c for c in _DISTINCT_CLAIMS if c is not fields]
        this_sig = _claim_signature(normalize_roe(dict(fields)))
        for other in others:
            assert this_sig != _claim_signature(normalize_roe(dict(other)))


# ===========================================================================
# Requirement 1.6/1.7 — Extended ROE Block-style alias coverage
# ===========================================================================


class TestRoeBlockAliases:
    """ROE paper-form Block numbers map onto the canonical Claim fields."""

    def test_all_supported_block_aliases_resolve(self):
        aliased = {
            "socialInsuranceNumber": "130692544",
            "businessNumber": "123456789RT0001",
            "block8": "100 Wellington St, Ottawa ON",   # employer address
            "block10": "2024-03-16",                     # first day worked
            "block11": "2024-03-15",                     # last day for which paid
            "block15A": "1820.50",                       # total insurable hours
            "block15B": "54600.00",                      # total insurable earnings
            "block16": "A",                              # reason code
            "block18": "Shortage of work / end of contract",  # comments
        }
        claim = normalize_roe(aliased)

        assert claim.employer_bn == "123456789RT0001"
        assert claim.employer_work_location == "100 Wellington St, Ottawa ON"
        assert claim.claim_start_date.isoformat() == "2024-03-16"
        assert claim.separation_date.isoformat() == "2024-03-15"
        assert claim.insurable_hours == Decimal("1820.50")
        assert claim.insurable_earnings == Decimal("54600.00")
        assert claim.separation_reason_code == "A"
        assert claim.separation_reason == "Shortage of work / end of contract"

    def test_block_aliases_match_camelcase_equivalent(self):
        """A Block-keyed payload and a camelCase-keyed payload of the same data
        normalise to the same signature."""
        block_payload = {
            "sin": "193456787",
            "employerBusinessNumber": "987654321RP0002",
            "block15A": "900",
            "block15B": "27000.75",
            "block11": "2023-11-30",
            "block10": "2023-12-01",
            "block16": "D",
            "block18": "Seasonal layoff",
            "block8": "500 Rue Sainte-Catherine, Montreal QC",
            "economicRegionCode": "24",
            "nocCode": "65100",
            "residentialPostalCode": "H2X 1Y4",
        }
        canonical = dict(_CLAIM_B)
        assert _claim_signature(normalize_roe(block_payload)) == _claim_signature(
            normalize_roe(canonical)
        )


# ===========================================================================
# Requirement 1.8 — Encoding detection edge cases (full parse path)
# ===========================================================================


class TestEncodingEdgeCases:
    def test_pure_ascii_decodes_as_utf8(self):
        assert detect_and_decode(b"pure ascii payload 12345") == "pure ascii payload 12345"

    def test_utf8_bom_is_stripped(self):
        assert detect_and_decode(b"\xef\xbb\xbf" + "Réduction".encode("utf-8")) == "Réduction"

    def test_mixed_accents_iso_8859_1_fallback(self):
        text = "Café — Montréal, Trois-Rivières, Québec àéîôû"
        raw = text.encode("iso-8859-1", errors="replace")
        # ISO-8859-1 cannot represent the em dash; ensure bytes are not valid UTF-8
        # so the detector must use the Latin-1 fallback path.
        with pytest.raises(UnicodeDecodeError):
            raw.decode("utf-8")
        decoded = detect_and_decode(raw)
        assert "Montr" in decoded and "Qu" in decoded

    @pytest.mark.parametrize("encoding", ["utf-8", "iso-8859-1"])
    def test_accented_reason_normalises_via_full_body(self, encoding):
        """An accented separation reason survives decode + JSON parse + normalise
        for both UTF-8 and ISO-8859-1 encoded bodies."""
        payload = dict(_CLAIM_A)
        payload["separation_reason"] = "Réduction des activités"
        raw_bytes = json.dumps(payload, ensure_ascii=False).encode(encoding)
        decoded = detect_and_decode(raw_bytes)
        claim = normalize_roe(json.loads(decoded))
        assert claim.separation_reason == "Réduction des activités"


# ===========================================================================
# Requirement 1.8 — Failure modes: structured error + no publish
# ===========================================================================


def _field_set(exc: "NormalizationError") -> set:
    return {e["field"] for e in exc.errors}


class TestNormalisationFailureModes:
    """Each malformed payload raises NormalizationError carrying the offending
    field. normalize_roe raising *before* any publish is what guarantees Kafka
    is never called on failure (see TestPublishNeverCalledOnFailure)."""

    def test_invalid_sin_bad_luhn(self):
        payload = dict(_CLAIM_A)
        payload["sin"] = "123456789"  # 9 digits but fails Luhn
        with pytest.raises(NormalizationError) as ei:
            normalize_roe(payload)
        assert "sin" in _field_set(ei.value)

    def test_invalid_bn(self):
        payload = dict(_CLAIM_A)
        payload["employer_bn"] = "12345"  # wrong length / format
        with pytest.raises(NormalizationError) as ei:
            normalize_roe(payload)
        assert "employer_bn" in _field_set(ei.value)

    def test_negative_hours(self):
        payload = dict(_CLAIM_A)
        payload["insurable_hours"] = "-10"
        with pytest.raises(NormalizationError) as ei:
            normalize_roe(payload)
        assert "insurable_hours" in _field_set(ei.value)

    def test_zero_hours(self):
        payload = dict(_CLAIM_A)
        payload["insurable_hours"] = "0"
        with pytest.raises(NormalizationError) as ei:
            normalize_roe(payload)
        assert "insurable_hours" in _field_set(ei.value)

    def test_negative_earnings(self):
        payload = dict(_CLAIM_A)
        payload["insurable_earnings"] = "-0.01"
        with pytest.raises(NormalizationError) as ei:
            normalize_roe(payload)
        assert "insurable_earnings" in _field_set(ei.value)

    def test_zero_earnings(self):
        payload = dict(_CLAIM_A)
        payload["insurable_earnings"] = "0.00"
        with pytest.raises(NormalizationError) as ei:
            normalize_roe(payload)
        assert "insurable_earnings" in _field_set(ei.value)

    def test_unparseable_separation_date(self):
        payload = dict(_CLAIM_A)
        payload["separation_date"] = "not-a-date"
        with pytest.raises(NormalizationError) as ei:
            normalize_roe(payload)
        assert "separation_date" in _field_set(ei.value)

    def test_non_dict_payload(self):
        with pytest.raises(NormalizationError) as ei:
            normalize_roe(["not", "a", "dict"])  # type: ignore[arg-type]
        assert "_root" in _field_set(ei.value)

    def test_multiple_errors_are_all_reported(self):
        """Several bad fields are collected into a single structured error list
        rather than failing on the first one."""
        payload = dict(_CLAIM_A)
        payload["sin"] = "123456789"        # bad Luhn
        payload["employer_bn"] = "bad"      # bad BN
        payload["insurable_hours"] = "-1"   # non-positive
        with pytest.raises(NormalizationError) as ei:
            normalize_roe(payload)
        fields = _field_set(ei.value)
        assert {"sin", "employer_bn", "insurable_hours"}.issubset(fields)


# ===========================================================================
# Requirement 1.8 — Publish is NEVER called when normalisation fails
# ===========================================================================


class TestPublishNeverCalledOnFailure:
    """Wire normalisation into the API path with an exploding Kafka producer to
    prove no publish occurs for any failure mode."""

    @pytest.fixture()
    def app_module(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "normalizer", normalizer)
        monkeypatch.setitem(sys.modules, "config", _load("config"))
        return _load("main")

    @pytest.mark.parametrize(
        "mutate,bad_field",
        [
            (lambda p: p.update(sin="123456789"), "sin"),
            (lambda p: p.update(employer_bn="nope"), "employer_bn"),
            (lambda p: p.update(insurable_hours="-5"), "insurable_hours"),
            (lambda p: p.update(insurable_hours="0"), "insurable_hours"),
            (lambda p: p.update(insurable_earnings="-1"), "insurable_earnings"),
            (lambda p: p.update(insurable_earnings="0"), "insurable_earnings"),
            (lambda p: p.update(separation_date="31/31/2024"), "separation_date"),
            (lambda p: p.pop("sin"), "sin"),
        ],
    )
    def test_each_failure_mode_suppresses_publish(self, app_module, monkeypatch, mutate, bad_field):
        class ExplodingProducer:
            async def send_and_wait(self, *a, **k):
                raise AssertionError("Kafka must NOT be called on normalisation failure")

        async def fake_get_producer():
            return ExplodingProducer()

        monkeypatch.setattr(app_module, "_get_producer", fake_get_producer)

        payload = dict(_CLAIM_A)
        mutate(payload)
        raw = app_module.parse_body_to_raw(
            json.dumps(payload).encode("utf-8"), "application/json"
        )
        with pytest.raises(NormalizationError) as ei:
            claim = app_module.normalize_roe(raw)
            # Defensive: if normalisation ever stopped raising, publish would run
            # and the ExplodingProducer would fail the test.
            asyncio.run(app_module.publish_roe_received(claim))
        assert bad_field in {e["field"] for e in ei.value.errors}

    def test_non_dict_body_returns_root_error_and_no_publish(self, app_module, monkeypatch):
        class ExplodingProducer:
            async def send_and_wait(self, *a, **k):
                raise AssertionError("Kafka must NOT be called on parse failure")

        async def fake_get_producer():
            return ExplodingProducer()

        monkeypatch.setattr(app_module, "_get_producer", fake_get_producer)

        # A JSON array body is not a ROE object.
        with pytest.raises(NormalizationError) as ei:
            app_module.parse_body_to_raw(b"[1, 2, 3]", "application/json")
        assert "_root" in {e["field"] for e in ei.value.errors}


# ===========================================================================
# Requirement 1.6/1.7 — Success path publishes exactly once, keyed by claim_id
# ===========================================================================


class TestPublishSuccessPath:
    @pytest.fixture()
    def app_module(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "normalizer", normalizer)
        monkeypatch.setitem(sys.modules, "config", _load("config"))
        return _load("main")

    @pytest.mark.parametrize("fields", _DISTINCT_CLAIMS, ids=["claimA", "claimB", "claimC"])
    def test_publishes_exactly_once_keyed_by_claim_id(self, app_module, monkeypatch, fields):
        sent: list = []

        class RecordingProducer:
            async def send_and_wait(self, topic, value, key):
                sent.append((topic, value, key))

        async def fake_get_producer():
            return RecordingProducer()

        monkeypatch.setattr(app_module, "_get_producer", fake_get_producer)

        raw = app_module.parse_body_to_raw(
            json.dumps(fields).encode("utf-8"), "application/json"
        )
        claim = app_module.normalize_roe(raw)
        published = asyncio.run(app_module.publish_roe_received(claim))

        assert published is True
        assert len(sent) == 1  # exactly once
        topic, value, key = sent[0]
        assert topic == "roe.received"
        assert key == str(claim.claim_id).encode("utf-8")  # keyed by claim_id

        # Event body carries the correct event type and claim id.
        event = json.loads(value.decode("utf-8"))
        assert event["event_type"] == "roe.received"
        assert event["claim_id"] == str(claim.claim_id)

    def test_graceful_degradation_publishes_nothing(self, app_module, monkeypatch):
        """When the producer is unavailable (None), publish returns False and no
        event is emitted — success normalisation still does not fabricate a send."""

        async def no_producer():
            return None

        monkeypatch.setattr(app_module, "_get_producer", no_producer)

        raw = app_module.parse_body_to_raw(
            json.dumps(_CLAIM_A).encode("utf-8"), "application/json"
        )
        claim = app_module.normalize_roe(raw)
        published = asyncio.run(app_module.publish_roe_received(claim))
        assert published is False
