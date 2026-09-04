"""
tests/unit/test_roe_normalizer.py
=================================
Unit tests for the ROE Adapter (services/roe-adapter).

Covers:
  - normalize_roe: JSON, XML, and SOAP-style payloads normalise to identical
    canonical Claim objects
  - detect_and_decode: UTF-8 and ISO-8859-1 (Latin-1) decoding
  - normalization failure returns a structured error and publishes nothing
    (the Kafka producer is mocked and asserted never to be used)

Requirements: 1.6, 1.7, 1.8
"""

from __future__ import annotations

import importlib.util
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


# A canonical valid ROE, expressed once and rendered into each transport format.
# SIN 130692544 is a Luhn-valid test SIN that does not start with 0.
_VALID = {
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


# ===========================================================================
# detect_and_decode
# ===========================================================================


class TestDetectAndDecode:
    def test_utf8_roundtrip(self):
        text = "Réduction des activités — fin d'emploi"
        assert detect_and_decode(text.encode("utf-8")) == text

    def test_iso_8859_1_decoding(self):
        text = "Réduction des activités"
        raw = text.encode("iso-8859-1")
        # These bytes are NOT valid UTF-8, so the detector must fall back to Latin-1.
        with pytest.raises(UnicodeDecodeError):
            raw.decode("utf-8")
        assert detect_and_decode(raw) == text

    def test_ascii_is_utf8(self):
        assert detect_and_decode(b"plain ascii") == "plain ascii"

    def test_utf8_bom_stripped(self):
        assert detect_and_decode(b"\xef\xbb\xbfhello") == "hello"

    def test_iso_payload_normalises_correctly(self):
        """A Latin-1 encoded JSON body decodes and normalises without error."""
        import json

        payload = dict(_VALID)
        payload["separation_reason"] = "Réduction des activités"
        raw_bytes = json.dumps(payload, ensure_ascii=False).encode("iso-8859-1")
        decoded = detect_and_decode(raw_bytes)
        claim = normalize_roe(json.loads(decoded))
        assert claim.separation_reason == "Réduction des activités"


# ===========================================================================
# normalize_roe — cross-transport equivalence
# ===========================================================================


class TestNormalizeEquivalence:
    def test_json_normalises(self):
        claim = normalize_roe(dict(_VALID))
        assert claim.employer_bn == "123456789RT0001"
        assert claim.insurable_hours == Decimal("1820.50")
        assert claim.insurable_earnings == Decimal("54600.00")
        # Raw SIN must never appear; only its hash.
        assert claim.sin_hash and "130692544" not in claim.sin_hash
        assert claim.residential_postal_code == "K1A"  # FSA only

    def test_xml_matches_json(self):
        json_claim = normalize_roe(dict(_VALID))
        xml_body = "<ROE>" + "".join(
            f"<{k}>{v}</{k}>" for k, v in _VALID.items()
        ) + "</ROE>"
        xml_claim = normalize_roe(xml_to_dict(xml_body))
        assert _claim_signature(xml_claim) == _claim_signature(json_claim)

    def test_soap_matches_json(self):
        json_claim = normalize_roe(dict(_VALID))
        # SOAP envelope with namespaced body; xml_to_dict strips namespaces.
        inner = "".join(f"<roe:{k}>{v}</roe:{k}>" for k, v in _VALID.items())
        soap_body = (
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:roe="urn:esdc:ei-ads:roe">'
            f"<soap:Body><roe:SubmitROE>{inner}</roe:SubmitROE></soap:Body>"
            "</soap:Envelope>"
        )
        soap_claim = normalize_roe(xml_to_dict(soap_body))
        assert _claim_signature(soap_claim) == _claim_signature(json_claim)

    def test_field_aliases_map_to_canonical(self):
        """ROE Block-style and camelCase aliases resolve to canonical fields."""
        aliased = {
            "socialInsuranceNumber": "130692544",
            "businessNumber": "123456789RT0001",
            "block15A": "1820.50",
            "block15B": "54600.00",
            "block11": "2024-03-15",
            "block10": "2024-03-16",
            "block18": "Shortage of work / end of contract",
            "block16": "A",
        }
        claim = normalize_roe(aliased)
        assert claim.employer_bn == "123456789RT0001"
        assert claim.insurable_hours == Decimal("1820.50")
        assert claim.separation_date.isoformat() == "2024-03-15"


# ===========================================================================
# normalize_roe — failure handling
# ===========================================================================


class TestNormalizeFailure:
    def test_missing_sin_raises_structured_error(self):
        payload = dict(_VALID)
        del payload["sin"]
        with pytest.raises(NormalizationError) as exc_info:
            normalize_roe(payload)
        fields = {e["field"] for e in exc_info.value.errors}
        assert "sin" in fields

    def test_invalid_bn_raises(self):
        payload = dict(_VALID)
        payload["employer_bn"] = "not-a-bn"
        with pytest.raises(NormalizationError):
            normalize_roe(payload)

    def test_negative_hours_raises(self):
        payload = dict(_VALID)
        payload["insurable_hours"] = "-5"
        with pytest.raises(NormalizationError):
            normalize_roe(payload)

    def test_non_object_payload_raises(self):
        with pytest.raises(NormalizationError):
            normalize_roe(["not", "a", "dict"])  # type: ignore[arg-type]


# ===========================================================================
# Endpoint behaviour — publish on success, suppress on failure (mocked Kafka)
# ===========================================================================


class TestPublishSuppression:
    """Verify the API publishes on success and never publishes on failure."""

    @pytest.fixture()
    def app_module(self, monkeypatch):
        # Ensure the normalizer module the main module imports is the same one.
        monkeypatch.setitem(sys.modules, "normalizer", normalizer)
        monkeypatch.setitem(sys.modules, "config", _load("config"))
        return _load("main")

    def test_success_publishes(self, app_module, monkeypatch):
        import asyncio
        import json

        sent: list = []

        class FakeProducer:
            async def send_and_wait(self, topic, value, key):
                sent.append((topic, value, key))

        async def fake_get_producer():
            return FakeProducer()

        monkeypatch.setattr(app_module, "_get_producer", fake_get_producer)

        raw = app_module.parse_body_to_raw(
            json.dumps(_VALID).encode("utf-8"), "application/json"
        )
        claim = app_module.normalize_roe(raw)
        published = asyncio.run(app_module.publish_roe_received(claim))
        assert published is True
        assert len(sent) == 1
        topic, value, key = sent[0]
        assert topic == "roe.received"
        assert key == str(claim.claim_id).encode("utf-8")

    def test_failure_suppresses_publish(self, app_module, monkeypatch):
        import json

        class ExplodingProducer:
            async def send_and_wait(self, *a, **k):
                raise AssertionError("Kafka must NOT be called on normalisation failure")

        async def fake_get_producer():
            return ExplodingProducer()

        monkeypatch.setattr(app_module, "_get_producer", fake_get_producer)

        bad = dict(_VALID)
        del bad["sin"]
        # Normalisation must raise before any publish attempt.
        with pytest.raises(NormalizationError):
            claim = app_module.normalize_roe(
                app_module.parse_body_to_raw(json.dumps(bad).encode("utf-8"), "application/json")
            )
