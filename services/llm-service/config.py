"""
services/llm-service/config.py
==============================
Runtime configuration for the EI-ADS LLM Service.

Configuration is expressed with ``pydantic-settings`` so every value can be
overridden by an environment variable (prefix ``LLM_``) or an ``.env`` file.
The service wraps Llama 3.1 (70B, GGUF 4-bit) via ``llama-cpp-python`` behind a
LangChain chain with a Chroma vector store, and exposes a sentence-transformers
based narrative-similarity endpoint.

In a developer sandbox the multi-gigabyte model weights and the heavy ML
dependencies (``llama-cpp-python``, ``sentence-transformers``, ``chromadb``)
are typically absent. Every path here is therefore *configurable* and every
consumer degrades gracefully to a deterministic fallback when a resource is
missing — see ``similarity.py`` and ``llm_chain.py``.

Requirements: 3.5, 6.6
"""

from __future__ import annotations

from functools import lru_cache

try:  # pydantic-settings is the canonical config library used across EI-ADS
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover - exercised only in a bare sandbox
    _HAS_PYDANTIC_SETTINGS = False


if _HAS_PYDANTIC_SETTINGS:

    class Settings(BaseSettings):
        """Typed configuration for the LLM Service."""

        model_config = SettingsConfigDict(
            env_prefix="LLM_",
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
        )

        # ── Service ────────────────────────────────────────────────
        service_name: str = "llm-service"
        host: str = "0.0.0.0"  # noqa: S104 - bind-all is intended inside the container
        port: int = 8005

        # ── Llama 3.1 (GGUF 4-bit via llama-cpp-python) ────────────
        # Path to the GGUF weights. Absent in dev/sandbox → StubLLM is used.
        llama_model_path: str = "/app/models/llama-3.1-70b-instruct-q4_k_m.gguf"
        llama_n_ctx: int = 8192
        llama_n_gpu_layers: int = -1  # -1 → offload all layers to GPU (A100)
        llama_max_tokens: int = 512
        llama_temperature: float = 0.2

        # ── Chroma vector store (RAG retrieval) ────────────────────
        chroma_dir: str = "data/chroma"
        chroma_collection: str = "ei_ads_explanations"
        retriever_top_k: int = 4

        # ── Sentence-Transformers (narrative similarity) ───────────
        embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"

        # ── Timeouts (seconds) ─────────────────────────────────────
        similarity_timeout_s: float = 5.0
        explanation_timeout_s: float = 5.0

else:  # pragma: no cover - minimal fallback when pydantic-settings is unavailable

    import os
    from dataclasses import dataclass

    def _env(key: str, default: str) -> str:
        return os.environ.get(f"LLM_{key.upper()}", default)

    @dataclass
    class Settings:  # type: ignore[no-redef]
        """Environment-backed fallback configuration (no pydantic dependency)."""

        service_name: str = "llm-service"
        host: str = "0.0.0.0"  # noqa: S104
        port: int = int(_env("port", "8005"))

        llama_model_path: str = _env(
            "llama_model_path",
            "/app/models/llama-3.1-70b-instruct-q4_k_m.gguf",
        )
        llama_n_ctx: int = int(_env("llama_n_ctx", "8192"))
        llama_n_gpu_layers: int = int(_env("llama_n_gpu_layers", "-1"))
        llama_max_tokens: int = int(_env("llama_max_tokens", "512"))
        llama_temperature: float = float(_env("llama_temperature", "0.2"))

        chroma_dir: str = _env("chroma_dir", "data/chroma")
        chroma_collection: str = _env("chroma_collection", "ei_ads_explanations")
        retriever_top_k: int = int(_env("retriever_top_k", "4"))

        embedding_model_name: str = _env(
            "embedding_model_name", "sentence-transformers/all-MiniLM-L6-v2"
        )

        similarity_timeout_s: float = float(_env("similarity_timeout_s", "5.0"))
        explanation_timeout_s: float = float(_env("explanation_timeout_s", "5.0"))


@lru_cache(maxsize=1)
def get_settings() -> "Settings":
    """Return a cached ``Settings`` instance (constructed once per process)."""
    return Settings()


__all__ = ["Settings", "get_settings"]
