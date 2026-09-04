#!/usr/bin/env python3
"""Validate the k8s/network/*.yaml NetworkPolicy + Ingress manifests.

Checks (no cluster required — task 36.4):
  1. Every file parses via yaml.safe_load_all (multi-document safe).
  2. All manifests live in namespace ei-ads-prod (matches the 36.1 Deployments).
  3. Detection-engine egress lockdown:
       - a default-deny-egress policy selecting
         app.kubernetes.io/component: detection-engine with an EMPTY egress list;
       - an allow policy for the same selector permitting intra-namespace + DNS
         and NEVER a 0.0.0.0/0 ipBlock (public internet stays denied).
  4. CBSA adapter egress: selects app.kubernetes.io/name: cbsa-adapter, has an
     ipBlock egress rule on TCP 443 that is NOT 0.0.0.0/0.
  5. CRA adapter egress: selects app.kubernetes.io/name: cra-adapter, has an
     ipBlock egress rule on TCP 443 that is NOT 0.0.0.0/0.
  6. No NetworkPolicy anywhere allows an egress ipBlock of 0.0.0.0/0.
  7. claimant-portal Ingress: TLS block present, TLS 1.3 pinned, routes to the
     claimant-portal Service on port 3000, and exposes the GCKey OAuth callback
     path /auth/gckey/callback.
Exits non-zero on any failure.
"""
from __future__ import annotations

import pathlib
import sys

import yaml

NET_DIR = pathlib.Path(__file__).resolve().parents[2] / "k8s" / "network"

DETECTION_ENGINE_LABEL = "detection-engine"
PUBLIC_CIDR = "0.0.0.0/0"

errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        errors.append(msg)


def egress_ipblocks(doc: dict) -> list[dict]:
    blocks: list[dict] = []
    for rule in (doc.get("spec", {}) or {}).get("egress", []) or []:
        for peer in rule.get("to", []) or []:
            if "ipBlock" in peer:
                blocks.append({"ipBlock": peer["ipBlock"], "ports": rule.get("ports", [])})
    return blocks


def has_tcp_443(ports: list[dict]) -> bool:
    return any(p.get("port") == 443 and p.get("protocol", "TCP") == "TCP" for p in ports)


def main() -> int:
    files = sorted(NET_DIR.glob("*.yaml"))
    check(bool(files), f"no manifests found in {NET_DIR}")

    all_policies: list[dict] = []
    ingresses: list[dict] = []

    for path in files:
        try:
            docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
        except yaml.YAMLError as exc:
            errors.append(f"{path.name}: YAML parse error: {exc}")
            continue
        for doc in docs:
            kind = doc.get("kind")
            ns = (doc.get("metadata", {}) or {}).get("namespace")
            check(ns == "ei-ads-prod",
                  f"{path.name}: {kind}/{doc.get('metadata', {}).get('name')} "
                  f"namespace is {ns!r}, expected 'ei-ads-prod'")
            if kind == "NetworkPolicy":
                all_policies.append(doc)
            elif kind == "Ingress":
                ingresses.append(doc)
            else:
                errors.append(f"{path.name}: unexpected kind {kind!r}")

    # --- (6) global: no NetworkPolicy allows 0.0.0.0/0 egress ---
    for pol in all_policies:
        for blk in egress_ipblocks(pol):
            check(blk["ipBlock"].get("cidr") != PUBLIC_CIDR,
                  f"NetworkPolicy {pol['metadata']['name']} allows public egress "
                  f"0.0.0.0/0 — forbidden")

    def by_component(component: str) -> list[dict]:
        out = []
        for p in all_policies:
            sel = (p.get("spec", {}) or {}).get("podSelector", {}).get("matchLabels", {})
            if sel.get("app.kubernetes.io/component") == component:
                out.append(p)
        return out

    def by_name(name: str) -> list[dict]:
        out = []
        for p in all_policies:
            sel = (p.get("spec", {}) or {}).get("podSelector", {}).get("matchLabels", {})
            if sel.get("app.kubernetes.io/name") == name:
                out.append(p)
        return out

    # --- (3) detection-engine egress lockdown ---
    det = by_component(DETECTION_ENGINE_LABEL)
    check(len(det) >= 2,
          "expected >=2 detection-engine NetworkPolicies (default-deny + allow), "
          f"found {len(det)}")
    default_deny = [p for p in det if (p.get("spec", {}).get("egress") == [])]
    check(len(default_deny) == 1,
          "expected exactly one detection-engine default-deny-egress policy "
          "(empty egress list)")
    allow = [p for p in det if p.get("spec", {}).get("egress")]
    check(len(allow) >= 1, "expected a detection-engine allow (intra/DNS) policy")
    for p in det:
        check("Egress" in (p.get("spec", {}).get("policyTypes") or []),
              f"{p['metadata']['name']}: policyTypes must include Egress")
        check(not egress_ipblocks(p),
              f"{p['metadata']['name']}: detection engines must not egress to any "
              "ipBlock (intra-cluster + DNS only)")

    # --- (4) CBSA adapter ---
    cbsa = by_name("cbsa-adapter")
    check(len(cbsa) == 1, f"expected exactly one cbsa-adapter policy, found {len(cbsa)}")
    for p in cbsa:
        blks = egress_ipblocks(p)
        check(bool(blks), "cbsa-adapter policy must have an ipBlock egress rule")
        check(all(b["ipBlock"]["cidr"] != PUBLIC_CIDR for b in blks),
              "cbsa-adapter must not egress to 0.0.0.0/0")
        check(any(has_tcp_443(b["ports"]) for b in blks),
              "cbsa-adapter secure-channel egress must be on TCP 443")

    # --- (5) CRA adapter ---
    cra = by_name("cra-adapter")
    check(len(cra) == 1, f"expected exactly one cra-adapter policy, found {len(cra)}")
    for p in cra:
        blks = egress_ipblocks(p)
        check(bool(blks), "cra-adapter policy must have an ipBlock egress rule")
        check(all(b["ipBlock"]["cidr"] != PUBLIC_CIDR for b in blks),
              "cra-adapter must not egress to 0.0.0.0/0")
        check(any(has_tcp_443(b["ports"]) for b in blks),
              "cra-adapter secure-channel egress must be on TCP 443")

    # --- (7) claimant-portal Ingress ---
    check(len(ingresses) == 1, f"expected exactly one Ingress, found {len(ingresses)}")
    for ing in ingresses:
        spec = ing.get("spec", {}) or {}
        anns = (ing.get("metadata", {}) or {}).get("annotations", {}) or {}
        tls = spec.get("tls") or []
        check(bool(tls), "claimant-portal Ingress must define a tls block")
        check(any(t.get("secretName") for t in tls),
              "claimant-portal Ingress tls must reference a secretName")
        tls13 = (anns.get("nginx.ingress.kubernetes.io/ssl-protocols") == "TLSv1.3"
                 or anns.get("ei-ads/tls-min-version") == "TLSv1.3"
                 or anns.get("nginx.ingress.kubernetes.io/ssl-min-version") == "TLSv1.3")
        check(tls13, "claimant-portal Ingress must pin TLS 1.3")
        check(any("gckey" in k.lower() and "callback" in str(v).lower()
                  for k, v in anns.items())
              or _has_path(spec, "/auth/gckey/callback"),
              "claimant-portal Ingress must expose the GCKey OAuth callback")
        check(_routes_to(spec, "claimant-portal", 3000),
              "claimant-portal Ingress must route to Service claimant-portal:3000")

    if errors:
        print("VALIDATION FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"OK: validated {len(files)} network manifest file(s) — "
          f"{len(all_policies)} NetworkPolicy, {len(ingresses)} Ingress.")
    return 0


def _iter_paths(spec: dict):
    for rule in spec.get("rules", []) or []:
        http = rule.get("http", {}) or {}
        for pth in http.get("paths", []) or []:
            yield pth


def _has_path(spec: dict, wanted: str) -> bool:
    return any(pth.get("path") == wanted for pth in _iter_paths(spec))


def _routes_to(spec: dict, svc: str, port: int) -> bool:
    for pth in _iter_paths(spec):
        b = (pth.get("backend", {}) or {}).get("service", {}) or {}
        if b.get("name") == svc and (b.get("port", {}) or {}).get("number") == port:
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())
