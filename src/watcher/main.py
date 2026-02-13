import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Set

import requests
from kubernetes import client, config, watch
from kubernetes.client import V1Node


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class NodeHealthWatcher:
    def __init__(self) -> None:
        self.cluster_name = os.getenv("CLUSTER_NAME", "pi-k3s")
        self.airflow_base_url = os.getenv("AIRFLOW_BASE_URL", "").rstrip("/")
        self.airflow_dag_id = os.getenv("AIRFLOW_DAG_ID", "node_health_alert")
        self.airflow_username = os.getenv("AIRFLOW_USERNAME", "")
        self.airflow_password = os.getenv("AIRFLOW_PASSWORD", "")
        self.gha_dispatch_url = os.getenv("GHA_DISPATCH_URL", "").strip()
        self.gha_token = os.getenv("GHA_TOKEN", "").strip()
        self.gha_event_type = os.getenv("GHA_EVENT_TYPE", "k3s-node-alert").strip()
        self.watch_debounce_seconds = int(os.getenv("WATCH_DEBOUNCE_SECONDS", "5"))
        self.airflow_max_retries = int(os.getenv("AIRFLOW_MAX_RETRIES", "5"))
        self.airflow_timeout_seconds = int(os.getenv("AIRFLOW_TIMEOUT_SECONDS", "10"))

        self.node_states: Dict[str, str] = {}
        self.pending_down: Set[str] = set()
        self.pending_recovered: Set[str] = set()
        self.flush_deadline: Optional[float] = None

        self.logger = logging.getLogger("node-health-watcher")

    def log_event(self, event: str, **fields: object) -> None:
        payload = {
            "event": event,
            "cluster": self.cluster_name,
            "timestamp": utc_timestamp(),
            **fields,
        }
        self.logger.info(json.dumps(payload, sort_keys=True))

    @staticmethod
    def ready_status(node: V1Node) -> str:
        conditions = node.status.conditions or []
        for condition in conditions:
            if condition.type == "Ready":
                return condition.status or "Unknown"
        return "Unknown"

    def load_k8s_config(self) -> None:
        try:
            config.load_incluster_config()
            self.log_event("k8s_config", mode="incluster")
        except Exception:
            config.load_kube_config()
            self.log_event("k8s_config", mode="kubeconfig")

    def prime_node_state(self, api: client.CoreV1Api) -> None:
        nodes = api.list_node().items
        for node in nodes:
            name = node.metadata.name
            self.node_states[name] = self.ready_status(node)
        self.log_event("initial_state_loaded", nodes=len(nodes))

    def handle_node_update(self, node: V1Node) -> None:
        name = node.metadata.name
        current = self.ready_status(node)

        if name not in self.node_states:
            self.node_states[name] = current
            self.log_event("node_first_observed", node=name, status=current)
            return

        previous = self.node_states[name]
        if previous == current:
            return

        self.node_states[name] = current

        if previous == "True" and current != "True":
            self.pending_down.add(name)
            self.pending_recovered.discard(name)
            transition = "node_became_non_ready"
        elif previous != "True" and current == "True":
            self.pending_recovered.add(name)
            self.pending_down.discard(name)
            transition = "node_became_ready"
        else:
            self.log_event("node_state_changed_non_actionable", node=name, previous=previous, current=current)
            return

        if self.flush_deadline is None:
            self.flush_deadline = time.time() + self.watch_debounce_seconds

        self.log_event(
            "node_transition_detected",
            transition=transition,
            node=name,
            previous=previous,
            current=current,
            pending_down=sorted(self.pending_down),
            pending_recovered=sorted(self.pending_recovered),
        )

    def build_payload(self) -> Dict[str, str]:
        nodes_down = sorted(self.pending_down)
        nodes_recovered = sorted(self.pending_recovered)
        nodes_down_current = sorted(name for name, status in self.node_states.items() if status != "True")

        if nodes_down and nodes_recovered:
            event = "mixed"
            status = "node-change"
        elif nodes_down:
            event = "incident"
            status = "node-down"
        else:
            event = "resolved"
            status = "node-recovered"

        table_lines = ["node\tready_status"] + [f"{name}\t{self.node_states.get(name, 'Unknown')}" for name in sorted(self.node_states)]

        return {
            "cluster": self.cluster_name,
            "event": event,
            "status": status,
            "nodes_down": ",".join(nodes_down),
            "nodes_down_current": ",".join(nodes_down_current),
            "nodes_recovered": ",".join(nodes_recovered),
            "timestamp": utc_timestamp(),
            "nodes_table": "\n".join(table_lines),
        }

    def trigger_airflow(self, payload: Dict[str, str]) -> bool:
        if not self.airflow_base_url or not self.airflow_username or not self.airflow_password:
            self.log_event("airflow_trigger_skipped_missing_config", payload=payload)
            return False

        url = f"{self.airflow_base_url}/api/v1/dags/{self.airflow_dag_id}/dagRuns"
        body = {"conf": payload}

        for attempt in range(1, self.airflow_max_retries + 1):
            try:
                response = requests.post(
                    url,
                    auth=(self.airflow_username, self.airflow_password),
                    json=body,
                    timeout=self.airflow_timeout_seconds,
                )
                if 200 <= response.status_code < 300:
                    self.log_event("airflow_triggered", attempt=attempt, status_code=response.status_code, url=url)
                    return True

                message = response.text[:500]
                raise RuntimeError(f"status={response.status_code} response={message}")
            except Exception as exc:
                sleep_seconds = min(2 ** (attempt - 1), 30)
                self.log_event(
                    "airflow_trigger_retry",
                    attempt=attempt,
                    max_attempts=self.airflow_max_retries,
                    sleep_seconds=sleep_seconds,
                    error=str(exc),
                )
                if attempt == self.airflow_max_retries:
                    self.log_event("airflow_trigger_failed", error=str(exc), payload=payload)
                    return False
                time.sleep(sleep_seconds)

        return False

    def trigger_github_dispatch(self, payload: Dict[str, str]) -> bool:
        if not self.gha_dispatch_url or not self.gha_token:
            self.log_event("github_dispatch_skipped_missing_config", payload=payload)
            return False

        body = {
            "event_type": self.gha_event_type,
            "client_payload": payload,
        }

        try:
            response = requests.post(
                self.gha_dispatch_url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"token {self.gha_token}",
                },
                json=body,
                timeout=self.airflow_timeout_seconds,
            )
            if 200 <= response.status_code < 300:
                self.log_event(
                    "github_dispatch_triggered",
                    status_code=response.status_code,
                    url=self.gha_dispatch_url,
                    event_type=self.gha_event_type,
                )
                return True
            self.log_event(
                "github_dispatch_failed",
                status_code=response.status_code,
                response=response.text[:500],
                url=self.gha_dispatch_url,
            )
            return False
        except Exception as exc:
            self.log_event("github_dispatch_error", error=str(exc), url=self.gha_dispatch_url)
            return False

    def flush_if_due(self, force: bool = False) -> None:
        if not self.pending_down and not self.pending_recovered:
            return

        if not force and self.flush_deadline is not None and time.time() < self.flush_deadline:
            return

        payload = self.build_payload()
        airflow_ok = self.trigger_airflow(payload)
        gha_ok = self.trigger_github_dispatch(payload)
        self.log_event("flush_dispatched", airflow_ok=airflow_ok, github_dispatch_ok=gha_ok, payload=payload)

        self.pending_down.clear()
        self.pending_recovered.clear()
        self.flush_deadline = None

    def run(self) -> None:
        self.load_k8s_config()
        api = client.CoreV1Api()
        self.prime_node_state(api)

        while True:
            self.flush_if_due()
            watcher = watch.Watch()
            try:
                stream = watcher.stream(api.list_node, timeout_seconds=30)
                for event in stream:
                    event_type = event.get("type")
                    node = event.get("object")

                    if not isinstance(node, V1Node):
                        continue

                    if event_type == "DELETED":
                        name = node.metadata.name
                        self.node_states.pop(name, None)
                        self.log_event("node_deleted", node=name)
                        continue

                    self.handle_node_update(node)
                    self.flush_if_due()
            except Exception as exc:
                self.log_event("watch_stream_error", error=str(exc))
                time.sleep(2)
            finally:
                watcher.stop()


def main() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format="%(message)s")
    NodeHealthWatcher().run()


if __name__ == "__main__":
    main()
