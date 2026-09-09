#!/usr/bin/env python3
"""
scripts/dev/mint-session-cookie.py
==================================
Mint a LOCAL-DEV portal-backend session cookie so the investigator dashboard is
browsable WITHOUT a GCKey IdP.

The portal-backend authenticates every ``/portal/*`` and read ``/api/*`` handler
from a signed HttpOnly session cookie (``ei_ads_session``) that is normally
issued by the GCKey OAuth code exchange. There is no GCKey IdP in the local
compose stack, so this script mints the SAME cookie the BFF would
(``services/portal-backend/auth.py::sign_session``): a value of the form
``<b64url(json)>.<b64url(HMAC-SHA256(json, SESSION_SECRET))>`` whose payload
carries ``role`` (+ optional ``sin_hash``/``access_token``/``subject``) and an
``iat`` issued-at stamp. Nothing is bypassed — the BFF runs its real
``verify_session`` HMAC check against ``SESSION_SECRET``; this script simply
produces a valid signature using the same secret.

The secret MUST match the running portal-backend's ``SESSION_SECRET``. In the
compose stack that is ``local_dev_portal_session_secret_change_me`` (see
podman-compose.yml → portal-backend). Override with ``--secret`` or the
``SESSION_SECRET`` env var if you changed it.

Usage (from the repo root, with the venv python):

    .venv-core/bin/python scripts/dev/mint-session-cookie.py --role INVESTIGATOR

It prints the raw cookie value, a ready-to-paste ``Cookie:`` header, and a
sample ``curl`` against the proxy (http://localhost:8090). To browse the SPA,
set the ``ei_ads_session`` cookie for ``localhost:8090`` in your browser
(DevTools → Application → Cookies) and load the dashboard.

Roles: CLAIMANT | REVIEWER | INVESTIGATOR | ADMINISTRATOR (case-insensitive).
Only INVESTIGATOR / ADMINISTRATOR see the queue + unmasked detail (Req 22.6);
REVIEWER gets the masked queue; CLAIMANT is a claimant-only session.

Uses only the Python standard library (matches the BFF's dependency-free HMAC).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid

_VALID_ROLES = {"CLAIMANT", "REVIEWER", "INVESTIGATOR", "ADMINISTRATOR"}
_COOKIE_NAME = "ei_ads_session"
_DEFAULT_SECRET = "local_dev_portal_session_secret_change_me"


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign_session(payload: dict, secret: str) -> str:
    """Replicate portal-backend auth.sign_session exactly.

    ``<b64url(json)>.<b64url(HMAC-SHA256(b64url(json), secret))>`` with the JSON
    serialised as ``separators=(",", ":"), sort_keys=True`` and an ``iat`` stamp.
    """
    body = dict(payload)
    body.setdefault("iat", int(time.time()))
    encoded = _b64url_encode(
        json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(
        secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded}.{_b64url_encode(signature)}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mint a local-dev portal-backend session cookie."
    )
    parser.add_argument(
        "--role",
        default="INVESTIGATOR",
        help="RBAC role for the session (default: INVESTIGATOR).",
    )
    parser.add_argument(
        "--secret",
        default=os.environ.get("SESSION_SECRET", _DEFAULT_SECRET),
        help="HMAC secret; must match the portal-backend SESSION_SECRET.",
    )
    parser.add_argument(
        "--sin-hash",
        default=None,
        help="Optional sin_hash claim (for a CLAIMANT own-data session).",
    )
    parser.add_argument(
        "--subject",
        default=None,
        help="Optional GCKey subject (used as investigator_id on dispositions).",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8090",
        help="Proxy base URL for the sample curl (default: http://localhost:8090).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print ONLY the raw cookie value (for scripting).",
    )
    args = parser.parse_args()

    role = args.role.strip().upper()
    if role not in _VALID_ROLES:
        parser.error(
            f"unknown role {args.role!r}; expected one of {sorted(_VALID_ROLES)}"
        )

    payload: dict = {"role": role}
    if args.sin_hash:
        payload["sin_hash"] = args.sin_hash
    payload["subject"] = args.subject or f"dev-{role.lower()}-{uuid.uuid4()}"

    cookie_value = sign_session(payload, args.secret)

    if args.quiet:
        print(cookie_value)
        return

    print(f"role:          {role}")
    print(f"cookie name:   {_COOKIE_NAME}")
    print(f"cookie value:  {cookie_value}")
    print()
    print("Cookie header:")
    print(f"  Cookie: {_COOKIE_NAME}={cookie_value}")
    print()
    print("Sample request (investigator queue via the proxy):")
    print(
        f'  curl -s "{args.base_url}/portal/investigator/claims" '
        f'-H "Cookie: {_COOKIE_NAME}={cookie_value}" | jq'
    )
    print()
    print("Browser: DevTools -> Application -> Cookies -> add for the proxy origin:")
    print(f"  name={_COOKIE_NAME}  value=<the cookie value above>")
    print(
        "  (secret must match the running portal-backend SESSION_SECRET; "
        "override with --secret or the SESSION_SECRET env var.)"
    )


if __name__ == "__main__":
    main()
