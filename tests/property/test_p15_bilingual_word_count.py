"""
tests/property/test_p15_bilingual_word_count.py
===============================================
Property 15: Bilingual character-length balance. Validates: Requirements 20.6

Metric reconciliation note
--------------------------
This property was **reconciled from word-count balance to character-length
balance** per an explicit user decision. French naturally uses more words than
English for comparable content (~1.3x in the fallback templates) but comparable
character length, which makes word count a linguistically unfair metric.
Requirement 20.6 now measures the English/French balance by **character
length**. The filename still references "word_count" for historical/back-compat
reasons, but every assertion below uses character length.

Statement
---------
*For any* claim scored by the Ensemble_Scorer and explained by the LLM_Service,
the English and French explanations SHALL be balanced in length: the relative
difference in their character counts stays within a 20% band:

    max(char_count_en, char_count_fr) == 0
        OR
    |char_count_en - char_count_fr| / max(char_count_en, char_count_fr) <= 0.20

This module exercises the invariant on the bilingual output produced by
:meth:`explanation.ExplanationGenerator.generate` across two paths, both of
which are designed to be length-balanced:

  1. **Proportional-mock LLM path** — a mock ``LLMChain`` is injected into the
     generator so it returns a JSON ``{"en": ..., "fr": ...}`` where the French
     text is a linguistically-typical ~1.05x the English *character* length
     (French runs slightly longer than English). This is the "LLM succeeded"
     branch of the pipeline.

  2. **Template-fallback path** — the same generator is driven with a mock LLM
     that returns malformed output, forcing the deterministic bilingual template
     in ``templates.py``. The EN/FR templates are built from parallel sentence
     structures, so they stay balanced in character length. This is the
     deterministic sandbox path.

For every generated Indicator set we also assert that both languages are
non-empty (for non-empty Indicator sets) and that the 500-character per-language
cap (Req 9.5 / 20.3) is respected.

Generators randomise the engine (:class:`EngineType`), severity
(:class:`Severity`), and ``shap_contribution`` of each Indicator. Every
Indicator gets a fresh UUID and all Indicators in a set share a single
``claim_id`` (as they would for a real claim).

Sandbox note
------------
The LLM service directory contains a hyphen, so it is not importable as a
package; we add it to ``sys.path`` (as the sibling llm-service tests do).

Property 15: Bilingual character-length balance. Validates: Requirements 20.6
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# --- Make the llm-service package importable ---------------------------------
_SERVICE_DIR = Path(__file__).resolve().parents[2] / "services" / "llm-service"
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

from shared.models import (  # noqa: E402
    EngineType,
    Indicator,
    RiskDecision,
    Severity,
)

import explanation as explanation_module  # noqa: E402
from explanation import (  # noqa: E402
    MAX_CHARS_PER_LANGUAGE,
    ExplanationGenerator,
    count_chars,
)


# ---------------------------------------------------------------------------
# Hypothesis profile: CI runs 200 examples (design.md — Testing Strategy;
# tasks.md Notes: settings.register_profile("ci", max_examples=200, ...)).
#
# Registration is guarded so a duplicate registration (from a conftest or a
# sibling property-test module already loaded in the same session) does not
# raise. We always (re)load the "ci" profile so this file runs 200 examples.
# ---------------------------------------------------------------------------
try:
    settings.register_profile(
        "ci", max_examples=200, suppress_health_check=[HealthCheck.too_slow]
    )
except Exception:  # noqa: BLE001 - already registered elsewhere
    pass
settings.load_profile("ci")


# The P15 acceptance band: relative character-length difference must be within
# 20% (reconciled metric — see module docstring).
BALANCE_BAND = 0.20
# The mock LLM models French as ~1.05x English *characters* (linguistically
# typical and comfortably inside the 20% band).
FR_EN_CHAR_RATIO = 1.05


# ---------------------------------------------------------------------------
# Mock LLM chains
# ---------------------------------------------------------------------------


class ProportionalMockChain:
    """Mock ``LLMChain`` that returns a length-balanced bilingual JSON payload.

    The English text is derived from the prompt; the French text mirrors it with
    ``FR_EN_CHAR_RATIO`` (~1.05x) the *character* length, which is linguistically
    typical (French runs slightly longer than English) and stays within the 20%
    balance band. Output is JSON of the form ``{"en": "...", "fr": "..."}`` so it
    flows through the generator's normal parse path rather than the template
    fallback.
    """

    is_stub = True

    def generate(self, prompt: str) -> str:  # noqa: D401 - simple contract
        # Base English char count derived deterministically from the prompt so
        # different indicator sets yield different lengths. Kept modest so that
        # after truncation both languages remain well under the 500-char cap and
        # the ratio is preserved.
        base_en_chars = 60 + (len(prompt) % 40)
        en_text = "r" * base_en_chars
        fr_char_count = round(base_en_chars * FR_EN_CHAR_RATIO)
        fr_text = "q" * fr_char_count
        payload = {"en": en_text, "fr": fr_text}
        return json.dumps(payload)


class MalformedMockChain:
    """Mock ``LLMChain`` returning unparseable text, forcing the template path.

    The generator treats any non-JSON / empty response as a malformed LLM
    output and falls back to the deterministic bilingual template in
    ``templates.py``.
    """

    is_stub = True

    def generate(self, prompt: str) -> str:  # noqa: D401 - simple contract
        return "not-json: the model returned prose without any JSON object"


# ---------------------------------------------------------------------------
# Generators — build valid Indicator objects (shared/models.py)
# ---------------------------------------------------------------------------

_ENGINES = list(EngineType)
_SEVERITIES = list(Severity)
_DECISIONS = list(RiskDecision)


def _make_indicator(draw: st.DrawFn, claim_id: uuid.UUID, now: datetime) -> Indicator:
    """Draw a single valid :class:`Indicator` with randomised fields."""
    engine = draw(st.sampled_from(_ENGINES))
    severity = draw(st.sampled_from(_SEVERITIES))
    shap_contribution = draw(
        st.one_of(
            st.none(),
            st.floats(
                min_value=-50.0,
                max_value=50.0,
                allow_nan=False,
                allow_infinity=False,
            ),
        )
    )
    return Indicator(
        indicator_id=uuid.uuid4(),
        claim_id=claim_id,
        engine=engine,
        severity=severity,
        algorithm=draw(st.text(min_size=0, max_size=40)),
        title=draw(st.text(min_size=0, max_size=60)),
        explanation_en=draw(st.text(min_size=0, max_size=80)),
        explanation_fr=draw(st.text(min_size=0, max_size=80)),
        payload={},
        produced_at=now,
        shap_contribution=shap_contribution,
    )


@st.composite
def indicator_set_strategy(draw, min_size: int = 0, max_size: int = 10):
    """Generate a list of 0..``max_size`` valid :class:`Indicator` objects.

    All Indicators in a set share a single ``claim_id`` (as they would for a
    real claim); each has its own UUID and randomised engine/severity/fields.
    """
    count = draw(st.integers(min_value=min_size, max_value=max_size))
    claim_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    return [_make_indicator(draw, claim_id, now) for _ in range(count)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_generate(
    generator: ExplanationGenerator,
    indicators: Sequence[Indicator],
    risk_score: int,
    decision: RiskDecision,
) -> dict[str, Any]:
    """Drive the async generator synchronously and return its result dict."""
    return asyncio.run(
        generator.generate(indicators, risk_score, decision)
    )


def _assert_balance(result: dict[str, Any], *, layer: str, n: int) -> None:
    """Assert the P15 character-length balance invariant on a generator result."""
    en_chars = result["char_count_en"]
    fr_chars = result["char_count_fr"]
    # Sanity: the reported char counts match the returned text.
    assert en_chars == count_chars(result["en"])
    assert fr_chars == count_chars(result["fr"])

    largest = max(en_chars, fr_chars)
    if largest == 0:
        # Both empty => balanced by definition (only possible for empty sets).
        return
    ratio = abs(en_chars - fr_chars) / largest
    assert ratio <= BALANCE_BAND, (
        f"Bilingual character-length balance violated ({layer}): "
        f"|{en_chars} - {fr_chars}| / {largest} = {ratio:.4f} > {BALANCE_BAND} "
        f"for {n} indicators"
    )


def _assert_caps_and_nonempty(
    result: dict[str, Any], *, indicator_count: int, layer: str
) -> None:
    """Assert the 500-char cap holds and both languages are non-empty."""
    assert len(result["en"]) <= MAX_CHARS_PER_LANGUAGE, (
        f"EN exceeds {MAX_CHARS_PER_LANGUAGE} chars ({layer}): {len(result['en'])}"
    )
    assert len(result["fr"]) <= MAX_CHARS_PER_LANGUAGE, (
        f"FR exceeds {MAX_CHARS_PER_LANGUAGE} chars ({layer}): {len(result['fr'])}"
    )
    # For any non-empty indicator set both languages must carry text.
    if indicator_count > 0:
        assert result["en"].strip(), f"EN empty for non-empty set ({layer})"
        assert result["fr"].strip(), f"FR empty for non-empty set ({layer})"


# ---------------------------------------------------------------------------
# Property 15 — proportional-mock LLM path
# ---------------------------------------------------------------------------


@given(
    indicator_set=indicator_set_strategy(min_size=0, max_size=10),
    risk_score=st.integers(min_value=0, max_value=100),
    decision=st.sampled_from(_DECISIONS),
)
def test_P15_char_length_balance_proportional_mock(indicator_set, risk_score, decision):
    """Property 15: Bilingual character-length balance. Validates: Requirements 20.6

    With a mock LLM that emits proportional EN/FR pairs (FR ~1.05x EN chars),
    the generated explanation satisfies
    ``|en_chars - fr_chars| / max(en_chars, fr_chars) <= 0.20``.
    """
    generator = ExplanationGenerator(chain=ProportionalMockChain())
    result = _run_generate(generator, indicator_set, risk_score, decision)

    _assert_balance(result, layer="proportional-mock", n=len(indicator_set))
    _assert_caps_and_nonempty(
        result, indicator_count=len(indicator_set), layer="proportional-mock"
    )


# ---------------------------------------------------------------------------
# Property 15 — deterministic template-fallback path
# ---------------------------------------------------------------------------


@given(
    indicator_set=indicator_set_strategy(min_size=0, max_size=10),
    risk_score=st.integers(min_value=0, max_value=100),
    decision=st.sampled_from(_DECISIONS),
)
def test_P15_char_length_balance_template_fallback(indicator_set, risk_score, decision):
    """Property 15: Bilingual character-length balance. Validates: Requirements 20.6

    When the LLM response is malformed, the generator falls back to the
    deterministic bilingual template. The EN/FR templates are length-balanced in
    character length, so the 20% balance invariant holds.
    """
    generator = ExplanationGenerator(chain=MalformedMockChain())
    result = _run_generate(generator, indicator_set, risk_score, decision)

    _assert_balance(result, layer="template-fallback", n=len(indicator_set))
    _assert_caps_and_nonempty(
        result, indicator_count=len(indicator_set), layer="template-fallback"
    )


# ---------------------------------------------------------------------------
# Edge case: empty Indicator set still yields balanced, non-empty EN/FR text.
# ---------------------------------------------------------------------------


def test_P15_empty_indicator_set_is_balanced():
    """Property 15: Bilingual character-length balance. Validates: Requirements 20.6

    The empty Indicator set still produces a balanced bilingual explanation via
    the "no indicators" template (both preamble and no-indicators sentences are
    parallel EN/FR), well within the 20% band and under the 500-char cap.
    """
    generator = ExplanationGenerator(chain=MalformedMockChain())
    result = _run_generate(generator, [], 0, RiskDecision.AUTO_APPROVE)

    assert result["en"].strip()
    assert result["fr"].strip()
    _assert_balance(result, layer="empty-template", n=0)
    assert len(result["en"]) <= MAX_CHARS_PER_LANGUAGE
    assert len(result["fr"]) <= MAX_CHARS_PER_LANGUAGE
