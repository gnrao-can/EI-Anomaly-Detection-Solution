#!/usr/bin/env python3
"""Generate Kubernetes Deployment + HorizontalPodAutoscaler manifests for all
13 EI-ADS microservices.

Requirements: 17.5 (horizontal pod autoscaling to hold latency targets under
load spikes of up to 3x baseline).

Design source (design.md > Infrastructure Design > Deployment Topology):
  - Per-service min/max replica counts.
  - GPU node pools: fraud-ring-engine (NVIDIA T4), document-forgery-engine
    (NVIDIA T4), llm-service (NVIDIA A100).
  - HPA triggers: CPU utilization > 70% OR Kafka consumer lag > 500 messages.

Ports are taken from each service's Containerfile (task 35.1). Health check is
GET /healthz on the service port, except claimant-portal which serves a static
SPA and is probed on GET /.

The Kafka consumer-lag trigger uses the external metric `kafka_consumergroup_lag`,
which is exposed to the Kubernetes custom/external metrics API by the Prometheus
Adapter (prometheus-adapter) reading from kafka-exporter / Strimzi broker metrics.

This script emits one YAML file per service into k8s/deployments/. It does NOT
apply anything to a cluster.
"""
from __future__ import annotations

import pathlib

OUT_DIR = pathlib.Path(__file__).resolve().parents[2] / "k8s" / "deployments"
NAMESPACE = "ei-ads-prod"
REGISTRY = "registry.ei-ads.gc.ca/ei-ads"

# Kafka consumer-lag external metric name (Prometheus Adapter).
LAG_METRIC = "kafka_consumergroup_lag"
LAG_THRESHOLD = 500  # messages per pod (design: > 500 messages)
CPU_TARGET = 70      # percent (design: > 70%)


# Resource tiers: (cpu_request, mem_request, cpu_limit, mem_limit)
TIERS = {
    "high": ("500m", "512Mi", "1500m", "1Gi"),
    "standard": ("300m", "512Mi", "1000m", "1Gi"),
    "gpu-t4": ("1", "4Gi", "4", "8Gi"),
    "gpu-a100": ("2", "16Gi", "8", "32Gi"),
    "portal": ("250m", "256Mi", "1000m", "512Mi"),
}

# GPU pool config: (accelerator label value, gpu count)
GPU_POOLS = {
    "T4": "nvidia-tesla-t4",
    "A100": "nvidia-tesla-a100",
}

# name, image, port, min, max, tier, component, health_path, gpu(None|"T4"|"A100"),
# kafka_consumer(bool)
SERVICES = [
    ("claim-intake-service", "claim-intake", 8001, 3, 20, "high", "ingress", "/healthz", None, True),
    ("roe-adapter", "roe-adapter", 8002, 2, 10, "standard", "adapter", "/healthz", None, True),
    ("pipeline-orchestrator", "pipeline-orchestrator", 8003, 3, 15, "high", "orchestration", "/healthz", None, True),
    ("repetitive-pattern-engine", "repetitive-pattern-engine", 8021, 2, 10, "standard", "detection-engine", "/healthz", None, True),
    ("geographic-anomaly-engine", "geographic-anomaly-engine", 8022, 2, 10, "standard", "detection-engine", "/healthz", None, True),
    ("fraud-ring-engine", "fraud-ring-engine", 8023, 2, 10, "gpu-t4", "detection-engine", "/healthz", "T4", True),
    ("history-inconsistency-engine", "history-inconsistency-engine", 8010, 2, 10, "standard", "detection-engine", "/healthz", None, True),
    ("document-forgery-engine", "document-forgery-engine", 8015, 2, 10, "gpu-t4", "detection-engine", "/healthz", "T4", True),
    ("regional-analysis-engine", "regional-analysis-engine", 8020, 2, 10, "standard", "detection-engine", "/healthz", None, True),
    ("ensemble-scorer", "ensemble-scorer", 8004, 3, 15, "high", "scoring", "/healthz", None, True),
    ("llm-service", "llm-service", 8005, 2, 8, "gpu-a100", "llm", "/healthz", "A100", True),
    ("graph-service", "graph-service", 8006, 2, 8, "standard", "graph", "/healthz", None, True),
    ("claimant-portal", "claimant-portal", 3000, 3, 20, "portal", "frontend", "/", None, False),
]


def deployment(svc) -> str:
    (name, image, port, mn, mx, tier, component, health, gpu, kafka) = svc
    cpu_req, mem_req, cpu_lim, mem_lim = TIERS[tier]

    # GPU deployments add nvidia.com/gpu resource + node selector/tolerations.
    gpu_resource_req = ""
    gpu_resource_lim = ""
    node_selector = ""
    tolerations = ""
    runtime_class = ""
    if gpu:
        gpu_resource_req = '\n              nvidia.com/gpu: "1"'
        gpu_resource_lim = '\n              nvidia.com/gpu: "1"'
        node_selector = (
            "\n      nodeSelector:\n"
            f"        cloud.google.com/gke-accelerator: {GPU_POOLS[gpu]}\n"
            f"        ei-ads/gpu-pool: nvidia-{gpu.lower()}"
        )
        tolerations = (
            "\n      tolerations:\n"
            "        - key: nvidia.com/gpu\n"
            "          operator: Exists\n"
            "          effect: NoSchedule"
        )
        runtime_class = "\n      runtimeClassName: nvidia"

    # Static SPA portal cannot run a read-only root reliably with `serve`;
    # keep read-only FS off for the portal only.
    read_only_fs = "false" if name == "claimant-portal" else "true"

    return f"""---
# {name} — Deployment + HorizontalPodAutoscaler
# Requirements: 17.5 (horizontal pod autoscaling; hold latency under 3x load spikes)
# Design: Deployment Topology — {name} (min: {mn}, max: {mx} pods){' -- GPU node pool: NVIDIA ' + gpu if gpu else ''}
# Port {port}. Health probe: GET {health}.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {NAMESPACE}
  labels:
    app.kubernetes.io/name: {name}
    app.kubernetes.io/part-of: ei-ads
    app.kubernetes.io/component: {component}
    ei-ads/tier: {tier}
spec:
  replicas: {mn}
  selector:
    matchLabels:
      app.kubernetes.io/name: {name}
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    metadata:
      labels:
        app.kubernetes.io/name: {name}
        app.kubernetes.io/part-of: ei-ads
        app.kubernetes.io/component: {component}
        ei-ads/tier: {tier}
    spec:
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault{runtime_class}{node_selector}{tolerations}
      containers:
        - name: {name}
          image: {REGISTRY}/{image}:latest
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: {port}
              protocol: TCP
          resources:
            requests:
              cpu: "{cpu_req}"
              memory: "{mem_req}"{gpu_resource_req}
            limits:
              cpu: "{cpu_lim}"
              memory: "{mem_lim}"{gpu_resource_lim}
          livenessProbe:
            httpGet:
              path: {health}
              port: {port}
            initialDelaySeconds: 20
            periodSeconds: 15
            timeoutSeconds: 3
            failureThreshold: 3
          readinessProbe:
            httpGet:
              path: {health}
              port: {port}
            initialDelaySeconds: 10
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 3
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: {read_only_fs}
            capabilities:
              drop:
                - ALL
"""


def hpa(svc) -> str:
    (name, image, port, mn, mx, tier, component, health, gpu, kafka) = svc

    kafka_block = ""
    if kafka:
        kafka_block = f"""
    # Trigger 2: scale out when Kafka consumer lag exceeds {LAG_THRESHOLD} messages.
    # External metric `{LAG_METRIC}` is published to the Kubernetes external
    # metrics API by the Prometheus Adapter (prometheus-adapter), sourced from
    # kafka-exporter / Strimzi broker metrics. averageValue means {LAG_THRESHOLD}
    # lagging messages per replica before scaling out.
    - type: External
      external:
        metric:
          name: {LAG_METRIC}
          selector:
            matchLabels:
              consumergroup: {name}
        target:
          type: AverageValue
          averageValue: "{LAG_THRESHOLD}\""""

    return f"""---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {name}
  namespace: {NAMESPACE}
  labels:
    app.kubernetes.io/name: {name}
    app.kubernetes.io/part-of: ei-ads
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {name}
  minReplicas: {mn}
  maxReplicas: {mx}
  metrics:
    # Trigger 1: scale out when average CPU utilization exceeds {CPU_TARGET}%.
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {CPU_TARGET}{kafka_block}
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 30
      policies:
        - type: Percent
          value: 100
          periodSeconds: 30
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
        - type: Percent
          value: 50
          periodSeconds: 60
"""


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for svc in SERVICES:
        name = svc[0]
        content = deployment(svc) + hpa(svc)
        path = OUT_DIR / f"{name}.yaml"
        path.write_text(content)
        print(f"wrote {path}")
    print(f"\nGenerated {len(SERVICES)} Deployment+HPA manifests.")


if __name__ == "__main__":
    main()
