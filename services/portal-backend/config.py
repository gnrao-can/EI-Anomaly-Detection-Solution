"""
services/portal-backend/config.py
=================================
Centralised configuration for the Portal Backend-for-Frontend (BFF).

The Portal BFF is the single :8000 origin the ``claimant-portal`` SPA talks to.
It authenticates every request via GCKey OAuth 2.0 (authorization-code + PKCE),
serves the ``/portal/*`` read APIs directly from PostgreSQL, and reverse-proxies
the ``/api/v1/claims`` and ``/api/v1/privacy/*`` write paths to the Claim Intake
Service — forwarding the caller's GCKey Bearer token — so the browser only ever
sees one origin.

All runtime configuration is read from environment variables (see the repo
``.env.example``) using ``pydantic-settings`` when it is installed, with a plain
``os.environ`` fallback so the module imports cleanly in a minimal environment.
This mirrors ``services/claim-intake/config.py`` exactly.

Requirements: 19.7, 22.1, 22.4, 22.6
"""

from __future__ import annotations

from functools import lru_cache

try:  # pydantic-settings is the canonical settings library for pydantic v2
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover - fallback for minimal environments
    _HAS_PYDANTIC_SETTINGS = False


if _HAS_PYDANTIC_SETTINGS:

    class Settings(BaseSettings):
        """Environment-driven configuration for the Portal BFF."""

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        # ── Deployment ──────────────────────────────────────────────
        app_env: str = "development"
        service_name: str = "portal-backend"
        portal_backend_port: int = 8000

        # ── Session cookie ──────────────────────────────────────────
        session_cookie_name: str = "ei_ads_session"
        # HMAC signing secret for the session cookie. MUST be overridden in
        # any non-local environment.
        session_secret: str = "changeme_portal_session_secret_local_only"
        # Cookie lifetime in seconds (default 8 hours).
        session_max_age: int = 8 * 60 * 60
        # Only set the Secure flag when serving over HTTPS (not in local dev).
        session_cookie_secure: bool = False

        # ── PostgreSQL (read side of the BFF) ───────────────────────
        postgres_host: str = "localhost"
        postgres_port: int = 5432
        postgres_db: str = "ei_ads"
        postgres_user: str = "ei_ads_app"
        postgres_password: str = "changeme_local_only"
        postgres_ssl_mode: str = "disable"

        # ── Kafka (outcome.feedback publish on disposition) ─────────
        kafka_bootstrap_servers: str = "localhost:9092"

        # ── Claim Intake Service (reverse-proxy target) ─────────────
        claim_intake_url: str = "http://localhost:8001"

        # ── ROE Adapter (DEV-ONLY auto-ROE convenience) ─────────────
        # In production the ROE arrives from ROE Web (the employer), a separate
        # actor from the claimant (design.md "ROE Adapter"). For a smooth LOCAL
        # demo the portal can, right after a successful claim submit, post a
        # matching ROE to this adapter so the pipeline pairs + scores without a
        # manual terminal step. Gated by ``is_development`` — NEVER active in prod.
        roe_adapter_url: str = "http://localhost:8002"
        # Explicit off-switch even in dev (set false to require the real ROE Web
        # path / a manually posted ROE).
        dev_auto_roe: bool = True

        # ── DEV-ONLY one-click role login (no GCKey IdP locally) ────
        # Path to the dev JWKS signing key (written by gen-service-token.py and
        # served as the JWKS). The dev-login endpoint signs a per-role GCKey
        # Bearer with it so a browser session gets a real, JWKS-valid token
        # without a GCKey IdP. Mounted read-only into the container. Gated by
        # is_development; the endpoint 404s in prod.
        dev_signing_key_path: str = "/app/dev-jwks/signing-key.pem"
        dev_login_enabled: bool = True

        # ── GCKey OAuth 2.0 ─────────────────────────────────────────
        gckey_jwks_url: str = "https://auth.id.gc.ca/.well-known/jwks.json"
        gckey_client_id: str = "ei-ads-local"
        gckey_client_secret: str = ""
        gckey_issuer: str = ""
        gckey_authorize_url: str = "https://auth.id.gc.ca/oauth/authorize"
        gckey_token_url: str = "https://auth.id.gc.ca/oauth/token"
        gckey_redirect_uri: str = "http://localhost:8000/auth/gckey/callback"
        gckey_scope: str = "openid profile"

        # ── SIN hashing (must match claim-intake to compare hashes) ──
        sin_hash_salt: str = "changeme_generate_a_real_64char_hex_salt_for_local_dev"

        @property
        def postgres_dsn(self) -> str:
            """asyncpg-compatible DSN built from the discrete PG settings."""
            return (
                f"postgresql://{self.postgres_user}:{self.postgres_password}"
                f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            )

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local"}

else:  # pragma: no cover - executed only when pydantic-settings is absent

    import os

    def _env_bool(name: str, default: bool) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    class Settings:  # type: ignore[no-redef]
        """Minimal environment-backed settings fallback (no pydantic-settings)."""

        def __init__(self) -> None:
            self.app_env = os.environ.get("APP_ENV", "development")
            self.service_name = os.environ.get("SERVICE_NAME", "portal-backend")
            self.portal_backend_port = int(
                os.environ.get("PORTAL_BACKEND_PORT", "8000")
            )

            self.session_cookie_name = os.environ.get(
                "SESSION_COOKIE_NAME", "ei_ads_session"
            )
            self.session_secret = os.environ.get(
                "SESSION_SECRET", "changeme_portal_session_secret_local_only"
            )
            self.session_max_age = int(
                os.environ.get("SESSION_MAX_AGE", str(8 * 60 * 60))
            )
            self.session_cookie_secure = _env_bool("SESSION_COOKIE_SECURE", False)

            self.postgres_host = os.environ.get("POSTGRES_HOST", "localhost")
            self.postgres_port = int(os.environ.get("POSTGRES_PORT", "5432"))
            self.postgres_db = os.environ.get("POSTGRES_DB", "ei_ads")
            self.postgres_user = os.environ.get("POSTGRES_USER", "ei_ads_app")
            self.postgres_password = os.environ.get(
                "POSTGRES_PASSWORD", "changeme_local_only"
            )
            self.postgres_ssl_mode = os.environ.get("POSTGRES_SSL_MODE", "disable")

            self.kafka_bootstrap_servers = os.environ.get(
                "KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"
            )

            self.claim_intake_url = os.environ.get(
                "CLAIM_INTAKE_URL", "http://localhost:8001"
            )

            # DEV-ONLY auto-ROE convenience (see the pydantic Settings above).
            self.roe_adapter_url = os.environ.get(
                "ROE_ADAPTER_URL", "http://localhost:8002"
            )
            self.dev_auto_roe = _env_bool("DEV_AUTO_ROE", True)

            # DEV-ONLY one-click role login (see the pydantic Settings above).
            self.dev_signing_key_path = os.environ.get(
                "DEV_SIGNING_KEY_PATH", "/app/dev-jwks/signing-key.pem"
            )
            self.dev_login_enabled = _env_bool("DEV_LOGIN_ENABLED", True)

            self.gckey_jwks_url = os.environ.get(
                "GCKEY_JWKS_URL", "https://auth.id.gc.ca/.well-known/jwks.json"
            )
            self.gckey_client_id = os.environ.get("GCKEY_CLIENT_ID", "ei-ads-local")
            self.gckey_client_secret = os.environ.get("GCKEY_CLIENT_SECRET", "")
            self.gckey_issuer = os.environ.get("GCKEY_ISSUER", "")
            self.gckey_authorize_url = os.environ.get(
                "GCKEY_AUTHORIZE_URL", "https://auth.id.gc.ca/oauth/authorize"
            )
            self.gckey_token_url = os.environ.get(
                "GCKEY_TOKEN_URL", "https://auth.id.gc.ca/oauth/token"
            )
            self.gckey_redirect_uri = os.environ.get(
                "GCKEY_REDIRECT_URI", "http://localhost:8000/auth/gckey/callback"
            )
            self.gckey_scope = os.environ.get("GCKEY_SCOPE", "openid profile")

            self.sin_hash_salt = os.environ.get(
                "SIN_HASH_SALT",
                "changeme_generate_a_real_64char_hex_salt_for_local_dev",
            )

        @property
        def postgres_dsn(self) -> str:
            return (
                f"postgresql://{self.postgres_user}:{self.postgres_password}"
                f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            )

        @property
        def is_development(self) -> bool:
            return self.app_env.lower() in {"development", "dev", "local"}


@lru_cache
def get_settings() -> "Settings":
    """Return a cached ``Settings`` instance."""
    return Settings()


__all__ = ["Settings", "get_settings"]
