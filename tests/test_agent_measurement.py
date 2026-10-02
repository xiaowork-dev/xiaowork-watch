"""VPS-origin measurements: real local TCP and bounded DNS, no public traffic."""
import json
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from test_agent import agent, CONFIG, TASK, PING_OK


class MeasurementTests(unittest.TestCase):
    def test_unversioned_foreign_and_probe_tasks_never_execute(self):
        invalid = [dict(TASK, taskVersion=1), {key: value for key, value in TASK.items() if key != "taskVersion"},
                   dict(TASK, hostId=2), dict(TASK, nodeId=True), dict(TASK, protocol="UDP"),
                   dict(TASK, port=80), dict(TASK, protocol="TCP", port=True), dict(TASK, protocol="TCP", port=0)]
        for task in invalid:
            with self.subTest(task=task):
                client, measure = Mock(), Mock()
                client.post.return_value = {"role": "vps", "heartbeatSeconds": 30, "tasks": [task]}
                with self.assertRaises(agent.AgentError):
                    agent.Agent(CONFIG, client=client, measure=measure).step()
                measure.assert_not_called()
        client, measure = Mock(), Mock()
        client.post.return_value = {"role": "probe", "heartbeatSeconds": 30, "tasks": [TASK]}
        with self.assertRaises(agent.AgentError):
            agent.Agent(dict(CONFIG, role="probe"), client=client, measure=measure).step()
        measure.assert_not_called()

    def test_dns_rejects_any_unsafe_candidate_and_freezes_one_safe_ip(self):
        for addresses in (["8.8.8.8", "127.0.0.1"], ["8.8.8.8", "169.254.169.254"],
                          ["2001:4860:4860::8888", "::ffff:8.8.8.8"], [], ["8.8.8.8"] * 17):
            run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(addresses).encode(), b""))
            with self.subTest(addresses=addresses), self.assertRaises(agent.AgentError):
                agent.pin_target("target.example.test", runner=run)
        run = Mock(return_value=subprocess.CompletedProcess([], 0, b'["8.8.8.8","1.1.1.1"]', b""))
        self.assertEqual(str(agent.pin_target("target.example.test", runner=run)), "8.8.8.8")
        command = run.call_args.args[0]
        self.assertEqual(command[:3], [sys.executable, "-I", "-B"])
        self.assertEqual(command[-2:], ["--resolve-target", "target.example.test"])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 2)
        resolver = lambda *_args, **kwargs: agent.public_target("8.8.8.8")
        ping = Mock(return_value=agent.parse_ping(TASK["id"], PING_OK, 0))
        result = agent.execute_measurement(dict(TASK, target="target.example.test"), resolver=resolver, ping=ping)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(ping.call_args.args[0]["target"], "8.8.8.8")
        self.assertLessEqual(ping.call_args.kwargs["deadline"] - time.monotonic(), 8)

    def test_dns_capacity_timeout_and_worker_errors_are_bounded_and_release_slot(self):
        for _ in range(agent.MAX_TASKS):
            self.assertTrue(agent._DNS_SLOTS.acquire(blocking=False))
        try:
            run = Mock()
            with self.assertRaises(agent.AgentError):
                agent.pin_target("target.example.test", runner=run)
            run.assert_not_called()
        finally:
            for _ in range(agent.MAX_TASKS):
                agent._DNS_SLOTS.release()
        for failure in (subprocess.TimeoutExpired("resolver", 2), OSError("spawn failed")):
            with self.assertRaises(agent.AgentError):
                agent.pin_target("target.example.test", runner=Mock(side_effect=failure))
        good = Mock(return_value=subprocess.CompletedProcess([], 0, b'["8.8.8.8"]', b""))
        self.assertEqual(str(agent.pin_target("target.example.test", runner=good)), "8.8.8.8")
        ping, tcp = Mock(), Mock()
        error = agent.execute_measurement(dict(TASK, target="target.example.test"),
            resolver=Mock(side_effect=agent.AgentError("unsafe DNS")), ping=ping, tcp=tcp)
        self.assertEqual((error["status"], error["sent"], error["received"]), ("ERROR", 0, 0))
        ping.assert_not_called()
        tcp.assert_not_called()

    def test_actual_resolver_worker_localhost_and_stuck_child_timeout(self):
        self.assertTrue(agent.pin_target("localhost", allow_loopback=True).is_loopback)
        with self.assertRaises(agent.AgentError):
            agent.pin_target("localhost")
        # Actual run() timeout kills and reaps a child blocked in a stand-in DNS
        # call. This creates no resolver threads or public network requests.
        def blocked(_arguments, **kwargs):
            return subprocess.run([sys.executable, "-I", "-c", "import time; time.sleep(60)"], **kwargs)
        before = time.monotonic()
        with self.assertRaises(agent.AgentError):
            agent.pin_target("target.example.test", deadline=before + 0.15, runner=blocked)
        self.assertLess(time.monotonic() - before, 3)

    def test_real_local_tcp_five_connects_and_refused_port_are_measured(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(10)
        listener.settimeout(0.2)
        stopped = threading.Event()
        accepted = []
        def accept():
            while not stopped.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                accepted.append(1)
                connection.close()
        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        task = dict(TASK, protocol="TCP", port=listener.getsockname()[1], target="127.0.0.1")
        try:
            result = agent.execute_measurement(task, allow_loopback=True)
            self.assertEqual((result["status"], result["sent"], result["received"]), ("OK", 5, 5), result)
            self.assertGreaterEqual(result["avgRttMs"], 0)
            deadline = time.monotonic() + 2
            while len(accepted) < 5 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(accepted), 5)
            with self.assertRaises(agent.AgentError):
                agent.execute_measurement(task)
        finally:
            stopped.set()
            listener.close()
            thread.join(timeout=2)
        # Keep the same port bound but not listening, eliminating port reuse
        # races and making each real connect fail without any public traffic.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as refused:
            refused.bind(("127.0.0.1", 0))
            task["port"] = refused.getsockname()[1]
            result = agent.execute_measurement(task, allow_loopback=True)
            self.assertEqual((result["status"], result["sent"], result["received"], result["avgRttMs"]),
                             ("TIMEOUT", 5, 0, None))

    def test_tcp_uses_numeric_endpoint_and_reports_partial_success_actual_mean(self):
        connections = [Mock() for _ in range(5)]
        connections[1].connect.side_effect = ConnectionRefusedError()
        connections[3].connect.side_effect = socket.timeout()
        factory = Mock(side_effect=connections)
        task = dict(TASK, protocol="TCP", port=80)
        result = agent.execute_tcp(task, agent.public_target("8.8.8.8"), time.monotonic() + 8, socket_factory=factory)
        self.assertEqual((result["status"], result["sent"], result["received"]), ("OK", 5, 3))
        self.assertGreaterEqual(result["avgRttMs"], 0)
        for connection in connections:
            connection.connect.assert_called_once_with(("8.8.8.8", 80))
            connection.close.assert_called_once_with()
            self.assertLessEqual(connection.settimeout.call_args.args[0], 1)


if __name__ == "__main__":
    unittest.main()
