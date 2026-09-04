# EI-ADS Stateful Workloads (StatefulSets)

Kubernetes/OpenShift manifests for the three stateful backing stores of EI-ADS.
Each manifest is a multi-document YAML containing a **StatefulSet**, a
**headless Service** (stable per-pod DNS for clustering), a **client-facing
Service**, and a **PodDisruptionBudget**.

Task: 36.2 &nbsp;|&nbsp; Requirements: **23.4** (Kafka RF≥3 / ISR=2), **23.5**
(Redis clustered with ≥2 replicas).

| File | Workload | Replicas | HA config |
|---|---|---|---|
| `neo4j-cluster.yaml` | Neo4j causal cluster (Graph Service) | 3 | Raft quorum, PDB `minAvailable: 2` |
| `redis-cluster.yaml` | Redis Cluster (cache + pairing store) | 5 (3 primary + 2 replica) | PDB `maxUnavailable: 1` |
| `kafka-cluster.yaml` | Kafka Bus (KRaft, 6 brokers) | 6 | RF=3, `min.insync.replicas=2`, PDB `maxUnavailable: 1` |

## Design decisions

- **Neo4j — 3 nodes:** a 3-member core cluster provides Raft quorum and
  tolerates the loss of one member. Ports: 7474 (HTTP), 7687 (Bolt), plus the
  discovery/raft/transaction cluster ports. Uses the Neo4j **Enterprise** image
  (clustering requires an Enterprise licence — pin the licensed tag at deploy).
- **Redis — 5 nodes (3 primary + 2 replica):** matches
  `scripts/redis/redis-cluster.conf` and satisfies Requirement 23.5. The base
  node config is mounted from the `redis-cluster-config` ConfigMap (created in
  task 36.3). The cluster is formed once with `redis-cli --cluster create`
  (see `scripts/redis/README.md`).
- **Kafka — 6 brokers, KRaft combined mode:** cluster defaults set
  `default.replication.factor=3` and `min.insync.replicas=2` (Requirement
  23.4), matching every topic in `scripts/kafka/topics.json`. Topics are
  provisioned separately by `scripts/kafka/create-topics.sh` with
  `KAFKA_ENV=prod`.

All three use `volumeClaimTemplates` for durable per-pod storage, `podAntiAffinity`
to spread pods across hosts, and readiness/liveness probes appropriate to each
system (Bolt/HTTP for Neo4j, `redis-cli ping` for Redis, TCP on 9092 for Kafka).

## Referenced Secrets / ConfigMaps (created in task 36.3)

These manifests reference the following objects, which are provisioned by
task 36.3 (`k8s/secrets/`, `k8s/configmaps/`):

- `neo4j-auth` Secret — key `NEO4J_AUTH`
- `redis-auth` Secret — key `REDIS_PASSWORD`
- `kafka-cluster-id` Secret — key `KAFKA_KRAFT_CLUSTER_ID`
- `redis-cluster-config` ConfigMap — key `redis-cluster.conf`
  (from `scripts/redis/redis-cluster.conf`)

All objects live in the `ei-ads` namespace.

## Validation status

- **YAML well-formedness:** all four `.yaml` documents (and every embedded
  sub-document) parse cleanly with PyYAML `safe_load_all` (multi-doc safe).
- **Cluster apply:** NOT performed. No Kubernetes cluster is available in the
  sandbox, so `kubectl apply --dry-run` / server-side schema validation and a
  live rollout have **not** been run. Before deploying: create the referenced
  Secrets/ConfigMaps, pin the Neo4j Enterprise image tag, and run
  `kubectl apply --dry-run=server -f k8s/statefulsets/` against the target
  cluster.
