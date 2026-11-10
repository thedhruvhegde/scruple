# Service level objectives (PROPOSALS)

**Every target on this page is a proposal.** Nothing has run in production yet, no
design partner has agreed to any of it, and no number here was measured on real
traffic. The only measurements that exist are local, synthetic load runs on a
shared 4-core container (docs/PERFORMANCE.md, "Load"), which say what one process
can do on a busy machine, not what a deployment will see. Replace each proposal
with an agreed number once a staging environment has a few weeks of data.

Metrics named below are the ones `/metrics` exposes (docs/DEPLOY.md, "Observability").

## What the engine is for, and therefore what to protect

The engine decides whether a learned rule may go live and keeps the evidence. Two
things matter far more than speed:

1. **Integrity**: the audit chain verifies and every decision matches its audit entry.
2. **Correctness of state**: a verdict is never applied twice or half-applied (each
   state change is one transaction).

Latency matters for the read API and the agent-facing write calls; evaluation
(`POST .../evaluate`) runs a shadow replay and is expected to be slow.

## Proposed SLOs

| # | SLI | how it is measured | proposed objective | window |
|---|---|---|---|---|
| 1 | integrity | `/ready` audit_chain check, plus a scheduled `scruple audit verify --expected-head <anchored head>` | 100%: any failure is an incident, not budget | always |
| 2 | availability | share of requests to the API (excluding `/metrics`) answered with a non-5xx status, at the load balancer | 99.5% (proposal) | 30 days |
| 3 | read latency | `scruple_http_request_duration_seconds` for GET routes except `/audit/verify` | 99% under 250 ms (proposal) | 30 days |
| 4 | write latency | same histogram, `POST /rules`, `POST .../approve`, `POST .../rollback`, shadow batch routes | 99% under 1 s (proposal) | 30 days |
| 5 | evaluation latency | same histogram, `POST .../evaluate` | 95% under 60 s (proposal; depends on `GateConfig.n_cases`) | 30 days |
| 6 | readiness | share of scrape intervals with at least one ready pod (`kube_deployment_status_replicas_available` > 0) | 99.9% (proposal) | 30 days |
| 7 | recoverability | restore drill (docs/DEPLOY.md) ends with `restore: OK` | one passing drill per quarter (proposal) | quarter |

Why 99.5% and not more: a pilot runs one region, one database instance (Multi-AZ
in the Terraform skeleton) and a small team on call. 99.5% allows about 3.6 hours
of failed requests in 30 days, which a single bad deploy plus a database failover
can use up. Raise it only with the redundancy to back it.

`/audit/verify` re-hashes the whole chain, so its latency grows with the chain
(docs/PERFORMANCE.md). It is left out of SLI 3 on purpose; watch
`scruple_audit_chain_length` and its latency together instead.

## Error budget policy (proposal)

- Budget left: ship normally.
- Budget burned faster than 2x for 6 hours, or under 25% left: only fixes and
  reliability work go out; every deploy needs a staging smoke run first.
- Budget gone: freeze feature deploys until the 30-day window recovers.
- SLI 1 (integrity) has no budget. One failure stops deploys and starts the
  incident procedure in docs/DEPLOY.md.

## Alerts (proposal, Prometheus expressions)

```
# availability: 14.4x burn over 1h and 5m (pages)
(
  sum(rate(scruple_http_request_duration_seconds_count{status=~"5..",route!="/metrics"}[1h]))
  / sum(rate(scruple_http_request_duration_seconds_count{route!="/metrics"}[1h]))
) > 14.4 * 0.005
and
(
  sum(rate(scruple_http_request_duration_seconds_count{status=~"5..",route!="/metrics"}[5m]))
  / sum(rate(scruple_http_request_duration_seconds_count{route!="/metrics"}[5m]))
) > 14.4 * 0.005

# read latency: p99 over 250 ms for 15 minutes (tickets)
histogram_quantile(0.99, sum by (le) (rate(
  scruple_http_request_duration_seconds_bucket{method="GET",route!~"/audit/verify|/metrics"}[15m]
))) > 0.25

# integrity: any pod reporting not ready for the audit chain (pages)
# (from logs: logger scruple.api.health, msg "readiness: audit chain broken")

# no ready pod (pages); needs kube-state-metrics
kube_deployment_status_replicas_available{deployment=~".*scruple.*"} < 1

# saturation: evaluations hold a worker for their whole run (tickets)
max(scruple_http_requests_in_flight) >= 0.8 * <workers per pod>
```

`scruple_gate_decisions_total` and `scruple_audit_chain_length` are read from the
database, so every replica reports the same value: aggregate them with `max`, not
`sum`.

## Not covered yet

- No synthetic probe from outside the cluster (scripts/smoke.sh is a deploy check,
  not a monitor).
- No per-tenant SLIs: the deployment is single-tenant.
- Database-side SLIs (replication lag, storage) come from RDS/Cloud SQL monitoring,
  not from the engine.
