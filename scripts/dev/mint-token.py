#!/usr/bin/env python3
"""
scripts/dev/mint-token.py
=========================
Mint a LOCAL-DEV GCKey-style Bearer token for a given role, signed by the SAME
dev keypair that `gen-service-token.py` published as the JWKS the running stack
serves. Use it to exercise auth-gated endpoints from curl without a GCKey IdP.

The most common use in the task-30 walkthrough is a CLAIMANT token to submit a
claim (POST /api/v1/claims requires the CLAIMANT role; the service token in
.env.dev-token is INVESTIGATOR and cannot submit).

Prerequisite: run `scripts/dev/gen-service-token.py` FIRST — it writes the
signing key to `scripts/dev/jwks/signing-key.pem` (gitignored) and the public
JWKS the services validate against. This script reuses that key so the minted
token validates end-to-end (nothing is bypassed — Req 19.7).

Usage (from the repo root, with the venv python):

    .venv-core/bin/python scripts/dev/mint-token.py --role CLAIMANT
    # capture it for curl:
    CLAIMANT_TOKEN=$(.venv-core/bin/python scripts/dev/mint-token.py --role CLAIMANT)

Roles: CLAIMANT | REVIEWER | INVESTIGATOR | ADMINISTRATOR (case-insensitive).

Requires `python-jose` (present in .venv-core / .venv-itest).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid

from jose import jwt

_KID = "ei-ads-local-dev"
_HERE = os.path.dirname(os.path.abspath(__file__))
_KEY_PATH = os.path.join(_HERE, "jwks", "signing-key.pem")
_VALID_ROLES = {"CLAIMANT", "REVIEWER", "INVESTIGATOR", "ADMINISTRATOR"}
_DEFAULT_TTL = int(os.environ.get("DEV_TOKEN_TTL_SECONDS", str(24 * 60 * 60)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint a local-dev GCKey Bearer token.")
    parser.add_argument(
        "--role",
        default="CLAIMANT",
        help="RBAC role to encode (default: CLAIMANT).",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=_DEFAULT_TTL,
        help="Token lifetime in seconds (default: 24h).",
    )
    parser.add_argument(
        "--sub",
        default=None,
        help="Optional token subject; defaults to a generated svc-<role> id.",
    )
    args = parser.parse_args()

    role = args.role.strip().upper()
    if role not in _VALID_ROLES:
        parser.error(
            f"unknown role {args.role!r}; expected one of {sorted(_VALID_ROLES)}"
        )

    if not os.path.exists(_KEY_PATH):
        sys.stderr.write(
            "error: signing key not found at "
            f"{os.path.relpath(_KEY_PATH)}.\n"
            "Run `scripts/dev/gen-service-token.py` first (it writes the key + JWKS).\n"
        )
        sys.exit(1)

    with open(_KEY_PATH, "r", encoding="utf-8") as handle:
        private_pem = handle.read()

    now = int(time.time())
    sub = args.sub or f"svc-{role.lower()}-{uuid.uuid4()}"
    claims = {
        "sub": sub,
        "iat": now,
        "nbf": now,
        "exp": now + args.ttl,
        "roles": [role],
    }
    token = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": _KID})
    # Print ONLY the token to stdout so it is safe to capture in a shell var.
    print(token)


if __name__ == "__main__":
    main()
