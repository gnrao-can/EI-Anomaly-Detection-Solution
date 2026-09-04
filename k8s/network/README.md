# EI-ADS Network Policies + Ingress (task 36.4)

NetworkPolicy and Ingress manifests that lock down egress for the Protected B
adapters and detection engines, and expose the Claimant Portal over TLS 1.3 with
GCKey OAuth 2.0. Namespace: `ei-ads-prod` (matches the 36.1 Deployments).
_Requirements: 13.4, 19.1 (also 19.2, 19.7, 1.1)._

These files are scoped under `k8s/network/` (separate from `k8s/network-policies/`
and `k8s/ingress/` placeholders) to avoid conflicting with the concurrent 36.3
Secrets/ConfigMaps task.

## Files

| File | Kind(s) | Purpose |
|---|---|---|
| `detection-engines-egress.yaml` | 2× NetworkPolicy | Default-deny egress + intra-cluster/DNS allow for all six detection engines. |
| `cbsa-adapter-egress.yaml` | NetworkPolicy | Restrict `cbsa-adapter` egress to the CBSA Protected B channel only. |
| `cra-adapter-egress.yaml` | NetworkPolicy | Restrict `cra-adapter` egress to the GC secure data exchange only. |
| `claimant-portal-ingress.yaml` | Ingress | TLS 1.3 termination + GCKey OAuth 2.0 redirect URIs, routing to `claimant-portal:3000`. |

## Selectors (matched to the 36.1 Deployments)

Pod selectors reuse the labels the 36.1 Deployment manifests actually set:

- Detection engines: `app.kubernetes.io/component: detection-engine`
  (`repetitive-pattern-engine`, `geographic-anomaly-engine`, `fraud-ring-engine`,
  `history-inconsistency-engine`, `document-forgery-engine`,
  `regional-analysis-engine`).
- Adapters: `app.kubernetes.io/name: cbsa-adapter` / `cra-adapter` — the project
  label convention (matches `services/cbsa-adapter/`, `services/cra-adapter/`).
  The 36.1 topology deployed `roe-adapter` only; the external-data adapters use
  the same `app.kubernetes.io/{name,part-of}` scheme, so these policies select
  them by name and take effect once their Deployments are added.

## How the egress lockdown works

Kubernetes NetworkPolicy is allow-list based per pod. Once **any** egress policy
selects a pod, all egress is denied except what a rule explicitly allows.

- `detection-engines-default-deny-egress` has an **empty** `egress: []` list —
  a hard default-deny for the six engines.
- `detection-engines-allow-intra-cluster-and-dns` re-permits only same-namespace
  pods and DNS to `kube-system`. No rule allows a `0.0.0.0/0` ipBlock, so
  **public-internet egress stays denied**.
- `cbsa-adapter` / `cra-adapter` policies allow intra-namespace + DNS **plus** a
  single `ipBlock` on TCP 443 pointing at the secure-channel CIDR (never
  `0.0.0.0/0`; RFC1918/link-local excluded via `except`).

> The secure-channel CIDRs (`198.103.0.0/16` for CBSA, `205.193.0.0/16` for the
> GC secure data exchange) are **placeholders**. Replace them with the real
> Protected B interconnect ranges before applying. They are recorded in the
> policy annotations (`ei-ads/cbsa-protected-b-cidr`,
> `ei-ads/gc-secure-exchange-cidr`).

## Claimant Portal Ingress

- **TLS 1.3 termination** on `portal.ei-ads.gc.ca` using Secret
  `claimant-portal-tls` (provisioned by 36.3). Protocol pinned to TLSv1.3 via
  `ssl-protocols`/`ssl-min-version` annotations, with forced HTTPS + HSTS.
- **GCKey OAuth 2.0** redirect URIs / paths:
  - `/submit` — unauthenticated; portal issues a 302 to GCKey `/authorize`.
  - `/auth/gckey/callback` — the registered `redirect_uri`
    (`https://portal.ei-ads.gc.ca/auth/gckey/callback`) where the authorization
    code lands and is exchanged for tokens.
  - `/auth/gckey/logout` — post-logout redirect.
  - `/` (SPA) and `/api/v1/claims` (authenticated submission).
- Routes to Service `claimant-portal` on port `3000` (the Deployment's
  containerPort from 36.1). A `Service/claimant-portal` exposing 3000 is assumed
  and deployed alongside the Deployment.

## Validation

```bash
python3 scripts/k8s/validate_network.py
```

The validator uses PyYAML `safe_load_all` (multi-document safe) and asserts:
namespace `ei-ads-prod`; the detection-engine default-deny + allow pair with no
ipBlock egress; CBSA/CRA adapter egress on TCP 443 with a non-public CIDR; no
policy anywhere allows `0.0.0.0/0`; and the Ingress terminates TLS 1.3, exposes
the GCKey callback, and routes to `claimant-portal:3000`.

## Sandbox limitation

These manifests were **validated for YAML/structural correctness only**. They
were **not** applied to a live cluster — no Kubernetes/OpenShift cluster is
available in the sandbox, and enforcement additionally depends on a CNI that
supports NetworkPolicy egress rules, an ingress controller honouring the TLS 1.3
annotations, the `claimant-portal-tls` Secret (36.3), a `Service/claimant-portal`,
and the real Protected B secure-channel CIDRs. Live apply and enforcement
verification happen at the checkpoint in task 37.
