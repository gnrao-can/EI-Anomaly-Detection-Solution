# Portal Backend-for-Frontend (BFF)

The Portal BFF is the single `http://localhost:8000` origin the
[`claimant-portal`](../claimant-portal) SPA talks to. Its Vite dev server proxies
`/auth`, `/api`, and `/portal` to this service (see the SPA `vite.config.ts`).

It:

- authenticates the browser via **GCKey OAuth 2.0** (authorization-code + PKCE)
  and issues a signed **HttpOnly session cookie**; the GCKey access token is
  carried in that session so downstream calls can forward it as a Bearer;
- serves the claimant status view and the investigator queue / detail from
  PostgreSQL, applying **role-based PII masking** server-side (Req 22.1, 22.2,
  22.6);
- records investigator dispositions and publishes `outcome.feedback` to Kafka
  for `CONFIRMED_FRAUD` / `FALSE_POSITIVE` (Req 22.4);
- streams flagged claims over **SSE** (Req 22.1); and
- forwards the claim-submission and Privacy Act write paths to the Claim Intake
  Service — attaching the caller's **GCKey Bearer** (design GCKey flow "Forward
  with Bearer token"; Req 19.7) — so the browser only ever sees one origin. The
  write logic is *not* duplicated here; Claim Intake independently validates the
  Bearer via JWKS.

Every external dependency (PostgreSQL, Kafka, httpx) is guarded, so the service
imports and runs locally with **no live DB / Kafka** — it degrades to an
in-memory repository and a no-op feedback publisher, mirroring `claim-intake`.
Authentication, however, requires a reachable GCKey IdP (there is no dev-auth
stub); tests use faked token-exchange functions to exercise the callback wiring.

## Endpoints

| Method | Path | Auth | Notes |
| ------ | ---- | ---- | ----- |
| GET | `/healthz` | none | DB/Kafka connectivity, 200/503 |
| GET | `/portal/claims/{claim_id}` | session | Claimant own-data status; 404 when absent |
| GET | `/portal/investigator/claims` | REVIEWER / INVESTIGATOR / ADMINISTRATOR | `{claims:[...]}`, sorted by `risk_score` desc; REVIEWER PII masked |
| GET | `/portal/investigator/claims/{claim_id}` | REVIEWER / INVESTIGATOR / ADMINISTRATOR | Full assessment; 404 when absent |
| PUT | `/api/v1/claims/{claim_id}/disposition` | INVESTIGATOR / ADMINISTRATOR | Persists disposition; publishes `outcome.feedback` for confirmed/false-positive |
| GET | `/portal/events/claims` | REVIEWER / INVESTIGATOR / ADMINISTRATOR | SSE `text/event-stream` of flagged claims |
| POST | `/api/v1/claims` | CLAIMANT session | Forward multipart to Claim Intake with GCKey Bearer |
| GET | `/api/v1/privacy/access` | CLAIMANT session | Forward to Claim Intake with GCKey Bearer |
| DELETE | `/api/v1/privacy/delete` | CLAIMANT session | Forward to Claim Intake with GCKey Bearer |
| GET | `/submit` | — | GCKey flow start (302 to GCKey `/authorize`) |
| GET | `/auth/authorize` | — | SPA login landing (302 to GCKey `/authorize`) |
| GET | `/auth/gckey/callback` | — | GCKey code exchange (browser redirect) + cookie |
| POST | `/auth/callback` | — | GCKey code exchange (SPA PKCE) + cookie |
| GET | `/auth/session` | session | UserSession for the SPA AuthContext; 401 when absent |
| POST | `/auth/logout` | — | Clears the session cookie |
| GET | `/auth/gckey/logout` | — | Clears the session cookie |

## Authentication model

GCKey OAuth 2.0 (authorization-code + PKCE) is the **only** auth mode:

1. `GET /submit` (or `/auth/authorize`) → 302 to the GCKey `/authorize` endpoint,
   with the server-owned PKCE verifier + CSRF `state` in a short-lived signed
   cookie.
2. GCKey redirects back to `/auth/gckey/callback` (browser) or the SPA posts to
   `/auth/callback` (PKCE) with the authorization `code`.
3. The BFF exchanges the code for tokens, validates the ID token via
   `shared.auth.validate_gckey_token` (JWKS), and issues the signed HttpOnly
   session cookie — which carries the GCKey **access token**.
4. On `POST /api/v1/claims` and the Privacy Act proxies, the BFF attaches
   `Authorization: Bearer <access token>` from the session when forwarding to
   Claim Intake. It never forwards the browser session cookie upstream. A
   session without an access token yields 401.

## Run locally

From this directory (`services/portal-backend`):

```bash
PYTHONPATH=$(git rev-parse --show-toplevel) \
  python -m uvicorn main:app --port 8000
```

`PYTHONPATH` must point at the repo root so `shared/` imports resolve (the same
convention every EI-ADS service uses). A reachable GCKey IdP (or a compatible
OIDC provider configured via the `GCKEY_*` env vars) is required to obtain a
session.

## Configuration

Environment variables (sensible local defaults apply where possible):

- `SESSION_SECRET` — HMAC secret for the session cookie (override outside dev).
- `SESSION_COOKIE_SECURE` — set `true` to add the `Secure` flag (HTTPS deploys).
- `CLAIM_INTAKE_URL` — downstream forward target (default `http://localhost:8001`).
- `POSTGRES_*` — read-side database connection.
- `KAFKA_BOOTSTRAP_SERVERS` — for the `outcome.feedback` publish.
- `GCKEY_*` — `GCKEY_AUTHORIZE_URL`, `GCKEY_TOKEN_URL`, `GCKEY_JWKS_URL`,
  `GCKEY_CLIENT_ID`, `GCKEY_CLIENT_SECRET`, `GCKEY_REDIRECT_URI`, `GCKEY_SCOPE`,
  `GCKEY_ISSUER`.
- `SIN_HASH_SALT` — must match the Claim Intake Service to compare `sin_hash`.

## Tests

```bash
PYTHONPATH=$(git rev-parse --show-toplevel) \
  python -m pytest tests/unit/test_portal_backend.py -q
```
