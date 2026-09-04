# Portal Backend-for-Frontend (BFF)

The Portal BFF is the single `http://localhost:8000` origin the
[`claimant-portal`](../claimant-portal) SPA talks to. Its Vite dev server proxies
`/auth`, `/api`, and `/portal` to this service (see the SPA `vite.config.ts`).

It:

- authenticates every request via a signed **HttpOnly session cookie**
  (`AUTH_MODE=dev` stub, or real `AUTH_MODE=gckey` OAuth 2.0 + PKCE);
- serves the claimant status view and the investigator queue / detail from
  PostgreSQL, applying **role-based PII masking** server-side (Req 22.1, 22.2,
  22.6);
- records investigator dispositions and publishes `outcome.feedback` to Kafka
  for `CONFIRMED_FRAUD` / `FALSE_POSITIVE` (Req 22.4);
- streams flagged claims over **SSE** (Req 22.1); and
- **reverse-proxies** the claim-submission and Privacy Act write paths to the
  Claim Intake Service so the browser only ever sees one origin. The write logic
  is *not* duplicated here.

Every external dependency (PostgreSQL, Kafka, httpx) is guarded, so the service
imports and runs locally with **no live DB / Kafka** — it degrades to an
in-memory repository and a no-op feedback publisher, mirroring `claim-intake`.

## Endpoints

| Method | Path | Auth | Notes |
| ------ | ---- | ---- | ----- |
| GET | `/healthz` | none | DB/Kafka connectivity, 200/503 |
| GET | `/portal/claims/{claim_id}` | session | Claimant own-data status; 404 when absent |
| GET | `/portal/investigator/claims` | REVIEWER / INVESTIGATOR / ADMINISTRATOR | `{claims:[...]}`, sorted by `risk_score` desc; REVIEWER PII masked |
| GET | `/portal/investigator/claims/{claim_id}` | REVIEWER / INVESTIGATOR / ADMINISTRATOR | Full assessment; 404 when absent |
| PUT | `/api/v1/claims/{claim_id}/disposition` | INVESTIGATOR / ADMINISTRATOR | Persists disposition; publishes `outcome.feedback` for confirmed/false-positive |
| GET | `/portal/events/claims` | REVIEWER / INVESTIGATOR / ADMINISTRATOR | SSE `text/event-stream` of flagged claims |
| POST | `/api/v1/claims` | (upstream) | Reverse-proxy to Claim Intake `POST /api/v1/claims` |
| GET | `/api/v1/privacy/access` | (upstream) | Reverse-proxy to Claim Intake |
| DELETE | `/api/v1/privacy/delete` | (upstream) | Reverse-proxy to Claim Intake |
| GET | `/auth/dev/login` | — | Dev-auth stub (only when `AUTH_MODE=dev`) |
| GET | `/auth/logout` | — | Clears the session cookie |
| GET | `/submit` | — | GCKey flow start (302 to `/authorize`) — `AUTH_MODE=gckey` |
| GET | `/auth/gckey/callback` | — | GCKey code exchange + cookie |
| GET | `/auth/gckey/logout` | — | Clears the session cookie |

## Run locally (dev auth, no DB/Kafka needed)

From this directory (`services/portal-backend`):

```bash
PYTHONPATH=$(git rev-parse --show-toplevel) AUTH_MODE=dev \
  python -m uvicorn main:app --port 8000
```

`PYTHONPATH` must point at the repo root so `shared/` imports resolve (the same
convention every EI-ADS service uses).

### Dev login URLs

Open one of these in the browser (or `curl -c cookies.txt`) to mint a session
cookie, then load the SPA at `http://localhost:3000`:

- Investigator: `http://localhost:8000/auth/dev/login?role=INVESTIGATOR`
- Reviewer (PII masked): `http://localhost:8000/auth/dev/login?role=REVIEWER`
- Administrator: `http://localhost:8000/auth/dev/login?role=ADMINISTRATOR`
- Claimant (own data): `http://localhost:8000/auth/dev/login?role=CLAIMANT&sin_hash=<hash>`
- Log out: `http://localhost:8000/auth/logout`

## Configuration

Environment variables (all optional in dev — sensible defaults apply):

- `AUTH_MODE` — `dev` (default) or `gckey`.
- `SESSION_SECRET` — HMAC secret for the session cookie (override outside dev).
- `CLAIM_INTAKE_URL` — reverse-proxy target (default `http://localhost:8001`).
- `POSTGRES_*` — read-side database connection.
- `KAFKA_BOOTSTRAP_SERVERS` — for the `outcome.feedback` publish.
- `GCKEY_*` — authorize/token/JWKS/redirect/client settings for `AUTH_MODE=gckey`.
- `SIN_HASH_SALT` — must match the Claim Intake Service to compare `sin_hash`.

## Tests

```bash
PYTHONPATH=$(git rev-parse --show-toplevel) \
  python -m pytest tests/unit/test_portal_backend.py -q
```
