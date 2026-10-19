import json
import os
import socket
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.watcher.main import NodeHealthWatcher


def node(name, status, address="192.0.2.10"):
    return SimpleNamespace(metadata=SimpleNamespace(name=name), status=SimpleNamespace(
        conditions=[SimpleNamespace(type="Ready", status=status)],
        addresses=[SimpleNamespace(type="InternalIP", address=address)] if address else []))


class ReachabilityConfirmationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, {
            "CLUSTER_NAME": "test", "STATE_PATH": directory.name+"/state.json",
            "INCIDENT_LOG_PATH": directory.name+"/incidents.ndjson",
            "DOWN_CONFIRM_SECONDS": "600", "RECOVERY_CONFIRM_SECONDS": "900",
            "WATCH_DEBOUNCE_SECONDS": "5", "NODE_PROBE_ENABLED": "true",
            "NODE_PROBE_INTERVAL_SECONDS": "30", "NODE_PROBE_TIMEOUT_SECONDS": "1",
            "HOST_UNREACHABLE_CONFIRM_SECONDS": "300", "CLUSTER_UNAVAILABLE_CONFIRM_SECONDS": "1800",
            "MAX_OBSERVATION_GAP_SECONDS": "60", "GHA_DISPATCH_URL": "https://example.invalid",
            "GHA_TOKEN": "test", "AIRFLOW_BASE_URL": "", "DELIVERY_MAX_ATTEMPTS": "3",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.now, self.reachable = 0.0, False
        clock = patch("src.watcher.main.time.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.nodes = {}
        self.w = self.watcher()

    def watcher(self):
        watcher = NodeHealthWatcher()
        watcher.probe_host = Mock(side_effect=lambda address: {"reachable": self.reachable, "address": address})
        watcher.trigger_github_dispatch = Mock(return_value=True)
        return watcher

    def observe(self, status, address="192.0.2.10", name="n1"):
        self.nodes[name] = node(name, status, address)
        self.w.handle_node_update(self.nodes[name])
        self.w.flush_if_due()

    def advance(self, seconds):
        end = self.now + seconds
        while self.now < end:
            self.now = min(end, self.now+5)
            self.w.reconcile_nodes(list(self.nodes.values()))
            self.w.flush_if_due()

    def incident(self):
        self.observe("Unknown")
        self.advance(605)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 1)

    def diagnostics(self):
        payload = self.w.trigger_github_dispatch.call_args.args[0]
        return json.loads(payload["node_diagnostics"])["n1"]

    def test_minor_network_gap_sends_neither_incident_nor_recovery(self):
        self.observe("True")
        self.observe("Unknown")
        self.advance(599)
        self.observe("True")
        self.advance(1800)
        self.w.trigger_github_dispatch.assert_not_called()

    def test_sustained_unreachable_node_notifies_once(self):
        self.incident()
        self.assertEqual(self.diagnostics()["classification"], "host_unreachable")
        self.assertFalse(self.diagnostics()["power_off_or_reboot_proven"])
        self.advance(1800)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 1)

    def test_responding_host_suppressed_until_long_cluster_failure(self):
        self.reachable = True
        self.observe("Unknown")
        self.advance(1799)
        self.w.trigger_github_dispatch.assert_not_called()
        self.advance(16)
        self.assertEqual(self.diagnostics()["classification"], "cluster_unavailable_host_responding")
        self.assertIn("unavailable to Kubernetes", self.w.trigger_github_dispatch.call_args.args[0]["summary"])

    def test_host_unreachability_needs_continuous_corroboration(self):
        self.reachable = True
        self.observe("False")
        self.advance(600)
        self.reachable = False
        self.advance(329)
        self.w.trigger_github_dispatch.assert_not_called()
        self.advance(16)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 1)

    def test_short_recoveries_keep_one_incident_open(self):
        self.incident()
        self.observe("True")
        self.advance(300)
        self.observe("Unknown")
        self.advance(605)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 1)
        self.observe("True")
        self.advance(899)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 1)
        self.advance(16)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 2)
        self.assertEqual(self.diagnostics()["classification"], "sustained_ready_recovery")

    def test_false_unknown_do_not_reset_unavailability(self):
        self.observe("False")
        self.advance(300)
        self.observe("Unknown")
        self.advance(305)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 1)

    def test_restart_preserves_open_incident_without_repeat(self):
        self.incident()
        self.w = self.watcher()
        self.advance(1810)
        self.w.trigger_github_dispatch.assert_not_called()
        self.observe("True")
        self.advance(905)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 1)
        self.assertEqual(self.w.trigger_github_dispatch.call_args.args[0]["event"], "recovery")

    def test_watch_gap_cannot_confirm_and_relist_restarts_timer(self):
        self.observe("Unknown")
        self.advance(500)
        self.now += 500
        self.w.flush_if_due(force=True)
        self.w.trigger_github_dispatch.assert_not_called()
        self.observe("Unknown")
        self.advance(599)
        self.w.trigger_github_dispatch.assert_not_called()
        self.advance(16)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 1)

    def test_watch_error_discards_proof_but_not_accepted_pairing(self):
        self.incident()
        self.w.reset_observations()
        self.assertEqual(self.w.announced["github"], {"n1"})
        self.assertFalse(self.w.probe_results)
        self.assertFalse(self.w.observed)

    def test_stale_readiness_cannot_clear_incident(self):
        self.incident()
        self.observe("True")
        self.now += 1000
        self.w.flush_if_due(force=True)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 1)
        self.observe("True")
        self.advance(905)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 2)

    def test_ip_change_restarts_readiness_and_probe_evidence(self):
        self.observe("False")
        self.advance(590)
        self.observe("False", "198.51.100.10")
        self.advance(599)
        self.w.trigger_github_dispatch.assert_not_called()
        self.advance(16)
        self.assertEqual(self.diagnostics()["probe"]["address"], "198.51.100.10")

    def test_no_ip_does_not_claim_host_power_off(self):
        self.observe("Unknown", address=None)
        self.advance(605)
        self.w.probe_host.assert_not_called()
        self.w.trigger_github_dispatch.assert_not_called()
        self.advance(1200)
        self.assertEqual(self.diagnostics()["classification"], "cluster_unavailable_probe_inconclusive")

    def test_invalid_ip_is_not_resolved_or_probed(self):
        self.observe("False", address="attacker.invalid")
        self.advance(605)
        self.w.probe_host.assert_not_called()
        self.w.trigger_github_dispatch.assert_not_called()

    def test_address_order_and_dual_stack_do_not_reset_confirmation(self):
        self.observe("False")
        self.advance(590)
        self.nodes["n1"].status.addresses = [
            SimpleNamespace(type="InternalIP", address="2001:db8::10"),
            SimpleNamespace(type="InternalIP", address="192.0.2.10"),
        ]
        self.w.handle_node_update(self.nodes["n1"])
        self.advance(15)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 1)
        self.assertEqual(self.diagnostics()["probe"]["address"], "192.0.2.10")

    def test_unsafe_probe_configuration_rejected(self):
        for values in ({"NODE_PROBE_TIMEOUT_SECONDS": "0"}, {"NODE_PROBE_INTERVAL_SECONDS": "60"}):
            with self.subTest(values=values), patch.dict(os.environ, values):
                with self.assertRaises(ValueError):
                    NodeHealthWatcher()

    def test_unconfirmed_other_node_not_in_current_outage_list(self):
        self.incident()
        self.observe("Unknown", name="n2")
        payload = self.w.build_payload({"n1"}, set())
        self.assertEqual(payload["nodes_down_current"], "n1")
        wrapped = self.w.github_client_payload(payload)
        self.assertEqual(len(wrapped), 7)
        self.assertEqual(wrapped["details"]["node_diagnostics"]["n1"]["classification"], "host_unreachable")

    def test_retry_revalidates_and_relabels_host_that_now_responds(self):
        self.w.trigger_github_dispatch.return_value = False
        self.incident()
        self.reachable = True
        self.w.probed_at["n1"] = -100
        self.advance(100)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 1)
        self.w.trigger_github_dispatch.return_value = True
        self.advance(1100)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 2)
        self.assertEqual(self.diagnostics()["classification"], "cluster_unavailable_host_responding")

    def test_tcp_refusal_is_host_response_not_unreachability(self):
        with patch("src.watcher.main.socket.create_connection", side_effect=ConnectionRefusedError):
            result = NodeHealthWatcher.probe_host(self.w, "192.0.2.10")
        self.assertTrue(result["reachable"])
        self.assertEqual(result["response"], "tcp_refused")

    def test_probe_timeouts_are_bounded_on_both_ports(self):
        with patch("src.watcher.main.socket.create_connection", side_effect=socket.timeout) as connect:
            result = NodeHealthWatcher.probe_host(self.w, "192.0.2.10")
        self.assertFalse(result["reachable"])
        self.assertEqual(connect.call_count, 2)
        self.assertEqual([call.kwargs["timeout"] for call in connect.call_args_list], [1.0, 1.0])

    def test_probe_success_uses_no_http_or_ssh_credentials(self):
        with patch("src.watcher.main.socket.create_connection") as connect:
            result = NodeHealthWatcher.probe_host(self.w, "192.0.2.10")
        self.assertTrue(result["reachable"])
        connect.assert_called_once_with(("192.0.2.10", 10250), timeout=1.0)


if __name__ == "__main__":
    unittest.main()
