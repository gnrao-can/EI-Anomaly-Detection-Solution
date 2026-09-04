# EI-ADS Aggregate SBOM — SA&A / PIA artifact

This directory holds the **authoritative aggregate Software Bill of Materials**
for each EI-ADS release. It is the supply-chain artifact submitted with the
Government of Canada **Security Assessment and Authorisation (SA&A)** package
(Requirement 19.6) and referenced by the **Privacy Impact Assessment (PIA)**
document (Requirement 19.8).

## What lives here

| File | Format | Purpose |
|------|--------|---------|
| `ei-ads-<sha>-aggregate.cyclonedx.json` | CycloneDX 1.6 (JSON) | Merged, release-wide SBOM covering every EI-ADS service image built at Git SHA `<sha>` |

The file is a **copy** of the aggregate produced under `sbom/` at the monorepo
root by `scripts/build-images.sh` (task 39.5). It is copied here automatically
at the end of a build so the SA&A / PIA package always has a stable, versioned
location to point at, independent of the transient build workspace.

## How it is produced

After all per-service SBOMs are generated during the build loop,
`scripts/build-images.sh` merges them and scans the result:

```sh
syft --merge sbom/*.cyclonedx.json -o cyclonedx-json \
  > sbom/ei-ads-<sha>-aggregate.cyclonedx.json
grype sbom:sbom/ei-ads-<sha>-aggregate.cyclonedx.json --fail-on high
cp sbom/ei-ads-<sha>-aggregate.cyclonedx.json docs/security/sbom/
```

Full lifecycle, tooling versions (Syft 1.18.1, Grype 0.87.0), format rationale
(CycloneDX 1.6 + SPDX 2.3), and regeneration instructions are documented in
[`sbom/README.md`](../../../sbom/README.md) at the monorepo root.

## Referencing this artifact from the PIA

> **Note:** A standalone Privacy Impact Assessment (PIA) document does not yet
> exist in this repository. When the PIA is authored (Requirement 19.8), its
> supply-chain / third-party-component section MUST reference the aggregate SBOM
> for the assessed release by its Git SHA, i.e.
> `docs/security/sbom/ei-ads-<sha>-aggregate.cyclonedx.json`. Until then, this
> note records where the authoritative artifact lives so the reference can be
> wired in without hunting for it.
