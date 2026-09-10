"""
services/document-forgery-engine/ocr.py
=======================================
OCR extraction abstraction for the Document Forgery Engine (task 15.1).

Design step 1 of the Document Forgery Engine (design "5. Document Forgery
Engine") calls for LayoutLMv3 to extract *all 11 mandatory ROE fields* with a
per-field confidence score, and to flag any field whose confidence falls below
0.85 as ``EXTRACTION_LOW_CONFIDENCE`` (Requirement 7.1).

Because the production LayoutLMv3 stack (``transformers`` + ``torch``) is
multi-gigabyte and is not available in the CI sandbox, this module exposes an
:class:`OCRExtractor` abstraction with two concrete implementations:

  * :class:`StubOCRExtractor` — a deterministic, dependency-free extractor that
    returns an :class:`ExtractedFields` structure (the 11 fields, each with a
    per-field confidence). It is used in development, the sandbox, and every
    unit test, so the engine runs end-to-end without downloading a model.

  * :class:`LayoutLMv3OCRExtractor` — the production extractor. It lazily
    imports ``transformers``/``torch`` and loads the fine-tuned LayoutLMv3
    token-classification checkpoint. The exact model-inference wiring is marked
    with a ``# >>> REAL LAYOUTLMV3 PLUG-IN POINT`` comment; everything else
    (field schema, confidence contract, low-confidence flagging) is shared with
    the stub so swapping implementations changes nothing downstream.

:func:`get_extractor` selects the implementation from configuration: the stub is
returned whenever ``settings.force_stub_ocr`` is true (always in dev/test) or
when the ``transformers`` stack cannot be imported.

Requirements: 7.1
"""

from __future__ import annotations

import hashlib
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from shared.models import EngineType, Indicator, Severity

# ---------------------------------------------------------------------------
# The 11 mandatory ROE fields (design step 1, "extract all 11 mandatory ROE
# fields"). This is the canonical ordering used by both extractors and by the
# consistency checks.
# ---------------------------------------------------------------------------

MANDATORY_ROE_FIELDS: tuple[str, ...] = (
    "employer_name",          # 1  Block 1  — employer legal name
    "employer_bn",            # 2  Block 4  — 15-char Business Number
    "employee_name",          # 3  Block 5  — claimant legal name
    "sin",                    # 4  Block 6  — Social Insurance Number
    "occupation",             # 5  Block 8  — occupation / job title
    "first_day_worked",       # 6  Block 10 — first day worked
    "last_day_paid",          # 7  Block 11 — last day for which paid
    "total_insurable_hours",  # 8  Block 15A — total insurable hours
    "total_insurable_earnings",  # 9 Block 15B — total insurable earnings
    "hourly_rate",            # 10 derived / Block 15C pay-period rate
    "reason_for_separation",  # 11 Block 16 — ROE reason code / narrative
)

#: Default per-field confidence floor (mirrors ``Settings.ocr_confidence_threshold``).
DEFAULT_CONFIDENCE_THRESHOLD: float = 0.85


@dataclass
class ExtractedFields:
    """Structured result of OCR extraction over a single ROE document.

    Attributes
    ----------
    values:
        Mapping of each of the 11 mandatory ROE field names to its extracted
        value (string form as read off the document; numeric interpretation is
        performed by the consistency checks).
    confidences:
        Mapping of each of the 11 mandatory ROE field names to the model's
        per-field confidence in ``[0.0, 1.0]``.
    source:
        Identifier of the extractor that produced the result
        (``"stub"`` or ``"layoutlmv3"``); useful for the audit trail.
    """

    values: dict[str, str]
    confidences: dict[str, float]
    source: str = "stub"

    def low_confidence_fields(
        self, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    ) -> list[str]:
        """Return the field names whose confidence is *strictly below* ``threshold``.

        The comparison is strict, so a field with confidence exactly equal to
        the threshold is NOT considered low-confidence (Requirement 7.1
        boundary: 0.85 -> ok, 0.84 -> flagged).
        """
        return [
            name
            for name in MANDATORY_ROE_FIELDS
            if self.confidences.get(name, 0.0) < threshold
        ]

    def as_decimal(self, field_name: str) -> Optional[Decimal]:
        """Return an extracted numeric field as an exact ``Decimal`` (or None)."""
        raw = self.values.get(field_name)
        if raw is None or raw == "":
            return None
        try:
            return Decimal(str(raw))
        except (ArithmeticError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Extractor abstraction
# ---------------------------------------------------------------------------


class OCRExtractor(ABC):
    """Abstract OCR extractor over ROE documents.

    Concrete implementations must return an :class:`ExtractedFields` populated
    for all 11 :data:`MANDATORY_ROE_FIELDS`, each with a per-field confidence.
    """

    source: str = "abstract"

    @abstractmethod
    def extract(self, document: dict[str, Any]) -> ExtractedFields:
        """Extract the 11 mandatory ROE fields (with confidences) from a document.

        Parameters
        ----------
        document:
            The document payload. In production this carries the raw ROE bytes /
            image; the stub reads any already-known field values plus optional
            per-field confidence overrides so tests can drive low-confidence
            scenarios deterministically.
        """
        raise NotImplementedError


class StubOCRExtractor(OCRExtractor):
    """Deterministic, dependency-free OCR extractor for dev / sandbox / tests.

    The stub never loads a model. It builds an :class:`ExtractedFields` result
    from the document payload:

      * Field *values* are taken from ``document["fields"]`` when present;
        otherwise a deterministic placeholder is derived from the document id so
        results are stable across runs.
      * Field *confidences* are taken from ``document["confidences"]`` when
        present (this is how tests inject a below-threshold field); otherwise a
        deterministic high confidence in ``[0.90, 0.99]`` is derived per field,
        so a "clean" document yields no low-confidence flags.

    Every one of the 11 mandatory fields is always populated, satisfying the
    "extract all 11 mandatory ROE fields" contract.
    """

    source = "stub"

    def extract(self, document: dict[str, Any]) -> ExtractedFields:
        supplied_values: dict[str, Any] = dict(document.get("fields", {}) or {})
        supplied_conf: dict[str, Any] = dict(document.get("confidences", {}) or {})
        seed = str(
            document.get("document_id")
            or document.get("roe_document_id")
            or document.get("claim_id")
            or "stub-doc"
        )

        values: dict[str, str] = {}
        confidences: dict[str, float] = {}
        for name in MANDATORY_ROE_FIELDS:
            if name in supplied_values and supplied_values[name] is not None:
                values[name] = str(supplied_values[name])
            else:
                values[name] = self._deterministic_value(name, seed)

            if name in supplied_conf and supplied_conf[name] is not None:
                confidences[name] = float(supplied_conf[name])
            else:
                confidences[name] = self._deterministic_confidence(name, seed)

        return ExtractedFields(values=values, confidences=confidences, source=self.source)

    @staticmethod
    def _deterministic_value(field_name: str, seed: str) -> str:
        digest = hashlib.sha256(f"{seed}:{field_name}".encode("utf-8")).hexdigest()
        return f"{field_name}:{digest[:8]}"

    @staticmethod
    def _deterministic_confidence(field_name: str, seed: str) -> float:
        digest = hashlib.sha256(f"conf:{seed}:{field_name}".encode("utf-8")).digest()
        # Map the first byte into [0.90, 0.99] so clean docs never trip the floor.
        return round(0.90 + (digest[0] / 255.0) * 0.09, 4)


#: Normalise a model label to a MANDATORY_ROE_FIELDS name. Strips IOB/BILOU
#: prefixes (``B-``, ``I-``, ``E-``, ``S-``, ``L-``, ``U-``) and lower-cases, so
#: a fine-tuned checkpoint labelled e.g. ``B-TOTAL_INSURABLE_HOURS`` maps onto
#: ``total_insurable_hours``. Labels that are not one of the 11 fields (incl. the
#: ``O`` "outside" tag) map to ``None`` and are ignored during grouping.
def _label_to_field(label: str) -> Optional[str]:
    if not label:
        return None
    text = str(label).strip()
    for prefix in ("B-", "I-", "E-", "S-", "L-", "U-"):
        if text.upper().startswith(prefix):
            text = text[len(prefix):]
            break
    normalised = text.strip().lower()
    return normalised if normalised in set(MANDATORY_ROE_FIELDS) else None


class LayoutLMv3OCRExtractor(OCRExtractor):  # pragma: no cover - requires heavy deps
    """Production OCR extractor backed by LayoutLMv3 via ``transformers``.

    The ``transformers``/``torch``/``PIL`` stack is imported lazily so that
    merely importing this module (e.g. during unit-test collection) never pulls
    in the multi-gigabyte dependency. Construction loads the fine-tuned
    LayoutLMv3 token-classification checkpoint referenced by ``model_path`` (a
    Hugging Face id or a local artefact directory).

    The checkpoint is expected to be fine-tuned to label ROE tokens with the 11
    :data:`MANDATORY_ROE_FIELDS` (optionally IOB-prefixed); :func:`_label_to_field`
    maps its ``id2label`` scheme onto the canonical field names. This class does
    NOT ship or train that checkpoint — it is the inference wiring behind the
    plug-in point, exercised by the model-gated smoke test when a checkpoint is
    provided via ``DOC_FORGERY_LAYOUTLM_MODEL``.
    """

    source = "layoutlmv3"

    def __init__(self, model_path: str) -> None:
        # Lazy, guarded imports: the sandbox does not have these packages.
        import torch  # type: ignore  # noqa: F401
        from transformers import (  # type: ignore
            LayoutLMv3ForTokenClassification,
            LayoutLMv3Processor,
        )

        self._torch = torch
        self._processor = LayoutLMv3Processor.from_pretrained(model_path)
        self._model = LayoutLMv3ForTokenClassification.from_pretrained(model_path)
        self._model.eval()
        # id2label from the checkpoint config (fallback to a generic map).
        self._id2label = dict(getattr(self._model.config, "id2label", {}) or {})

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _render_image(document: dict[str, Any]) -> Any:
        """Render the ROE document to an RGB PIL image for LayoutLMv3.

        Accepts (in priority order): an already-open PIL image under
        ``document["image"]``; a raster image path/bytes under ``image_path`` /
        ``image_bytes``; or the first page of a PDF under ``pdf_path`` /
        ``pdf_bytes`` (rendered via ``pdf2image`` when available). Raises
        ``ValueError`` when no renderable input is present.
        """
        from PIL import Image  # type: ignore
        import io

        img = document.get("image")
        if img is not None:
            return img.convert("RGB")

        image_path = document.get("image_path")
        if image_path:
            return Image.open(image_path).convert("RGB")

        image_bytes = document.get("image_bytes")
        if image_bytes:
            return Image.open(io.BytesIO(image_bytes)).convert("RGB")

        pdf_path = document.get("pdf_path")
        pdf_bytes = document.get("pdf_bytes") or document.get("pdf")
        if pdf_path or pdf_bytes:
            from pdf2image import convert_from_path, convert_from_bytes  # type: ignore

            pages = (
                convert_from_path(pdf_path, first_page=1, last_page=1)
                if pdf_path
                else convert_from_bytes(pdf_bytes, first_page=1, last_page=1)
            )
            if pages:
                return pages[0].convert("RGB")

        raise ValueError(
            "LayoutLMv3OCRExtractor requires a renderable document "
            "(image / image_path / image_bytes / pdf_path / pdf_bytes)."
        )

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    def extract(self, document: dict[str, Any]) -> ExtractedFields:
        """Extract the 11 mandatory ROE fields + per-field confidences.

        Pipeline (design step 1): render -> processor -> model logits -> per-token
        argmax label + softmax probability. Tokens are grouped by their mapped
        field (:func:`_label_to_field`); each field's value is the joined token
        text and its confidence is the MIN token probability across the field's
        tokens (the weakest link, matching the design's per-field confidence).
        Any of the 11 fields not detected in the document is emitted with an
        empty value and confidence 0.0 so the low-confidence flagging (Req 7.1)
        and the consistency checks behave identically to the stub path.
        """
        torch = self._torch
        image = self._render_image(document)

        encoding = self._processor(image, return_tensors="pt")
        with torch.no_grad():
            logits = self._model(**encoding).logits  # (1, seq, num_labels)
        probs = logits.softmax(-1)[0]
        conf_tensor, pred_ids = probs.max(-1)
        pred_ids = pred_ids.tolist()
        confidences_per_token = conf_tensor.tolist()

        # Recover the token strings for value assembly.
        input_ids = encoding["input_ids"][0].tolist()
        tokens = self._processor.tokenizer.convert_ids_to_tokens(input_ids)

        grouped_values: dict[str, list[str]] = {}
        grouped_conf: dict[str, list[float]] = {}
        for tok, label_id, conf in zip(tokens, pred_ids, confidences_per_token):
            label = self._id2label.get(label_id, self._id2label.get(str(label_id), ""))
            field = _label_to_field(label)
            if field is None:
                continue
            grouped_values.setdefault(field, []).append(str(tok))
            grouped_conf.setdefault(field, []).append(float(conf))

        values: dict[str, str] = {}
        confidences: dict[str, float] = {}
        for name in MANDATORY_ROE_FIELDS:
            toks = grouped_values.get(name)
            if toks:
                # Detokenise defensively: drop the WordPiece ``▁``/``##`` markers.
                text = "".join(toks).replace("\u2581", " ").replace("##", "").strip()
                values[name] = text
                confidences[name] = min(grouped_conf.get(name, [0.0]))
            else:
                values[name] = ""
                confidences[name] = 0.0

        return ExtractedFields(values=values, confidences=confidences, source=self.source)


# ---------------------------------------------------------------------------
# Low-confidence flagging (design step 1 -> EXTRACTION_LOW_CONFIDENCE)
# ---------------------------------------------------------------------------


def low_confidence_indicators(
    extracted: ExtractedFields,
    *,
    claim_id: Optional[str] = None,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> list[Indicator]:
    """Return one ``EXTRACTION_LOW_CONFIDENCE`` Indicator per low-confidence field.

    A field is low-confidence when its extraction confidence is *strictly below*
    ``threshold`` (default 0.85). Boundary behaviour (Requirement 7.1):
      - confidence exactly 0.85 -> NO Indicator for that field
      - confidence 0.84         -> one EXTRACTION_LOW_CONFIDENCE Indicator

    Each Indicator's payload carries the field name, its confidence, and the
    threshold so the audit trail records exactly why the field was flagged.
    """
    indicators: list[Indicator] = []
    coerced_claim_id = _coerce_uuid(claim_id)
    for name in extracted.low_confidence_fields(threshold):
        confidence = float(extracted.confidences.get(name, 0.0))
        indicators.append(
            Indicator(
                indicator_id=uuid.uuid4(),
                claim_id=coerced_claim_id,
                engine=EngineType.DOCUMENT_FORGERY,
                severity=Severity.MEDIUM,
                algorithm="ocr_extraction_confidence",
                title=f"Low OCR extraction confidence for '{name}'",
                explanation_en=(
                    f"OCR extraction confidence for ROE field '{name}' was "
                    f"{confidence:.4f}, below the required threshold of "
                    f"{threshold:.2f} (EXTRACTION_LOW_CONFIDENCE)."
                ),
                explanation_fr=(
                    f"La confiance d'extraction OCR pour le champ '{name}' du "
                    f"releve d'emploi etait de {confidence:.4f}, sous le seuil "
                    f"requis de {threshold:.2f} (EXTRACTION_LOW_CONFIDENCE)."
                ),
                payload={
                    "flag": "EXTRACTION_LOW_CONFIDENCE",
                    "field": name,
                    "confidence": confidence,
                    "threshold": float(threshold),
                },
                produced_at=datetime.now(timezone.utc),
            )
        )
    return indicators


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_extractor(settings: Any) -> OCRExtractor:
    """Select the OCR extractor implementation from configuration.

    Returns :class:`StubOCRExtractor` whenever ``settings.force_stub_ocr`` is
    true (always in dev/test) or when the production ``transformers`` stack is
    not importable. Otherwise returns a :class:`LayoutLMv3OCRExtractor`.
    """
    force_stub = bool(getattr(settings, "force_stub_ocr", True))
    if force_stub:
        return StubOCRExtractor()

    try:  # pragma: no cover - heavy deps unavailable in sandbox
        import transformers  # noqa: F401
    except ImportError:  # pragma: no cover
        return StubOCRExtractor()

    model_path = getattr(settings, "layoutlmv3_model_path", "microsoft/layoutlmv3-base")
    return LayoutLMv3OCRExtractor(model_path)  # pragma: no cover


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_uuid(claim_id: Optional[str]) -> uuid.UUID:
    """Coerce a claim id string to a UUID, tolerating missing / non-UUID ids."""
    if isinstance(claim_id, uuid.UUID):
        return claim_id
    if claim_id:
        try:
            return uuid.UUID(str(claim_id))
        except (ValueError, AttributeError, TypeError):
            return uuid.uuid5(uuid.NAMESPACE_OID, str(claim_id))
    return uuid.uuid4()


__all__ = [
    "MANDATORY_ROE_FIELDS",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "ExtractedFields",
    "OCRExtractor",
    "StubOCRExtractor",
    "LayoutLMv3OCRExtractor",
    "low_confidence_indicators",
    "get_extractor",
]
