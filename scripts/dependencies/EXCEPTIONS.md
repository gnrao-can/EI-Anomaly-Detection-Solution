# Accepted CVE Exceptions

This file documents accepted exceptions to the zero-CVE policy.
All exceptions require a GitHub issue reference and a remediation date
no more than 30 days from the date of acceptance.

## Active Exceptions

| Package | CVE | CVSS | Rationale | Issue | Remediation Date |
|---------|-----|------|-----------|-------|-----------------|
| react-router-dom 7.1.1 (via react-router / turbo-stream) | GHSA-rxv8-25v2-qmq8 | 7.5 (High) | DoS via reflected user input in single-fetch. The Claimant Portal does not use React Router's single-fetch data APIs (it uses client-only routing with a FastAPI backend), so the vulnerable code path is not exercised. Version is pinned to 7.1.1 by the canonical frontend-versions manifest; bump to react-router-dom >= 7.11.1 requires updating the manifest and re-verifying. | TBD (frontend deps ticket) | 30 days from acceptance |
| vitest < 3.2.6 (dev-only) | GHSA-5xrq-8626-4rwp | 9.7 (Critical) | Arbitrary file read/exec only when the Vitest UI server is listening. CI and local runs use `vitest run` (no `--ui`), so the UI server is never started. Dev-only dependency, not shipped in the container image. Bump to vitest >= 3.2.6 requires updating the manifest. | TBD (frontend deps ticket) | 30 days from acceptance |

> Note: These exceptions were surfaced by `npm audit --audit-level=high --prefix services/claimant-portal` during scaffold of the Claimant Portal (task 26.1). The pinned versions match `scripts/dependencies/frontend-versions.json`; remediation should update that manifest and re-run verification.

## Format

| Package | CVE | CVSS | Rationale | Issue | Remediation Date |
|---------|-----|------|-----------|-------|-----------------|
