"""Offline integration tests: real local HTTP/TLS, SQLite, auth and agent jobs."""
import concurrent.futures
import gc
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import network, server


class LocalTarget(http.server.BaseHTTPRequestHandler):
    def log_message(self, *unused):
        pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self.server.methods.append(self.command)
        path = self.path.split("?")[0]
        try:
            if path == "/drip":
                self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Drip: ")
                self.wfile.flush()
                for unused in range(30):
                    self.wfile.write(b"a")
                    self.wfile.flush()
                    time.sleep(0.03)
                self.wfile.write(b"\r\nContent-Length: 0\r\n\r\n")
                return
            if path == "/hold":
                with self.server.count_lock:
                    self.server.holding += 1
                self.server.release.wait(2)
            if path in ("/redirect", "/metadata", "/loop"):
                self.send_response(302)
                self.send_header("Location", {"/redirect": "/ok", "/metadata": "http://169.254.169.254/latest/meta-data/", "/loop": "/loop"}[path])
                self.end_headers()
                return
            self.send_response(503 if path == "/bad" else 200)
            self.send_header("Content-Length", "100000000" if path == "/large" else "0")
            self.end_headers()
        except OSError:
            pass


class BackendCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 1700000000.0
        self.agent_dir = self.root / "agent"
        self.agent_dir.mkdir()
        (self.agent_dir / "agent.py").write_bytes(b"# fixture agent; no network\n")
        (self.agent_dir / "install.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
        self.app = server.Application(self.root / "data", allow_private=True, start_scheduler=False,
                                      clock=lambda: self.now, agent_dir=self.agent_dir)
        self.password = self.app.init_admin()
        self.target = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LocalTarget)
        self.target.daemon_threads = True
        self.target.methods = []
        self.target.holding = 0
        self.target.count_lock = threading.Lock()
        self.target.release = threading.Event()
        self.target_thread = threading.Thread(target=lambda: self.target.serve_forever(poll_interval=0.02), daemon=True)
        self.target_thread.start()
        self.url = "http://127.0.0.1:" + str(self.target.server_port)
        self.http = server.ControlServer(("127.0.0.1", 0), self.app)
        self.http_thread = threading.Thread(target=lambda: self.http.serve_forever(poll_interval=0.02), daemon=True)
        self.http_thread.start()
        self.host = "localhost:" + str(self.http.server_port)
        self.cookie = None
        self.csrf = None

    def tearDown(self):
        self.target.release.set()
        self.http.shutdown()
        self.http.server_close()
        self.app.close()
        self.target.shutdown()
        self.target.server_close()
        self.temp.cleanup()

    def request(self, method, path, body=None, admin=False, headers=None):
        final = {"Host": self.host}
        if method != "GET":
            final["Origin"] = "http://" + self.host
        if admin:
            final.update({"Cookie": self.cookie or "", "X-CSRF-Token": self.csrf or ""})
        final.update(headers or {})
        payload = None if body is None else json.dumps(body)
        if payload is not None:
            final["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=5)
        connection.request(method, path, body=payload, headers=final)
        response = connection.getresponse()
        raw = response.read()
        response_headers = dict(response.getheaders())
        status = response.status
        connection.close()
        content = json.loads(raw) if response_headers["Content-Type"].startswith("application/json") else raw
        return status, content, response_headers

    def login(self):
        status, result, headers = self.request("POST", "/api/auth/login", {"username": "admin", "password": self.password})
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";")[0]
        self.csrf = result["data"]["csrfToken"]
        return result, headers

    def monitor(self, suffix="/ok", **changes):
        body = {"name": "测试网站", "url": self.url + suffix, "method": "GET", "intervalSeconds": 30, "timeoutMs": 1000, "enabled": True}
        body.update(changes)
        return self.app.save_monitor(body)

    def probe(self, name="测试目标", address="8.8.8.8", protocol="ICMP", port=None):
        return self.app.save_fleet("nodes", {"name": name, "region": "测试区", "enabled": True,
                                              "address": address, "protocol": protocol, "port": port})

    def host_item(self, nodes):
        return self.app.save_fleet("hosts", {"name": "VPS", "region": "测试区", "address": "8.8.8.8", "enabled": True, "nodeIds": nodes})

    def register(self, role, object_id):
        info = self.app.enrollment(role, object_id, "https://watch.example.com")
        args = shlex.split(info["command"])
        token = args[args.index("--enrollment-token") + 1]
        registered = self.app.enroll({"token": token})
        self.app.heartbeat(registered["credential"], {"agentVersion": 2})
        return registered

    def task(self):
        node = self.probe()
        host = self.host_item([node["id"]])
        vps = self.register("vps", host["id"])
        self.app.queue_host(host["id"])
        job = self.app.heartbeat(vps["credential"], {"agentVersion": 2})["tasks"][0]
        return host, node, vps, vps, job

    def result_body(self, job, **changes):
        body = {"jobId": job["id"], "sent": 5, "received": 4, "avgRttMs": 12.3, "status": "OK"}
        body.update(changes)
        return body

    def assert_api_error(self, code, function, *args, **kwargs):
        with self.assertRaises(server.APIError) as failure:
            function(*args, **kwargs)
        self.assertEqual(failure.exception.status, code)

    def wait_history(self, object_id, total):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            actual = self.app.history("monitors", object_id, 1, 8)["total"]
            if actual == total:
                return
            time.sleep(0.01)
        self.fail("background check did not write expected history")

    def test_public_reads_empty_real_data_and_protected_writes(self):
        for path, expected in (("/api/monitors", []), ("/api/fleet", {"hosts": [], "nodes": [], "results": []}),
                               ("/api/vps", []), ("/api/probes", [])):
            status, content, unused = self.request("GET", path)
            self.assertEqual((status, content["code"], content["data"]), (200, 0, expected))
        status, content, unused = self.request("GET", "/api/auth/session")
        self.assertFalse(content["data"]["authenticated"])
        self.assertIsNone(content["data"]["csrfToken"])
        for method, path in (("POST", "/api/monitors"), ("POST", "/api/vps/1/checks"),
                             ("POST", "/api/probes/1/enrollment"), ("PATCH", "/api/vps/1/enabled")):
            status, content, unused = self.request(method, path, {})
            self.assertEqual((status, content["code"]), (401, 401))
        with patch.dict(os.environ, {"WATCH_RELEASE": "a" * 40}):
            status, content, unused = self.request("GET", "/api/health")
            self.assertEqual(content["data"]["release"], "a" * 40)

    def test_password_no_shared_default_and_init_does_not_overwrite(self):
        self.assertIsNone(self.app.init_admin())
        with self.app.lock:
            encoded = self.app.db.execute("SELECT password_hash FROM admin").fetchone()[0]
        self.assertNotIn(self.password, encoded)
        self.assertTrue(server.password_matches(self.password, encoded))
        self.assertFalse(server.password_matches("admin", encoded))

    def test_cli_isolated_init_and_reset_preserve_data_and_revoke_sessions(self):
        executable = str(Path(server.__file__))
        data = self.root / "cli-data"
        def cli(action):
            return subprocess.run([sys.executable, "-B", "-I", executable, "--data-dir", str(data), action],
                                  capture_output=True, text=True, encoding="utf-8", timeout=10, check=True).stdout
        initial = cli("--init-admin")
        self.assertIn("Username: admin", initial)
        password = next(line[10:] for line in initial.splitlines() if line.startswith("Password: "))
        self.assertGreaterEqual(len(password), 20)
        self.assertNotIn("Password:", cli("--init-admin"))
        second = server.Application(data, start_scheduler=False)
        try:
            unused, token = second.login({"username": "admin", "password": password}, "127.0.0.1")
            self.assertTrue(second.session(token)["authenticated"])
            reset = cli("--reset-admin")
            fresh = next(line[10:] for line in reset.splitlines() if line.startswith("Password: "))
            self.assertNotEqual(password, fresh)
            self.assertIsNone(second.session(token))
            self.assert_api_error(401, second.login, {"username": "admin", "password": password}, "127.0.0.1")
            self.assertTrue(second.login({"username": "admin", "password": fresh}, "127.0.0.1")[0]["authenticated"])
        finally:
            second.close()

    def test_login_cookie_csrf_origin_logout_and_reset(self):
        unused, headers = self.login()
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        monitor = self.monitor()
        for changes in ({"X-CSRF-Token": "wrong"}, {"Origin": "https://evil.example"}, {"Origin": ""}):
            status, unused, unused_headers = self.request("PATCH", "/api/monitors/%d/enabled" % monitor["id"], {"enabled": False}, admin=True, headers=changes)
            self.assertEqual(status, 403)
        self.assertTrue(self.app.monitors(monitor["id"])["enabled"])
        status, unused, unused_headers = self.request("POST", "/api/auth/logout", admin=True)
        self.assertEqual(status, 200)
        self.assertEqual(self.request("PATCH", "/api/monitors/%d/enabled" % monitor["id"], {"enabled": False}, admin=True)[0], 401)
        self.login()
        self.password = self.app.init_admin(reset=True)
        self.assertEqual(self.request("PATCH", "/api/monitors/%d/enabled" % monitor["id"], {"enabled": False}, admin=True)[0], 401)

    def test_https_secure_cookie_only_from_loopback_proxy(self):
        status, unused, headers = self.request("POST", "/api/auth/login", {"username": "admin", "password": self.password},
            headers={"X-Forwarded-Proto": "https", "Origin": "https://" + self.host})
        self.assertEqual(status, 200)
        self.assertIn("; Secure", headers["Set-Cookie"])
        handler = server.Handler.__new__(server.Handler)
        handler.client_address = ("8.8.8.8", 123)
        handler.headers = {"X-Forwarded-Proto": "https", "X-Real-IP": "1.1.1.1"}
        self.assertEqual(handler._scheme(), "http")
        self.assertEqual(handler._client_ip(), "8.8.8.8")

    def test_production_http_is_public_read_only_and_https_is_required(self):
        self.login()
        monitor = self.monitor("/ok?token=private-value")
        self.app.allow_private = False
        for method, path, body in (
            ("POST", "/api/auth/login", {"username": "admin", "password": self.password}),
            ("POST", "/api/auth/logout", {}),
            ("PATCH", "/api/monitors/%d/enabled" % monitor["id"], {"enabled": False}),
            ("POST", "/api/monitors/%d/check" % monitor["id"], {}),
            ("POST", "/api/agent/enroll", {"token": "x" * 32}),
            ("POST", "/api/agent/heartbeat", {}),
            ("POST", "/api/agent/results", {}),
        ):
            status, content, unused = self.request(method, path, body, admin=True)
            self.assertEqual((status, content["code"]), (403, 403))
            self.assertIn("菜单8配置HTTPS", content["message"])
        self.assertFalse(self.request("GET", "/api/auth/session", admin=True)[1]["data"]["authenticated"])
        public = self.request("GET", "/api/monitors", admin=True)[1]["data"]
        self.assertNotIn("private-value", json.dumps(public))
        self.assertEqual(self.request("GET", "/api/fleet")[0], 200)
        self.assertEqual(self.request("GET", "/agent/agent.py")[0], 200)
        secure = {"X-Forwarded-Proto": "https", "Origin": "https://" + self.host}
        status, content, headers = self.request("POST", "/api/auth/login", {"username": "admin", "password": self.password}, headers=secure)
        self.assertEqual(status, 200)
        self.assertIn("; Secure", headers["Set-Cookie"])
        self.cookie = headers["Set-Cookie"].split(";")[0]
        self.csrf = content["data"]["csrfToken"]
        self.assertEqual(self.request("PATCH", "/api/monitors/%d/enabled" % monitor["id"], {"enabled": False}, admin=True, headers=secure)[0], 200)
        self.assertEqual(self.request("POST", "/api/agent/heartbeat", {}, headers=secure)[0], 401)
        self.app.allow_private = True
        self.assertEqual(self.request("POST", "/api/auth/login", {}, headers={"Host": "watch.example.com", "Origin": "http://watch.example.com"})[0], 403)

    def test_login_rate_limit_uses_strict_trusted_real_ip(self):
        wrong = {"username": "admin", "password": "incorrect"}
        for unused in range(5):
            self.assertEqual(self.request("POST", "/api/auth/login", wrong, headers={"X-Real-IP": "8.8.8.8"})[0], 401)
        self.assertEqual(self.request("POST", "/api/auth/login", wrong, headers={"X-Real-IP": "8.8.8.8"})[0], 429)
        self.assertEqual(self.request("POST", "/api/auth/login", wrong, headers={"X-Real-IP": "1.1.1.1"})[0], 401)
        self.assertEqual(self.request("POST", "/api/auth/login", wrong, headers={"X-Real-IP": "8.8.8.8,1.1.1.1"})[0], 400)

    def test_public_url_query_redacted_and_admin_can_edit_full_url(self):
        monitor = self.monitor("/ok?api_key=fixture-secret#fragment")
        public = self.request("GET", "/api/monitors/%d" % monitor["id"])[1]["data"]
        self.assertEqual(public["url"], self.url + "/ok")
        self.login()
        private = self.request("GET", "/api/monitors/%d" % monitor["id"], admin=True)[1]["data"]
        self.assertIn("api_key=fixture-secret", private["url"])
        self.assertNotIn("fixture-secret", json.dumps(self.request("GET", "/api/fleet")[1]))

    def test_real_http_get_head_status_and_large_body(self):
        self.login()
        for suffix, method, success, error in (("/ok", "GET", True, None), ("/ok", "HEAD", True, None),
                                               ("/bad", "GET", False, "HTTP_STATUS"), ("/large", "GET", True, None),
                                               ("/redirect", "GET", True, None)):
            monitor = self.monitor(suffix, method=method)
            status, content, unused = self.request("POST", "/api/monitors/%d/check" % monitor["id"], admin=True)
            self.assertEqual(status, 200)
            self.assertEqual((content["data"]["success"], content["data"]["errorType"]), (success, error))
            self.assertGreaterEqual(content["data"]["responseTimeMs"], 0)
            self.assertEqual(self.app.monitors(monitor["id"])["lastStatus"], "UP" if success else "DOWN")
        self.assertIn("HEAD", self.target.methods)

    def test_absolute_deadline_stops_dripping_headers(self):
        start = time.monotonic()
        result = network.check_http(self.url + "/drip", timeout_ms=120, allow_private=True)
        self.assertEqual(result["errorType"], "TIMEOUT")
        self.assertLess(time.monotonic() - start, 0.7)

    def test_ssrf_default_rejects_private_ip_dns_and_credentials(self):
        for value in ("http://127.0.0.1/", "http://169.254.169.254/", "http://[::1]/"):
            self.assertEqual(network.check_http(value)["errorType"], "SSRF_BLOCKED")
        for value in ("http://user:password@example.com/", "ftp://example.com/", "http://example.com:0/", "http://example.com/%0a"):
            if "%0a" in value:
                continue  # Encoded path data cannot alter request headers.
            with self.assertRaises(network.TargetError):
                network.url_target(value)
        mixed = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        with patch.object(network.socket, "getaddrinfo", return_value=mixed):
            with self.assertRaises(network.TargetError):
                network.resolve_target("mixed.example")
        app = server.Application(self.root / "production", start_scheduler=False)
        try:
            self.assert_api_error(400, app.save_monitor, {"name": "private", "url": self.url, "method": "GET", "intervalSeconds": 30, "timeoutMs": 1000, "enabled": True})
        finally:
            app.close()

    def test_tcp_uses_pinned_dns_result_without_rebinding_lookup(self):
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", self.target.server_port))]
        with patch.object(network.socket, "getaddrinfo", return_value=records) as resolver:
            result = network.check_http("http://rebind.example:%d/ok" % self.target.server_port, allow_private=True)
            self.assertTrue(result["success"])
            self.assertEqual(resolver.call_count, 1)

    def test_redirect_revalidates_target_and_has_five_hop_limit(self):
        original = network.resolve_target
        def allow_fixture_only(host, port, unused_allow=False, deadline=None):
            return original(host, port, host == "127.0.0.1" and port == self.target.server_port, deadline)
        with patch.object(network, "resolve_target", side_effect=allow_fixture_only):
            result = network.check_http(self.url + "/metadata")
        self.assertEqual(result["errorType"], "SSRF_BLOCKED")
        self.assertEqual(network.check_http(self.url + "/loop", allow_private=True)["errorType"], "REDIRECT_LIMIT")

    def test_real_concurrency_repeated_monitor_and_global_cap(self):
        monitors = [self.monitor("/hold") for unused in range(5)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            work = [pool.submit(self.app.check_monitor, item["id"]) for item in monitors[:4]]
            deadline = time.monotonic() + 0.7
            while self.target.holding < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.target.holding, 4)
            self.assert_api_error(409, self.app.check_monitor, monitors[0]["id"])
            self.assert_api_error(429, self.app.check_monitor, monitors[4]["id"])
            self.target.release.set()
            for future in work:
                self.assertTrue(future.result(timeout=2)["success"])

    def test_history_persists_and_retention_and_paging_are_real(self):
        monitor = self.monitor()
        with patch.object(server, "MAX_HISTORY", 2):
            for unused in range(3):
                self.app.check_monitor(monitor["id"])
        history = self.app.history("monitors", monitor["id"], 2, 1)
        self.assertEqual((history["total"], len(history["records"])), (2, 1))
        self.app.close()
        self.app = server.Application(self.root / "data", allow_private=True, start_scheduler=False, clock=lambda: self.now, agent_dir=self.agent_dir)
        self.http.app = self.app
        self.assertEqual(self.app.history("monitors", monitor["id"], 1, 8)["total"], 2)
        self.assertEqual(self.app.monitors(monitor["id"])["lastStatus"], "UP")
        self.assertIsNone(self.app.init_admin())
        if os.name == "posix":
            self.assertEqual((self.root / "data").stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.root / "data/watch.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_scheduler_runs_due_enabled_monitors_not_fake_history(self):
        monitor = self.monitor()
        self.app.schedule_once()
        self.wait_history(monitor["id"], 1)
        self.app.schedule_once()
        self.assertEqual(self.app.history("monitors", monitor["id"], 1, 8)["total"], 1)
        self.now += 30
        self.app.schedule_once()
        self.wait_history(monitor["id"], 2)
        self.app.enable("monitors", monitor["id"], {"enabled": False})
        self.now += 60
        self.app.schedule_once()
        self.assertEqual(self.app.history("monitors", monitor["id"], 1, 8)["total"], 2)

    def test_input_limits_types_and_nonexistent_objects(self):
        for changes in ({"enabled": "yes"}, {"intervalSeconds": 29}, {"timeoutMs": 999}, {"method": "POST"}, {"name": ""}, {"name": "\ud800"}, {"extra": 1}):
            body = {"name": "test", "url": self.url, "method": "GET", "intervalSeconds": 30, "timeoutMs": 1000, "enabled": True}
            body.update(changes)
            self.assert_api_error(400, self.app.save_monitor, body)
        self.assert_api_error(404, self.app.monitors, 9999)
        self.assert_api_error(400, self.app.save_fleet, "hosts", {"name": "VPS", "region": "", "address": "127.0.0.1/path", "enabled": True, "nodeIds": []})
        self.assertEqual(self.request("GET", "/api/vps/1/checks?size=101")[0], 400)

    def test_enrollment_one_time_expiry_regeneration_and_sha_command(self):
        node = self.probe()
        host = self.host_item([node["id"]])
        first = self.app.enrollment("vps", host["id"], "https://watch.example.com")
        first_args = shlex.split(first["command"])
        first_token = first_args[first_args.index("--enrollment-token") + 1]
        second = self.app.enrollment("vps", host["id"], "https://watch.example.com")
        args = shlex.split(second["command"])
        token = args[args.index("--enrollment-token") + 1]
        self.assertIn("-fsS", args)
        self.assertNotIn("-fsSL", args)
        self.assertEqual(args[args.index("--proto") + 1], "=https")
        self.assertEqual(args[args.index("--agent-sha") + 1], hashlib.sha256((self.agent_dir / "agent.py").read_bytes()).hexdigest())
        self.assert_api_error(401, self.app.enroll, {"token": first_token})
        registered = self.app.enroll({"token": token})
        self.assertEqual(registered["role"], "vps")
        self.assert_api_error(401, self.app.enroll, {"token": token})
        third = self.app.enrollment("vps", host["id"], "https://watch.example.com")
        args = shlex.split(third["command"])
        self.now += 601
        self.assert_api_error(401, self.app.enroll, {"token": args[args.index("--enrollment-token") + 1]})
        self.assertNotIn(token, json.dumps(self.app.fleet()))
        self.assertNotIn(registered["credential"], json.dumps(self.app.fleet()))

    def test_agent_vps_tasks_cross_vps_binding_and_admin_separation(self):
        host, node, vps, probe, job = self.task()
        self.assertEqual(job["target"], "8.8.8.8")
        self.assertEqual((job["sent"], job["timeoutSeconds"]), (5, 8))
        self.assertEqual((job["taskVersion"], job["hostId"], job["nodeId"], job["protocol"], job["port"]), (2, host["id"], node["id"], "ICMP", None))
        other = self.register("vps", self.host_item([node["id"]])["id"])
        self.assertEqual(self.app.heartbeat(other["credential"], {"agentVersion": 2})["tasks"], [])
        self.assert_api_error(403, self.app.agent_result, other["credential"], self.result_body(job))
        self.assert_api_error(403, self.app.enrollment, "probe", node["id"], "https://watch.example.com")
        self.login()
        status, unused, unused_headers = self.request("PATCH", "/api/vps/%d/enabled" % host["id"], {"enabled": False}, admin=True,
                                                   headers={"Authorization": "Bearer " + probe["credential"]})
        self.assertEqual(status, 401)

    def test_job_retry_idempotency_and_no_overwrite(self):
        host, node, unused_vps, probe, job = self.task()
        repeated = self.app.heartbeat(probe["credential"], {"agentVersion": 2})["tasks"]
        self.assertEqual(repeated[0]["id"], job["id"])
        self.assertEqual(self.app.queue_host(host["id"])["count"], 0)
        body = self.result_body(job)
        self.assertFalse(self.app.agent_result(probe["credential"], body)["duplicate"])
        self.assertTrue(self.app.agent_result(probe["credential"], body)["duplicate"])
        self.assert_api_error(409, self.app.agent_result, probe["credential"], self.result_body(job, avgRttMs=999))
        self.assertEqual(self.app.history("vps", host["id"], 1, 8)["total"], 1)
        self.assertEqual(len(self.app.fleet()["results"]), 1)

    def test_task_expiry_and_offline_do_not_fabricate_failures(self):
        host, node, unused_vps, probe, job = self.task()
        self.now += 61
        self.assert_api_error(409, self.app.agent_result, probe["credential"], self.result_body(job))
        self.assertEqual(self.app.fleet()["results"], [])
        self.now += 31
        self.assertEqual(self.app.fleet_items("hosts", host["id"])["agentState"], "OFFLINE")
        self.assert_api_error(409, self.app.queue_host, host["id"])
        self.app.schedule_once()
        self.assertEqual(self.app.history("vps", host["id"], 1, 8)["records"], [])

    def test_offline_or_pending_vps_cannot_measure_until_v2_heartbeat(self):
        node = self.probe()
        host = self.host_item([node["id"]])
        self.assert_api_error(409, self.app.queue_host, host["id"])
        vps = self.register("vps", host["id"])
        self.now += 91
        self.assertEqual(self.app.fleet_items("hosts", host["id"])["agentState"], "OFFLINE")
        self.assert_api_error(409, self.app.queue_host, host["id"])
        self.app.heartbeat(vps["credential"], {"agentVersion": 2})
        self.assertEqual(self.app.queue_host(host["id"])["count"], 1)

    def test_fleet_timer_only_due_and_heartbeat_max_ten(self):
        nodes = [self.probe(name="target%d" % index) for index in range(12)]
        host = self.host_item([node["id"] for node in nodes])
        probe = self.register("vps", host["id"])
        self.app.schedule_once()
        self.assertEqual(self.app.heartbeat(probe["credential"], {"agentVersion": 2})["tasks"], [])
        self.now += 60
        self.app.schedule_once()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.app.lock:
                queued = self.app.db.execute("SELECT count(*) FROM jobs WHERE state='queued'").fetchone()[0]
            if queued == 12:
                break
            time.sleep(0.01)
        self.assertEqual(queued, 12)
        tasks = self.app.heartbeat(probe["credential"], {"agentVersion": 2})["tasks"]
        self.assertEqual(len(tasks), 10)
        self.assertEqual(len({task["id"] for task in tasks}), 10)
        self.assertEqual(self.app.fleet()["results"], [])

    def test_slow_fleet_queue_does_not_block_http_scheduler_and_queue_is_bounded(self):
        node = self.probe()
        for unused in range(80):
            host = self.host_item([node["id"]])
            self.register("vps", host["id"])
        monitor = self.monitor()
        self.now += 60
        release = threading.Event()
        started = threading.Event()
        original = self.app.queue_host
        def slow_fleet(host, *args, **kwargs):
            started.set()
            release.wait(3)
            return original(host, *args, **kwargs)
        try:
            with patch.object(self.app, "queue_host", slow_fleet):
                start = time.monotonic()
                self.app.schedule_once()
                self.app.schedule_once()
                self.assertLess(time.monotonic() - start, 0.5)
                self.assertTrue(started.wait(1))
                with self.app.fleet_lock:
                    self.assertEqual(len(self.app.fleet_running), 50)
                self.wait_history(monitor["id"], 1)
                self.assertFalse(release.is_set())
                release.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    with self.app.fleet_lock:
                        count = len(self.app.fleet_running)
                    if count == 0:
                        break
                    time.sleep(0.01)
                self.assertEqual(count, 0)
        finally:
            release.set()

    def test_result_actual_zero_errors_timeouts_and_validation(self):
        host, node, unused_vps, probe, job = self.task()
        for changes in ({"sent": True}, {"sent": 6}, {"received": 6}, {"avgRttMs": float("inf")}, {"avgRttMs": 10 ** 400},
                        {"status": "TIMEOUT", "sent": 0, "received": 0, "avgRttMs": None}, {"status": "ERROR", "avgRttMs": 1}):
            self.assert_api_error(400, self.app.agent_result, probe["credential"], self.result_body(job, **changes))
        self.app.agent_result(probe["credential"], self.result_body(job, status="ERROR", sent=0, received=0, avgRttMs=None, error="ping unavailable"))
        saved = self.app.history("vps", host["id"], 1, 8)["records"][0]
        self.assertEqual((saved["sent"], saved["received"], saved["avgRttMs"], saved["status"]), (0, 0, None, "ERROR"))
        self.app.queue_host(host["id"])
        timeout_job = self.app.heartbeat(probe["credential"], {"agentVersion": 2})["tasks"][0]
        self.app.agent_result(probe["credential"], self.result_body(timeout_job, status="TIMEOUT", received=0, avgRttMs=None))
        self.assertEqual(self.app.history("vps", host["id"], 1, 8)["total"], 2)

    def test_reenrollment_revokes_old_credential_and_lease(self):
        host, node, unused_vps, probe, job = self.task()
        fresh = self.register("vps", host["id"])
        self.assert_api_error(401, self.app.heartbeat, probe["credential"], {})
        self.assert_api_error(401, self.app.agent_result, probe["credential"], self.result_body(job))
        self.assert_api_error(403, self.app.agent_result, fresh["credential"], self.result_body(job))
        self.assertEqual(self.app.heartbeat(fresh["credential"], {"agentVersion": 2})["tasks"], [])

    def test_target_delete_and_host_display_address_change_keep_snapshot_history(self):
        host, node, unused_vps, probe, job = self.task()
        self.app.agent_result(probe["credential"], self.result_body(job))
        self.app.remove("nodes", node["id"])
        self.assertEqual(self.app.fleet_items("hosts", host["id"])["nodeIds"], [])
        saved = self.app.history("vps", host["id"], 1, 8)["records"][0]
        self.assertEqual(saved["nodeName"], node["name"])
        self.assertEqual(self.app.heartbeat(probe["credential"], {"agentVersion": 2})["tasks"], [])
        self.app.save_fleet("hosts", {"name": "VPS", "region": "", "enabled": True, "address": "1.1.1.1", "nodeIds": []}, host["id"])
        self.assertEqual(self.app.history("vps", host["id"], 1, 8)["total"], 1)

    def test_enrollment_http_production_rejected_dev_and_assets_work(self):
        self.login()
        node = self.probe()
        host = self.host_item([node["id"]])
        self.app.allow_private = False
        status, unused, unused_headers = self.request("POST", "/api/vps/%d/enrollment" % host["id"], admin=True)
        self.assertEqual(status, 403)
        self.app.allow_private = True
        status, content, unused = self.request("POST", "/api/vps/%d/enrollment" % host["id"], admin=True)
        self.assertEqual(status, 200)
        self.assertIn("--development", content["data"]["command"])
        source = self.request("GET", "/agent/agent.py")[1]
        checksum = self.request("GET", "/agent/agent.py.sha256")[1]
        self.assertEqual(checksum.decode().split()[0], hashlib.sha256(source).hexdigest())
        self.assertEqual(self.request("GET", "/agent/agent.py?sha=" + "0" * 64)[0], 409)

    def test_target_config_accepts_tcp_domain_and_ipv6_but_rejects_invalid_ports(self):
        target = self.probe(address="hb-ct-v4.ip.zstaticcdn.com:80", protocol="TCP")
        self.assertEqual((target["address"], target["protocol"], target["port"]), ("hb-ct-v4.ip.zstaticcdn.com", "TCP", 80))
        self.assertFalse(target["needsConfiguration"])
        self.assertNotIn("agentState", target)
        self.assertNotIn("lastSeenAt", target)
        ipv6 = self.probe(address="[2001:4860:4860::8888]:443", protocol="TCP")
        self.assertEqual((ipv6["address"], ipv6["port"]), ("2001:4860:4860::8888", 443))
        for changes in ({"protocol": "UDP"}, {"protocol": "TCP", "port": None}, {"port": 80},
                        {"protocol": "TCP", "port": True}, {"protocol": "TCP", "port": 0},
                        {"protocol": "TCP", "port": 65536}, {"protocol": "TCP", "address": "example.com:80", "port": 443},
                        {"protocol": "TCP", "address": "example.com:1", "port": True},
                        {"protocol": "TCP", "address": "example.com:80", "port": 80.0},
                        {"address": "https://example.com"}, {"address": "user@example.com"}):
            body = {"name": "target", "region": "", "enabled": True, "address": "8.8.8.8", "protocol": "ICMP", "port": None}
            body.update(changes)
            self.assert_api_error(400, self.app.save_fleet, "nodes", body)
        self.app.allow_private = False
        for address in ("127.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "::ffff:127.0.0.1"):
            self.assert_api_error(400, self.app.save_fleet, "nodes", {"name": "target", "region": "", "enabled": True,
                                                                      "address": address, "protocol": "ICMP", "port": None})
        with patch.object(server, "resolve_target", side_effect=AssertionError("metadata/target DNS must not run on control server")):
            target = self.probe(address="unresolvable.example", protocol="TCP", port=80)
            host = self.app.save_fleet("hosts", {"name": "private metadata", "region": "", "address": "192.168.1.8", "enabled": True, "nodeIds": [target["id"]]})
            vps = self.register("vps", host["id"])
            self.assertEqual(self.app.queue_host(host["id"])["count"], 1)
            self.assertEqual(self.app.heartbeat(vps["credential"], {"agentVersion": 2})["tasks"][0]["target"], "unresolvable.example")

    def test_tcp_result_snapshot_edit_cancels_jobs_and_filters_latest(self):
        target = self.probe(address="hb-ct-v4.ip.zstaticcdn.com", protocol="TCP", port=80)
        host = self.host_item([target["id"]])
        vps = self.register("vps", host["id"])
        self.app.queue_host(host["id"])
        job = self.app.heartbeat(vps["credential"], {"agentVersion": 2})["tasks"][0]
        self.assertEqual((job["target"], job["protocol"], job["port"]), (target["address"], "TCP", 80))
        self.app.agent_result(vps["credential"], self.result_body(job))
        first = self.app.fleet()["results"][0]
        self.assertEqual((first["direction"], first["protocol"], first["targetAddress"], first["targetPort"]), ("VPS_TO_TARGET", "TCP", target["address"], 80))
        self.app.queue_host(host["id"])
        old_job = self.app.heartbeat(vps["credential"], {"agentVersion": 2})["tasks"][0]
        self.app.save_fleet("nodes", {"name": "renamed", "region": "new", "address": "1.1.1.1", "protocol": "ICMP", "port": None, "enabled": True}, target["id"])
        self.assert_api_error(409, self.app.agent_result, vps["credential"], self.result_body(old_job))
        self.assertEqual(self.app.fleet()["results"], [])
        snapshot = self.app.history("vps", host["id"], 1, 8)["records"][0]
        self.assertEqual(snapshot, first)
        self.app.queue_host(host["id"])
        new_job = self.app.heartbeat(vps["credential"], {"agentVersion": 2})["tasks"][0]
        self.assertEqual((new_job["target"], new_job["protocol"], new_job["port"]), ("1.1.1.1", "ICMP", None))
        self.app.agent_result(vps["credential"], self.result_body(new_job, status="TIMEOUT", received=0, avgRttMs=None))
        self.assertEqual(self.app.fleet()["results"][0]["nodeName"], "renamed")
        self.assertEqual(self.app.history("vps", host["id"], 1, 8)["total"], 2)

    def test_legacy_heartbeat_requires_upgrade_and_never_receives_tasks(self):
        host, target, vps, unused, job = self.task()
        old = self.app.heartbeat(vps["credential"], {})
        self.assertEqual(old["tasks"], [])
        self.assertTrue(old["agentUpdateRequired"])
        self.assertEqual(old["requiredAgentVersion"], 2)
        model = self.app.fleet_items("hosts", host["id"])
        self.assertEqual((model["agentVersion"], model["agentState"]), (1, "ONLINE"))
        self.assertTrue(model["agentUpdateRequired"])
        self.assert_api_error(409, self.app.queue_host, host["id"])
        self.assert_api_error(409, self.app.agent_result, vps["credential"], self.result_body(job))
        for body in ({"agentVersion": True}, {"agentVersion": 3}, {"agentVersion": None}, {"extra": 2}):
            self.assert_api_error(400, self.app.heartbeat, vps["credential"], body)
        self.assertFalse(self.app.heartbeat(vps["credential"], {"agentVersion": 2})["agentUpdateRequired"])
        self.assertEqual(self.app.queue_host(host["id"])["count"], 1)
        self.assertEqual(self.request("GET", "/api/health")[1]["data"]["dataSchema"], 2)

    def test_disable_or_remove_association_cancels_leased_jobs(self):
        host, target, vps, unused, job = self.task()
        self.app.enable("nodes", target["id"], {"enabled": False})
        self.assert_api_error(409, self.app.agent_result, vps["credential"], self.result_body(job))
        self.assert_api_error(409, self.app.queue_host, host["id"])
        self.app.enable("nodes", target["id"], {"enabled": True})
        self.app.queue_host(host["id"])
        other_job = self.app.heartbeat(vps["credential"], {"agentVersion": 2})["tasks"][0]
        self.app.save_fleet("hosts", {"name": host["name"], "address": host["address"], "region": "", "enabled": True, "nodeIds": []}, host["id"])
        self.assert_api_error(409, self.app.agent_result, vps["credential"], self.result_body(other_job))
        self.assertEqual(self.app.history("vps", host["id"], 1, 8)["total"], 0)

    def test_target_has_no_enrollment_and_vps_upgrade_command_has_no_token(self):
        self.login()
        target = self.probe()
        self.assertEqual(self.request("POST", "/api/probes/%d/enrollment" % target["id"], {}, admin=True)[0], 403)
        host = self.host_item([target["id"]])
        data = self.request("POST", "/api/vps/%d/enrollment" % host["id"], {}, admin=True)[1]["data"]
        upgrade = shlex.split(data["upgradeCommand"])
        self.assertIn("--upgrade", upgrade)
        self.assertNotIn("--enrollment-token", upgrade)
        self.assertIn("--agent-sha", upgrade)
        self.assertIn("--development", upgrade)
        self.assertNotEqual(data["upgradeCommand"], data["command"])


class TLSCase(unittest.TestCase):
    def test_real_tls_verification_and_sni(self):
        executable = shutil.which("openssl")
        if executable is None and os.name == "nt":
            for path in (r"C:\Program Files\Git\usr\bin\openssl.exe", r"C:\Program Files\Git\mingw64\bin\openssl.exe"):
                if Path(path).is_file():
                    executable = path
                    break
        if executable is None:
            self.skipTest("OpenSSL fixture generation unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            cert, key = Path(temporary) / "cert.pem", Path(temporary) / "key.pem"
            subprocess.run([executable, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost", "-keyout", str(key), "-out", str(cert)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            target = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LocalTarget)
            target.daemon_threads = True
            target.methods = []
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(cert), str(key))
            names = []
            context.set_servername_callback(lambda unused_socket, name, unused_context: names.append(name))
            target.socket = context.wrap_socket(target.socket, server_side=True)
            thread = threading.Thread(target=lambda: target.serve_forever(poll_interval=0.02), daemon=True)
            thread.start()
            try:
                address = "https://localhost:%d/ok" % target.server_port
                self.assertEqual(network.check_http(address, allow_private=True)["errorType"], "SSL_ERROR")
                original = ssl.create_default_context
                with warnings.catch_warnings(record=True) as observed:
                    warnings.simplefilter("always", ResourceWarning)
                    with patch.object(network.ssl, "create_default_context", side_effect=lambda: original(cafile=str(cert))):
                        self.assertTrue(network.check_http(address, allow_private=True)["success"])
                        self.assertEqual(network.check_http("https://127.0.0.1:%d/ok" % target.server_port, allow_private=True)["errorType"], "SSL_ERROR")
                    # The availability check intentionally leaves the response
                    # body unread. Closing both HTTPResponse and HTTPConnection
                    # must still release the TLS descriptor before collection.
                    gc.collect()
                    self.assertEqual([warning for warning in observed if issubclass(warning.category, ResourceWarning)], [])
                self.assertIn("localhost", names)
            finally:
                target.shutdown()
                target.server_close()


if __name__ == "__main__":
    unittest.main()
