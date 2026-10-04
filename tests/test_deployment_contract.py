from pathlib import Path
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class DeploymentContractTests(unittest.TestCase):
    def test_shared_rwx_state_has_isolated_paths_and_single_writer(self):
        deployment = yaml.safe_load((ROOT / "k8s/deployment.yaml").read_text())
        spec = deployment["spec"]
        self.assertEqual(spec["replicas"], 1)
        self.assertEqual(spec["strategy"], {"type": "Recreate", "rollingUpdate": None})
        volume = next(volume for volume in spec["template"]["spec"]["volumes"]
                      if volume["name"] == "watcher-state")
        self.assertEqual(volume["persistentVolumeClaim"]["claimName"], "k3s-node-alert-state")
        config = yaml.safe_load((ROOT / "k8s/configmap.yaml").read_text())["data"]
        self.assertEqual(config["DOWN_CONFIRM_SECONDS"], "180")
        self.assertEqual(config["RECOVERY_CONFIRM_SECONDS"], "120")
        self.assertEqual(config["STATE_PATH"], "/var/lib/node-health-watcher/watcher/state.json")
        self.assertEqual(config["INCIDENT_LOG_PATH"], "/var/lib/node-health-watcher/watcher/incidents.ndjson")

    def test_actions_restart_and_capture_nonsecret_rollback(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yaml").read_text())
        steps = workflow["jobs"]["deploy"]["steps"]
        deploy = next(step["run"] for step in steps if step.get("name") == "Deploy manifests")
        self.assertIn("rollout restart deployment/node-health-watcher", deploy)
        self.assertLess(deploy.index("rollback.yaml"), deploy.index("kubectl apply"))
        self.assertNotIn("get secret", deploy)
        collection = next(step["run"] for step in steps if step.get("name") == "Collect rollback evidence")
        self.assertIn("test -f /tmp/node-health-watcher/rollback.yaml", collection)
        for script in (deploy, collection):
            subprocess.run(["bash", "-n"], input=script, text=True, check=True)


if __name__ == "__main__":
    unittest.main()
