# node-health-watcher

Event-driven Kubernetes node readiness watcher that triggers Airflow DAG runs and/or GitHub `repository_dispatch` events when Node `Ready` condition transitions are detected.

## What it does

- Watches Kubernetes Node updates using `get/list/watch`.
- Tracks `Ready` condition state (`True`, `False`, `Unknown`) per node.
- Detects actionable transitions:
  - `incident`: `Ready=True -> Ready!=True`
  - `recovery`: `Ready!=True -> Ready=True`
  - `mixed`: both transitions observed in debounce window
- Triggers Airflow DAG run at `/api/v1/dags/<dag_id>/dagRuns` with `conf` payload.
- Triggers GitHub dispatch at `/repos/<org>/<repo>/dispatches` for downstream alert workflows.
- Retries Airflow API calls with bounded exponential backoff.
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
- `GHA_EVENT_TYPE` (default `k3s-node-alert`)
- `GHA_TOKEN` (token allowed to call repository dispatch)
- `WATCH_DEBOUNCE_SECONDS` (default `5`)
- `AIRFLOW_MAX_RETRIES` (default `5`)
- `AIRFLOW_TIMEOUT_SECONDS` (default `10`)
- `INCIDENT_LOG_PATH` (default `/var/lib/node-health-watcher/incidents.ndjson`)
- `LOG_LEVEL` (default `INFO`)

## Local run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m src.watcher.main
```

## Kubernetes deploy (monitoring namespace)

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/rbac.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml
kubectl apply -f k8s/deployment.yaml
```

Create secret from example:

```bash
cp k8s/secret.example.yaml k8s/secret.yaml
# edit credentials
kubectl apply -f k8s/secret.yaml
```

## Container

```bash
docker build -t ghcr.io/jmake-space/node-health-watcher:latest .
```
