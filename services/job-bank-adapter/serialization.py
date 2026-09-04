"""
services/job-bank-adapter/serialization.py
===========================================
Canonical ``JobBankData`` model and its exact serialise / parse contract for
the Job Bank Adapter (task 24.1).

The Regional Analysis Engine consumes Job Bank posting data (active posting
count and median advertised wage) for a claimant's NOC code within an ESDC
economic region. Because the median wage is a monetary value, it MUST be
represented as an exact ``Decimal`` and must survive a serialise -> deserialise
round-trip without any loss of precision — that is the P12 correctness property
(task 24.2). To guarantee this, the ``Decimal`` median wage is serialised as a
*string* rather than a JSON number (which would go through binary float and
lose precision).

The functions here are implemented exactly as specified in the design's
"Round-trip property implementation":

    def serialise_job_bank_data(data: JobBankData) -> dict:
        return {"noc_code": ..., "region_code": ...,
                "active_posting_count": int(...),
                "median_hourly_wage": str(...)}  # Decimal serialised as string

    def parse_job_bank_data(raw: dict) -> JobBankData:
        return JobBankData(..., median_hourly_wage=Decimal(raw["median_hourly_wage"]))

Requirements: 16.1, 16.2, 16.3, 16.4, 16.5
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Optional


@dataclass
class JobBankData:
    """Canonical Job Bank posting summary for a NOC code within a region.

    Attributes:
        noc_code: NOC 2021 code the postings were queried for.
        region_code: ESDC economic region code.
        active_posting_count: Number of postings active during the claim
            period. A non-negative integer.
        median_hourly_wage: Median advertised hourly wage across the matched
            postings, held as an exact ``Decimal``. Serialised as a string to
            preserve precision (P12).
        reference_month: Optional reference month (``date``) the snapshot/cache
            entry pertains to. Not part of the round-trip equality contract for
            counts and wage, but preserved through serialisation when present.
        data_stale: ``True`` when the data was served from a snapshot/cache
            rather than a fresh API call.
    """

    noc_code: str
    region_code: str
    active_posting_count: int
    median_hourly_wage: Decimal
    reference_month: Optional[date] = None
    data_stale: bool = False

    def __post_init__(self) -> None:
        # Normalise types so equality and round-trips are exact and predictable.
        self.noc_code = str(self.noc_code)
        self.region_code = str(self.region_code)
        self.active_posting_count = int(self.active_posting_count)
        if not isinstance(self.median_hourly_wage, Decimal):
            self.median_hourly_wage = Decimal(str(self.median_hourly_wage))
        if self.reference_month is not None and not isinstance(
            self.reference_month, date
        ):
            self.reference_month = date.fromisoformat(str(self.reference_month))


def serialise_job_bank_data(data: JobBankData) -> dict:
    """Serialise a :class:`JobBankData` to a canonical JSON-ready dict.

    The Decimal median wage is emitted as a string so that
    ``parse_job_bank_data(serialise_job_bank_data(d))`` reproduces the exact
    same ``Decimal`` value (P12). No fields are added, removed, or reordered
    relative to this canonical contract.

    Args:
        data: The canonical Job Bank posting summary.

    Returns:
        A dict with keys ``noc_code``, ``region_code``, ``active_posting_count``
        (int), ``median_hourly_wage`` (str), plus optional ``reference_month``
        (ISO ``YYYY-MM-DD`` string) and ``data_stale`` (bool).
    """
    payload: dict[str, Any] = {
        "noc_code": data.noc_code,
        "region_code": data.region_code,
        "active_posting_count": int(data.active_posting_count),
        "median_hourly_wage": str(data.median_hourly_wage),
    }
    if data.reference_month is not None:
        payload["reference_month"] = data.reference_month.isoformat()
    payload["data_stale"] = bool(data.data_stale)
    return payload


def parse_job_bank_data(raw: dict) -> JobBankData:
    """Parse a canonical dict back into a :class:`JobBankData`.

    The median wage is reconstructed with ``Decimal(raw["median_hourly_wage"])``
    directly from the string representation, preserving exact precision.

    Args:
        raw: A dict produced by :func:`serialise_job_bank_data` (or an
            equivalently shaped payload/fixture).

    Returns:
        The reconstructed :class:`JobBankData`.

    Raises:
        KeyError: If a required field is missing.
    """
    reference_month = raw.get("reference_month")
    parsed_month: Optional[date] = None
    if reference_month:
        text = str(reference_month)
        if len(text) == 7:  # YYYY-MM
            text = f"{text}-01"
        parsed_month = date.fromisoformat(text)

    return JobBankData(
        noc_code=raw["noc_code"],
        region_code=raw["region_code"],
        active_posting_count=int(raw["active_posting_count"]),
        median_hourly_wage=Decimal(str(raw["median_hourly_wage"])),
        reference_month=parsed_month,
        data_stale=bool(raw.get("data_stale", False)),
    )


__all__ = [
    "JobBankData",
    "serialise_job_bank_data",
    "parse_job_bank_data",
]
