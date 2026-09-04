"""
services/claim-intake/hashing.py
================================
SIN hashing utilities for the Claim Intake Service.

The raw Social Insurance Number (SIN) is classified Protected B and MUST NEVER
be persisted to any storage medium or written to any log. It is held only in
memory during intake validation and is immediately replaced by a one-way
SHA-256 digest (salted per environment) on receipt.

This module exposes a single pure function, ``hash_sin()``, which:
  - normalises the raw SIN (strips spaces and dashes),
  - concatenates it with the per-environment salt read from settings
    (``SIN_HASH_SALT``),
  - returns the SHA-256 hex digest.

The function never logs the raw SIN and never returns it.

Requirements: 1.11, 19.1
"""

from __future__ import annotations

import hashlib

__all__ = ["hash_sin", "normalize_sin_for_hash"]


def normalize_sin_for_hash(raw_sin: str) -> str:
    """Normalise a SIN prior to hashing.

    Strips ASCII spaces and dashes so that logically identical SINs entered
    with different separators (``"123-456-789"`` vs ``"123456789"``) produce
    the same digest. No validation is performed here — validation is the
    responsibility of ``shared.validation``.

    Parameters
    ----------
    raw_sin:
        The raw SIN string as received from the claimant.

    Returns
    -------
    str
        The SIN with spaces and dashes removed.
    """
    if raw_sin is None:  # defensive: never allow a None to reach hashlib
        raise ValueError("SIN must not be None")
    return raw_sin.replace(" ", "").replace("-", "")


def hash_sin(raw_sin: str, salt: str) -> str:
    """Return the SHA-256 hex digest of a normalised SIN combined with *salt*.

    The digest is deterministic for a given ``(raw_sin, salt)`` pair and is
    salt-dependent: the same SIN under two different salts produces two
    different digests. The raw SIN is never logged, stored, or returned.

    Parameters
    ----------
    raw_sin:
        The raw SIN string. Spaces and dashes are normalised away before hashing.
    salt:
        The per-environment salt (read from ``settings.sin_hash_salt`` /
        ``SIN_HASH_SALT``). Must be a non-empty string.

    Returns
    -------
    str
        A 64-character lowercase SHA-256 hexadecimal digest.

    Raises
    ------
    ValueError
        If ``salt`` is empty or missing (a real salt is mandatory to protect
        against rainbow-table attacks on the small SIN keyspace).
    """
    if not salt:
        raise ValueError(
            "A non-empty SIN_HASH_SALT is required; refusing to hash without a salt."
        )

    normalized = normalize_sin_for_hash(raw_sin)
    digest_input = f"{normalized}{salt}".encode("utf-8")
    return hashlib.sha256(digest_input).hexdigest()
