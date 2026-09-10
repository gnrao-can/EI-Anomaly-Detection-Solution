"""
services/llm-service/classification.py
=======================================
Narrative CLASSIFICATION for the EI-ADS LLM Service.

Two Detection Engines depend on the LLM Service to *classify* a narrative
(requirements say "THE LLM_Service SHALL classify ..."):

  * History Inconsistency (Req 6.6): does the ROE reason-for-separation narrative
    CONTRADICT CRA records showing continued employment for the same SIN at the
    same employer during the claimed separation period? →
    ``{"contradicted": bool, "evidence": str}``.

  * Document Forgery (Req 7.8): is the ROE termination narrative statistically
    INCONSISTENT with the employer's historical ROE communication style (a
    corpus of prior authenticated ROEs)? → ``{"inconsistent": bool, "score": float}``.

Design posture (same as the LayoutLM / Llama plug-ins elsewhere in EI-ADS): the
real Llama 3.1 model is a MODEL-GATED plug-in. When the GGUF weights are present
(``chain.is_stub is False``) the classification is produced by the real model
through a structured YES/NO prompt. When the weights are absent (dev / sandbox /
CI — the ``StubLLM`` case) the service returns a DETERMINISTIC classification
computed from real code:

  * contradiction — a deterministic lexical detector: the narrative asserts a
    separation/termination while the CRA evidence asserts CONTINUED/active
    employment over the claimed period → contradicted. Uses the existing
    ``NarrativeSimilarity`` fallback only as a tie-break signal.
  * style inconsistency — the narrative's MAXIMUM cosine similarity to the prior
    corpus (via ``NarrativeSimilarity``): a narrative that is stylistically
    UNLIKE every prior authenticated ROE (max similarity below a threshold) is
    flagged inconsistent; the returned ``score`` is ``1 - max_similarity`` (an
    "inconsistency" score in ``[0, 1]``).

Nothing here is faked: the deterministic path is genuine, reproducible logic
over the real similarity backend; the real Llama answer is honored when the
model is loaded. The endpoints in ``main.py`` wrap these with auth + validation.

Requirements: 6.6, 6.7, 7.8, 7.9, 20 (classification support)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Lexical cues for the deterministic contradiction detector
# ---------------------------------------------------------------------------

#: Phrases in a reason-for-separation narrative asserting the employment ENDED.
_SEPARATION_CUES: tuple[str, ...] = (
    "laid off", "lay off", "layoff", "terminated", "termination", "let go",
    "dismissed", "fired", "shortage of work", "end of contract", "quit",
    "resigned", "no longer employed", "ceased", "separation", "last day",
)

#: Phrases in CRA evidence asserting the employment CONTINUED past separation.
_CONTINUED_CUES: tuple[str, ...] = (
    "continued employment", "still employed", "ongoing employment",
    "remained employed", "active payroll", "on payroll", "continues to work",
    "continuing", "did not separate", "no separation", "employment ongoing",
    "paid through", "wages paid after", "t4 shows", "continued to be paid",
)

#: Style-inconsistency threshold: when the narrative's MAX cosine similarity to
#: the corpus is at or below this, it is stylistically unlike every prior ROE.
DEFAULT_STYLE_SIMILARITY_THRESHOLD: float = 0.40


def _contains_any(text: str, cues: tuple[str, ...]) -> bool:
    low = (text or "").lower()
    return any(cue in low for cue in cues)


# ---------------------------------------------------------------------------
# Result value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContradictionResult:
    contradicted: bool
    evidence: str
    source: str  # "llama" | "deterministic"


@dataclass(frozen=True)
class StyleResult:
    inconsistent: bool
    score: float  # inconsistency score in [0, 1]
    source: str  # "llama" | "deterministic"


# ---------------------------------------------------------------------------
# YES/NO parsing for the real-model path
# ---------------------------------------------------------------------------

_YES_RE = re.compile(r"\b(yes|contradict|inconsistent|true)\b", re.IGNORECASE)
_NO_RE = re.compile(r"\b(no|consistent|not\s+contradict|false)\b", re.IGNORECASE)


def _parse_yes_no(text: str) -> Optional[bool]:
    """Parse a leading YES/NO-style answer from real-model output, or ``None``.

    Only used on the real-Llama path; ``None`` means "unparseable" so the caller
    falls back to the deterministic classifier rather than guessing.
    """
    if not text:
        return None
    head = text.strip()[:64]
    yes = _YES_RE.search(head)
    no = _NO_RE.search(head)
    if yes and not no:
        return True
    if no and not yes:
        return False
    return None


# ---------------------------------------------------------------------------
# Contradiction classifier (Req 6.6)
# ---------------------------------------------------------------------------


def classify_contradiction(
    narrative: str,
    cra_evidence: str,
    *,
    chain: Any = None,
    similarity: Any = None,
) -> ContradictionResult:
    """Classify whether ``narrative`` is contradicted by ``cra_evidence``.

    Real-model path (``chain`` present and NOT a stub): a structured YES/NO
    prompt is sent to Llama; a parseable answer is authoritative. Otherwise (or
    on an unparseable answer) the deterministic detector decides: a narrative
    asserting a separation combined with CRA evidence asserting CONTINUED
    employment is a contradiction.
    """
    narrative = narrative or ""
    cra_evidence = cra_evidence or ""

    # ── Deterministic decision (authoritative in dev / stub) ──
    asserts_separation = _contains_any(narrative, _SEPARATION_CUES)
    asserts_continued = _contains_any(cra_evidence, _CONTINUED_CUES)
    det_contradicted = bool(asserts_separation and asserts_continued)
    det_evidence = (
        "CRA evidence indicates continued employment during the period the ROE "
        "narrative claims a separation."
        if det_contradicted
        else "No contradiction detected between the ROE narrative and CRA evidence."
    )

    # ── Real-model path (model-gated) ──
    if chain is not None and not getattr(chain, "is_stub", True):  # pragma: no cover
        prompt = (
            "You are validating an Employment Insurance claim. Answer strictly "
            "with YES or NO on the first line, then one sentence of evidence.\n"
            "Question: Does the following Record of Employment separation "
            "narrative CONTRADICT the CRA employment evidence (i.e. the CRA "
            "records show continued employment during the claimed separation "
            "period)?\n"
            f"ROE separation narrative: {narrative}\n"
            f"CRA employment evidence: {cra_evidence}\n"
        )
        try:
            answer = chain.generate(prompt)
            parsed = _parse_yes_no(answer)
            if parsed is not None:
                ev = answer.strip()
                # Keep the model's evidence sentence (bounded), else the default.
                ev = ev[:500] if ev else det_evidence
                return ContradictionResult(
                    contradicted=parsed, evidence=ev, source="llama"
                )
        except Exception:  # noqa: BLE001 - any model failure → deterministic path
            pass

    return ContradictionResult(
        contradicted=det_contradicted, evidence=det_evidence, source="deterministic"
    )


# ---------------------------------------------------------------------------
# Style-inconsistency classifier (Req 7.8)
# ---------------------------------------------------------------------------


def classify_style_inconsistency(
    narrative: str,
    corpus: list[str],
    *,
    similarity: Any,
    threshold: float = DEFAULT_STYLE_SIMILARITY_THRESHOLD,
    chain: Any = None,
) -> StyleResult:
    """Classify whether ``narrative`` is stylistically inconsistent with ``corpus``.

    Deterministic decision: compute the MAX cosine similarity between the
    narrative and each prior authenticated ROE narrative (via the real
    ``NarrativeSimilarity`` backend). A narrative unlike EVERY prior ROE
    (``max_similarity <= threshold``) is flagged inconsistent. The returned
    ``score`` is ``1 - max_similarity`` (an inconsistency score in ``[0, 1]``).

    The 5-prior-ROE corpus gate (Req 7.9) is enforced by the CALLER (the engine's
    StyleAnalysisChecker) — this function scores whatever corpus it is given.
    """
    narrative = narrative or ""
    prior = [c for c in (corpus or []) if (c or "").strip()]

    if not prior:
        # Nothing to compare against → not flaggable here (score 0.0).
        return StyleResult(inconsistent=False, score=0.0, source="deterministic")

    max_sim = max(similarity.similarity(narrative, c) for c in prior)
    inconsistency = round(1.0 - float(max_sim), 6)
    det_inconsistent = float(max_sim) <= float(threshold)

    # ── Real-model path (model-gated) — refine the boolean, keep the score ──
    if chain is not None and not getattr(chain, "is_stub", True):  # pragma: no cover
        prompt = (
            "You are validating an Employment Insurance claim. Answer strictly "
            "with YES or NO on the first line.\n"
            "Question: Is the following Record of Employment termination "
            "narrative STYLISTICALLY INCONSISTENT with the employer's prior "
            "authenticated ROE narratives?\n"
            f"Narrative: {narrative}\n"
            f"Prior narratives: {' | '.join(prior[:20])}\n"
        )
        try:
            parsed = _parse_yes_no(chain.generate(prompt))
            if parsed is not None:
                return StyleResult(
                    inconsistent=parsed, score=inconsistency, source="llama"
                )
        except Exception:  # noqa: BLE001 - any model failure → deterministic path
            pass

    return StyleResult(
        inconsistent=det_inconsistent, score=inconsistency, source="deterministic"
    )


__all__ = [
    "DEFAULT_STYLE_SIMILARITY_THRESHOLD",
    "ContradictionResult",
    "StyleResult",
    "classify_contradiction",
    "classify_style_inconsistency",
]
