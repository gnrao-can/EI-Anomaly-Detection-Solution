"""
tests/unit/test_document_forgery_layoutlm_model.py
===================================================
MODEL-GATED smoke test for the real LayoutLMv3 OCR extractor plug-in
(services/document-forgery-engine/ocr.py :: LayoutLMv3OCRExtractor).

This test exercises the ACTUAL model-inference path — render -> processor ->
model logits -> per-token argmax/softmax -> group into the 11 mandatory ROE
fields with a min-token confidence. It is DELIBERATELY GATED so it never runs in
the core unit suite / CI (which has no torch/transformers and no fine-tuned ROE
checkpoint):

  * skipped unless ``transformers``, ``torch`` and ``PIL`` all import, AND
  * skipped unless a checkpoint is provided via the ``DOC_FORGERY_LAYOUTLM_MODEL``
    environment variable (a Hugging Face id or a local artefact directory).

To run it against a model:

    DOC_FORGERY_LAYOUTLM_MODEL=/path/to/finetuned-roe-layoutlmv3 \
        .venv-ml/bin/python -m pytest \
        tests/unit/test_document_forgery_layoutlm_model.py -q

The dev/test/CI default remains the deterministic StubOCRExtractor
(``config.force_stub_ocr`` is True), so the pipeline slice is fully verifiable
without the multi-gigabyte stack; this test proves the plug-in is real and
produces the 11-field contract the moment a checkpoint is available.

The label-normalisation helper ``_label_to_field`` is pure and IS covered in the
core suite (no model needed) so the IOB-prefix mapping logic is always tested.

Requirements: 7.1, 7.13 (model path)
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import load_service_module

_ocr = load_service_module("document-forgery-engine", "ocr", siblings=["ocr"])
MANDATORY_ROE_FIELDS = _ocr.MANDATORY_ROE_FIELDS
_label_to_field = _ocr._label_to_field


# ---------------------------------------------------------------------------
# Pure label-mapping tests (ALWAYS run — no model required)
# ---------------------------------------------------------------------------


def test_label_to_field_strips_iob_prefixes_and_matches_fields():
    assert _label_to_field("B-total_insurable_hours") == "total_insurable_hours"
    assert _label_to_field("I-EMPLOYER_BN") == "employer_bn"
    assert _label_to_field("S-sin") == "sin"
    assert _label_to_field("total_insurable_earnings") == "total_insurable_earnings"


def test_label_to_field_ignores_outside_and_unknown_labels():
    assert _label_to_field("O") is None
    assert _label_to_field("") is None
    assert _label_to_field("B-SOMETHING_ELSE") is None
    assert _label_to_field(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Model-gated smoke test (skipped without deps + a checkpoint)
# ---------------------------------------------------------------------------

_MODEL_PATH = os.environ.get("DOC_FORGERY_LAYOUTLM_MODEL")


@pytest.mark.skipif(
    not _MODEL_PATH,
    reason="Set DOC_FORGERY_LAYOUTLM_MODEL to a LayoutLMv3 ROE checkpoint to run.",
)
def test_layoutlmv3_extractor_produces_eleven_fields_from_a_rendered_roe():
    """Real LayoutLMv3 inference returns all 11 fields with [0,1] confidences."""
    pytest.importorskip("torch")
    pytest.importorskip("transformers")
    Image = pytest.importorskip("PIL.Image")

    extractor = _ocr.LayoutLMv3OCRExtractor(_MODEL_PATH)

    # A blank white "ROE" page is enough to exercise the full inference path and
    # the 11-field contract; field values depend on the fine-tuned checkpoint.
    image = Image.new("RGB", (1000, 1400), "white")
    result = extractor.extract({"image": image})

    assert result.source == "layoutlmv3"
    # All 11 mandatory fields are present in both maps (empty/0.0 when absent).
    assert set(result.values.keys()) == set(MANDATORY_ROE_FIELDS)
    assert set(result.confidences.keys()) == set(MANDATORY_ROE_FIELDS)
    for name in MANDATORY_ROE_FIELDS:
        assert 0.0 <= float(result.confidences[name]) <= 1.0
    # The low-confidence flagging contract works over the real output.
    low = result.low_confidence_fields(threshold=0.85)
    assert isinstance(low, list)
