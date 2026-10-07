import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.watcher.main import NodeHealthWatcher


def node(name, status):
    conditions = [] if status is None else [SimpleNamespace(type="Ready", status=status)]
    return SimpleNamespace(metadata=SimpleNamespace(name=name),
                           status=SimpleNamespace(conditions=conditions))


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {
            "CLUSTER_NAME": "test", "INCIDENT_LOG_PATH": self.temp.name + "/incidents.ndjson",
            "STATE_PATH": self.temp.name + "/state.json", "DOWN_CONFIRM_SECONDS": "180",
            "RECOVERY_CONFIRM_SECONDS": "120", "WATCH_DEBOUNCE_SECONDS": "5",
            "DELIVERY_MAX_ATTEMPTS": "3", "GHA_DISPATCH_URL": "https://example.invalid/dispatch",
            "GHA_TOKEN": "test", "AIRFLOW_BASE_URL": "http://example.invalid",
            "AIRFLOW_USERNAME": "test", "AIRFLOW_PASSWORD": "test",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = 0.0
        clock = patch("src.watcher.main.time.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.w = self.watcher()

    def watcher(self):
        watcher = NodeHealthWatcher()
        watcher.trigger_airflow = Mock(return_value=True)
        watcher.trigger_github_dispatch = Mock(return_value=True)
        return watcher

    def observe(self, name, status):
        self.w.handle_node_update(node(name, status))
        self.w.flush_if_due()

    def advance(self, seconds):
        self.now += seconds
        self.w.flush_if_due()

    def incident(self, name="n1"):
        self.observe(name, "False")
        self.advance(180)
        self.advance(5)

    def test_defaults_and_initial_down_not_preannounced(self):
        api = SimpleNamespace(list_node=Mock(return_value=SimpleNamespace(items=[node("n1", "False")])))
        self.w.prime_node_state(api)
        api.list_node.assert_called_once_with(_request_timeout=10)
        self.assertFalse(self.w.announced["github"])
        self.advance(179)
        self.w.trigger_github_dispatch.assert_not_called()
        self.advance(1)
        self.w.trigger_github_dispatch.assert_not_called()
        self.advance(5)
        self.assertEqual(self.w.announced["github"], {"n1"})

    def test_short_down_and_unpaired_recovery_suppressed(self):
        self.observe("n1", "True")
        self.observe("n1", "False")
        self.advance(179)
        self.observe("n1", "True")
        self.advance(200)
        self.w.trigger_github_dispatch.assert_not_called()

    def test_false_unknown_share_one_timer_and_missing_ready_is_down(self):
        self.observe("n1", "False")
        self.advance(100)
        self.observe("n1", "Unknown")
        self.advance(80)
        self.advance(5)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 1)
        self.observe("n2", None)
        self.advance(180)
        self.advance(5)
        self.assertEqual(self.w.announced["github"], {"n1", "n2"})

    def test_short_up_suppressed_then_confirmed_recovery(self):
        self.incident()
        self.observe("n1", "True")
        self.advance(119)
        self.observe("n1", "False")
        self.advance(200)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 1)
        self.observe("n1", "True")
        self.advance(120)
        self.advance(5)
        payload = self.w.trigger_github_dispatch.call_args.args[0]
        self.assertEqual(payload["event"], "recovery")
        self.assertFalse(self.w.announced["github"])

    def test_restart_accepted_down_no_repeat_and_ready_confirms(self):
        self.incident()
        self.w = self.watcher()
        self.observe("n1", "Unknown")
        self.advance(300)
        self.w.trigger_github_dispatch.assert_not_called()
        self.observe("n1", "True")
        self.advance(119)
        self.w.trigger_github_dispatch.assert_not_called()
        self.advance(1)
        self.advance(5)
        self.assertEqual(self.w.trigger_github_dispatch.call_args.args[0]["event"], "recovery")

    def test_restart_unannounced_down_reconfirms_not_false_recovery(self):
        self.observe("n1", "False")
        self.advance(100)
        self.w = self.watcher()
        self.observe("n1", "Unknown")
        self.advance(179)
        self.w.trigger_github_dispatch.assert_not_called()
        self.advance(1)
        self.advance(5)
        self.assertEqual(self.w.trigger_github_dispatch.call_args.args[0]["event"], "incident")

    def test_restart_short_initial_down_to_ready_is_silent(self):
        self.observe("n1", "False")
        self.w = self.watcher()
        self.observe("n1", "True")
        self.advance(300)
        self.w.trigger_github_dispatch.assert_not_called()

    def test_grouping_starts_after_per_node_confirmation(self):
        self.observe("n1", "False")
        self.advance(3)
        self.observe("n2", "False")
        self.advance(177)
        self.advance(3)
        self.w.trigger_github_dispatch.assert_not_called()
        self.advance(2)
        self.assertEqual(self.w.trigger_github_dispatch.call_args.args[0]["nodes_down"], "n1,n2")

    def test_mixed_confirmed_transitions(self):
        self.incident("n1")
        self.observe("n2", "False")
        self.advance(60)
        self.observe("n1", "True")
        self.advance(120)
        self.advance(5)
        self.assertEqual(self.w.trigger_github_dispatch.call_args.args[0]["event"], "mixed")

    def test_wall_clock_jump_cannot_confirm(self):
        self.observe("n1", "False")
        with patch("src.watcher.main.time.time", return_value=10**12):
            self.w.flush_if_due()
        self.w.trigger_github_dispatch.assert_not_called()

    def test_partial_delivery_retry_and_restart_do_not_resend_accepted(self):
        self.w.trigger_airflow.return_value = False
        self.incident()
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 1)
        self.assertEqual(self.w.outbox[0]["deliveries"]["airflow"]["attempts"], 1)
        self.w = self.watcher()
        self.observe("n1", "False")
        self.advance(180)
        self.w.trigger_github_dispatch.assert_not_called()
        self.w.trigger_airflow.assert_called_once()
        self.assertEqual(self.w.announced["airflow"], {"n1"})

    def test_retry_budget_bounded_retained_and_recovery_paired_per_transport(self):
        self.w.trigger_airflow.return_value = False
        self.incident()
        self.advance(1)
        self.advance(2)
        self.advance(100)
        self.assertEqual(self.w.trigger_airflow.call_count, 3)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 1)
        self.assertEqual(self.w.outbox[0]["deliveries"]["airflow"]["status"], "exhausted")
        self.w = self.watcher()
        self.observe("n1", "True")
        self.advance(120)
        self.advance(5)
        self.w.trigger_airflow.assert_not_called()
        self.assertEqual(self.w.trigger_github_dispatch.call_args.args[0]["event"], "recovery")
        self.assertEqual(len(self.w.outbox), 1)

    def test_failed_both_no_unmatched_recovery_and_retry_superseded(self):
        self.w.trigger_airflow.return_value = False
        self.w.trigger_github_dispatch.return_value = False
        self.incident()
        self.observe("n1", "True")
        self.advance(120)
        self.advance(5)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 1)
        self.assertFalse(self.w.announced["github"])

    def test_failed_recovery_then_down_no_repeat_incident(self):
        self.incident()
        self.w.trigger_github_dispatch.return_value = False
        self.observe("n1", "True")
        self.advance(120)
        self.advance(5)
        self.observe("n1", "False")
        self.advance(180)
        self.advance(5)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 2)
        self.assertEqual(self.w.announced["github"], {"n1"})

    def test_force_never_bypasses_confirmation(self):
        self.observe("n1", "False")
        self.w.flush_if_due(force=True)
        self.w.trigger_github_dispatch.assert_not_called()

    def test_corrupt_state_fails_closed(self):
        self.w.state_path.write_text("not json", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            NodeHealthWatcher()

    def test_state_failure_prevents_dispatch(self):
        self.observe("n1", "False")
        self.advance(180)
        with patch.object(self.w, "save_state", side_effect=OSError("disk full")):
            self.now += 5
            with self.assertRaises(OSError):
                self.w.flush_if_due()
        self.w.trigger_github_dispatch.assert_not_called()

    def test_real_github_body_matches_deployed_contract(self):
        payload = self.w.build_payload({"n1"}, set())
        with patch("src.watcher.main.requests.post", return_value=SimpleNamespace(status_code=204)) as post:
            self.assertTrue(NodeHealthWatcher.trigger_github_dispatch(self.w, payload))
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["event_type"], "k3s-alert")
        client_payload = body["client_payload"]
        self.assertEqual(set(client_payload), {"cluster", "event", "status", "resource_type",
                                               "summary", "timestamp", "details"})
        self.assertLessEqual(len(client_payload), 10)
        self.assertEqual(client_payload["details"]["nodes_down"], "n1")
        self.assertEqual(client_payload["details"]["raw_details"], payload["details"])

    def test_relist_removal_cleans_all_state_without_recovery(self):
        self.incident()
        self.w.reconcile_nodes([])
        self.advance(300)
        self.assertNotIn("n1", self.w.node_states)
        self.assertNotIn("n1", self.w.confirmed_down)
        self.assertNotIn("n1", self.w.observed_since)
        self.assertNotIn("n1", self.w.pending_down | self.w.pending_recovered)
        self.assertFalse(any("n1" in names for names in self.w.announced.values()))
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 1)
        self.assertEqual(self.w.build_payload()["nodes_down_current"], "")
        self.w = self.watcher()
        self.assertNotIn("n1", self.w.node_states)

    def test_delete_cancels_unsent_recovery_without_dispatch(self):
        self.incident()
        self.w.trigger_github_dispatch.return_value = False
        self.observe("n1", "True")
        self.advance(120)
        self.advance(5)
        self.w.remove_node("n1")
        self.advance(300)
        self.assertEqual(self.w.trigger_github_dispatch.call_count, 2)
        self.assertFalse(self.w.announced["github"])
        self.assertFalse(self.w.outbox)


if __name__ == "__main__":
    unittest.main()
