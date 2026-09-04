# EI-ADS Redis Cluster

This directory holds the Redis Cluster configuration used by every EI-ADS
service for caching external-data-adapter responses and for the pipeline
orchestrator's event-pairing store.

## Topology (3 primary + 2 replica)

The reference deployment runs **5 nodes**: 3 primaries and 2 replicas
(Requirement 23.5).

- The **3 primaries** collectively own the **16384 hash slots**. Redis Cluster
  uses consistent hashing (`CRC16(key) mod 16384`) to map every key to a slot,
  and each primary owns a contiguous slot range (roughly `0–5460`,
  `5461–10922`, `10923–16383`).
- The **2 replicas** each follow a primary and are promoted automatically if
  that primary fails, so slot ownership and cache availability are preserved.
- With 5 nodes and 3 primaries, only 2 of the 3 primaries have a replica.
  Production **should** prefer a 3-primary + 3-replica (6-node) layout for full
  redundancy; the 3+2 layout satisfies Req 23.5 for the reference deployment.

The `allkeys-lru` eviction policy plus per-key TTLs target a **>= 90% cache hit
rate** during peak periods (Requirement 17.4).

In production this runs as the `redis-cluster` StatefulSet (see task 36.2);
`redis-cluster.conf` in this directory is the base config mounted into every
pod.

## Bootstrapping the cluster

Each node starts with `redis-cluster.conf`. Once all 5 nodes are up, form the
cluster once with:

```bash
redis-cli --cluster create \
  node0:6379 node1:6379 node2:6379 node3:6379 node4:6379 \
  --cluster-replicas 0 \
  -a "$REDIS_PASSWORD"
```

To create it with exactly 3 primaries and hand-assign the 2 replicas, either
pass the 3 primaries first with `--cluster-replicas 0` and then attach replicas
with `redis-cli --cluster add-node --cluster-slave`, or supply all 5 nodes and
let Redis assign masters/replicas. Verify with:

```bash
redis-cli -a "$REDIS_PASSWORD" cluster info      # cluster_state:ok
redis-cli -a "$REDIS_PASSWORD" cluster nodes      # 3 master + 2 slave
```

> **Never commit real passwords.** `requirepass` / `masterauth` in
> `redis-cluster.conf` are placeholders; set them from a Kubernetes Secret in
> production.

## Local development

For local dev the services use a **single-node** Redis (`redis.asyncio.Redis`)
rather than the cluster. `shared/cache.py`'s `RedisCache` selects the client via
its `cluster_mode` flag.

## Cache key patterns

All keys are built through the helper functions in
[`shared/cache_keys.py`](../../shared/cache_keys.py). Do not format these
strings inline.

| Key pattern | TTL | Owning service |
|---|---|---|
| `statscan:lfs:{region_code}:{year_month}` | 30 days | Stats Canada Adapter |
| `cra:t4:{sin_hash}:{bn}:{year}` | 24 hours | CRA Adapter |
| `geonames:coord:{postal_code}` | 90 days | GeoNames Adapter |
| `geonames:dist:{hash(A,B)}` | 7 days | GeoNames Adapter |
| `biz_reg:employer:{bn}` | 24 hours | Business Registry Adapter |
| `mvr:ownership:{sin_hash}:{province}` | 1 hour | MVR Adapter |
| `job_bank:postings:{noc}:{region}:{year_month}` | 7 days | Job Bank Adapter |
| `cbsa:travel:{sin_hash}:{period_hash}` | 1 hour | CBSA Adapter |
| `orchestrator:pairing:{claim_id}` | 60 seconds | Pipeline Orchestrator |

The `geonames:dist` key is **symmetric**: the coordinate pair is canonicalised
(sorted) before hashing so `distance(A, B)` and `distance(B, A)` share one cache
entry.
