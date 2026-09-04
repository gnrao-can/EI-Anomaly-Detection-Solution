"""
tests/unit/test_business_registry_round_trip.py
===============================================
Round-trip serialisation unit tests for the Business Registry Adapter's
canonical ``EmployerRegistration`` model (task 20.2).

These tests verify that ``EmployerRegistration`` survives a serialise /
re-parse cycle with no loss of information:

  - ``from_dict(to_dict(reg))`` reconstructs a registration whose every field
    matches the original (the model's ``__eq__`` only compares BN + jurisdiction,
    so each field is asserted individually as well as via ``==``).
  - ``json.dumps(reg.to_dict(), sort_keys=True)`` is byte-identical to the JSON
    produced after a full re-parse, i.e. serialisation is stable and lossless.
  - A ``None`` ``registration_date`` serialises to JSON ``null`` and restores to
    ``None``.
  - Every ``AddressType`` and ``CorporateStatus`` enum value round-trips.

The hyphenated ``business-registry-adapter`` service directory is loaded with
the shared, isolation-safe :func:`tests.conftest.load_service_module` helper so
this file can run standalone AND alongside the rest of the unit suite without
bare-name ``config`` / ``client`` collisions.

Requirements: 12.7
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import load_service_module

# Load ``client`` from services/business-registry-adapter under a namespaced key.
# ``client`` imports ``config`` by bare name, so both siblings are provided.
_client = load_service_module(
    "business-registry-adapter", "client", siblings=["config", "client"]
)

AddressType = _client.AddressType
CorporateStatus = _client.CorporateStatus
EmployerRegistration = _client.EmployerRegistration

try:  # ``date`` comes from the stdlib; import here for building fixtures.
    from datetime import date
except ImportError:  # pragma: no cover - defensive, datetime is always present
    raise


# ===========================================================================
# Canonical registration builders
# ===========================================================================


def _canonical_registration() -> EmployerRegistration:
    """A fully-populated canonical registration (all fields set)."""
    return EmployerRegistration(
        bn="900000001RT0001",
        legal_name="Fully Populated Employer Inc.",
        jurisdiction="ON",
        registration_date=date(2016, 3, 14),
        corporate_status=CorporateStatus.ACTIVE,
        registered_address="1 Test Street, Toronto, ON",
        registered_postal_code="M5J 2J2",
        address_type=AddressType.COMMERCIAL,
        unverified_zoning=False,
    )


def _representative_registrations() -> list[EmployerRegistration]:
    """A handful of representative registrations covering the field space."""
    return [
        _canonical_registration(),
        # Residential address, unverified zoning True, dissolved status.
        EmployerRegistration(
            bn="900000002RT0001",
            legal_name="Shell Co.",
            jurisdiction="CA",
            registration_date=date(2024, 2, 29),  # leap-day date
            corporate_status=CorporateStatus.DISSOLVED,
            registered_address="88 Maple Street",
            registered_postal_code="M4E 1B1",
            address_type=AddressType.RESIDENTIAL,
            unverified_zoning=True,
        ),
        # None registration_date, UNKNOWN status + address type.
        EmployerRegistration(
            bn="900000003RT0001",
            legal_name="Unknown Origins Ltd.",
            jurisdiction="BC",
            registration_date=None,
            corporate_status=CorporateStatus.UNKNOWN,
            registered_address="",
            registered_postal_code="",
            address_type=AddressType.UNKNOWN,
            unverified_zoning=False,
        ),
        # Inactive status, unverified zoning True.
        EmployerRegistration(
            bn="900000004RT0001",
            legal_name="Inactive Holdings",
            jurisdiction="AB",
            registration_date=date(1999, 12, 31),
            corporate_status=CorporateStatus.INACTIVE,
            registered_address="500 Corporate Blvd",
            registered_postal_code="T2N 1N4",
            address_type=AddressType.COMMERCIAL,
            unverified_zoning=True,
        ),
    ]


def _assert_all_fields_equal(
    original: EmployerRegistration, restored: EmployerRegistration
) -> None:
    """Assert every field matches (not just BN + jurisdiction ``__eq__``)."""
    assert restored.bn == original.bn
    assert restored.legal_name == original.legal_name
    assert restored.jurisdiction == original.jurisdiction
    assert restored.registration_date == original.registration_date
    assert restored.corporate_status is original.corporate_status
    assert restored.registered_address == original.registered_address
    assert restored.registered_postal_code == original.registered_postal_code
    assert restored.address_type is original.address_type
    assert restored.unverified_zoning is original.unverified_zoning


# ===========================================================================
# Round-trip: from_dict(to_dict(reg)) reconstructs the registration
# ===========================================================================


class TestRoundTripReconstruction:
    def test_canonical_registration_round_trips_all_fields(self):
        reg = _canonical_registration()
        restored = EmployerRegistration.from_dict(reg.to_dict())

        # Model __eq__ is BN + jurisdiction; confirm it holds ...
        assert restored == reg
        # ... AND confirm every individual field survived the round trip.
        _assert_all_fields_equal(reg, restored)

    @pytest.mark.parametrize("reg", _representative_registrations())
    def test_representative_registrations_round_trip(self, reg):
        restored = EmployerRegistration.from_dict(reg.to_dict())
        assert restored == reg
        _assert_all_fields_equal(reg, restored)


# ===========================================================================
# Byte-equivalent JSON: re-parse then re-serialise yields identical bytes
# ===========================================================================


class TestByteEquivalentJson:
    def test_canonical_json_is_byte_identical_after_reparse(self):
        reg = _canonical_registration()
        first = json.dumps(reg.to_dict(), sort_keys=True)
        second = json.dumps(
            EmployerRegistration.from_dict(reg.to_dict()).to_dict(),
            sort_keys=True,
        )
        assert first == second

    @pytest.mark.parametrize("reg", _representative_registrations())
    def test_representative_json_byte_identical(self, reg):
        first = json.dumps(reg.to_dict(), sort_keys=True)
        second = json.dumps(
            EmployerRegistration.from_dict(reg.to_dict()).to_dict(),
            sort_keys=True,
        )
        assert first == second


# ===========================================================================
# None registration_date -> JSON null -> None
# ===========================================================================


class TestNoneRegistrationDate:
    def test_to_dict_emits_null_for_none_date(self):
        reg = EmployerRegistration(
            bn="900000003RT0001",
            legal_name="No Date Co.",
            jurisdiction="BC",
            registration_date=None,
            corporate_status=CorporateStatus.UNKNOWN,
            registered_address="",
            registered_postal_code="",
        )
        as_dict = reg.to_dict()
        assert as_dict["registration_date"] is None
        # And the JSON literal is `null`.
        assert '"registration_date": null' in json.dumps(as_dict, sort_keys=True)

    def test_from_dict_restores_none_date(self):
        reg = EmployerRegistration(
            bn="900000003RT0001",
            legal_name="No Date Co.",
            jurisdiction="BC",
            registration_date=None,
            corporate_status=CorporateStatus.UNKNOWN,
            registered_address="",
            registered_postal_code="",
        )
        restored = EmployerRegistration.from_dict(reg.to_dict())
        assert restored.registration_date is None
        _assert_all_fields_equal(reg, restored)


# ===========================================================================
# Every enum value round-trips
# ===========================================================================


class TestEnumRoundTrip:
    @pytest.mark.parametrize("address_type", list(AddressType))
    def test_all_address_types_round_trip(self, address_type):
        reg = EmployerRegistration(
            bn="900000005RT0001",
            legal_name="Address Type Co.",
            jurisdiction="ON",
            registration_date=date(2020, 1, 1),
            corporate_status=CorporateStatus.ACTIVE,
            registered_address="10 Zoning Ave",
            registered_postal_code="K1S 5B6",
            address_type=address_type,
            unverified_zoning=False,
        )
        restored = EmployerRegistration.from_dict(reg.to_dict())
        assert restored.address_type is address_type
        _assert_all_fields_equal(reg, restored)

    @pytest.mark.parametrize("corporate_status", list(CorporateStatus))
    def test_all_corporate_statuses_round_trip(self, corporate_status):
        reg = EmployerRegistration(
            bn="900000006RT0001",
            legal_name="Status Co.",
            jurisdiction="QC",
            registration_date=date(2021, 6, 15),
            corporate_status=corporate_status,
            registered_address="20 Status Rd",
            registered_postal_code="H3C 1A3",
            address_type=AddressType.COMMERCIAL,
            unverified_zoning=True,
        )
        restored = EmployerRegistration.from_dict(reg.to_dict())
        assert restored.corporate_status is corporate_status
        _assert_all_fields_equal(reg, restored)

    @pytest.mark.parametrize("unverified_zoning", [True, False])
    def test_unverified_zoning_both_values_round_trip(self, unverified_zoning):
        reg = EmployerRegistration(
            bn="900000007RT0001",
            legal_name="Zoning Flag Co.",
            jurisdiction="NS",
            registration_date=date(2018, 8, 8),
            corporate_status=CorporateStatus.ACTIVE,
            registered_address="30 Flag St",
            registered_postal_code="B3H 1A1",
            address_type=AddressType.RESIDENTIAL,
            unverified_zoning=unverified_zoning,
        )
        restored = EmployerRegistration.from_dict(reg.to_dict())
        assert restored.unverified_zoning is unverified_zoning
        _assert_all_fields_equal(reg, restored)


# ===========================================================================
# Optional Hypothesis-based round-trip (skipped if hypothesis unavailable)
# ===========================================================================

try:
    from hypothesis import given
    from hypothesis import strategies as st

    _HYPOTHESIS_AVAILABLE = True
    _HYPO_SKIP = ""
except Exception as _exc:  # noqa: BLE001
    _HYPOTHESIS_AVAILABLE = False
    _HYPO_SKIP = f"hypothesis unavailable: {_exc}"


@pytest.mark.skipif(not _HYPOTHESIS_AVAILABLE, reason=_HYPO_SKIP)
class TestRoundTripProperty:
    """Property-based round-trip over the full canonical field space."""

    @given(
        bn=st.text(min_size=1, max_size=20),
        legal_name=st.text(max_size=40),
        jurisdiction=st.sampled_from(
            ["CA", "ON", "AB", "BC", "QC", "NS", "MB", "SK"]
        ),
        reg_date=st.one_of(
            st.none(),
            st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 12, 31)),
        ),
        corporate_status=st.sampled_from(list(CorporateStatus)),
        registered_address=st.text(max_size=60),
        registered_postal_code=st.text(max_size=7),
        address_type=st.sampled_from(list(AddressType)),
        unverified_zoning=st.booleans(),
    )
    def test_round_trip_is_lossless(
        self,
        bn,
        legal_name,
        jurisdiction,
        reg_date,
        corporate_status,
        registered_address,
        registered_postal_code,
        address_type,
        unverified_zoning,
    ):
        reg = EmployerRegistration(
            bn=bn,
            legal_name=legal_name,
            jurisdiction=jurisdiction,
            registration_date=reg_date,
            corporate_status=corporate_status,
            registered_address=registered_address,
            registered_postal_code=registered_postal_code,
            address_type=address_type,
            unverified_zoning=unverified_zoning,
        )
        restored = EmployerRegistration.from_dict(reg.to_dict())
        _assert_all_fields_equal(reg, restored)
        # Byte-equivalent JSON after a full re-parse.
        first = json.dumps(reg.to_dict(), sort_keys=True)
        second = json.dumps(restored.to_dict(), sort_keys=True)
        assert first == second
