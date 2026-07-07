# mcm-engine Helm chart

Deploys the mcm-engine MCP daemon on Kubernetes in the "database is authoritative"
posture, the k8s equivalent of [`examples/docker-compose.yml`](../../../examples/docker-compose.yml).
Storage, counters, and search run on Postgres; all durable state lives there. The session
axis is in-memory only (live nudge counters, reset on restart), so the app pod is stateless
and can scale horizontally.

By default the chart bundles a minimal single-instance Postgres (a first-party StatefulSet
plus a PersistentVolumeClaim, pinned to `postgres:16-alpine`), so there is no external chart
dependency. Set `postgresql.enabled: false` to point at an external managed database instead.

## Install

```bash
helm install mcm deploy/helm/mcm-engine \
  --set postgresql.auth.password=<pick-a-password> \
  --set mcm.allowedHosts={mcm-engine.example.com}
```

Reach it in-cluster, or port-forward for a local client:

```bash
kubectl port-forward svc/mcm 8080:8080
# then point an MCP client at http://127.0.0.1:8080/mcp
```

## External database

```bash
helm install mcm deploy/helm/mcm-engine \
  --set postgresql.enabled=false \
  --set externalDatabase.dsn='postgresql://user:pass@host:5432/db' \
  --set mcm.allowedHosts={mcm-engine.example.com}
```

Or reference an existing secret (key `dsn`) with `externalDatabase.existingSecret`.

## Key values

| Key | Default | Purpose |
|-----|---------|---------|
| `image.repository` / `image.tag` | `ghcr.io/unxmaal/mcm-engine` / chart appVersion | Image to run. **Pin an immutable ref**: a release `X.Y.Z`, a per-commit `sha-<commit>`, or best of all a digest via `image.digest` — never `:main`/`:latest` in a real deployment (they move). See [docs/releasing.md](../../../docs/releasing.md). |
| `replicaCount` | `1` | Daemon replicas. **Keep at 1 unless you have session affinity.** KB state is in Postgres, but per-session governance state (`ScopedTracker`, #83) and the streamable-HTTP session transport live **in-process** — a client's requests must reach the pod that ran its `initialize`. See [docs/scaling.md](../../../docs/scaling.md) before raising this. |
| `mcm.allowedHosts` | `[]` | Host values clients connect by. Ingress hosts are added automatically. Required past the DNS-rebinding guard. |
| `mcm.dnsRebindingProtection` | `true` | Set false to accept any Host header (trusted networks only). |
| `mcm.authRequired` | `false` | Require a bearer token on the HTTP transport. |
| `postgresql.enabled` | `true` | Bundle Postgres. False uses `externalDatabase`. |
| `postgresql.auth.password` | `""` | Required when bundling Postgres. |
| `postgresql.persistence.size` | `8Gi` | Bundled-Postgres PVC size. |
| `service.type` | `ClusterIP` | Service type. |
| `ingress.enabled` | `false` | Enable an Ingress (hosts add themselves to `allowedHosts`). |

## Probes

Liveness hits `GET /healthz`, readiness hits `GET /readyz`, both always mounted by the daemon.

## Upgrades and schema

Schema migrations run automatically on daemon startup, idempotent and `IF NOT EXISTS`-guarded.
Back up the database before an upgrade on live data. With bundled Postgres:

```bash
kubectl exec sts/mcm-postgres -- pg_dump -U mcm -d mcm -Fc > backup.dump
```
