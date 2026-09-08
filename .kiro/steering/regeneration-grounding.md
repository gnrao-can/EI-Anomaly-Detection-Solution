---
inclusion: always
---

# EI-ADS Regeneration — Authoritative Grounding

These are the source-of-truth documents for the EI-ADS regeneration. Every
implementation and test decision MUST be grounded in them. Do not infer,
reshape, or "work around" what they say to make something pass.

## Authoritative inputs (pinned — always consult before deciding)

- Requirements (acceptance criteria + 15 properties):
  #[[file:.kiro/specs/ei-anomaly-detection-solution/requirements.md]]
- Design (architecture, schemas, time budgets, security):
  #[[file:.kiro/specs/ei-anomaly-detection-solution/design.md]]
- Tasks (implementation plan):
  #[[file:.kiro/specs/ei-anomaly-detection-solution/tasks.md]]
- Regeneration status & roadmap (living log):
  #[[file:docs/regeneration/STATUS.md]]

## Operating rules (set by the project owner)

1. **Validate the implementation against the spec — do NOT reshape the test or
   the design to fit a quirk of the current code.** Tests assert what the
   implementation genuinely does. If that reveals a gap against requirements,
   we refactor the IMPLEMENTATION, not the test.
   - Anti-pattern that caused prior fragility: bending topology/wiring so a
     thread goes "green" (e.g. pointing all six engine URLs at one engine to
     satisfy the ≥3-engine rule). This is forbidden.

2. **The ≥3-engine minimum is real (Req 2.5 / design).** The pipeline runs all
   SIX engines concurrently (Req 2.1, 2.3). If fewer than three produce a
   result, the decision is INDETERMINATE, payment is halted, and the claim is
   queued for manual review. With only one engine implemented, the
   requirement-faithful outcome of the thin thread is INDETERMINATE — assert
   that, do not manufacture completion.

3. **Audit trail must be per requirement (Req 2.6, 2.7, 19.3, 11.6).** Complete,
   IMMUTABLE audit trail; each entry written within 500 ms; ≥7-year retention.
   PostgreSQL `audit_log` immutability is enforced via Row-Level Security (design
   "Audit Log Immutability"). Audit tests are requirement-backed and must not be
   skipped or weakened.

4. **No seam is "done" until an integration test exercises it over REAL
   transports** (real Kafka, real HTTP, real auth). In-process loopback HTTP for
   a service under test is acceptable; faking the seam is not.

5. **Small, verified increments.** Do not delegate large multi-service work to a
   subagent. Commit each verified step. Keep the archive branch as recovery.

6. **When an implementation choice arises, cite the specific requirement/design
   line it satisfies.** If the spec is silent or contradictory, STOP and ask
   rather than inventing a resolution.

## Key spec facts to not re-derive incorrectly

- Risk_Decision bands (Req 9.2 / glossary): AUTO_APPROVE [0,29], FLAG_FOR_REVIEW
  [30,69], INVESTIGATE [70,100]; INDETERMINATE is the <3-engine degradation.
- Per-engine time budgets (design "Time Budget Allocation"): Repetitive 400 ms,
  Geographic 600 ms, Fraud Ring 800 ms, History 500 ms, Document Forgery 700 ms,
  Regional 500 ms; Ensemble Scorer 300 ms; end-to-end p99 ≤ 2000 ms (Req 2.2, 17.1).
- All EI-ADS endpoints validate a GCKey Bearer token: 401 unauthenticated,
  403 insufficient scope (Req 19.7).
