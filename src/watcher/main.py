import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Set

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
        self.gha_event_type = os.getenv("GHA_EVENT_TYPE", "k3s-alert").strip()
        self.watch_debounce_seconds = int(os.getenv("WATCH_DEBOUNCE_SECONDS", "5"))
        self.down_confirm_seconds = int(os.getenv("DOWN_CONFIRM_SECONDS", "180"))
        self.recovery_confirm_seconds = int(os.getenv("RECOVERY_CONFIRM_SECONDS", "120"))
        self.airflow_max_retries = int(os.getenv("AIRFLOW_MAX_RETRIES", "5"))
        self.delivery_max_attempts = int(os.getenv("DELIVERY_MAX_ATTEMPTS", str(self.airflow_max_retries)))
        self.airflow_timeout_seconds = int(os.getenv("AIRFLOW_TIMEOUT_SECONDS", "10"))
        self.incident_log_path = Path(
            os.getenv("INCIDENT_LOG_PATH", "/var/lib/node-health-watcher/watcher/incidents.ndjson").strip()
        )

        self.node_states: Dict[str, str] = {}
        self.pending_down: Set[str] = set()
        self.pending_recovered: Set[str] = set()
        self.flush_deadline: Optional[float] = None
        self.state_path = Path(os.getenv("STATE_PATH", str(self.incident_log_path.with_name("state.json"))))
        self.observed_since: Dict[str, float] = {}
        self.observed: Set[str] = set()
        self.confirmed_down: Set[str] = set()
        self.announced: Dict[str, Set[str]] = {"airflow": set(), "github": set()}
        self.outbox: list = []
        self.retry_deadlines: Dict[str, float] = {}
        self.logger = logging.getLogger("node-health-watcher")
        if min(self.down_confirm_seconds, self.recovery_confirm_seconds, self.watch_debounce_seconds) < 0:
            raise ValueError("Confirmation and debounce thresholds must be nonnegative")
        if self.delivery_max_attempts < 1:
            raise ValueError("DELIVERY_MAX_ATTEMPTS must be positive")
        self.load_state()

    def load_state(self) -> None:
        if not self.state_path.exists():
            return
        # Fail closed on corrupt state rather than silently forgetting accepted incidents.
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state["version"] != 1 or state["cluster"] != self.cluster_name:
            raise ValueError("Watcher state version/cluster mismatch")
        self.node_states = state["node_states"]
        self.confirmed_down = set(state["confirmed_down"])
        self.announced = {key: set(state["announced"][key]) for key in self.announced}
        self.pending_down = set(state["pending_down"])
        self.pending_recovered = set(state["pending_recovered"])
        self.outbox = state["outbox"]

    def save_state(self) -> None:
        state = {
            "version": 1, "cluster": self.cluster_name, "node_states": self.node_states,
            "confirmed_down": sorted(self.confirmed_down),
            "announced": {key: sorted(value) for key, value in self.announced.items()},
            "pending_down": sorted(self.pending_down),
            "pending_recovered": sorted(self.pending_recovered), "outbox": self.outbox,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.state_path)
        directory = os.open(self.state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

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
        nodes = api.list_node(_request_timeout=10).items
        self.reconcile_nodes(nodes)
        self.log_event("initial_state_loaded", nodes=len(nodes))

    def reconcile_nodes(self, nodes: list) -> None:
        present = {node.metadata.name for node in nodes}
        for name in set(self.node_states) - present:
            self.remove_node(name)
        for node in nodes:
            self.handle_node_update(node)

    def remove_node(self, name: str) -> None:
        # Deletion is inventory cleanup, not a readiness recovery.
        self.node_states.pop(name, None)
        self.observed.discard(name)
        self.observed_since.pop(name, None)
        self.confirmed_down.discard(name)
        self.pending_down.discard(name)
        self.pending_recovered.discard(name)
        for nodes in self.announced.values():
            nodes.discard(name)
        for event in self.outbox:
            for delivery in event["deliveries"].values():
                if delivery["status"] != "pending":
                    continue
                down = set(delivery["down"]) - {name}
                recovered = set(delivery["recovered"]) - {name}
                delivery["down"], delivery["recovered"] = sorted(down), sorted(recovered)
                delivery["payload"] = self.build_payload(down, recovered)
                if not down and not recovered:
                    delivery["status"] = "superseded"
        if not self.pending_down and not self.pending_recovered:
            self.flush_deadline = None
        self.save_state()
        self.log_event("node_deleted", node=name)

    def handle_node_update(self, node: V1Node) -> None:
        name = node.metadata.name
        current = self.ready_status(node)

        previous = self.node_states.get(name)
        if name not in self.observed or (previous == "True") != (current == "True"):
            self.observed_since[name] = time.monotonic()
        self.observed.add(name)
        self.node_states[name] = current
        if previous != current:
            self.log_event("node_observed", node=name, previous=previous, current=current)
            self.save_state()

    def direction_confirmed(self, name: str, down: bool) -> bool:
        threshold = self.down_confirm_seconds if down else self.recovery_confirm_seconds
        return (name in self.observed and (self.node_states[name] != "True") == down
                and time.monotonic() - self.observed_since[name] >= threshold)

    def confirm_transitions(self) -> None:
        changed = False
        for name in self.observed:
            down = self.node_states[name] != "True"
            if down == (name in self.confirmed_down) or not self.direction_confirmed(name, down):
                continue
            if down:
                self.confirmed_down.add(name)
                self.pending_down.add(name)
                self.pending_recovered.discard(name)
            else:
                self.confirmed_down.discard(name)
                self.pending_down.discard(name)
                if any(name in nodes for nodes in self.announced.values()):
                    self.pending_recovered.add(name)
            changed = True
            self.log_event("node_transition_confirmed", node=name, down=down)
        if (self.pending_down or self.pending_recovered) and self.flush_deadline is None:
            self.flush_deadline = time.monotonic() + self.watch_debounce_seconds
        if changed:
            self.save_state()

    def build_payload(self, down: Optional[Set[str]] = None,
                      recovered: Optional[Set[str]] = None) -> Dict[str, str]:
        nodes_down = sorted(self.pending_down if down is None else down)
        nodes_recovered = sorted(self.pending_recovered if recovered is None else recovered)
        nodes_down_current = sorted(name for name, status in self.node_states.items() if status != "True")

        legacy_event = "mixed"
        if nodes_down and nodes_recovered:
            event = "mixed"
            status = "node-change"
        elif nodes_down:
            event = "incident"
            status = "node-down"
            legacy_event = "incident"
        else:
            event = "recovery"
            status = "node-recovered"
            legacy_event = "resolved"

        if event == "incident":
            error_type = "node_not_ready"
            error_code = "NODE_NOT_READY"
            summary = f"Node incident in {self.cluster_name}: {','.join(nodes_down)}"
        elif event == "recovery":
            error_type = "node_recovered"
            error_code = "NODE_RECOVERED"
            summary = f"Node recovery in {self.cluster_name}: {','.join(nodes_recovered)}"
        else:
            error_type = "node_state_change"
            error_code = "NODE_CHANGE"
            summary = f"Node state changed in {self.cluster_name}"

        table_lines = ["node\tready_status"] + [f"{name}\t{self.node_states.get(name, 'Unknown')}" for name in sorted(self.node_states)]
        details = f"nodes_down_new={','.join(nodes_down)};nodes_recovered={','.join(nodes_recovered)};nodes_down_current={','.join(nodes_down_current)}"

        return {
            "cluster": self.cluster_name,
            "event_type": "node",
            "event": event,
            "status": status,
            "service": "",
            "error_type": error_type,
            "error_code": error_code,
            "summary": summary,
            "nodes_down": ",".join(nodes_down),
            "nodes_down_current": ",".join(nodes_down_current),
            "nodes_recovered": ",".join(nodes_recovered),
            "timestamp": utc_timestamp(),
            "nodes_table": "\n".join(table_lines),
            "details": details,
            "legacy_event": legacy_event,
        }

    def append_incident_log(self, payload: Dict[str, str]) -> None:
        if payload["event"] == "incident":
            recovery_status = "open"
        elif payload["event"] == "recovery":
            recovery_status = "recovered"
        else:
            recovery_status = "partial"

        record: Dict[str, Any] = {
            "service": payload.get("service", ""),
            "log_message": payload.get("summary", ""),
            "error_type": payload.get("error_type", ""),
            "error_code": payload.get("error_code", ""),
            "datetime": payload.get("timestamp", utc_timestamp()),
            "recovery_status": recovery_status,
        }

        try:
            self.incident_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.incident_log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True))
                handle.write("\n")
        except Exception as exc:
            self.log_event("incident_log_write_failed", error=str(exc), path=str(self.incident_log_path))

    def trigger_airflow(self, payload: Dict[str, str]) -> bool:
        if not self.airflow_base_url or not self.airflow_username or not self.airflow_password:
            self.log_event("airflow_trigger_skipped_missing_config", payload=payload)
            return False

        url = f"{self.airflow_base_url}/api/v1/dags/{self.airflow_dag_id}/dagRuns"
        body = {"conf": payload}

        try:
            response = requests.post(
                url, auth=(self.airflow_username, self.airflow_password),
                json=body, timeout=self.airflow_timeout_seconds,
            )
            if 200 <= response.status_code < 300:
                self.log_event("airflow_triggered", status_code=response.status_code, url=url)
                return True
            self.log_event("airflow_trigger_failed", status_code=response.status_code,
                           response=response.text[:500])
        except Exception as exc:
            self.log_event("airflow_trigger_failed", error=str(exc))
        return False

    def github_client_payload(self, payload: Dict[str, str]) -> Dict[str, Any]:
        # Preserve the deployed routing contract and GitHub's top-level key limit.
        return {
            "cluster": payload.get("cluster", self.cluster_name),
            "event": payload.get("event", ""),
            "status": payload.get("status", ""),
            "resource_type": payload.get("event_type", "node"),
            "summary": payload.get("summary", ""),
            "timestamp": payload.get("timestamp", utc_timestamp()),
            "details": {
                "service": payload.get("service", ""),
                "error_type": payload.get("error_type", ""),
                "error_code": payload.get("error_code", ""),
                "nodes_down": payload.get("nodes_down", ""),
                "nodes_down_current": payload.get("nodes_down_current", ""),
                "nodes_recovered": payload.get("nodes_recovered", ""),
                "nodes_table": payload.get("nodes_table", ""),
                "legacy_event": payload.get("legacy_event", ""),
                "raw_details": payload.get("details", ""),
            },
        }

    def trigger_github_dispatch(self, payload: Dict[str, str]) -> bool:
        if not self.gha_dispatch_url or not self.gha_token:
            self.log_event("github_dispatch_skipped_missing_config", payload=payload)
            return False

        body = {
            "event_type": self.gha_event_type,
            "client_payload": self.github_client_payload(payload),
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
        self.confirm_transitions()
        now = time.monotonic()
        if force or (self.flush_deadline is not None and now >= self.flush_deadline):
            down = {name for name in self.pending_down if self.direction_confirmed(name, True)}
            recovered = {name for name in self.pending_recovered if self.direction_confirmed(name, False)}
            transports = {
                "airflow": bool(self.airflow_base_url and self.airflow_username and self.airflow_password),
                "github": bool(self.gha_dispatch_url and self.gha_token),
            }
            if down or recovered:
                event = {"id": uuid.uuid4().hex, "deliveries": {}}
                for transport, enabled in transports.items():
                    if not enabled:
                        continue
                    incident = down - self.announced[transport]
                    recovery = recovered & self.announced[transport]
                    if incident or recovery:
                        event["deliveries"][transport] = {
                            "payload": self.build_payload(incident, recovery),
                            "down": sorted(incident), "recovered": sorted(recovery),
                            "attempts": 0, "status": "pending",
                        }
                self.outbox.append(event)
                self.pending_down -= down
                self.pending_recovered -= recovered
                self.save_state()
                self.append_incident_log(self.build_payload(down, recovered))
            if not self.pending_down and not self.pending_recovered:
                self.flush_deadline = None
        self.deliver_outbox()

    def deliver_outbox(self) -> None:
        for event in self.outbox:
            for transport, delivery in event["deliveries"].items():
                if delivery["status"] != "pending":
                    continue
                # Supersede unsent transitions only after the opposite state confirms.
                down = set(delivery["down"]) & self.confirmed_down
                recovered = set(delivery["recovered"]) - self.confirmed_down
                if down != set(delivery["down"]) or recovered != set(delivery["recovered"]):
                    delivery["down"], delivery["recovered"] = sorted(down), sorted(recovered)
                    delivery["payload"] = self.build_payload(down, recovered)
                    if not down and not recovered:
                        delivery["status"] = "superseded"
                    self.save_state()
                if delivery["status"] != "pending":
                    continue
                if not all(self.direction_confirmed(name, True) for name in down):
                    continue
                if not all(self.direction_confirmed(name, False) for name in recovered):
                    continue
                key = f"{event['id']}:{transport}"
                if time.monotonic() < self.retry_deadlines.get(key, 0):
                    continue
                # Persist the attempt before I/O so restarts cannot reset the retry budget.
                if delivery["attempts"] >= self.delivery_max_attempts:
                    delivery["status"] = "exhausted"
                    self.save_state()
                    continue
                delivery["attempts"] += 1
                self.save_state()
                trigger = self.trigger_airflow if transport == "airflow" else self.trigger_github_dispatch
                accepted = trigger(delivery["payload"])
                if accepted:
                    delivery["status"] = "accepted"
                    self.announced[transport].update(down)
                    self.announced[transport].difference_update(recovered)
                elif delivery["attempts"] >= self.delivery_max_attempts:
                    delivery["status"] = "exhausted"
                else:
                    self.retry_deadlines[key] = time.monotonic() + min(2 ** (delivery["attempts"] - 1), 30)
                self.save_state()
                self.log_event("delivery_result", transport=transport, status=delivery["status"],
                               attempts=delivery["attempts"], payload=delivery["payload"])
        # Accepted receipts are represented by announced state; retain failed events for audit.
        retained = [event for event in self.outbox if any(
            item["status"] in {"pending", "exhausted"} for item in event["deliveries"].values())]
        if len(retained) != len(self.outbox):
            self.outbox = retained
            active_ids = {event["id"] for event in retained}
            self.retry_deadlines = {key: value for key, value in self.retry_deadlines.items()
                                    if key.split(":")[0] in active_ids}
            self.save_state()

    def run(self) -> None:
        self.load_k8s_config()
        api = client.CoreV1Api()
        self.prime_node_state(api)

        while True:
            watcher = watch.Watch()
            try:
                # Reconcile each short watch session: deadlines also progress on a quiet cluster,
                # and relisting repairs changes missed during disconnects.
                snapshot = api.list_node(_request_timeout=10)
                self.reconcile_nodes(snapshot.items)
                self.flush_if_due()
                stream = watcher.stream(api.list_node, resource_version=snapshot.metadata.resource_version,
                                        timeout_seconds=5, _request_timeout=(10, 10))
                for event in stream:
                    event_type = event.get("type")
                    node = event.get("object")

                    if not isinstance(node, V1Node):
                        continue

                    if event_type == "DELETED":
                        name = node.metadata.name
                        self.remove_node(name)
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
