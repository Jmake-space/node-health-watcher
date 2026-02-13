# node-health-watcher

Event-driven Kubernetes node readiness watcher that triggers Airflow DAG runs when Node `Ready` condition transitions are detected.

## What it does

- Watches Kubernetes Node updates using `get/list/watch`.
- Tracks `Ready` condition state (`True`, `False`, `Unknown`) per node.
- Detects actionable transitions:
  - `incident`: `Ready=True -> Ready!=True`
  - `resolved`: `Ready!=True -> Ready=True`
  - `mixed`: both transitions observed in debounce window
- Triggers Airflow DAG run at `/api/v1/dags/<dag_id>/dagRuns` with `conf` payload.
- Retries Airflow API calls with bounded exponential backoff.

## Payload (`conf`)

```json
{
  "cluster": "pi-k3s",
  "event": "incident|resolved|mixed",
  "status": "node-down|node-recovered|node-change",
  "nodes_down": "pi5d04",
  "nodes_down_current": "pi5d04,pi5d02",
  "nodes_recovered": "pi5d03",
  "timestamp": "2026-02-12T08:00:00Z",
  "nodes_table": "node\tready_status\npi5d01\tTrue"
}
```

## Configuration

Environment variables:

- `CLUSTER_NAME` (default `pi-k3s`)
- `AIRFLOW_BASE_URL`
- `AIRFLOW_DAG_ID` (default `node_health_alert`)
- `AIRFLOW_USERNAME`
- `AIRFLOW_PASSWORD`
- `WATCH_DEBOUNCE_SECONDS` (default `5`)
- `AIRFLOW_MAX_RETRIES` (default `5`)
- `AIRFLOW_TIMEOUT_SECONDS` (default `10`)
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
