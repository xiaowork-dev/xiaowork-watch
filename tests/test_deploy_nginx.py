"""Offline Linux integration: actual Nginx renderer, local TLS, and ACME files.

Requires nginx and openssl; never contacts an ACME server or changes /etc.
Run as the ordinary CI user after installing the two executable dependencies.
"""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "console.py"
if not SOURCE.exists():
    SOURCE = HERE.parent / "scripts" / "deploy" / "console.py"
SPEC = importlib.util.spec_from_file_location("deploy_nginx_console", str(SOURCE))
console = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = console
SPEC.loader.exec_module(console)
RUNTIME_SPEC = importlib.util.spec_from_file_location("deploy_nginx_runtime", str(SOURCE.with_name("runtime.py")))
runtime = importlib.util.module_from_spec(RUNTIME_SPEC)
RUNTIME_SPEC.loader.exec_module(runtime)

NGINX = shutil.which("nginx")
if not NGINX and sys.platform.startswith("linux") and Path("/usr/sbin/nginx").is_file():
    NGINX = "/usr/sbin/nginx"
OPENSSL = shutil.which("openssl")
AVAILABLE = sys.platform.startswith("linux") and bool(NGINX and OPENSSL)
REASON = "Linux with nginx and openssl is required; no public ACME request is made"
DOMAIN = "watch.example.test"
NEW_DOMAIN = "new-watch.example.test"
SHA = "a" * 40


def nginx_quote(path):
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


class Manager:
    def __init__(self, root):
        self.root = root

    def _pointed_sha(self, name):
        return SHA if name == "current" else None


class Backend(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?", 1)[0] == "/release.json":
            value = self.server.release
        elif self.path.split("?", 1)[0] == "/api/health":
            self.server.health_requests += 1
            value = {"code": 0, "message": "", "data": {"release": self.server.health_release}}
        else:
            value = {"path": self.path, "host": self.headers.get("Host"),
                     "proto": self.headers.get("X-Forwarded-Proto"),
                     "realIp": self.headers.get("X-Real-IP"),
                     "forwardedFor": self.headers.get("X-Forwarded-For")}
        body = json.dumps(value).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class NginxFixture:
    def __init__(self, tls=True, migrating=False, live=False, public_alias=True):
        self.temporary = tempfile.TemporaryDirectory(prefix="xiaowork-nginx-")
        self.base = Path(self.temporary.name)
        self.root = self.base / "isolated site"
        self.root.mkdir()
        self.manager = Manager(self.root)
        self.process = None
        self.backend = None
        self.thread = None
        self.reservations = []
        try:
            # Permit worker traversal even when these fixtures are run as root.
            os.chmod(str(self.base), 0o755)
            os.chmod(str(self.root), 0o755)
            shared = self.root / "shared"
            self.folders = {key: shared / name for key, name in (
                ("certs", "letsencrypt"), ("work", "certbot-work"),
                ("logs", "certbot-logs"), ("acme", "acme"))}
            self.challenge = self.folders["acme"] / ".well-known" / "acme-challenge"
            self.challenge.mkdir(parents=True)
            for directory in (shared, self.folders["acme"], self.challenge.parent, self.challenge):
                os.chmod(str(directory), 0o755)
            self.token = "fixture-token"
            (self.challenge / self.token).write_text("offline-acme-token", encoding="ascii")
            archive = self.folders["certs"] / "archive" / console.TLS_NAME
            live = self.folders["certs"] / "live" / console.TLS_NAME
            archive.mkdir(parents=True)
            live.mkdir(parents=True)
            self.certificate = archive / "fullchain1.pem"
            self.key = archive / "privkey1.pem"
            openssl_config = self.base / "openssl.cnf"
            openssl_config.write_text(
                "[req]\nprompt=no\ndistinguished_name=subject\nx509_extensions=extensions\n"
                "[subject]\nCN=" + DOMAIN + "\n"
                "[extensions]\nsubjectAltName=DNS:" + DOMAIN + "\n"
                "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n"
                "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\nextendedKeyUsage=serverAuth\n",
                encoding="ascii")
            self.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                      "-config", str(openssl_config), "-keyout", str(self.key), "-out", str(self.certificate)])
            os.chmod(str(self.key), 0o600)
            # Certbot uses live -> archive symlinks; exercise that actual layout.
            for name in ("cert", "chain", "fullchain", "privkey"):
                target = archive / (name + "1.pem")
                if not target.exists():
                    shutil.copyfile(str(self.certificate), str(target))
                (live / (name + ".pem")).symlink_to(Path("../../archive") / console.TLS_NAME / target.name)
            self.context = ssl.create_default_context(cafile=str(self.certificate))
            self.backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
            self.backend.daemon_threads = True
            self.backend.release = {"schema": 1, "kind": "frontend-prototype", "commit": SHA}
            self.backend.health_release = SHA
            self.backend.health_requests = 0
            self.backend_port = self.backend.server_address[1]
            self.thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
            self.thread.start()
            self.http_port = self.reserve_port()
            self.https_port = self.reserve_port()
            inner = ""
            self.inner_port = self.reserve_port() if live else self.backend_port
            if live:
                self.frontend = self.root / "frontend"
                self.frontend.mkdir()
                self.release_path = self.frontend / "release.json"
                self.release_path.write_text(json.dumps({"schema": 1, "kind": "monitoring-server", "commit": SHA}), encoding="utf-8")
                (self.frontend / "index.html").write_text("offline frontend", encoding="ascii")
                base_site = ("server {\n    listen 127.0.0.1:" + str(self.inner_port) + ";\n"
                             "    server_name backend.example.test;\n    root " + nginx_quote(self.frontend) + ";\n"
                             "    location / { try_files $uri $uri/ /index.html; }\n}\n")
                self.inner_before_alias = runtime.nginx_site(base_site, self.backend_port)
                self.inner_rendered = (console._site_alias(self.inner_before_alias, DOMAIN)
                                       if public_alias else self.inner_before_alias)
                # An unrelated default vhost must win if the public Host alias
                # is missing. It shares only this fixture's temporary port.
                inner = ("server {\n    listen 127.0.0.1:" + str(self.inner_port) + " default_server;\n"
                         "    server_name foreign.example.test;\n    location / { return 421 'foreign-default'; }\n}\n"
                         + self.inner_rendered)
            config = {"healthUrl": "http://127.0.0.1:" + str(self.inner_port) + "/release.json",
                      "healthHost": "backend.example.test"}
            if live:
                config["backendEnabled"] = True
            rendered = console._tls_proxy(
                self.manager, NEW_DOMAIN if migrating else DOMAIN, config, self.folders,
                tls_domain=DOMAIN if tls else None,
                http_domains=DOMAIN + " " + NEW_DOMAIN if migrating else None)
            # Only listeners change for isolation; all locations/TLS/upstream are
            # the actual production renderer output, without a second template.
            rendered, http_count = re.subn(r"(?m)^(\s*)listen 80;$",
                                          r"\1listen 127.0.0.1:" + str(self.http_port) + ";", rendered)
            rendered, tls_count = re.subn(r"(?m)^(\s*)listen 443 ssl;$",
                                         r"\1listen 127.0.0.1:" + str(self.https_port) + " ssl;", rendered)
            if http_count != 1 or tls_count != int(tls):
                raise AssertionError("Renderer listeners changed; update isolated fixture explicitly")
            # Set every compiled/default state location inside the fixture.
            preamble = "worker_processes 1;\npid " + nginx_quote(self.base / "nginx.pid") + ";\n"
            if os.geteuid() == 0:
                import pwd
                preamble += "user " + pwd.getpwuid(os.geteuid()).pw_name + ";\n"
            preamble += "error_log " + nginx_quote(self.base / "error.log") + " info;\nevents { worker_connections 64; }\nhttp {\n"
            preamble += "access_log " + nginx_quote(self.base / "access.log") + ";\n"
            for directive in ("client_body_temp_path", "proxy_temp_path", "fastcgi_temp_path",
                              "uwsgi_temp_path", "scgi_temp_path"):
                folder = self.base / directive
                folder.mkdir()
                preamble += directive + " " + nginx_quote(folder) + ";\n"
            self.config_path = self.base / "nginx.conf"
            self.config_path.write_text(preamble + inner + rendered + "}\n", encoding="utf-8")
            self.command = [NGINX, "-p", str(self.base) + "/", "-c", str(self.config_path)]
            self.syntax_output = self.run(self.command + ["-t"])
            for reserved in self.reservations:
                reserved.close()
            self.reservations = []
            self.process = subprocess.Popen(self.command + ["-g", "daemon off;"],
                                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    output = self.process.communicate()[1].decode("utf-8", "replace")
                    raise AssertionError("Isolated nginx exited at startup: " + output)
                try:
                    with socket.create_connection(("127.0.0.1", self.http_port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                raise AssertionError("Isolated nginx did not listen within five seconds")
            if live:
                self.client_ip = self.client_address()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def run(arguments):
        result = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=15, universal_newlines=True)
        if result.returncode:
            raise AssertionError("Fixture command failed: " + " ".join(arguments)
                                 + "\n" + result.stdout + result.stderr)
        return result.stdout + result.stderr

    def reserve_port(self):
        connection = socket.socket()
        connection.bind(("127.0.0.1", 0))
        self.reservations.append(connection)
        return connection.getsockname()[1]

    def client_address(self):
        # Discover a local interface without DNS or a connection to the public
        # network. A second loopback address also exercises the untrusted path
        # on Linux runners with no non-loopback IPv4 interface.
        import fcntl
        import struct
        candidates = []
        with socket.socket() as connection:
            for _, interface in socket.if_nameindex():
                try:
                    data = fcntl.ioctl(connection.fileno(), 0x8915,
                                       struct.pack("256s", interface.encode("ascii")[:15]))
                    address = socket.inet_ntoa(data[20:24])
                    if not address.startswith("127."):
                        candidates.append(address)
                except (OSError, UnicodeError):
                    pass
        for address in candidates + ["127.0.0.2"]:
            try:
                with socket.create_connection(("127.0.0.1", self.http_port), timeout=0.5,
                                              source_address=(address, 0)):
                    return address
            except OSError:
                pass
        raise AssertionError("Cannot bind a distinct local client address")

    def request(self, path, tls=False, host=DOMAIN, port=None, headers=None, source_address=None):
        if tls:
            class SourceHTTPS(console._LoopbackHTTPS):
                def connect(self):
                    raw = socket.create_connection(("127.0.0.1", self.port), self.timeout,
                                                   source_address=source_address)
                    try:
                        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                    except BaseException:
                        raw.close()
                        raise
            connection = SourceHTTPS(host, port or self.https_port, timeout=3, context=self.context)
        else:
            connection = http.client.HTTPConnection("127.0.0.1", port or self.http_port, timeout=3,
                                                   source_address=source_address)
        try:
            request_headers = {"Host": host}
            request_headers.update(headers or {})
            connection.request("GET", path, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def close(self):
        for reserved in self.reservations:
            reserved.close()
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.communicate(timeout=5)
        if self.backend is not None:
            if self.thread is not None and self.thread.is_alive():
                self.backend.shutdown()
            self.backend.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3)
        self.temporary.cleanup()


@unittest.skipUnless(AVAILABLE, REASON)
class NginxHTTPSIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = NginxFixture(tls=True)
        cls.addClassCleanup(cls.fixture.close)

    def test_real_nginx_syntax_accepts_renderer_and_certbot_symlinks(self):
        self.assertIn("test is successful", self.fixture.syntax_output)
        console._certificate_target(self.fixture.folders, "fullchain.pem")
        console._certificate_target(self.fixture.folders, "privkey.pem")

    def test_certificate_preflight_rejects_wrong_hostname_on_real_openssl(self):
        console._validate_certificates(self.fixture.folders, DOMAIN)
        with self.assertRaises(console.ConsoleError):
            console._validate_certificates(self.fixture.folders, NEW_DOMAIN)

    def test_http_challenge_is_served_and_missing_token_is_404(self):
        status, headers, body = self.fixture.request("/.well-known/acme-challenge/" + self.fixture.token)
        self.assertEqual((status, body), (200, b"offline-acme-token"))
        self.assertEqual(headers["Content-Type"], "text/plain")
        self.assertNotIn("Location", headers)
        self.assertEqual(self.fixture.request("/.well-known/acme-challenge/missing")[0], 404)

    def test_http_redirect_preserves_path_query_and_uses_configured_domain(self):
        path = "/sites/alpha?sort=name&next=%2F"
        for host in (DOMAIN, "untrusted.example.test"):
            status, headers, _ = self.fixture.request(path, host=host)
            self.assertEqual(status, 301)
            self.assertEqual(headers["Location"], "https://" + DOMAIN + path)

    def test_trusted_tls_hostname_and_proxy_headers(self):
        status, _, raw = self.fixture.request("/probe?view=all", tls=True)
        self.assertEqual(status, 200)
        value = json.loads(raw)
        self.assertEqual(value["path"], "/probe?view=all")
        self.assertEqual(value["host"], "backend.example.test")
        self.assertEqual(value["proto"], "https")
        self.assertEqual(value["realIp"], "127.0.0.1")
        self.assertEqual(value["forwardedFor"], "127.0.0.1")
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.fixture.request("/release.json", tls=True, host=NEW_DOMAIN)

    def test_real_tls_health_checks_current_release(self):
        with patch.object(console, "HTTPS_PORT", self.fixture.https_port), \
                patch.object(console.ssl, "create_default_context", return_value=self.fixture.context):
            console._https_health(self.fixture.manager, DOMAIN)
            previous = self.fixture.backend.release
            try:
                self.fixture.backend.release = dict(previous, commit="b" * 40)
                with self.assertRaises(console.ConsoleError):
                    console._https_health(self.fixture.manager, DOMAIN)
            finally:
                self.fixture.backend.release = previous

    def test_original_http_backend_health_remains_direct(self):
        status, headers, body = self.fixture.request("/release.json", port=self.fixture.backend_port)
        self.assertEqual(status, 200)
        self.assertNotIn("Location", headers)
        self.assertEqual(json.loads(body)["commit"], SHA)


@unittest.skipUnless(AVAILABLE, REASON)
class NginxBootstrapIntegrationTests(unittest.TestCase):
    def test_initial_http_bootstrap_keeps_site_available_and_serves_challenge(self):
        fixture = NginxFixture(tls=False)
        try:
            self.assertIn("test is successful", fixture.syntax_output)
            self.assertEqual(fixture.request("/.well-known/acme-challenge/" + fixture.token)[2], b"offline-acme-token")
            status, headers, body = fixture.request("/release.json")
            self.assertEqual(status, 200)
            self.assertNotIn("Location", headers)
            self.assertEqual(json.loads(body)["commit"], SHA)
        finally:
            fixture.close()

    def test_domain_migration_bootstrap_keeps_prior_tls_until_new_certificate(self):
        fixture = NginxFixture(tls=True, migrating=True)
        try:
            status, _, body = fixture.request("/.well-known/acme-challenge/" + fixture.token, host=NEW_DOMAIN)
            self.assertEqual((status, body), (200, b"offline-acme-token"))
            status, headers, _ = fixture.request("/sites?filter=up", host=NEW_DOMAIN)
            self.assertEqual(status, 301)
            self.assertEqual(headers["Location"], "https://" + DOMAIN + "/sites?filter=up")
            self.assertEqual(fixture.request("/release.json", tls=True)[0], 200)
        finally:
            fixture.close()


@unittest.skipUnless(AVAILABLE, REASON)
class NginxLiveIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = NginxFixture(live=True)
        cls.addClassCleanup(cls.fixture.close)

    def test_two_layer_tls_preserves_public_host_scheme_and_actual_client(self):
        fixture = self.fixture
        public_host = DOMAIN + ":" + str(fixture.https_port)
        status, _, body = fixture.request("/api/echo?view=all", tls=True,
            headers={"Host": public_host, "X-Real-IP": "203.0.113.77", "X-Forwarded-Proto": "http"},
            source_address=(fixture.client_ip, 0))
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertEqual(value["path"], "/api/echo?view=all")
        self.assertEqual(value["host"], public_host)
        self.assertEqual(value["proto"], "https")
        self.assertEqual(value["realIp"], fixture.client_ip)
        self.assertEqual(value["forwardedFor"], fixture.client_ip + ", 127.0.0.1")
        self.assertIn("server_name backend.example.test;", fixture.inner_before_alias)
        self.assertIn("server_name backend.example.test " + DOMAIN + ";", fixture.inner_rendered)
        self.assertEqual(fixture.request("/api/echo", host="foreign.example.test", port=fixture.inner_port)[0], 421)

    def test_inner_http_only_trusts_forwarded_headers_from_proxy_loopback(self):
        fixture = self.fixture
        headers = {"X-Forwarded-Proto": "https", "X-Real-IP": "203.0.113.77"}
        # A direct client with any source other than the trusted proxy addresses
        # cannot turn its HTTP connection into HTTPS or replace its own IP.
        status, _, body = fixture.request("/api/echo", port=fixture.inner_port, headers=headers,
                                          source_address=(fixture.client_ip, 0))
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertEqual(value["proto"], "http")
        self.assertEqual(value["realIp"], fixture.client_ip)
        # The local outer proxy is the explicitly trusted case.
        status, _, body = fixture.request("/api/echo", port=fixture.inner_port, headers=headers,
                                          source_address=("127.0.0.1", 0))
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertEqual(value["proto"], "https")
        self.assertEqual(value["realIp"], "203.0.113.77")

    def test_real_tls_health_requires_frontend_and_backend_current_release(self):
        fixture = self.fixture
        with patch.object(console, "HTTPS_PORT", fixture.https_port), \
                patch.object(console.ssl, "create_default_context", return_value=fixture.context):
            before = fixture.backend.health_requests
            console._https_health(fixture.manager, DOMAIN)
            self.assertGreater(fixture.backend.health_requests, before)
            try:
                fixture.backend.health_release = "b" * 40
                with self.assertRaises(console.ConsoleError):
                    console._https_health(fixture.manager, DOMAIN)
            finally:
                fixture.backend.health_release = SHA
            previous = fixture.release_path.read_text(encoding="utf-8")
            try:
                fixture.release_path.write_text(json.dumps({"schema": 1, "kind": "monitoring-server", "commit": "b" * 40}), encoding="utf-8")
                before = fixture.backend.health_requests
                with self.assertRaises(console.ConsoleError):
                    console._https_health(fixture.manager, DOMAIN)
                self.assertEqual(fixture.backend.health_requests, before)
            finally:
                fixture.release_path.write_text(previous, encoding="utf-8")

    def test_missing_public_alias_reaches_foreign_default_and_fails_tls_health(self):
        fixture = NginxFixture(live=True, public_alias=False)
        try:
            self.assertEqual(fixture.request("/api/echo", tls=True)[0], 421)
            # The original installation health Host still selects our site.
            status, _, body = fixture.request("/release.json", host="backend.example.test", port=fixture.inner_port)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["kind"], "monitoring-server")
            with patch.object(console, "HTTPS_PORT", fixture.https_port), \
                    patch.object(console.ssl, "create_default_context", return_value=fixture.context):
                with self.assertRaises(console.ConsoleError):
                    console._https_health(fixture.manager, DOMAIN)
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
