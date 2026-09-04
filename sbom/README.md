# EI-ADS Software Bill of Materials (SBOM)

This directory holds the per-image Software Bill of Materials generated for every
EI-ADS container image at build time. Committing the SBOMs alongside the Git SHA
means every release tag has a corresponding, auditable supply-chain snapshot —
one of the supporting artifacts for the Government of Canada Security Assessment
and Authorisation (SA&A) review (Requirement 19.6).

## What lives here

For each service built by `scripts/build-images.sh`, two SBOM files are written,
named by the service and the short Git SHA of the build:

| File | Format | Purpose |
|------|--------|---------|
| `<service>-<sha>.cyclonedx.json` | CycloneDX 1.6 (JSON) | Primary SBOM; consumed by Grype (39.2) and embedded as an OCI annotation (39.3) |
| `<service>-<sha>.spdx.json`      | SPDX 2.3 (JSON)      | Interchange format for tools/partners that require SPDX |

`.gitkeep` keeps the directory tracked when it is otherwise empty.

Additional artifacts in this directory:
- `39.2` — `<service>-<sha>-grype-report.json` (OSV vulnerability scan results)
- `39.3` — `oci/<service>-<sha>/` (OCI layout carrying the SBOM-digest annotation)
- `39.4` — `manifest.json` (index of all SBOMs by service, SHA, image digest, timestamp)
- `39.5` — `ei-ads-<sha>-aggregate.cyclonedx.json` (merged release-wide SBOM)

## Tooling

SBOMs are produced with **Syft** and scanned with **Grype**, both from Anchore,
and **skopeo** embeds the SBOM digest into each image as an OCI annotation.
These are standalone binaries (not pip packages). The required, pinned versions
are recorded under `[tool.sbom]` in `scripts/dependencies/python-versions.toml`:

| Tool | Version | Used by |
|------|---------|---------|
| Syft   | `1.18.1` | 39.1 — CycloneDX + SPDX generation |
| Grype  | `0.87.0` | 39.2 — OSV vulnerability scan |
| skopeo | `1.16.1` | 39.3 — embed SBOM digest as OCI annotation |

Install (CI or build host):

```sh
curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh \
  | sh -s -- -b /usr/local/bin v1.18.1
curl -sSfL https://raw.githubusercontent.com/anchore/grype/main/install.sh \
  | sh -s -- -b /usr/local/bin v0.87.0
# skopeo ships in most distro repos, e.g. `dnf install -y skopeo` / `apt-get install -y skopeo`.
```

## SBOM digest as an OCI annotation (39.3)

After each image is scanned, `build-images.sh` computes the SHA-256 digest of
the CycloneDX SBOM (`sha256sum sbom/<service>-<sha>.cyclonedx.json`) and
re-materialises the image as an OCI layout under `sbom/oci/<service>-<sha>/`,
baking three provenance annotations into the manifest via
`skopeo copy --dest-oci-accept-uncompressed-layers`:

| Annotation | Value |
|------------|-------|
| `org.opencontainers.image.ref.name` | the SHA-tagged image reference |
| `org.cyclonedx.sbom.digest`         | `sha256:<digest of the CycloneDX SBOM>` |
| `org.cyclonedx.sbom.url`            | repo-relative path to the CycloneDX SBOM |

This lets OpenShift's image admission controller verify at deploy time that the
image is accompanied by the exact SBOM it was scanned against. As with SBOM
generation, a missing `skopeo` (or a missing CycloneDX SBOM) is **fatal in CI**
(`REQUIRE_SBOM=true`) and a **soft skip** in a local sandbox.

## Aggregate release SBOM (39.5)

After the per-service build loop finishes, `build-images.sh` merges every
per-service CycloneDX SBOM into a single **release-wide** document and scans it
as a whole:

```sh
syft --merge sbom/*.cyclonedx.json -o cyclonedx-json \
  > sbom/ei-ads-<sha>-aggregate.cyclonedx.json
grype sbom:sbom/ei-ads-<sha>-aggregate.cyclonedx.json --fail-on high
```

The merge deliberately excludes any pre-existing `*-aggregate.cyclonedx.json`
so a re-run never folds a stale aggregate back into the new one. A HIGH or
CRITICAL CVE in the merged SBOM aborts the run (via `set -e`) exactly like the
per-service scan.

The aggregate is the **authoritative supply-chain artifact** submitted with the
Government of Canada Security Assessment and Authorisation (SA&A) package
(Requirement 19.6). A clean copy is published to `docs/security/sbom/`, where
the Privacy Impact Assessment (PIA, Requirement 19.8) references it — see
`docs/security/sbom/README.md`.

As with per-service SBOMs, a missing `syft`/`grype` (or the absence of any
per-service SBOM to merge) is **fatal in CI** (`REQUIRE_SBOM=true`) and a
**soft skip** in a local sandbox.

## SBOM lifecycle

The SBOM lifecycle is anchored to the immutable short Git SHA of the build and
runs inside `scripts/build-images.sh`:

1. **Generate** (39.1) — after each successful `podman build`, Syft emits the
   image's SBOM in both CycloneDX 1.6 and SPDX 2.3 JSON.
2. **Scan** (39.2) — Grype scans the per-service CycloneDX SBOM against the OSV
   database and gates the build on `--fail-on high` before `latest` is tagged.
3. **Annotate** (39.3) — the SBOM's SHA-256 digest is baked into an OCI layout
   of the image so admission control can verify provenance at deploy time.
4. **Publish** (39.4) — SBOMs are pushed to the OpenShift registry / artifact
   store with a `manifest.json` index.
5. **Aggregate** (39.5) — once every service is built, all per-service
   CycloneDX SBOMs are merged into one release-wide SBOM, scanned as a whole,
   and copied to `docs/security/sbom/` for the SA&A / PIA package.

Every artifact is named by the short Git SHA, so a release tag always maps to a
reproducible, auditable supply-chain snapshot.

## Format choices

| Format | Version | Why |
|--------|---------|-----|
| CycloneDX | 1.6 (JSON) | Primary format; native input for Grype, OCI annotations, and the aggregate merge. The security-focused schema the SA&A package expects. |
| SPDX | 2.3 (JSON) | Interchange format for tools and partners that standardise on SPDX. |

## Regenerating SBOMs for a given Git SHA

1. Check out the Git SHA you want to reproduce: `git checkout <sha>`.
2. Build the images: `scripts/build-images.sh` (optionally `SERVICES=<name>` to
   scope to one service). Each successful build regenerates the CycloneDX and
   SPDX SBOMs for that image into this directory.

To regenerate an SBOM for an image that is already present locally, without a
full rebuild:

```sh
syft ei-ads/<service>:<sha> -o cyclonedx-json > sbom/<service>-<sha>.cyclonedx.json
syft ei-ads/<service>:<sha> -o spdx-json      > sbom/<service>-<sha>.spdx.json
```

To rebuild just the **aggregate** release SBOM from per-service SBOMs already
present in this directory (without a full image rebuild):

```sh
syft --merge sbom/*.cyclonedx.json -o cyclonedx-json \
  > sbom/ei-ads-<sha>-aggregate.cyclonedx.json
grype sbom:sbom/ei-ads-<sha>-aggregate.cyclonedx.json --fail-on high
cp sbom/ei-ads-<sha>-aggregate.cyclonedx.json docs/security/sbom/
```

## Notes

- In **CI**, Syft is **required**: a missing or version-mismatched binary is a
  build failure so no image ships without an SBOM.
- In a local **sandbox** where Syft is not installed, `build-images.sh` emits a
  warning and skips SBOM generation so local iteration is not blocked.
