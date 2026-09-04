"""
services/llm-service/explanation.py
===================================
Bilingual (EN/FR) risk-explanation generation for the EI-ADS LLM Service.

The :class:`ExplanationGenerator` turns an Ensemble-Scorer risk decision and its
supporting indicators into a short, plain-language explanation in **both**
official languages. It implements the LLM explanation pipeline from the design:

1. Rank indicators by ``|shap_contribution|`` descending (ties broken by
   arrival order) and select the top 3.
2. Build a chain-of-thought prompt with an EN/FR bilingual system role and ask
   the LLM to return ``{"en": ..., "fr": ...}``.
3. Enforce a hard 5-second timeout on the LLM call (:func:`asyncio.wait_for`).
   On timeout the result carries the ``EXPLANATION_UNAVAILABLE`` flag.
4. Enforce a 500-character limit per language, truncating at a word boundary.
5. On a malformed / empty LLM response, fall back to a deterministic
   template-based bilingual explanation built from the top indicators.

The bilingual balance metric (Requirement 20.6) is measured by **character
length**, not word count: the generate() result exposes ``char_count_en`` and
``char_count_fr`` for that check (word counts are still returned for
back-compatibility). This was reconciled from word-count to character-length
per an explicit user decision, because French uses more words than English for
comparable content while character length stays comparable.

The generator runs unchanged in production (real Llama chain) and in the
sandbox (``StubLLM`` from ``llm_chain``), because it only depends on the small
``LLMChain.generate(prompt) -> str`` contract.

Requirements: 9.4, 9.5, 9.9, 20.2, 20.3
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional, Sequence

import templates
from llm_chain import LLMChain, build_llm_chain

import uuid
from datetime import datetime, timezone

from shared.models import EngineType, Indicator, RiskDecision, Severity

try:  # LLM call span (shared source of truth; task 32.1 / Req 21.5)
    from shared.telemetry import llm_span
except ImportError:  # pragma: no cover - shared.telemetry always present in repo
    from contextlib import contextmanager as _contextmanager

    @_contextmanager
    def llm_span(*_args, **_kwargs):  # type: ignore[misc]
        yield None

# Maximum characters allowed in each language's explanation (Req 9.5 / 20.3).
MAX_CHARS_PER_LANGUAGE = 500
# Hard wall-clock timeout on the LLM explanation call, in seconds (Req 9.9).
DEFAULT_EXPLANATION_TIMEOUT_S = 5.0
# Number of top indicators to cite in the explanation.
TOP_N_INDICATORS = 3
# Flag surfaced when the LLM call exceeds the timeout budget.
EXPLANATION_UNAVAILABLE = "EXPLANATION_UNAVAILABLE"


def build_indicators_from_payload(
    payload: Sequence[dict[str, Any]],
) -> list[Indicator]:
    """Build :class:`Indicator` objects from loosely-shaped request dicts.

    The Ensemble Scorer emits full ``Indicator.to_dict()`` payloads, but API
    callers may send only the fields the explanation pipeline needs (engine,
    severity, title, shap_contribution). Missing identifiers/timestamps are
    synthesised so ranking and templating always work. Unknown engine/severity
    values fall back to sensible defaults rather than raising.
    """
    indicators: list[Indicator] = []
    for raw in payload or []:
        try:
            engine = EngineType(raw.get("engine"))
        except (ValueError, KeyError):
            engine = EngineType.REPETITIVE_PATTERN
        try:
            severity = Severity(raw.get("severity"))
        except (ValueError, KeyError):
            severity = Severity.LOW

        contribution = raw.get("shap_contribution")
        try:
            contribution = float(contribution) if contribution is not None else None
        except (TypeError, ValueError):
            contribution = None

        indicators.append(
            Indicator(
                indicator_id=_coerce_uuid(raw.get("indicator_id")),
                claim_id=_coerce_uuid(raw.get("claim_id")),
                engine=engine,
                severity=severity,
                algorithm=str(raw.get("algorithm", "")),
                title=str(raw.get("title", "")),
                explanation_en=str(raw.get("explanation_en", "")),
                explanation_fr=str(raw.get("explanation_fr", "")),
                payload=raw.get("payload") or {},
                produced_at=_coerce_datetime(raw.get("produced_at")),
                shap_contribution=contribution,
            )
        )
    return indicators


def _coerce_uuid(value: Any) -> "uuid.UUID":
    """Return a UUID from ``value`` or a fresh UUID4 when it is missing/invalid."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return uuid.uuid4()


def _coerce_datetime(value: Any) -> datetime:
    """Return a datetime from an ISO string or ``now`` when missing/invalid."""
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def _abs_shap(indicator: Indicator) -> float:
    """Return ``|shap_contribution|`` treating ``None`` as 0.0."""
    contribution = indicator.shap_contribution
    if contribution is None:
        return 0.0
    return abs(float(contribution))


def rank_top_indicators(
    indicators: Sequence[Indicator], top_n: int = TOP_N_INDICATORS
) -> list[Indicator]:
    """Rank indicators by ``|shap_contribution|`` desc and return the top ``top_n``.

    Ties are broken by arrival order (stable sort preserves the input order),
    matching the design's "ties broken by Indicator arrival order" rule.
    """
    ranked = sorted(indicators, key=_abs_shap, reverse=True)
    return ranked[:top_n]


def truncate_at_word_boundary(text: str, limit: int = MAX_CHARS_PER_LANGUAGE) -> str:
    """Truncate ``text`` to at most ``limit`` characters at a word boundary.

    If truncation happens mid-text, we cut back to the last whitespace so words
    are never split. If a single token exceeds the limit, we hard-cut at
    ``limit``. The returned string is always ``<= limit`` characters.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = window.rfind(" ")
    if cut <= 0:
        # No whitespace to break on — hard cut.
        return window.rstrip()
    return window[:cut].rstrip()


def count_words(text: str) -> int:
    """Count whitespace-delimited words in ``text``."""
    return len((text or "").split())


def count_chars(text: str) -> int:
    """Count characters in ``text`` (the reconciled P15 balance metric).

    Requirement 20.6 measures bilingual balance by *character length* rather
    than word count: French naturally uses more words than English for
    comparable content, so word count is a linguistically unfair metric, while
    character length reflects the true information volume in each language.
    """
    return len(text or "")


class ExplanationGenerator:
    """Generate bilingual EN/FR risk explanations from indicators + decision."""

    def __init__(
        self,
        chain: Optional[LLMChain] = None,
        timeout_s: float = DEFAULT_EXPLANATION_TIMEOUT_S,
    ) -> None:
        # The chain is built lazily on first use if not injected, so importing
        # this module never loads the multi-gigabyte model.
        self._chain = chain
        self.timeout_s = timeout_s

    @property
    def chain(self) -> LLMChain:
        """Return the (lazily constructed) LLM chain."""
        if self._chain is None:
            self._chain = build_llm_chain()
        return self._chain

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate(
        self,
        indicators: Sequence[Indicator],
        risk_score: int,
        risk_decision: RiskDecision | str,
        language_pair: tuple[str, str] = ("en", "fr"),
    ) -> dict[str, Any]:
        """Produce a bilingual explanation for a scored claim.

        Returns a dict with keys ``en``, ``fr``, ``word_count_en``,
        ``word_count_fr``, ``char_count_en``, ``char_count_fr``. The
        ``char_count_*`` fields carry the reconciled P15 balance metric
        (Requirement 20.6 now measures balance by character length); the
        ``word_count_*`` fields are retained for back-compatibility. On LLM
        timeout the dict additionally carries ``flag == EXPLANATION_UNAVAILABLE``
        and a deterministic template-based explanation so downstream consumers
        always have readable text.
        """
        decision = (
            risk_decision
            if isinstance(risk_decision, RiskDecision)
            else RiskDecision(str(risk_decision))
        )
        top = rank_top_indicators(indicators)

        flag: Optional[str] = None
        raw: Optional[str] = None
        try:
            # Child ``llm.explain_bilingual`` span (design trace structure).
            # Only non-PII metadata is attached. No-op when OTel is unavailable.
            with llm_span(
                "explain_bilingual",
                risk_score=int(risk_score),
                risk_decision=decision.value,
                indicator_count=len(top),
            ):
                raw = await asyncio.wait_for(
                    self._invoke_llm(top, risk_score, decision),
                    timeout=self.timeout_s,
                )
        except asyncio.TimeoutError:
            # Timeout → publish without the LLM text; flag it and fall back.
            flag = EXPLANATION_UNAVAILABLE
            raw = None
        except Exception:  # noqa: BLE001 - any LLM error degrades to template
            raw = None

        parsed = self._parse_llm_json(raw) if raw is not None else None

        if parsed is None:
            # Malformed / empty / timed-out response → deterministic template.
            en, fr = self._template_explanation(top, risk_score, decision)
        else:
            en, fr = parsed

        en = truncate_at_word_boundary(en)
        fr = truncate_at_word_boundary(fr)

        result: dict[str, Any] = {
            "en": en,
            "fr": fr,
            # Retained for back-compatibility; no longer the balance metric.
            "word_count_en": count_words(en),
            "word_count_fr": count_words(fr),
            # Reconciled P15 balance metric (Requirement 20.6): character length.
            "char_count_en": count_chars(en),
            "char_count_fr": count_chars(fr),
        }
        if flag is not None:
            result["flag"] = flag
        return result

    # ------------------------------------------------------------------
    # LLM invocation
    # ------------------------------------------------------------------

    async def _invoke_llm(
        self,
        top_indicators: Sequence[Indicator],
        risk_score: int,
        decision: RiskDecision,
    ) -> str:
        """Run the (blocking) chain generation off the event loop."""
        prompt = self.build_prompt(top_indicators, risk_score, decision)
        return await asyncio.to_thread(self.chain.generate, prompt)

    def build_prompt(
        self,
        top_indicators: Sequence[Indicator],
        risk_score: int,
        decision: RiskDecision,
    ) -> str:
        """Build a chain-of-thought prompt with an EN/FR bilingual system role."""
        indicator_lines = []
        for i, ind in enumerate(top_indicators, start=1):
            contribution = (
                f"{ind.shap_contribution:+.4f}"
                if ind.shap_contribution is not None
                else "n/a"
            )
            indicator_lines.append(
                f"{i}. [{ind.engine.value}/{ind.severity.value}] {ind.title} "
                f"(SHAP {contribution})"
            )
        indicator_block = "\n".join(indicator_lines) or "None"

        system = (
            "You are a bilingual Government of Canada fraud analyst. "
            "Explain the risk decision in plain language, citing the indicators "
            "provided. Reason step by step about which indicators drove the score, "
            "then write one concise explanation in English and one in French. "
            f"Use at most {MAX_CHARS_PER_LANGUAGE} characters per language and keep "
            "the two versions balanced in length. "
            'Return ONLY JSON of the form {"en": "...", "fr": "..."}.'
        )
        user = (
            f"Decision: {decision.value}. Score: {risk_score}/100.\n"
            f"Top indicators (ranked by |SHAP| contribution):\n{indicator_block}"
        )
        return f"<system>\n{system}\n</system>\n<user>\n{user}\n</user>"

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_llm_json(raw: str) -> Optional[tuple[str, str]]:
        """Extract ``(en, fr)`` from an LLM response, or ``None`` if malformed.

        Accepts a JSON object anywhere in the response text (LLMs often wrap
        JSON in prose or code fences). Both ``en`` and ``fr`` must be present
        and non-empty for the parse to succeed.
        """
        if not raw or not raw.strip():
            return None
        # Find the first balanced-looking JSON object in the text.
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        en = data.get("en")
        fr = data.get("fr")
        if not isinstance(en, str) or not isinstance(fr, str):
            return None
        if not en.strip() or not fr.strip():
            return None
        return en.strip(), fr.strip()

    # ------------------------------------------------------------------
    # Deterministic template fallback
    # ------------------------------------------------------------------

    @staticmethod
    def _template_explanation(
        top_indicators: Sequence[Indicator],
        risk_score: int,
        decision: RiskDecision,
    ) -> tuple[str, str]:
        """Build a deterministic bilingual explanation from the top indicators."""
        preamble_en = templates.decision_preamble_en(decision, risk_score)
        preamble_fr = templates.decision_preamble_fr(decision, risk_score)

        if not top_indicators:
            en = f"{preamble_en} {templates._NO_INDICATORS_EN}"
            fr = f"{preamble_fr} {templates._NO_INDICATORS_FR}"
            return en, fr

        phrases_en = [
            templates.engine_phrase_en(ind.engine, ind.title)
            for ind in top_indicators
        ]
        phrases_fr = [
            templates.engine_phrase_fr(ind.engine, ind.title)
            for ind in top_indicators
        ]

        body_en = templates._LEAD_EN + "; ".join(phrases_en) + "."
        body_fr = templates._LEAD_FR + "; ".join(phrases_fr) + "."

        en = f"{preamble_en} {body_en}"
        fr = f"{preamble_fr} {body_fr}"
        return en, fr


__all__ = [
    "ExplanationGenerator",
    "build_indicators_from_payload",
    "rank_top_indicators",
    "truncate_at_word_boundary",
    "count_words",
    "count_chars",
    "MAX_CHARS_PER_LANGUAGE",
    "DEFAULT_EXPLANATION_TIMEOUT_S",
    "TOP_N_INDICATORS",
    "EXPLANATION_UNAVAILABLE",
]
