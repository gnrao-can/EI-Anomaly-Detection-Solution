#!/usr/bin/env python3
"""Validate the generated k8s/deployments/*.yaml manifests.

Checks (no cluster required):
  1. Every file parses via yaml.safe_load_all (multi-document safe).
  2. Each file contains exactly one Deployment and one HorizontalPodAutoscaler.
  3. Deployment + HPA agree on min replicas; HPA has CPU (70%) trigger and,
     where applicable, a Kafka consumer-lag external metric at 500.
  4. Probes point at /healthz (or / for claimant-portal) on the container port.
  5. GPU services declare nvidia.com/gpu and a node selector.

Exits non-zero on any failure.
"""
from __future__ import annotations

import pathlib
import sys

import yaml

DEP_DIR = pathlib.Path(__file__).resolve().parents[2] / "k8s" / "deployments"

EXPECTED = {
    # name: (port, min, max, health_path, gpu)
    "claim-intake-service": (8001, 3, 20, "/healthz", None),
    "roe-adapter": (8002, 2, 10, "/healthz", None),
    "pipeline-orchestrator": (8003, 3, 15, "/healthz", None),
    "repetitive-pattern-engine": (8021, 2, 10, "/healthz", None),
    "geographic-anomaly-engine": (8022, 2, 10, "/healthz", None),
    "fraud-ring-engine": (8023, 2, 10, "/healthz", "T4"),
    "history-inconsistency-engine": (8010, 2, 10, "/healthz", None),
    "document-forgery-engine": (8015, 2, 10, "/healthz", "T4"),
    "regional-analysis-engine": (8020, 2, 10, "/healthz", None),
    "ensemble-scorer": (8004, 3, 15, "/healthz", None),
    "llm-service": (8005, 2, 8, "/healthz", "A100"),
    "graph-service": (8006, 2, 8, "/healthz", None),
    "claimant-portal": (3000, 3, 20, "/", None),
}

errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        errors.append(msg)


def main() -> int:
    files = sorted(DEP_DIR.glob("*.yaml"))
    check(len(files) == len(EXPECTED),
          f"expected {len(EXPECTED)} manifests, found {len(files)}: "
          f"{[f.name for f in files]}")

    seen = set()
    for path in files:
        name = path.stem
        try:
            docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
        except yaml.YAMLError as exc:
            errors.append(f"{path.name}: YAML parse error: {exc}")
            continue

        if name not in EXPECTED:
            errors.append(f"{path.name}: unexpected service name")
            continue
        seen.add(name)
        port, mn, mx, health, gpu = EXPECTED[name]

        deps = [d for d in docs if d.get("kind") == "Deployment"]
        hpas = [d for d in docs if d.get("kind") == "HorizontalPodAutoscaler"]
        check(len(deps) == 1, f"{path.name}: expected 1 Deployment, got {len(deps)}")
        check(len(hpas) == 1, f"{path.name}: expected 1 HPA, got {len(hpas)}")
        if len(deps) != 1 or len(hpas) != 1:
            continue
        dep, h = deps[0], hpas[0]

        # Replicas.
        check(dep["spec"]["replicas"] == mn,
              f"{path.name}: Deployment replicas {dep['spec']['replicas']} != {mn}")
        check(h["spec"]["minReplicas"] == mn,
              f"{path.name}: HPA minReplicas {h['spec']['minReplicas']} != {mn}")
        check(h["spec"]["maxReplicas"] == mx,
              f"{path.name}: HPA maxReplicas {h['spec']['maxReplicas']} != {mx}")

        # Container / port / probes.
        c = dep["spec"]["template"]["spec"]["containers"][0]
        check(c["ports"][0]["containerPort"] == port,
              f"{path.name}: containerPort {c['ports'][0]['containerPort']} != {port}")
        for probe in ("livenessProbe", "readinessProbe"):
            hg = c[probe]["httpGet"]
            check(hg["path"] == health,
                  f"{path.name}: {probe} path {hg['path']} != {health}")
            check(hg["port"] == port,
                  f"{path.name}: {probe} port {hg['port']} != {port}")

        # Resources present.
        res = c["resources"]
        check("requests" in res and "limits" in res,
              f"{path.name}: missing resource requests/limits")

        # HPA metrics: CPU 70% present.
        metrics = h["spec"]["metrics"]
        cpu = [m for m in metrics
               if m.get("type") == "Resource"
               and m["resource"]["name"] == "cpu"]
        check(len(cpu) == 1
              and cpu[0]["resource"]["target"]["averageUtilization"] == 70,
              f"{path.name}: HPA missing CPU>70% trigger")

        # Kafka lag external metric (all except claimant-portal).
        ext = [m for m in metrics
               if m.get("type") == "External"
               and m["external"]["metric"]["name"] == "kafka_consumergroup_lag"]
        if name == "claimant-portal":
            check(len(ext) == 0,
                  f"{path.name}: claimant-portal should not have kafka lag metric")
        else:
            check(len(ext) == 1
                  and ext[0]["external"]["target"]["averageValue"] == "500",
                  f"{path.name}: HPA missing Kafka lag>500 external metric")

        # GPU checks.
        podspec = dep["spec"]["template"]["spec"]
        if gpu:
            check(c["resources"]["requests"].get("nvidia.com/gpu") == "1",
                  f"{path.name}: GPU service missing nvidia.com/gpu request")
            check(c["resources"]["limits"].get("nvidia.com/gpu") == "1",
                  f"{path.name}: GPU service missing nvidia.com/gpu limit")
            ns = podspec.get("nodeSelector", {})
            check(any("t4" in str(v).lower() or "a100" in str(v).lower()
                      for v in ns.values()),
                  f"{path.name}: GPU service missing T4/A100 node selector")
        else:
            check("nvidia.com/gpu" not in c["resources"].get("requests", {}),
                  f"{path.name}: non-GPU service unexpectedly requests GPU")

    missing = set(EXPECTED) - seen
    check(not missing, f"missing manifests for: {sorted(missing)}")

    if errors:
        print("VALIDATION FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"OK: {len(seen)} Deployment+HPA manifests validated "
          f"({len(files)} files, multi-doc YAML parsed cleanly).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
