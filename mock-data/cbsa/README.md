# CBSA Mock Data

Deterministic fixtures used by the CBSA Adapter (`services/cbsa-adapter/`) in
development and test. No live CBSA Protected B channel or mTLS certificate is
used in these modes.

## How it works

The `SyntheticCBSAGenerator` (see `services/cbsa-adapter/client.py`) is keyed by
the claimant's **province of employment** and their `sin_hash`. For a given
province it looks for `mock-data/cbsa/<PROVINCE>.json`; if none exists it falls
back to a built-in default profile. Identical inputs always produce identical
crossing records (determinism).

## Fixture formats

A province fixture is a JSON object with one of two shapes:

1. **Explicit records** — pins an exact `records` list. Used verbatim.

   ```json
   {
     "province": "ON",
     "records": [
       { "direction": "EXIT",  "crossing_date": "2024-02-01", "port_of_entry": "YYZ" },
       { "direction": "ENTRY", "crossing_date": "2024-02-11", "port_of_entry": "YYZ" }
     ]
   }
   ```

2. **Profile** — parameters the generator uses to derive a reproducible
   EXIT/ENTRY sequence from `sin_hash`:

   ```json
   { "province": "BC", "trips": 2, "base_days": 14, "port": "YVR" }
   ```

   - `trips` — number of cross-border round trips to generate.
   - `base_days` — base absence length in days per trip (small deterministic
     jitter is added from the seed).
   - `port` — port-of-entry code stamped on each crossing.

## Absence detection

A crossing sequence produces an **absence period** when a consecutive
`EXIT -> ENTRY` gap is **>= 10 days** (inclusive). A 9-day gap is not an
absence; a 10-day gap is. The `ON.json` fixture deliberately includes both a
10-day trip (qualifies) and a 9-day trip (does not) to exercise the boundary.

## Privacy

Travel-record contents are Protected B and must never be written to any
general-purpose application log.
