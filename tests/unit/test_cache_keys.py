"""
tests/unit/test_cache_keys.py
=============================
Unit tests for shared/cache_keys.py.

Verifies that:
  - each builder function produces the exact key string from the design, and
  - each TTL constant has the correct value (in seconds).

Requirements: 17.4, 23.5
"""

from __future__ import annotations

from shared import cache_keys as ck


# ===========================================================================
# TTL constants
# ===========================================================================


class TestTtlConstants:
    """The TTLs must match the design's Redis caching strategy table."""

    def test_statscan_lfs_ttl_is_30_days(self):
        assert ck.STATSCAN_LFS_TTL_SECONDS == 30 * 24 * 3600

    def test_cra_t4_ttl_is_24_hours(self):
        assert ck.CRA_T4_TTL_SECONDS == 24 * 3600

    def test_geonames_coord_ttl_is_90_days(self):
        assert ck.GEONAMES_COORD_TTL_SECONDS == 90 * 24 * 3600

    def test_geonames_dist_ttl_is_7_days(self):
        assert ck.GEONAMES_DIST_TTL_SECONDS == 7 * 24 * 3600

    def test_biz_reg_employer_ttl_is_24_hours(self):
        assert ck.BIZ_REG_EMPLOYER_TTL_SECONDS == 24 * 3600

    def test_mvr_ownership_ttl_is_1_hour(self):
        assert ck.MVR_OWNERSHIP_TTL_SECONDS == 3600

    def test_job_bank_postings_ttl_is_7_days(self):
        assert ck.JOB_BANK_POSTINGS_TTL_SECONDS == 7 * 24 * 3600

    def test_cbsa_travel_ttl_is_1_hour(self):
        assert ck.CBSA_TRAVEL_TTL_SECONDS == 3600

    def test_orchestrator_pairing_ttl_is_60_seconds(self):
        assert ck.ORCHESTRATOR_PAIRING_TTL_SECONDS == 60


# ===========================================================================
# Key builders
# ===========================================================================


class TestKeyBuilders:
    """Each builder must produce the exact documented key string."""

    def test_statscan_lfs_key(self):
        assert ck.statscan_lfs_key("35", "2026-03") == "statscan:lfs:35:2026-03"

    def test_cra_t4_key(self):
        assert (
            ck.cra_t4_key("abc123", "123456789RT0001", 2025)
            == "cra:t4:abc123:123456789RT0001:2025"
        )

    def test_cra_t4_key_accepts_string_year(self):
        assert (
            ck.cra_t4_key("abc123", "123456789RT0001", "2025")
            == "cra:t4:abc123:123456789RT0001:2025"
        )

    def test_geonames_coord_key(self):
        assert ck.geonames_coord_key("K1A") == "geonames:coord:K1A"

    def test_biz_reg_employer_key(self):
        assert (
            ck.biz_reg_employer_key("123456789RT0001")
            == "biz_reg:employer:123456789RT0001"
        )

    def test_mvr_ownership_key(self):
        assert ck.mvr_ownership_key("sinhash", "ON") == "mvr:ownership:sinhash:ON"

    def test_job_bank_postings_key(self):
        assert (
            ck.job_bank_postings_key("21234", "35", "2026-03")
            == "job_bank:postings:21234:35:2026-03"
        )

    def test_cbsa_travel_key(self):
        assert (
            ck.cbsa_travel_key("sinhash", "periodhash")
            == "cbsa:travel:sinhash:periodhash"
        )

    def test_orchestrator_pairing_key(self):
        claim_id = "11111111-2222-3333-4444-555555555555"
        assert (
            ck.orchestrator_pairing_key(claim_id)
            == f"orchestrator:pairing:{claim_id}"
        )


# ===========================================================================
# GeoNames distance key — symmetry
# ===========================================================================


class TestGeonamesDistKey:
    """The distance key must be symmetric and follow the documented prefix."""

    def test_prefix_and_shape(self):
        key = ck.geonames_dist_key(45.0, -75.0, 43.0, -79.0)
        assert key.startswith("geonames:dist:")
        # 16-char hex hash appended after the prefix
        suffix = key[len("geonames:dist:") :]
        assert len(suffix) == 16
        int(suffix, 16)  # must be valid hex

    def test_symmetric_key(self):
        forward = ck.geonames_dist_key(45.0, -75.0, 43.0, -79.0)
        reverse = ck.geonames_dist_key(43.0, -79.0, 45.0, -75.0)
        assert forward == reverse

    def test_self_pair_stable(self):
        a = ck.geonames_dist_key(45.0, -75.0, 45.0, -75.0)
        b = ck.geonames_dist_key(45.0, -75.0, 45.0, -75.0)
        assert a == b

    def test_distinct_pairs_differ(self):
        assert ck.geonames_dist_key(
            45.0, -75.0, 43.0, -79.0
        ) != ck.geonames_dist_key(45.0, -75.0, 40.0, -80.0)


# ===========================================================================
# Templates
# ===========================================================================


class TestTemplateConstants:
    """Template constants must expose the documented placeholder patterns."""

    def test_templates_render_via_format(self):
        assert (
            ck.STATSCAN_LFS.format(region_code="35", year_month="2026-03")
            == "statscan:lfs:35:2026-03"
        )
        assert ck.GEONAMES_COORD.format(postal_code="K1A") == "geonames:coord:K1A"
        assert (
            ck.ORCHESTRATOR_PAIRING.format(claim_id="cid")
            == "orchestrator:pairing:cid"
        )
