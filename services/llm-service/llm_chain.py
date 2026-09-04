"""
services/llm-service/llm_chain.py
=================================
LangChain configuration for the EI-ADS LLM Service.

This module wires Llama 3.1 (70B, GGUF 4-bit) into a LangChain chain backed by a
Chroma vector store (RAG retrieval over historical claim explanations). It is
designed to run in three environments without code changes:

1. **Production** — ``llama-cpp-python`` loads the GGUF weights at
   ``settings.llama_model_path`` and LangChain/Chroma provide retrieval. This is
   where the real model plugs in.

2. **Partial sandbox** — LangChain/Chroma are installed but the multi-gigabyte
   GGUF file is absent. The chain is still constructed, but text generation is
   served by :class:`StubLLM`, a deterministic templated LLM.

3. **Bare sandbox** — none of ``langchain``, ``chromadb`` or ``llama-cpp-python``
   are importable. Everything degrades to in-process stubs so the FastAPI
   service still starts, serves ``/healthz`` and the similarity endpoint, and
   returns deterministic explanation text.

All heavy imports are performed lazily inside ``build_llm_chain`` so importing
this module never pulls in torch/llama/chroma. That keeps ``main.py`` startup
fast and non-blocking (the chain is built on first use, not at import time).

Requirements: 3.5, 6.6
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from config import Settings, get_settings


# ---------------------------------------------------------------------------
# StubLLM — deterministic fallback used whenever the GGUF model is unavailable
# ---------------------------------------------------------------------------


class StubLLM:
    """Deterministic, dependency-free stand-in for the Llama 3.1 chat model.

    Returns templated text derived from the prompt so that callers (and tests)
    get stable, inspectable output without loading multi-gigabyte weights. The
    output is intentionally boring and reproducible.
    """

    model_name = "stub-llm"

    def invoke(self, prompt: str, **_: Any) -> str:
        """Return a deterministic templated response for ``prompt``."""
        return self._generate(prompt)

    # LangChain-style alias so the stub is a drop-in for a Runnable/LLM.
    def __call__(self, prompt: str, **kwargs: Any) -> str:
        return self.invoke(prompt, **kwargs)

    def generate(self, prompt: str) -> str:
        return self._generate(prompt)

    @staticmethod
    def _generate(prompt: str) -> str:
        snippet = (prompt or "").strip().replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:157] + "..."
        return (
            "[STUB-LLM] Deterministic response generated without the Llama 3.1 "
            f"model. Prompt summary: {snippet}"
        )


# ---------------------------------------------------------------------------
# Chain container
# ---------------------------------------------------------------------------


@dataclass
class LLMChain:
    """Bundle of the LLM, the vector store, and its retriever.

    Any of the fields may be ``None`` when the corresponding backend is
    unavailable; ``is_stub`` reports whether real weights were loaded.
    """

    llm: Any
    vector_store: Any = None
    retriever: Any = None
    is_stub: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def generate(self, prompt: str) -> str:
        """Generate text from the underlying LLM (real or stub)."""
        # Both StubLLM and LangChain Runnables expose ``invoke``.
        result = self.llm.invoke(prompt)
        # LangChain chat models return message objects; normalise to text.
        if hasattr(result, "content"):
            return str(result.content)
        return str(result)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _build_llama_llm(settings: Settings) -> tuple[Any, bool, dict[str, Any]]:
    """Return ``(llm, is_stub, details)``.

    Loads the real Llama 3.1 GGUF model via ``llama-cpp-python`` through
    LangChain when both the library and the weights file are present; otherwise
    returns a :class:`StubLLM`.
    """
    details: dict[str, Any] = {"model_path": settings.llama_model_path}

    model_present = os.path.isfile(settings.llama_model_path)
    details["model_file_present"] = model_present

    if not model_present:
        details["reason"] = "GGUF model file not found; using StubLLM"
        return StubLLM(), True, details

    try:  # pragma: no cover - requires llama-cpp-python + multi-GB weights
        from langchain_community.llms import LlamaCpp

        llm = LlamaCpp(
            model_path=settings.llama_model_path,
            n_ctx=settings.llama_n_ctx,
            n_gpu_layers=settings.llama_n_gpu_layers,
            max_tokens=settings.llama_max_tokens,
            temperature=settings.llama_temperature,
            verbose=False,
        )
        details["reason"] = "Loaded Llama 3.1 via llama-cpp-python"
        return llm, False, details
    except Exception as exc:  # noqa: BLE001 - any failure → deterministic stub
        details["reason"] = f"LlamaCpp unavailable ({exc!r}); using StubLLM"
        return StubLLM(), True, details


def _build_vector_store(settings: Settings) -> tuple[Any, Any, dict[str, Any]]:
    """Return ``(vector_store, retriever, details)`` for the Chroma RAG store.

    Persists to ``settings.chroma_dir``. When Chroma or the embedding function
    is unavailable the store and retriever are ``None`` and the chain runs
    without retrieval augmentation.
    """
    details: dict[str, Any] = {"chroma_dir": settings.chroma_dir}
    # Ensure the persistence directory exists so a real Chroma store can attach.
    try:
        os.makedirs(settings.chroma_dir, exist_ok=True)
        details["chroma_dir_created"] = True
    except OSError as exc:  # pragma: no cover - unusual filesystem error
        details["chroma_dir_created"] = False
        details["chroma_dir_error"] = repr(exc)

    try:  # pragma: no cover - requires chromadb + sentence-transformers
        from langchain_community.embeddings import (
            HuggingFaceEmbeddings,  # type: ignore
        )
        from langchain_community.vectorstores import Chroma

        embeddings = HuggingFaceEmbeddings(model_name=settings.embedding_model_name)
        vector_store = Chroma(
            collection_name=settings.chroma_collection,
            embedding_function=embeddings,
            persist_directory=settings.chroma_dir,
        )
        retriever = vector_store.as_retriever(
            search_kwargs={"k": settings.retriever_top_k}
        )
        details["reason"] = "Chroma vector store initialised"
        return vector_store, retriever, details
    except Exception as exc:  # noqa: BLE001 - degrade to no-retrieval mode
        details["reason"] = f"Chroma unavailable ({exc!r}); retrieval disabled"
        return None, None, details


def build_llm_chain(settings: Optional[Settings] = None) -> LLMChain:
    """Construct the LLM chain (LLM + Chroma retriever).

    This is intentionally lazy: it is called on first use rather than at import
    or process-startup time, so the FastAPI service starts without blocking on a
    multi-gigabyte model load.
    """
    settings = settings or get_settings()

    llm, is_stub, llm_details = _build_llama_llm(settings)
    vector_store, retriever, store_details = _build_vector_store(settings)

    return LLMChain(
        llm=llm,
        vector_store=vector_store,
        retriever=retriever,
        is_stub=is_stub,
        details={"llm": llm_details, "vector_store": store_details},
    )


__all__ = ["StubLLM", "LLMChain", "build_llm_chain"]
