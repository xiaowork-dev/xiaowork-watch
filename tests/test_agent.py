"""Agent protocol and measured ICMP regression tests (no external servers)."""
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parent.parent / "agent" / "agent.py"
SPEC = importlib.util.spec_from_file_location("watch_live_agent", SOURCE)
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)
TOKEN = "enrollment_token_fixture_123456789"
CREDENTIAL = "credential_fixture_123456789012345"
TASK = {"id": "job_1", "hostId": 1, "target": "8.8.8.8", "sent": 5, "timeoutSeconds": 8}
CONFIG = {"schema": 1, "server": "https://watch.example.com", "credential": CREDENTIAL,
          "id": 1, "role": "probe", "heartbeatSeconds": 30, "development": False}
PING_OK = """PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.

--- 8.8.8.8 ping statistics ---
5 packets transmitted, 5 received, 0% packet loss, time 4005ms
rtt min/avg/max/mdev = 12.183/13.247/14.981/0.914 ms
"""
PING_PARTIAL = "5 packets transmitted, 3 received, 40% packet loss, time 4004ms\nrtt min/avg/max/mdev = 0.201/0.345/0.500/0.126 ms\n"
PING_TIMEOUT = "5 packets transmitted, 0 received, +5 errors, 100% packet loss, time 4049ms\n"


@contextlib.contextmanager
def http_fixture(callback):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            calls.append((self.path, dict(self.headers), body))
            status, headers, content = callback(self.path, body)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(content)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def success(data):
    return 200, {"Content-Type": "application/json"}, json.dumps({"code": 0, "message": "", "data": data}).encode()


class PingTests(unittest.TestCase):
    def test_measured_full_and_partial_packet_statistics(self):
        for text, code, received, average in ((PING_OK, 0, 5, 13.247), (PING_PARTIAL, 1, 3, 0.345)):
            with self.subTest(received=received):
                self.assertEqual(agent.parse_ping("job_1", text, code),
                                 {"jobId": "job_1", "sent": 5, "received": received,
                                  "avgRttMs": average, "status": "OK"})
        alternate = "5 packets transmitted, 5 packets received, 0% packet loss\nround-trip min/avg/max/stddev = 1.0/2.0/3.0/0.5 ms\n"
        self.assertEqual(agent.parse_ping("job_1", alternate, 0)["avgRttMs"], 2.0)

    def test_timeout_and_execution_error_are_distinct(self):
        timeout = agent.parse_ping("job_1", PING_TIMEOUT, 1)
        self.assertEqual((timeout["status"], timeout["sent"], timeout["received"], timeout["avgRttMs"]), ("TIMEOUT", 5, 0, None))
        for output in ("ping: socket: Operation not permitted\n", "", "5 packets transmitted, 0 received\n"):
            self.assertEqual(agent.parse_ping("job_1", output, 2)["status"], "ERROR")
        for output in ("9 packets transmitted, 1 received\n", "5 packets transmitted, 7 received\n", "5 packets transmitted, 1 received\n"):
            self.assertEqual(agent.parse_ping("job_1", output, 0)["status"], "ERROR")

    def test_task_rejects_injection_private_addresses_and_unbounded_values(self):
        targets = ("8.8.8.8; touch /tmp/unsafe", "--help", "google.com", "127.0.0.1", "10.0.0.1", "169.254.169.254",
                   "100.64.0.1", "224.0.0.1", "::1", "fe80::1%eth0", "fc00::1", "2001:db8::1", "::ffff:8.8.8.8")
        for target in targets:
            with self.subTest(target=target):
                run = Mock()
                result = agent.execute_ping(dict(TASK, target=target), runner=run)
                self.assertEqual((result["status"], result["sent"]), ("ERROR", 0))
                run.assert_not_called()
        for changes in ({"sent": 1000}, {"sent": True}, {"timeoutSeconds": 9999}, {"timeoutSeconds": "8"},
                        {"id": "bad;id"}, {"hostId": False}):
            with self.subTest(changes=changes), self.assertRaises(agent.AgentError):
                agent.validate_task(dict(TASK, **changes))

    def test_ipv4_ipv6_ping_argv_timeout_locale_and_errors(self):
        for target, family in (("8.8.8.8", "-4"), ("2606:4700:4700::1111", "-6")):
            run = Mock(return_value=subprocess.CompletedProcess([], 0, PING_OK.encode(), b""))
            result = agent.execute_ping(dict(TASK, target=target), runner=run)
            self.assertEqual(result["status"], "OK")
            arguments = run.call_args.args[0]
            self.assertEqual(arguments, ["/usr/bin/ping", family, "-n", "-q", "-c", "5", "-i", "1", "-W", "2", "-w", "8", "--", target])
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")
            self.assertEqual(run.call_args.kwargs["timeout"], 10)
        for failure in (OSError("missing ping"), subprocess.TimeoutExpired("ping", 10)):
            run = Mock(side_effect=failure)
            self.assertEqual(agent.execute_ping(TASK, runner=run)["status"], "ERROR")

    @unittest.skipUnless(sys.platform == "linux", "Real iputils ping requires Linux")
    def test_real_loopback_ping_reports_real_five_packet_summary(self):
        self.assertTrue(Path("/usr/bin/ping").is_file(), "CI must install iputils-ping")
        result = agent.execute_ping(dict(TASK, target="127.0.0.1"), allow_loopback=True)
        self.assertEqual((result["status"], result["sent"], result["received"]), ("OK", 5, 5), result)
        self.assertIsInstance(result["avgRttMs"], float)
        self.assertGreaterEqual(result["avgRttMs"], 0)
        with self.assertRaises(agent.AgentError):
            agent.validate_task(dict(TASK, target="127.0.0.1"))

    def test_cloud_special_and_transition_ranges_match_controller_public_policy(self):
        for target in ("168.63.129.16", "192.0.0.9", "192.0.0.10", "64:ff9b::808:808", "2001:3::1"):
            with self.subTest(target=target), self.assertRaises(agent.AgentError):
                agent.public_target(target)
        for target in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "2001:4860:4860::8888"):
            with self.subTest(target=target):
                self.assertEqual(str(agent.public_target(target)), target)


class ProtocolTests(unittest.TestCase):
    def test_https_required_and_development_http_limited_to_loopback(self):
        for value in ("http://watch.example.com", "http://10.0.0.1", "https://user:password@watch.example.com", "https://watch.example.com/path",
                      "https://watch.example.com/?token=x", "https://watch.example.com/#fragment", "https://watch.example.com\n"):
            with self.subTest(value=value), self.assertRaises(agent.AgentError):
                agent.server_url(value, development=True)
        with self.assertRaises(agent.AgentError):
            agent.server_url("http://127.0.0.1:8888")
        self.assertEqual(agent.server_url("http://[::1]:8888/", True), "http://[::1]:8888")
        self.assertEqual(agent.server_url("https://WATCH.example.com/"), "https://watch.example.com")
        with patch.object(agent.ssl, "create_default_context", wraps=agent.ssl.create_default_context) as tls:
            agent.Client("https://watch.example.com")
        tls.assert_called_once_with()

    def test_real_local_enrollment_bearer_heartbeat_and_result_protocol(self):
        def response(path, body):
            if path.endswith("enroll"):
                self.assertEqual(body, {"token": TOKEN})
                return success({"credential": CREDENTIAL, "role": "probe", "id": 1, "heartbeatSeconds": 30})
            if path.endswith("heartbeat"):
                return success({"role": "probe", "heartbeatSeconds": 30, "tasks": [TASK]})
            return success({"accepted": True})
        with tempfile.TemporaryDirectory() as temporary, http_fixture(response) as (server, calls):
            path = Path(temporary) / "config.json"
            config = agent.enroll(server, TOKEN, path, development=True)
            self.assertNotIn(TOKEN, path.read_text())
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            runtime = agent.Agent(config, ping=lambda task: agent.parse_ping(task["id"], PING_OK, 0))
            self.assertEqual(runtime.step(), 1)
            self.assertEqual(calls[0][0], "/api/agent/enroll")
            self.assertNotIn("Authorization", calls[0][1])
            self.assertEqual(calls[1][1]["Authorization"], "Bearer " + CREDENTIAL)
            self.assertEqual(calls[2][2]["sent"], 5)
            self.assertEqual(calls[2][2]["avgRttMs"], 13.247)
            self.assertEqual(agent.read_config(path), config)
            with self.assertRaises(agent.AgentError):
                agent.enroll(server, TOKEN, path, development=True)
            self.assertEqual(len(calls), 3)

    def test_redirect_never_sends_credentials_to_another_origin(self):
        with http_fixture(lambda *_: success({})) as (target, target_calls):
            with http_fixture(lambda *_: (302, {"Location": target + "/steal"}, b"")) as (server, calls):
                with self.assertRaises(agent.ApiError):
                    agent.Client(server, CREDENTIAL, development=True).post("/api/agent/heartbeat", {})
                self.assertEqual(len(calls), 1)
            self.assertEqual(target_calls, [])

    def test_error_response_does_not_expose_secret_or_accept_bad_envelope(self):
        for status, content in ((401, CREDENTIAL.encode()), (200, b'{"code":false,"data":{}}'),
                                (200, b'{"code":0,"data":{"number":NaN}}'), (200, b"not json"),
                                (200, b"x" * (agent.MAX_RESPONSE + 1))):
            with self.subTest(status=status), http_fixture(lambda *_: (status, {}, content)) as (server, _):
                with self.assertRaises(agent.ApiError) as error:
                    agent.Client(server, CREDENTIAL, development=True).post("/api/agent/heartbeat", {})
                self.assertNotIn(CREDENTIAL, str(error.exception))

    def test_offline_results_retry_exact_payload_without_repeating_ping(self):
        client = Mock()
        results = []
        attempts = [agent.ApiError(), agent.ApiError(status=409)]
        def post(path, body):
            if path.endswith("heartbeat"):
                return {"role": "probe", "heartbeatSeconds": 30, "tasks": [TASK]}
            results.append(dict(body))
            failure = attempts.pop(0) if attempts else None
            if failure:
                raise failure
            return {}
        client.post.side_effect = post
        ping = Mock(return_value=agent.parse_ping(TASK["id"], PING_PARTIAL, 1))
        runtime = agent.Agent(CONFIG, client=client, ping=ping)
        runtime.step()
        self.assertIn(TASK["id"], runtime.pending)
        runtime.step()
        self.assertEqual(runtime.pending, {})
        self.assertEqual(results[0], results[1])
        ping.assert_called_once_with(TASK)

    def test_vps_never_pings_and_task_limit_or_wrong_role_rejected(self):
        ping = Mock()
        client = Mock()
        client.post.return_value = {"role": "vps", "heartbeatSeconds": 30, "tasks": []}
        runtime = agent.Agent(dict(CONFIG, role="vps"), client=client, ping=ping)
        self.assertEqual(runtime.step(), 0)
        ping.assert_not_called()
        for payload in ({"role": "probe", "heartbeatSeconds": 30, "tasks": [TASK] * 11},
                        {"role": "probe", "heartbeatSeconds": 30, "tasks": [TASK, TASK]},
                        {"role": "vps", "heartbeatSeconds": 30, "tasks": [TASK]},
                        {"role": "probe", "heartbeatSeconds": 0, "tasks": []}):
            with self.subTest(payload=payload), self.assertRaises(agent.AgentError):
                agent.heartbeat_tasks(payload, payload["role"])

    def test_disconnect_loop_retries_with_bounded_backoff_and_no_secret_log(self):
        runtime = agent.Agent(CONFIG, client=Mock())
        runtime.step = Mock(side_effect=agent.ApiError(CREDENTIAL))
        stop = Mock()
        stop.is_set.side_effect = [False, False, False, False, True]
        with patch("sys.stderr", new=io.StringIO()) as output:
            runtime.run(stop)
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list], [5, 10, 20, 30])
        self.assertNotIn(CREDENTIAL, output.getvalue())


if __name__ == "__main__":
    unittest.main()
