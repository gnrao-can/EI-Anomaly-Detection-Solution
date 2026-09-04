"""
tests/unit/test_cbsa_adapter.py
===============================
Unit and property tests for the CBSA Adapter (task 21.1).

Coverage:
  * ``detect_absence_periods`` correctness, with emphasis on the 9-vs-10-day
    boundary (Requirement 13.3):
      - 9-day EXIT->ENTRY gap => NO absence.
      - 10-day EXIT->ENTRY gap => absence (inclusive lower bound).
      - Multiple absence periods across a record set.
      - EXIT with no matching ENTRY => no period.
      - ENTRY with no preceding EXIT => ignored.
      - Records are sorted by date regardless of input order.
  * Timeout handling: partial records preserved, NO auto-retry, structured
    failure result (Requirement 13.2).
  * Deterministic synthetic generator per province (Requirement 13.6).
  * Privacy: travel-record CONTENTS are never passed to the logger
    (Requirement 13.4).

The service directory is hyphenated (``services/cbsa-adapter``) and its modules
import each other by bare name, so the directory is placed on ``sys.path`` while
the modules are loaded.

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import date

import pytest

# ---------------------------------------------------------------------------
# Load the hyphenated cbsa-adapter service modules onto sys.path.
# ---------------------------------------------------------------------------

_SERVICE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "services", "cbsa-adapter")
)


# The service modules import each other by BARE name (``from absence import ...``)
# while executing. To satisfy those bare imports without polluting the shared
# ``sys.modules`` namespace (which would collide with other services' ``config``
# / ``client`` / ``main`` modules across the suite), we:
#   1. snapshot any pre-existing bare-name modules,
#   2. register our modules under both a namespaced key and the bare key so
#      intra-package imports resolve,
#   3. restore the snapshot afterwards, leaving only the namespaced ``cbsa_*``
#      entries behind.
_BARE_NAMES = ("absence", "config", "client", "main")
_SAVED = {name: sys.modules.get(name) for name in _BARE_NAMES}


def _load_service_modules():
    if os.path.isdir(_SERVICE_DIR) and _SERVICE_DIR not in sys.path:
        sys.path.insert(0, _SERVICE_DIR)

    loaded: dict[str, Any] = {}
    for bare, filename in (
        ("absence", "absence.py"),
        ("config", "config.py"),
        ("client", "client.py"),
        ("main", "main.py"),
    ):
        spec = importlib.util.spec_from_file_location(
            bare, os.path.join(_SERVICE_DIR, filename)
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # Register under the bare name so sibling ``from X import Y`` works, and
        # under a namespaced key so tests can reference it stably.
        sys.modules[bare] = module
        sys.modules[f"cbsa_{bare}"] = module
        spec.loader.exec_module(module)
        loaded[bare] = module

    # Restore the pre-existing bare-name modules so other test files that also
    # load a bare ``config`` / ``client`` / ``main`` are not affected by ours.
    for name, saved in _SAVED.items():
        if saved is not None:
            sys.modules[name] = saved
        else:
            sys.modules.pop(name, None)

    return loaded


from typing import Any  # noqa: E402  (used by _load_service_modules annotation)

_loaded = _load_service_modules()
_absence = _loaded["absence"]
_config = _loaded["config"]
_client = _loaded["client"]
_main = _loaded["main"]

CrossingRecord = _absence.CrossingRecord
AbsencePeriod = _absence.AbsencePeriod
detect_absence_periods = _absence.detect_absence_periods
MIN_ABSENCE_DAYS = _absence.MIN_ABSENCE_DAYS

SyntheticCBSAGenerator = _client.SyntheticCBSAGenerator
CBSAFetchError = _client.CBSAFetchError


def _override_auth() -> None:
    """Inject an authenticated INVESTIGATOR so endpoint tests exercise the
    handler rather than the task-29.1 GCKey auth gate (the 401/403 boundary is
    covered in tests/unit/test_auth_service_wiring.py)."""
    from shared.auth import get_current_user  # noqa: PLC0415

    _main.app.dependency_overrides[get_current_user] = lambda: {
        "roles": ["INVESTIGATOR"]
    }

try:
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False

try:
    from hypothesis import given, settings as hyp_settings
    from hypothesis import strategies as st

    _HAS_HYPOTHESIS = True
except Exception:  # noqa: BLE001
    _HAS_HYPOTHESIS = False


def _exit(d: str) -> CrossingRecord:
    return CrossingRecord(direction="EXIT", crossing_date=date.fromisoformat(d), port_of_entry="YYZ")


def _entry(d: str) -> CrossingRecord:
    return CrossingRecord(direction="ENTRY", crossing_date=date.fromisoformat(d), port_of_entry="YYZ")


# ===========================================================================
# detect_absence_periods — the 9 vs 10 day boundary and other cases (13.3)
# ===========================================================================


class TestDetectAbsencePeriods:
    def test_nine_day_gap_is_not_an_absence(self):
        """A 9-day EXIT->ENTRY gap does NOT qualify (boundary just below 10)."""
        records = [_exit("2024-01-01"), _entry("2024-01-10")]  # 9 days
        assert detect_absence_periods(records) == []

    def test_ten_day_gap_is_an_absence(self):
        """A 10-day EXIT->ENTRY gap qualifies (inclusive lower bound)."""
        records = [_exit("2024-01-01"), _entry("2024-01-11")]  # 10 days
        periods = detect_absence_periods(records)
        assert len(periods) == 1
        assert periods[0] == AbsencePeriod(
            start=date(2024, 1, 1), end=date(2024, 1, 11), days=10
        )

    def test_boundary_exactly_min_absence_days(self):
        """The threshold constant and the detected period agree at the boundary."""
        assert MIN_ABSENCE_DAYS == 10
        records = [_exit("2024-03-01"), _entry("2024-03-11")]
        assert detect_absence_periods(records)[0].days == MIN_ABSENCE_DAYS

    def test_multiple_absence_periods(self):
        """Several EXIT->ENTRY pairs each produce their own period, in order."""
        records = [
            _exit("2024-01-01"), _entry("2024-01-20"),   # 19 days -> absence
            _exit("2024-03-01"), _entry("2024-03-05"),   # 4 days  -> no
            _exit("2024-06-01"), _entry("2024-06-11"),   # 10 days -> absence
        ]
        periods = detect_absence_periods(records)
        assert [p.days for p in periods] == [19, 10]
        assert [p.start for p in periods] == [date(2024, 1, 1), date(2024, 6, 1)]

    def test_exit_with_no_matching_entry_produces_no_period(self):
        """A trailing EXIT with no following ENTRY yields nothing."""
        records = [_exit("2024-01-01"), _entry("2024-01-15"), _exit("2024-04-01")]
        periods = detect_absence_periods(records)
        assert len(periods) == 1  # only the first, closed pair
        assert periods[0].days == 14

    def test_entry_with_no_preceding_exit_is_ignored(self):
        """A leading ENTRY with no prior EXIT opens no period."""
        records = [_entry("2024-01-05"), _exit("2024-02-01"), _entry("2024-02-15")]
        periods = detect_absence_periods(records)
        assert len(periods) == 1
        assert periods[0].start == date(2024, 2, 1)
        assert periods[0].days == 14

    def test_records_sorted_regardless_of_input_order(self):
        """Out-of-order records are sorted by crossing_date before scanning."""
        ordered = [_exit("2024-01-01"), _entry("2024-01-12")]
        shuffled = [_entry("2024-01-12"), _exit("2024-01-01")]
        assert detect_absence_periods(shuffled) == detect_absence_periods(ordered)

    def test_empty_input_returns_empty(self):
        assert detect_absence_periods([]) == []

    def test_does_not_mutate_input(self):
        records = [_entry("2024-01-12"), _exit("2024-01-01")]
        snapshot = list(records)
        detect_absence_periods(records)
        assert records == snapshot

    def test_double_exit_uses_most_recent_exit(self):
        """Two EXITs with no ENTRY between: the later EXIT pairs with the ENTRY."""
        records = [_exit("2024-01-01"), _exit("2024-01-05"), _entry("2024-01-20")]
        periods = detect_absence_periods(records)
        assert len(periods) == 1
        assert periods[0].start == date(2024, 1, 5)  # most recent EXIT
        assert periods[0].days == 15


# ===========================================================================
# Property test — the boundary holds for arbitrary gaps (13.3)
# ===========================================================================

if _HAS_HYPOTHESIS:

    class TestDetectAbsencePropertyBoundary:
        @hyp_settings(max_examples=200)
        @given(gap=st.integers(min_value=0, max_value=400))
        def test_gap_qualifies_iff_at_least_ten_days(self, gap):
            """A single EXIT->ENTRY pair is an absence iff gap >= 10 days."""
            from datetime import timedelta

            start = date(2024, 1, 1)
            end = start + timedelta(days=gap)
            records = [
                CrossingRecord("EXIT", start, "YYZ"),
                CrossingRecord("ENTRY", end, "YYZ"),
            ]
            periods = detect_absence_periods(records)
            if gap >= 10:
                assert len(periods) == 1
                assert periods[0].days == gap
            else:
                assert periods == []


# ===========================================================================
# Deterministic synthetic generator per province (13.6)
# ===========================================================================


class TestSyntheticGenerator:
    def _gen(self) -> "SyntheticCBSAGenerator":
        mock_dir = os.path.abspath(
            os.path.join(_SERVICE_DIR, "..", "..", "mock-data", "cbsa")
        )
        return SyntheticCBSAGenerator(mock_dir)

    def test_deterministic_same_inputs_same_output(self):
        gen = self._gen()
        a = gen.generate("hash-abc", "BC")
        b = gen.generate("hash-abc", "BC")
        assert [r.to_dict() for r in a] == [r.to_dict() for r in b]

    def test_different_provinces_differ(self):
        gen = self._gen()
        bc = gen.generate("hash-abc", "BC")
        nl = gen.generate("hash-abc", "NL")
        # NL fixture has zero trips; BC has trips -> the outputs must differ.
        assert [r.to_dict() for r in bc] != [r.to_dict() for r in nl]

    def test_no_trips_province_returns_empty(self):
        gen = self._gen()
        assert gen.generate("hash-abc", "NL") == []

    def test_explicit_records_fixture_used_verbatim(self):
        """The ON fixture pins explicit records exercising the 9/10-day boundary."""
        gen = self._gen()
        records = gen.generate("any-hash", "ON")
        periods = detect_absence_periods(records)
        # ON.json: one 10-day trip (qualifies) + one 9-day trip (does not).
        assert len(periods) == 1
        assert periods[0].days == 10

    def test_different_sin_hash_may_differ_but_stays_deterministic(self):
        gen = self._gen()
        a1 = gen.generate("hash-1", "BC")
        a2 = gen.generate("hash-1", "BC")
        b1 = gen.generate("hash-2", "BC")
        assert [r.to_dict() for r in a1] == [r.to_dict() for r in a2]  # deterministic
        # Not asserting a1 != b1 strictly (seed collisions possible) but the
        # generator must still be reproducible for each.
        assert [r.to_dict() for r in b1] == [
            r.to_dict() for r in gen.generate("hash-2", "BC")
        ]


# ===========================================================================
# Endpoint: synthetic happy path + privacy no-logging (13.4, 13.5, 13.6)
# ===========================================================================


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestTravelEndpoint:
    def _client(self):
        # Ensure synthetic mode + a real mock dir on app.state.
        mock_dir = os.path.abspath(
            os.path.join(_SERVICE_DIR, "..", "..", "mock-data", "cbsa")
        )
        _main.app.state.settings = _main.get_settings()
        _main.app.state.synthetic = SyntheticCBSAGenerator(mock_dir)
        _main.app.state.cbsa_client = None
        _override_auth()
        return TestClient(_main.app)

    def teardown_method(self):
        _main.app.dependency_overrides.clear()

    def test_travel_returns_records_and_absences(self):
        resp = self._client().get("/cbsa/travel/hash-abc", params={"province": "ON"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "OK"
        assert body["partial"] is False
        assert body["record_count"] == 4
        # ON fixture -> exactly one qualifying (10-day) absence period.
        assert len(body["absence_periods"]) == 1
        assert body["absence_periods"][0]["days"] == 10

    def test_healthz_ok_in_synthetic_mode(self):
        resp = self._client().get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["mode"] == "synthetic"

    def test_travel_contents_not_logged(self, caplog):
        """Requirement 13.4 — crossing contents must never reach the logger.

        We capture all log records emitted while serving a request that returns
        real crossing data, then assert no crossing date, direction, or port
        appears in any log message or its arguments.
        """
        import logging as _logging

        client = self._client()
        with caplog.at_level(_logging.DEBUG, logger="cbsa-adapter"):
            resp = client.get("/cbsa/travel/hash-privacy", params={"province": "ON"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["record_count"] == 4  # data really was returned

        # Content tokens that must NOT appear anywhere in the captured logs.
        forbidden = {"2024-02-01", "2024-02-11", "2024-05-01", "2024-05-10", "YYZ"}
        forbidden |= {"EXIT", "ENTRY"}

        for record in caplog.records:
            haystack = record.getMessage()
            # Include structured 'extra' fields and any positional args.
            extras = " ".join(str(v) for v in getattr(record, "__dict__", {}).values())
            combined = f"{haystack} {extras}"
            for token in forbidden:
                assert token not in combined, (
                    f"Travel content '{token}' leaked into a log record"
                )


# ===========================================================================
# Timeout / failure: partial records preserved, no retry, structured result
# (13.2)
# ===========================================================================


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not importable")
class TestChannelFailureHandling:
    """Uses a fake client injected on app.state to simulate the mTLS channel."""

    def _client_with_fake(self, fake):
        # Force non-synthetic behaviour by pointing the endpoint at the fake
        # client and flipping the settings to a production-like mode.
        class _ProdSettings:
            cbsa_mock_data_dir = _main.get_settings().cbsa_mock_data_dir
            use_synthetic = False

        _main.app.state.settings = _ProdSettings()
        _main.app.state.cbsa_client = fake
        _override_auth()
        return TestClient(_main.app)

    def teardown_method(self):
        _main.app.dependency_overrides.clear()

    def test_timeout_preserves_partial_records_and_returns_failure(self):
        """A channel failure carrying partial records -> 502 with those records,
        no retry."""
        partial = [
            CrossingRecord("EXIT", date(2024, 1, 1), "YYZ"),
            CrossingRecord("ENTRY", date(2024, 1, 15), "YYZ"),
        ]

        class _FailingClient:
            def __init__(self):
                self.calls = 0

            def fetch_travel_records(self, sin_hash):
                self.calls += 1
                err = CBSAFetchError("CBSA channel fetch failed: TimeoutException")
                # Attach partial records the way a real interrupted read would.
                err.partial_records = partial
                raise err

            def close(self):
                pass

        fake = _FailingClient()
        resp = self._client_with_fake(fake).get("/cbsa/travel/hash-t")
        assert resp.status_code == 502
        body = resp.json()
        assert body["status"] == "FAILED"
        assert body["partial"] is True
        # Partial records were preserved and returned.
        assert body["record_count"] == 2
        assert len(body["records"]) == 2
        # Absence detection still ran over the partial records (14-day gap).
        assert len(body["absence_periods"]) == 1
        assert body["absence_periods"][0]["days"] == 14
        # NO auto-retry: exactly one attempt was made.
        assert fake.calls == 1

    def test_failure_reason_is_content_free(self):
        """The failure reason must not embed travel-record contents."""

        class _FailingClient:
            def fetch_travel_records(self, sin_hash):
                raise CBSAFetchError("CBSA channel fetch failed: ConnectError")

            def close(self):
                pass

        resp = self._client_with_fake(_FailingClient()).get("/cbsa/travel/hash-r")
        assert resp.status_code == 502
        reason = resp.json()["reason"]
        assert "YYZ" not in reason and "EXIT" not in reason and "ENTRY" not in reason

    def test_channel_unavailable_when_no_client(self):
        """No constructed mTLS client in production mode -> structured failure."""
        resp = self._client_with_fake(None).get("/cbsa/travel/hash-x")
        assert resp.status_code == 502
        body = resp.json()
        assert body["reason"] == "CHANNEL_UNAVAILABLE"
        assert body["partial"] is True
        assert body["record_count"] == 0
