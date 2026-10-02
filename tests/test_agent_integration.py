"""Real localhost controller/agent/TCP/SQLite integration; no public endpoints."""
import hashlib
import json
from pathlib import Path
import shlex
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

from test_agent import agent, SOURCE

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import server


class ControllerAgentTests(unittest.TestCase):
    def test_vps_tcp_result_lost_ack_retries_exactly_once_without_remeasurement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = server.Application(root / "data", allow_private=True, start_scheduler=False,
                                     agent_dir=SOURCE.parent)
            self.addCleanup(app.close)
            controller = server.ControlServer(("127.0.0.1", 0), app)
            thread = threading.Thread(target=lambda: controller.serve_forever(poll_interval=0.02), daemon=True)
            thread.start()
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(10)
            listener.settimeout(0.1)
            stop, accepted = threading.Event(), []
            def accept():
                while not stop.is_set():
                    try:
                        connection, _ = listener.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    accepted.append(1)
                    connection.close()
            tcp_thread = threading.Thread(target=accept, daemon=True)
            tcp_thread.start()
            try:
                target = app.save_fleet("nodes", {"name": "local TCP", "region": "fixture", "enabled": True,
                    "address": "127.0.0.1", "protocol": "TCP", "port": listener.getsockname()[1]})
                host = app.save_fleet("hosts", {"name": "VPS", "region": "fixture", "enabled": True,
                    "address": "8.8.8.8", "nodeIds": [target["id"]]})
                base = "http://127.0.0.1:" + str(controller.server_port)
                enrollment = app.enrollment("vps", host["id"], base)
                command = shlex.split(enrollment["command"])
                self.assertEqual(command[command.index("--agent-sha") + 1], hashlib.sha256(SOURCE.read_bytes()).hexdigest())
                self.assertIn("--upgrade", shlex.split(enrollment["upgradeCommand"]))
                self.assertNotIn("--enrollment-token", enrollment["upgradeCommand"])
                config_path = root / "config.json"
                config = agent.enroll(base, command[command.index("--enrollment-token") + 1], config_path, development=True)
                actual = agent.Client(base, config["credential"], development=True)
                self.assertEqual(actual.post("/api/agent/heartbeat", {"agentVersion": 2})["tasks"], [])
                self.assertEqual(app.queue_host(host["id"])["count"], 1)
                payloads = []
                class LostAck:
                    def post(self, endpoint, body):
                        response = actual.post(endpoint, body)
                        if endpoint.endswith("results"):
                            payloads.append(dict(body))
                            if len(payloads) == 1:
                                raise agent.ApiError("fixture drops the first acknowledgement")
                        return response
                measure = Mock(side_effect=lambda task: agent.execute_measurement(task, allow_loopback=True))
                runtime = agent.Agent(config, client=LostAck(), measure=measure)
                self.assertEqual(runtime.step(), 1)
                self.assertEqual(len(runtime.pending), 1)
                self.assertEqual(runtime.step(), 0)
                self.assertEqual(runtime.pending, {})
                self.assertEqual(payloads[0], payloads[1])
                measure.assert_called_once()
                history = app.history("vps", host["id"], 1, 8)
                self.assertEqual(history["total"], 1)
                record = history["records"][0]
                self.assertEqual((record["direction"], record["protocol"], record["targetAddress"]),
                                 ("VPS_TO_TARGET", "TCP", "127.0.0.1"))
                self.assertEqual((payloads[0]["status"], payloads[0]["sent"], payloads[0]["received"]), ("OK", 5, 5))
                self.assertEqual(json.loads(config_path.read_text())["credential"], config["credential"])
            finally:
                stop.set()
                listener.close()
                tcp_thread.join(timeout=2)
                controller.shutdown()
                controller.server_close()
                thread.join(timeout=2)
                app.close()


if __name__ == "__main__":
    unittest.main()
