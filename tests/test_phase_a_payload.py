import os
import tempfile
import unittest

from src.watcher.main import NodeHealthWatcher


class PhaseAPayloadTests(unittest.TestCase):
    def test_phase_a_payload_and_incident_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "incidents.ndjson")
            os.environ["CLUSTER_NAME"] = "pi-k3s"
            os.environ["INCIDENT_LOG_PATH"] = log_path

            watcher = NodeHealthWatcher()
            watcher.node_states = {"n1": "False", "n2": "True"}
            watcher.pending_down = {"n1"}
            watcher.pending_recovered = set()

            payload = watcher.build_payload()
            self.assertEqual(payload["event_type"], "node")
            self.assertEqual(payload["event"], "incident")
            self.assertEqual(payload["legacy_event"], "incident")
            self.assertEqual(payload["error_type"], "node_not_ready")
            self.assertEqual(payload["error_code"], "NODE_NOT_READY")
            self.assertIn("summary", payload)
            self.assertIn("details", payload)

            watcher.append_incident_log(payload)
            with open(log_path, "r", encoding="utf-8") as handle:
                lines = [line for line in handle.read().splitlines() if line.strip()]

            self.assertEqual(len(lines), 1)
            self.assertIn('"recovery_status": "open"', lines[0])


if __name__ == "__main__":
    unittest.main()
