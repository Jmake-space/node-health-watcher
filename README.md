# node-health-watcher

Event-driven Kubernetes node readiness watcher that triggers Airflow DAG runs and/or GitHub `repository_dispatch` events when Node `Ready` condition transitions are detected.

## What it does

- Watches Kubernetes Node updates using `get/list/watch`.
- Tracks `Ready` condition state (`True`, `False`, `Unknown`) per node.
- Confirms per-node readiness with monotonic timers (`False` and `Unknown` are one down state).
- Detects actionable transitions after sustained observation:
  - `incident`: non-ready for 180 seconds, including nodes down on first startup
  - `recovery`: ready for 120 seconds, only to transports that accepted an incident
  - `mixed`: both transitions observed in debounce window
- Triggers Airflow DAG run at `/api/v1/dags/<dag_id>/dagRuns` with `conf` payload.
- Triggers GitHub dispatch at `/repos/<org>/<repo>/dispatches` for downstream alert workflows.
- Retries each configured transport with bounded exponential backoff, independently.
- Persists baseline, confirmed transitions, accepted incidents and a delivery outbox atomically.
- Appends incident records to NDJSON (`INCIDENT_LOG_PATH`) for failure-history reporting.

## Payload (`conf`)

```json
{
  "cluster": "pi-k3s",
  "event_type": "node",
  "event": "incident|recovery|mixed",
  "status": "node-down|node-recovered|node-change",
  "service": "",
  "error_type": "node_not_ready|node_recovered|node_state_change",
  "error_code": "NODE_NOT_READY|NODE_RECOVERED|NODE_CHANGE",
  "summary": "Node incident in pi-k3s: pi5d04",
  "nodes_down": "pi5d04",
  "nodes_down_current": "pi5d04,pi5d02",
  "nodes_recovered": "pi5d03",
  "timestamp": "2026-02-12T08:00:00Z",
  "nodes_table": "node\tready_status\npi5d01\tTrue",
  "details": "nodes_down_new=pi5d04;nodes_recovered=pi5d03;nodes_down_current=pi5d04,pi5d02",
  "legacy_event": "incident|resolved|mixed"
}
```

## Configuration

Environment variables:

- `CLUSTER_NAME` (default `pi-k3s`)
- `AIRFLOW_BASE_URL`
- `AIRFLOW_DAG_ID` (default `node_health_alert`)
- `AIRFLOW_USERNAME`
- `AIRFLOW_PASSWORD`
- `GHA_DISPATCH_URL` (example: `https://api.github.com/repos/Jmake-space/homelab-actions/dispatches`)
- `GHA_EVENT_TYPE` (default `k3s-alert`)
- `GHA_TOKEN` (token allowed to call repository dispatch)
- `WATCH_DEBOUNCE_SECONDS` (default `5`)
- `DOWN_CONFIRM_SECONDS` (default `180`, continuous non-ready observation per node)
- `RECOVERY_CONFIRM_SECONDS` (default `120`, continuous ready observation per node)
- `DELIVERY_MAX_ATTEMPTS` (default `AIRFLOW_MAX_RETRIES`, total attempts per transport/event)
- `AIRFLOW_MAX_RETRIES` (default `5`, backward-compatible retry-budget default)
- `AIRFLOW_TIMEOUT_SECONDS` (default `10`)
- `INCIDENT_LOG_PATH` (default `/var/lib/node-health-watcher/watcher/incidents.ndjson`)
- `STATE_PATH` (default `state.json` beside the incident log)
- `LOG_LEVEL` (default `INFO`)

## Local run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m src.watcher.main
```

## State and Delivery

Grouping debounce starts after per-node confirmation; short flaps are suppressed.
Short watch sessions advance timers on quiet clusters; relists reconcile missed updates.
Restart resets monotonic confirmation timers (no downtime credit), but preserves baseline,
accepted incidents and retry budgets. Initial down nodes still need an incident;
accepted incidents do not repeat, and only paired, confirmed recoveries are sent.
`force` bypasses grouping debounce only. Deleted nodes are cleaned up without recovery.

Airflow keeps the flat `conf` shown above. GitHub uses the deployed seven-key wrapper:
`cluster`, `event`, `status`, `resource_type`, `summary`, `timestamp`, and `details`.
`details` contains `service`, `error_type`, `error_code`, `nodes_down`,
`nodes_down_current`, `nodes_recovered`, `nodes_table`, `legacy_event`, and `raw_details`.
Do not flatten it: GitHub permits at most ten top-level client payload keys.

Accepted transports are not retried while another fails. Exhausted deliveries remain
in `state.json`, log `delivery_result: exhausted`, and require operator review.
Unaccepted transitions are superseded when the opposite state confirms; a failed
recovery leaves the incident open. Corrupt state/write errors fail closed. Do not
delete state to retry: that loses pairing and can reannounce outages.
HTTP acceptance does not prove email delivery. A timeout/crash between remote acceptance
and durable local receipt can duplicate delivery (the at-least-once crash window).
Exactly-once is not promised; bounded retries can exhaust without any acceptance.

## Kubernetes Deployment

Deploy through GitHub Actions after a reviewed PR merges to `main`. The Action restarts
for ConfigMap-only changes and captures the previous Deployment and two non-secret
ConfigMaps as a checksummed `node-health-watcher-rollback-<run_id>` artifact.
Rollback through an approved Action: restore the bundle, restart, verify rollout.
`rollout undo` alone does not restore ConfigMaps. Preserve state/PVC during rollback.
This documents the procedure, not an executed rollout/rollback.

Single-writer `Recreate` reuses the monitoring `k3s-node-alert-state` RWX NFS PVC,
avoiding local-path node pinning. Only `watcher/` is written; root `down_nodes.txt`
is untouched. Shared claim provisioning belongs to its cluster owner. Supply
credentials through the existing secret-management workflow (`k8s/secret.example.yaml`).

## Container

```bash
docker build -t ghcr.io/jmake-space/node-health-watcher:latest .
```

## Tests

```bash
python -m unittest discover -s tests -v
```
