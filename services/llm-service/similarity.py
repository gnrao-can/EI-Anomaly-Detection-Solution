"""
services/llm-service/similarity.py
==================================
Narrative similarity scoring for the EI-ADS LLM Service.

The ``NarrativeSimilarity`` class embeds two free-text narratives (e.g. two
"reason for separation" statements) and returns their cosine similarity as a
float in the closed interval ``[0.0, 1.0]``. A value near ``1.0`` means the two
narratives are near-identical; a value near ``0.0`` means they share little.

Two backends implement the same interface:

* **Transformer backend** (production): sentence-transformers
  ``all-MiniLM-L6-v2`` produces dense semantic embeddings, then cosine
  similarity is computed and clamped to ``[0, 1]``.

* **Deterministic fallback** (dev/sandbox): when ``sentence-transformers`` or
  its model weights are unavailable, we embed each narrative as a sparse,
  hashed bag-of-tokens vector and compute cosine similarity over that. This is
  fully deterministic, dependency-free (stdlib only), and preserves the
  contract properties the callers rely on:
    - identical non-empty strings  → 1.0
    - token-disjoint strings       → 0.0 (low)
    - symmetric: sim(a, b) == sim(b, a)
    - bounded in [0.0, 1.0]

The backend is selected automatically at construction time; callers can force
the fallback via ``NarrativeSimilarity(use_fallback=True)`` (used by tests).

Where the real model plugs in: set ``LLM_EMBEDDING_MODEL_NAME`` and ensure
``sentence-transformers`` is installed with network/disk access to the model;
the transformer backend then activates transparently — no caller changes.

Requirements: 3.5, 6.6
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

# Number of buckets in the hashed bag-of-tokens vector used by the fallback.
# Large enough to make accidental collisions between distinct tokens rare.
_FALLBACK_DIM = 4096

# Split on any run of non-alphanumeric characters; case-folded downstream.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """Lower-case and split ``text`` into alphanumeric tokens."""
    return _TOKEN_RE.findall(text.lower())


def _cosine(vec_a: dict[int, float], vec_b: dict[int, float]) -> float:
    """Cosine similarity of two sparse vectors represented as dicts."""
    if not vec_a or not vec_b:
        return 0.0
    # Iterate over the smaller vector for the dot product.
    if len(vec_a) > len(vec_b):
        vec_a, vec_b = vec_b, vec_a
    dot = sum(weight * vec_b.get(idx, 0.0) for idx, weight in vec_a.items())
    norm_a = math.sqrt(sum(w * w for w in vec_a.values()))
    norm_b = math.sqrt(sum(w * w for w in vec_b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _clamp_unit(value: float) -> float:
    """Clamp a float into the closed interval ``[0.0, 1.0]``."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


class NarrativeSimilarity:
    """Embed two narratives and return their cosine similarity in ``[0, 1]``.

    Parameters
    ----------
    model_name:
        Sentence-transformers model identifier used by the transformer backend.
    use_fallback:
        Force the deterministic hashing fallback regardless of whether
        sentence-transformers is importable. Primarily for tests and sandboxes.
    """

    def __init__(
        self,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        use_fallback: bool = False,
    ) -> None:
        self.model_name = model_name
        self._model = None
        self._using_fallback = True

        if not use_fallback:
            self._try_load_transformer()

    # ------------------------------------------------------------------
    # Backend selection
    # ------------------------------------------------------------------

    def _try_load_transformer(self) -> None:
        """Attempt to load the sentence-transformers model; fall back on failure."""
        try:  # pragma: no cover - depends on optional heavy dependency
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
            self._using_fallback = False
        except Exception:  # noqa: BLE001 - any import/load failure → fallback
            self._model = None
            self._using_fallback = True

    @property
    def using_fallback(self) -> bool:
        """``True`` when the deterministic hashing backend is active."""
        return self._using_fallback

    @property
    def backend(self) -> str:
        """Human-readable name of the active backend."""
        return "hashing-fallback" if self._using_fallback else "sentence-transformers"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def similarity(self, narrative_a: str, narrative_b: str) -> float:
        """Return cosine similarity of two narratives, bounded to ``[0, 1]``.

        Empty / whitespace-only inputs are treated as having no content, so any
        pairing involving an empty narrative returns ``0.0`` unless *both* are
        empty, in which case there is nothing to distinguish them and the
        result is ``1.0`` (degenerate identical case).
        """
        a = (narrative_a or "").strip()
        b = (narrative_b or "").strip()

        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0

        if self._using_fallback or self._model is None:
            score = self._fallback_similarity(a, b)
        else:  # pragma: no cover - requires the optional transformer backend
            score = self._transformer_similarity(a, b)

        return round(_clamp_unit(score), 6)

    # ------------------------------------------------------------------
    # Deterministic fallback backend
    # ------------------------------------------------------------------

    def _embed_fallback(self, text: str) -> dict[int, float]:
        """Hash tokens into a sparse term-frequency vector of dim ``_FALLBACK_DIM``."""
        counts = Counter(_tokenize(text))
        vec: dict[int, float] = {}
        for token, freq in counts.items():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % _FALLBACK_DIM
            vec[idx] = vec.get(idx, 0.0) + float(freq)
        return vec

    def _fallback_similarity(self, a: str, b: str) -> float:
        return _cosine(self._embed_fallback(a), self._embed_fallback(b))

    # ------------------------------------------------------------------
    # Transformer backend
    # ------------------------------------------------------------------

    def _transformer_similarity(self, a: str, b: str) -> float:  # pragma: no cover
        """Cosine similarity of dense sentence-transformer embeddings."""
        embeddings = self._model.encode([a, b], normalize_embeddings=True)
        vec_a, vec_b = embeddings[0], embeddings[1]
        # normalize_embeddings=True → dot product is already cosine similarity.
        dot = float(sum(float(x) * float(y) for x, y in zip(vec_a, vec_b)))
        return dot


__all__ = ["NarrativeSimilarity"]
