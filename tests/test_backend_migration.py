"""Real SQLite v0.3 fixture migrations; no network or deployment mutations."""
import os
import contextlib
import http.client
import json
from pathlib import Path
import sqlite3
import stat
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import server


@contextlib.contextmanager
def connection(path, **kwargs):
    with contextlib.closing(sqlite3.connect(str(path), **kwargs)) as db:
        with db:
            yield db


# Frozen v0.3 structure, intentionally independent of the current schema code.
LEGACY = """
CREATE TABLE admin(username TEXT PRIMARY KEY,password_hash TEXT NOT NULL);
CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE monitors(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,url TEXT NOT NULL,method TEXT NOT NULL,interval_seconds INTEGER NOT NULL,timeout_ms INTEGER NOT NULL,enabled INTEGER NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,revision INTEGER NOT NULL DEFAULT 1,next_due REAL NOT NULL);
CREATE TABLE monitor_checks(id INTEGER PRIMARY KEY AUTOINCREMENT,monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,success INTEGER NOT NULL,http_code INTEGER,response_ms REAL NOT NULL,error_type TEXT,error_message TEXT,checked REAL NOT NULL);
CREATE TABLE hosts(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,address TEXT NOT NULL,region TEXT NOT NULL,enabled INTEGER NOT NULL,credential_hash TEXT UNIQUE,last_seen REAL,revision INTEGER NOT NULL DEFAULT 1,next_due REAL NOT NULL);
CREATE TABLE nodes(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,region TEXT NOT NULL,enabled INTEGER NOT NULL,credential_hash TEXT UNIQUE,last_seen REAL);
CREATE TABLE associations(host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,PRIMARY KEY(host_id,node_id));
CREATE TABLE enrollments(token_hash TEXT PRIMARY KEY,role TEXT NOT NULL,object_id INTEGER NOT NULL,expires REAL NOT NULL,UNIQUE(role,object_id));
CREATE TABLE jobs(id TEXT PRIMARY KEY,host_id INTEGER NOT NULL,node_id INTEGER NOT NULL,target TEXT NOT NULL,host_revision INTEGER NOT NULL,node_name TEXT NOT NULL,region TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,state TEXT NOT NULL,credential_hash TEXT,result_json TEXT);
CREATE UNIQUE INDEX active_pair ON jobs(host_id,node_id) WHERE state IN ('queued','leased');
CREATE TABLE fleet_results(id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT UNIQUE NOT NULL,host_id INTEGER NOT NULL,node_id INTEGER NOT NULL,node_name TEXT NOT NULL,region TEXT NOT NULL,sent INTEGER NOT NULL,received INTEGER NOT NULL,avg_rtt REAL,status TEXT NOT NULL,error TEXT,checked REAL NOT NULL);
"""


class MigrationCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name) / "data"
        self.data.mkdir()
        self.database = self.data / "watch.sqlite3"
        self.now = 1700000000.0
        self.vps = "legacy-vps-credential-test-only"
        self.probe = "legacy-probe-credential-test-only"
        self.vps_token = "legacy-vps-enrollment-test-only"
        self.probe_token = "legacy-probe-enrollment-test-only"
        self.session = "legacy-admin-session-test-only"
        self.password = "legacy-private-fixture-password"
        with connection(self.database) as db:
            db.executescript(LEGACY)
            db.execute("INSERT INTO admin VALUES (?,?)", ("admin", server.password_hash(self.password)))
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (server.digest(self.session), "csrf-fixture", self.now + 3600))
            db.execute("INSERT INTO monitors VALUES (1,'site','https://example.com/path?private=fixture','GET',30,1000,1,?,?,1,?)", (self.now, self.now, self.now))
            db.execute("INSERT INTO monitor_checks VALUES (1,1,1,200,12.5,NULL,NULL,?)", (self.now,))
            db.execute("INSERT INTO hosts VALUES (1,'legacy VPS','8.8.8.8','old region',1,?,?,1,?)", (server.digest(self.vps), self.now, self.now))
            db.execute("INSERT INTO nodes VALUES (1,'legacy probe name','old probe region',1,?,?)", (server.digest(self.probe), self.now))
            db.execute("INSERT INTO associations VALUES (1,1)")
            db.execute("INSERT INTO enrollments VALUES (?,'vps',1,?)", (server.digest(self.vps_token), self.now + 600))
            db.execute("INSERT INTO enrollments VALUES (?,'probe',1,?)", (server.digest(self.probe_token), self.now + 600))
            db.execute("INSERT INTO jobs VALUES (?,1,1,'8.8.8.8',1,'legacy probe name','old probe region',?,?,'leased',?,NULL)", ("a" * 32, self.now, self.now + 60, server.digest(self.probe)))
            db.execute("INSERT INTO jobs VALUES (?,1,1,'8.8.8.8',1,'legacy probe name','old probe region',?,?,'completed',?,'{}')", ("b" * 32, self.now, self.now + 60, server.digest(self.probe)))
            db.execute("INSERT INTO fleet_results VALUES (1,?,1,1,'snapshot probe','snapshot region',5,5,24.2,'OK',NULL,?)", ("b" * 32, self.now))
        self.app = None

    def tearDown(self):
        if self.app:
            self.app.close()
        self.temp.cleanup()

    def open(self):
        self.app = server.Application(self.data, start_scheduler=False, clock=lambda: self.now)
        return self.app

    def backups(self):
        return list(self.data.glob("watch.schema1-before-v2.*.sqlite3"))

    def assert_legacy(self):
        with connection(self.database) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertNotIn("address", [row[1] for row in db.execute("PRAGMA table_info(nodes)")])
            self.assertEqual(db.execute("SELECT enabled,credential_hash FROM nodes").fetchone(), (1, server.digest(self.probe)))
            self.assertEqual(db.execute("SELECT state FROM jobs").fetchone()[0], "leased")
            self.assertEqual(db.execute("SELECT count(*) FROM enrollments").fetchone()[0], 2)

    def test_migration_preserves_private_state_revokes_probe_and_marks_legacy_history(self):
        app = self.open()
        self.assertEqual(app.db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertTrue(app.session(self.session)["authenticated"])
        self.assertTrue(app.login({"username": "admin", "password": self.password}, "127.0.0.1")[0]["authenticated"])
        self.assertEqual(app.history("monitors", 1, 1, 8)["records"][0]["responseTimeMs"], 12.5)
        host = app.fleet_items("hosts", 1)
        self.assertEqual(host["nodeIds"], [1])
        self.assertTrue(host["agentUpdateRequired"])
        node = app.fleet_items("nodes", 1)
        self.assertEqual((node["name"], node["address"], node["enabled"], node["needsConfiguration"]), ("legacy probe name", "", False, True))
        self.assertNotIn("agentState", node)
        self.assertEqual(app.fleet()["results"], [])
        history = app.history("vps", 1, 1, 8)["records"][0]
        self.assertEqual((history["direction"], history["protocol"], history["targetAddress"], history["targetPort"]), ("NODE_TO_VPS", "ICMP", "8.8.8.8", None))
        self.assertEqual(history["nodeName"], "snapshot probe")
        old = app.heartbeat(self.vps, {})
        self.assertEqual(old["tasks"], [])
        self.assertTrue(old["agentUpdateRequired"])
        for call, args in ((app.heartbeat, (self.probe, {})), (app.enroll, ({"token": self.probe_token},))):
            with self.assertRaises(server.APIError) as failure:
                call(*args)
            self.assertEqual(failure.exception.status, 401)
        with self.assertRaises(server.APIError) as failure:
            app.enable("nodes", 1, {"enabled": True})
        self.assertEqual(failure.exception.status, 409)
        self.assertEqual(app.db.execute("SELECT state FROM jobs").fetchone()[0], "cancelled")
        self.assertEqual(app.db.execute("SELECT credential_hash FROM hosts").fetchone()[0], server.digest(self.vps))
        self.assertEqual(app.db.execute("SELECT credential_hash FROM nodes").fetchone()[0], None)
        self.assertEqual(len(self.backups()), 1)
        backup = self.backups()[0]
        with connection(backup) as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT credential_hash FROM nodes").fetchone()[0], server.digest(self.probe))
            self.assertEqual(db.execute("SELECT state FROM jobs").fetchone()[0], "leased")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.data.stat().st_mode), 0o700)
        app.close()
        self.app = None
        self.open()
        self.assertEqual(len(self.backups()), 1)

    def test_partial_alter_failure_rolls_back_all_changes_and_keeps_backup(self):
        original = server._apply_schema2
        def failure(db):
            original(db)
            raise sqlite3.OperationalError("fixture: migration commit not reached")
        with patch.object(server, "_apply_schema2", failure):
            with self.assertRaises(sqlite3.OperationalError):
                self.open()
        self.assert_legacy()
        self.assertEqual(len(self.backups()), 1)
        self.open()
        self.assertEqual(len(self.backups()), 2)
        self.assertEqual(self.app.db.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_configuring_legacy_target_keeps_inverse_history_and_new_latest_separate(self):
        app = self.open()
        app.save_fleet("nodes", {"name": "new TCP target", "region": "new region", "enabled": True,
                                 "address": "latency.example.com:80", "protocol": "TCP", "port": None}, 1)
        self.assertFalse(app.fleet_items("nodes", 1)["needsConfiguration"])
        app.heartbeat(self.vps, {"agentVersion": 2})
        self.assertEqual(app.queue_host(1)["count"], 1)
        task = app.heartbeat(self.vps, {"agentVersion": 2})["tasks"][0]
        self.assertEqual((task["hostId"], task["target"], task["protocol"], task["port"]), (1, "latency.example.com", "TCP", 80))
        app.agent_result(self.vps, {"jobId": task["id"], "sent": 5, "received": 5, "avgRttMs": 17.0, "status": "OK"})
        self.assertEqual([row["direction"] for row in app.fleet()["results"]], ["VPS_TO_TARGET"])
        history = app.history("vps", 1, 1, 8)["records"]
        self.assertEqual([row["direction"] for row in history], ["VPS_TO_TARGET", "NODE_TO_VPS"])
        self.assertEqual((history[1]["nodeName"], history[1]["targetAddress"]), ("snapshot probe", "8.8.8.8"))
        self.assertEqual((history[0]["nodeName"], history[0]["protocol"], history[0]["targetPort"]), ("new TCP target", "TCP", 80))

    def test_explicit_schema1_migrates_once_and_backup_keeps_its_version(self):
        with connection(self.database) as db:
            db.execute("PRAGMA user_version=1")
        self.open()
        with connection(self.backups()[0]) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(self.app.db.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_legacy_history_missing_job_does_not_invent_address_from_current_metadata(self):
        with connection(self.database) as db:
            db.execute("DELETE FROM jobs WHERE id=?", ("b" * 32,))
            db.execute("UPDATE hosts SET address='new-address.example.com'")
        app = self.open()
        old = app.history("vps", 1, 1, 8)["records"][0]
        self.assertEqual(old["direction"], "NODE_TO_VPS")
        self.assertEqual(old["targetAddress"], "")

    def test_backup_connection_failure_leaves_legacy_and_no_partial_backup(self):
        original = sqlite3.connect
        calls = []
        def connect(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise OSError("fixture: cannot open backup source")
            return original(*args, **kwargs)
        with patch.object(server.sqlite3, "connect", connect):
            with self.assertRaises(OSError):
                self.open()
        self.assert_legacy()
        self.assertEqual(self.backups(), [])

    def test_backup_holds_writer_lock_and_captures_consistent_original(self):
        original = server._backup_database
        blocked = []
        def backup(database):
            with connection(database, timeout=0.02) as writer:
                try:
                    writer.execute("UPDATE hosts SET name='should never be visible'")
                except sqlite3.OperationalError:
                    blocked.append(True)
            return original(database)
        with patch.object(server, "_backup_database", backup):
            self.open()
        self.assertEqual(blocked, [True])
        with connection(self.backups()[0]) as db:
            self.assertEqual(db.execute("SELECT name FROM hosts").fetchone()[0], "legacy VPS")

    def test_newer_database_refused_without_backup_or_schema_mutation(self):
        with connection(self.database) as db:
            db.execute("PRAGMA user_version=3")
        with self.assertRaisesRegex(ValueError, "newer"):
            self.open()
        self.assertEqual(self.backups(), [])
        with connection(self.database) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertNotIn("address", [row[1] for row in db.execute("PRAGMA table_info(nodes)")])


class MaintenanceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        assets = self.root / "agent"
        assets.mkdir()
        (assets / "agent.py").write_text("# local test fixture\n", encoding="utf-8")
        (assets / "install.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.now = 1700000000.0
        self.app = server.Application(self.root / "data", allow_private=True, start_scheduler=False,
                                      agent_dir=assets, clock=lambda: self.now)
        self.password = self.app.init_admin()
        session, token = self.app.login({"username": "admin", "password": self.password}, "127.0.0.1")
        self.cookie = server.COOKIE + "=" + token
        self.csrf = session["csrfToken"]
        self.http = server.ControlServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=lambda: self.http.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()
        self.host = "localhost:" + str(self.http.server_port)
        self.marker = self.app.data_dir / ".activation-in-progress"
        self.target = self.app.save_fleet("nodes", {"name": "TCP fixture target", "region": "", "enabled": True,
                                                    "address": "8.8.8.8", "protocol": "TCP", "port": 80})
        self.host_item = self.app.save_fleet("hosts", {"name": "VPS fixture", "region": "", "address": "192.168.1.8",
                                                       "enabled": True, "nodeIds": [self.target["id"]]})
        self.vps = self.app.enroll({"token": self.enrollment_token()})
        self.app.heartbeat(self.vps["credential"], {"agentVersion": 2})

    def enrollment_token(self):
        args = shlex.split(self.app.enrollment("vps", self.host_item["id"], "http://" + self.host)["command"])
        return args[args.index("--enrollment-token") + 1]

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.app.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, credential=None):
        headers = {"Host": self.host, "Origin": "http://" + self.host, "Content-Type": "application/json",
                   "Cookie": self.cookie, "X-CSRF-Token": self.csrf}
        if credential:
            headers["Authorization"] = "Bearer " + credential
        client = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=5)
        try:
            client.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            response = client.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            client.close()

    def test_maintenance_returns_no_success_ack_for_auth_admin_or_agent_and_recovers(self):
        self.app.queue_host(self.host_item["id"])
        task = self.app.heartbeat(self.vps["credential"], {"agentVersion": 2})["tasks"][0]
        result = {"jobId": task["id"], "sent": 5, "received": 5, "avgRttMs": 12.0, "status": "OK"}
        token = self.enrollment_token()
        self.marker.write_text("", encoding="utf-8")
        self.now += 5
        attempts = (
            ("POST", "/api/auth/login", {"username": "admin", "password": self.password}, None),
            ("POST", "/api/auth/logout", {}, None),
            ("PUT", "/api/probes/%d" % self.target["id"], {"invalid": "must never execute"}, None),
            ("PATCH", "/api/vps/%d/enabled" % self.host_item["id"], {"enabled": False}, None),
            ("DELETE", "/api/probes/%d" % self.target["id"], {}, None),
            ("POST", "/api/agent/enroll", {"token": token}, None),
            ("POST", "/api/agent/heartbeat", {"agentVersion": 2}, self.vps["credential"]),
            ("POST", "/api/agent/results", result, self.vps["credential"]),
        )
        for method, path, body, credential in attempts:
            status, envelope, headers = self.request(method, path, body, credential)
            self.assertEqual((status, envelope["code"]), (503, 503))
            self.assertEqual(envelope["message"], "主控正在升级，请稍后重试")
            self.assertIsNone(envelope["data"])
            self.assertNotIn("Set-Cookie", headers)
        self.assertTrue(self.app.session(self.cookie.split("=", 1)[1])["authenticated"])
        self.assertTrue(self.app.fleet_items("hosts", self.host_item["id"])["enabled"])
        self.assertEqual(self.app.db.execute("SELECT last_seen FROM hosts WHERE id=?", (self.host_item["id"],)).fetchone()[0], self.now - 5)
        self.assertEqual(self.app.history("vps", self.host_item["id"], 1, 8)["total"], 0)
        self.assertEqual(self.app.db.execute("SELECT state FROM jobs WHERE id=?", (task["id"],)).fetchone()[0], "leased")
        for path in ("/api/health", "/api/fleet", "/api/monitors"):
            self.assertEqual(self.request("GET", path)[0], 200)
        self.marker.unlink()
        status, envelope, unused = self.request("POST", "/api/agent/results", result, self.vps["credential"])
        self.assertEqual((status, envelope["data"]["duplicate"]), (200, False))
        self.assertEqual(self.app.history("vps", self.host_item["id"], 1, 8)["total"], 1)
        self.assertEqual(self.request("PATCH", "/api/vps/%d/enabled" % self.host_item["id"], {"enabled": False})[0], 200)
        self.assertEqual(self.request("POST", "/api/agent/enroll", {"token": token})[0], 200)

    def test_maintenance_suppresses_scheduling_without_advancing_due_then_recovers(self):
        self.now += 60
        monitor = self.app.save_monitor({"name": "real local control health", "url": "http://127.0.0.1:%d/api/health" % self.http.server_port,
                                         "method": "GET", "intervalSeconds": 30, "timeoutMs": 1000, "enabled": True})
        self.marker.write_bytes(b"")
        before = self.app.db.execute("SELECT next_due FROM hosts WHERE id=?", (self.host_item["id"],)).fetchone()[0]
        self.app.schedule_once()
        self.assertEqual(self.app.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(self.app.history("monitors", monitor["id"], 1, 8)["total"], 0)
        self.assertEqual(self.app.db.execute("SELECT next_due FROM hosts WHERE id=?", (self.host_item["id"],)).fetchone()[0], before)
        self.marker.unlink()
        self.app.schedule_once()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with self.app.lock:
                queued = self.app.db.execute("SELECT count(*) FROM jobs").fetchone()[0]
                checked = self.app.db.execute("SELECT count(*) FROM monitor_checks WHERE monitor_id=?", (monitor["id"],)).fetchone()[0]
            if queued and checked:
                break
            time.sleep(0.01)
        self.assertEqual(queued, 1)
        self.assertEqual(checked, 1)
        self.assertTrue(self.app.history("monitors", monitor["id"], 1, 8)["records"][0]["success"])

    def test_init_admin_cli_still_initializes_while_marker_exists(self):
        data = self.root / "init-data"
        data.mkdir()
        (data / ".activation-in-progress").write_bytes(b"")
        process = subprocess.run([sys.executable, "-B", "-I", server.__file__, "--data-dir", str(data), "--init-admin"],
                                 capture_output=True, text=True, encoding="utf-8", timeout=10, check=True)
        self.assertIn("Username: admin", process.stdout)
        with connection(data / "watch.sqlite3") as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM admin").fetchone()[0], 1)
        self.assertTrue((data / ".activation-in-progress").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX symlink fixture")
    def test_broken_maintenance_symlink_blocks_writes_and_scheduler(self):
        self.marker.symlink_to(self.root / "missing-marker-target")
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.request("POST", "/api/auth/login", {"username": "admin", "password": self.password})[0], 503)
        self.app.schedule_once()
        self.assertEqual(self.app.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
