"""Offline installer transactions; all Linux system commands point into a fixture."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_agent import CREDENTIAL, SOURCE, TOKEN, http_fixture, success

INSTALLER = SOURCE.with_name("install.sh")
FAKE_TOOLS = r'''#!/usr/bin/python3
import hashlib,json,os,pathlib,shutil,sys,tempfile
root=pathlib.Path(os.environ["AGENT_TEST_ROOT"])
state_file=root/"state.json"
state=json.loads(state_file.read_text())
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
state["calls"].append([name]+args)
code=0
if name=="getent":
    if args[0]=="passwd" and state["user"]:
        print("xiaowork-watch-agent:x:999:999:xiaowork Watch outbound agent:/nonexistent:/usr/sbin/nologin")
    elif args[0]=="group" and state["group"]:
        print("xiaowork-watch-agent:x:999:")
    else: code=2
elif name=="groupadd": state["group"]=True
elif name=="useradd": state["user"]=True
elif name=="groupdel": state["group"]=False
elif name=="userdel": state["user"]=False
elif name=="id": print("999")
elif name=="curl":
    target=pathlib.Path(args[args.index("-o")+1])
    url=args[args.index("-o")-1]
    source=pathlib.Path(os.environ["AGENT_TEST_SOURCE"])
    if url.endswith(".sha256"):
        value=hashlib.sha256(source.read_bytes()).hexdigest()
        if os.environ.get("AGENT_TEST_BAD_SHA"): value="f"*64
        target.write_text(value+"  agent.py\n")
    else: target.write_bytes(source.read_bytes())
elif name=="mktemp":
    print(tempfile.mkdtemp(prefix="agent-",dir=str(root/"work")))
elif name=="install":
    shutil.copyfile(args[-2],args[-1])
    os.chmod(args[-1],int(args[args.index("-m")+1],8))
elif name=="stat":
    if args[1]=="%a": print(oct(pathlib.Path(args[-1]).stat().st_mode & 0o7777)[2:])
    else: print("0")  # Owned root metadata in the isolated fixture.
elif name=="systemctl":
    if args[:2]==["enable","--now"]:
        if os.environ.get("AGENT_TEST_FAIL_START"): code=1
        else: state["active"]=True
    elif args[:2]==["disable","--now"]: state["active"]=False
    elif args[:1]==["restart"]:
        state["restarts"]=state.get("restarts",0)+1
        if os.environ.get("AGENT_TEST_FAIL_UPGRADE") and state["restarts"]==1:
            code=1
            state["active"]=False
        elif os.environ.get("AGENT_TEST_UPGRADE_INACTIVE") and state["restarts"]==1:
            state["active"]=False
        else: state["active"]=True
    elif args[:1]==["stop"]: state["active"]=False
    elif args[:2]==["is-active","--quiet"]: code=0 if state["active"] else 1
elif name in ("chown","apt-get"): pass
else: raise RuntimeError("Unexpected fixture command: "+name)
state_file.write_text(json.dumps(state))
sys.exit(code)
'''


class InstallerSourceTests(unittest.TestCase):
    def test_install_script_has_lf_and_no_exposed_configuration(self):
        raw = INSTALLER.read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertTrue(raw.startswith(b"#!/usr/bin/env bash\n"))
        unit = raw.decode().split("cat <<'UNIT_EOF'\n", 1)[1].split("\nUNIT_EOF", 1)[0]
        self.assertIn("User=xiaowork-watch-agent", unit)
        self.assertIn("AmbientCapabilities=CAP_NET_RAW", unit)
        self.assertIn("CapabilityBoundingSet=CAP_NET_RAW", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertNotIn("token", unit)
        self.assertNotIn("credential", unit)


@unittest.skipUnless(sys.platform == "linux", "Installer transaction fixtures require Linux Bash")
class InstallerTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="watch-agent-installer-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        (self.base / "work").mkdir()
        self.app, self.config = self.base / "opt" / "xiaowork-watch-agent", self.base / "etc" / "xiaowork-watch-agent"
        self.unit = self.base / "units" / "xiaowork-watch-agent.service"
        for directory in (self.app.parent, self.config.parent, self.unit.parent, self.base / "running-systemd"):
            directory.mkdir()
        (self.base / "os-release").write_text("ID=debian\n")
        self.state_path = self.base / "state.json"
        self.state_path.write_text(json.dumps({"user": False, "group": False, "active": False, "calls": []}))
        script = INSTALLER.read_text(encoding="utf-8")
        # Only the test clone is rewritten. Every mutating path and command is
        # inside this one TemporaryDirectory; no real apt/systemd/users run.
        root_guard = "[[ $EUID == 0 ]] || die 'Run this installer with sudo or as root.'"
        self.assertIn(root_guard, script)
        script = script.replace(root_guard, ": # fixture permits the CI user")
        for original, replacement in (("/opt/xiaowork-watch-agent", self.app), ("/etc/xiaowork-watch-agent", self.config),
                                       ("/etc/systemd/system/xiaowork-watch-agent.service", self.unit),
                                       ("/run/systemd/system", self.base / "running-systemd"),
                                       ("/etc/os-release", self.base / "os-release")):
            script = script.replace(original, str(replacement))
        self.script = self.base / "install.sh"
        self.script.write_text(script, encoding="utf-8")
        fake = self.bin / "fixture-tool"
        fake.write_text(FAKE_TOOLS, encoding="utf-8")
        fake.chmod(0o755)
        for name in ("getent", "groupadd", "useradd", "groupdel", "userdel", "id", "curl", "mktemp", "install",
                     "stat", "chown", "systemctl", "apt-get"):
            (self.bin / name).symlink_to(fake)
        self.environment = os.environ.copy()
        self.environment.update(PATH=str(self.bin) + os.pathsep + self.environment["PATH"],
                                AGENT_TEST_ROOT=str(self.base), AGENT_TEST_SOURCE=str(SOURCE.resolve()))
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        def respond(path, body):
            self.assertEqual(path, "/api/agent/enroll")
            self.assertEqual(body, {"token": TOKEN})
            return success({"credential": CREDENTIAL, "role": "vps", "id": 1, "heartbeatSeconds": 30})
        self.server, self.api_calls = self.stack.enter_context(http_fixture(respond))

    def state(self):
        return json.loads(self.state_path.read_text())

    def run_installer(self, uninstall=False, upgrade=False, extra_env=None, server=None):
        arguments = ["bash", str(self.script)]
        if uninstall:
            arguments += ["--uninstall"]
        else:
            arguments += ["--server", server or self.server,
                          "--agent-sha", hashlib.sha256(SOURCE.read_bytes()).hexdigest(), "--development"]
            arguments += ["--upgrade"] if upgrade else ["--enrollment-token", TOKEN]
        environment = dict(self.environment, **(extra_env or {}))
        result = subprocess.run(arguments, env=environment, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=12)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        self.assertNotIn(CREDENTIAL, result.stdout + result.stderr)
        return result

    def test_first_install_uses_fixed_source_secure_config_and_dedicated_user(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.app.joinpath("agent.py").read_bytes(), SOURCE.read_bytes())
        self.assertEqual(self.config.joinpath("config.json").stat().st_mode & 0o777, 0o640)
        self.assertEqual(json.loads(self.config.joinpath("config.json").read_text())["credential"], CREDENTIAL)
        self.assertEqual(len(self.api_calls), 1)
        self.assertTrue(self.state()["user"])
        self.assertTrue(self.state()["group"])
        self.assertTrue(self.state()["active"])
        curl_calls = [call for call in self.state()["calls"] if call[0] == "curl"]
        self.assertEqual(len(curl_calls), 2)
        for call in curl_calls:
            self.assertNotIn("-L", call)
            self.assertNotIn("-k", call)
            self.assertIn("--proto", call)
        self.assertEqual(list(self.base.joinpath("work").iterdir()), [])

    def test_existing_directory_is_not_overwritten_and_apt_not_called(self):
        self.app.mkdir()
        foreign = self.app / "foreign.txt"
        foreign.write_text("other application")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(foreign.read_text(), "other application")
        self.assertFalse(any(call[0] == "apt-get" for call in self.state()["calls"]))
        self.assertEqual(self.api_calls, [])

    def test_symbolic_link_path_is_rejected_without_touching_foreign_directory(self):
        foreign = self.base / "foreign"
        foreign.mkdir()
        self.app.symlink_to(foreign, target_is_directory=True)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(foreign.iterdir()), [])
        self.assertEqual(self.state()["calls"], [])

    def test_changed_source_checksum_stops_before_enrollment_and_user_creation(self):
        result = self.run_installer(extra_env={"AGENT_TEST_BAD_SHA": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.api_calls, [])
        self.assertFalse(self.state()["user"])
        self.assertFalse(self.app.exists())
        self.assertEqual(list(self.base.joinpath("work").iterdir()), [])

    def test_start_failure_cleans_only_this_new_installation(self):
        foreign = self.base / "foreign.txt"
        foreign.write_text("retain")
        result = self.run_installer(extra_env={"AGENT_TEST_FAIL_START": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.app.exists())
        self.assertFalse(self.config.exists())
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.state()["user"])
        self.assertFalse(self.state()["group"])
        self.assertEqual(foreign.read_text(), "retain")
        self.assertEqual(list(self.base.joinpath("work").iterdir()), [])

    def test_uninstall_removes_owned_agent_and_retains_unrelated_files_and_packages(self):
        self.assertEqual(self.run_installer().returncode, 0)
        before = len(self.state()["calls"])
        foreign = self.base / "foreign.txt"
        foreign.write_text("retain")
        result = self.run_installer(uninstall=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.app.exists())
        self.assertFalse(self.config.exists())
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.state()["user"])
        self.assertFalse(self.state()["group"])
        self.assertEqual(foreign.read_text(), "retain")
        self.assertFalse(any(call[0] == "apt-get" for call in self.state()["calls"][before:]))

    def test_foreign_unit_or_unexpected_data_stops_uninstall_before_any_mutation(self):
        self.assertEqual(self.run_installer().returncode, 0)
        original = self.unit.read_bytes()
        self.unit.write_bytes(original + b"# unrelated service change\n")
        before = len(self.state()["calls"])
        result = self.run_installer(uninstall=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.state()["active"])
        self.assertFalse(any(call[0] in ("systemctl", "userdel", "groupdel") for call in self.state()["calls"][before:]))
        self.unit.write_bytes(original)
        foreign = self.config / "other-app.json"
        foreign.write_text("retain")
        before = len(self.state()["calls"])
        self.assertNotEqual(self.run_installer(uninstall=True).returncode, 0)
        self.assertEqual(foreign.read_text(), "retain")
        self.assertFalse(any(call[0] in ("systemctl", "userdel", "groupdel") for call in self.state()["calls"][before:]))

    def test_upgrade_preserves_v03_vps_credential_config_unit_and_account(self):
        self.assertEqual(self.run_installer().returncode, 0)
        previous = SOURCE.read_bytes() + b"\n# v03 installed code fixture\n"
        self.app.joinpath("agent.py").write_bytes(previous)
        config = self.config.joinpath("config.json").read_bytes()
        unit = self.unit.read_bytes()
        before = len(self.state()["calls"])
        result = self.run_installer(upgrade=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.app.joinpath("agent.py").read_bytes(), SOURCE.read_bytes())
        self.assertEqual(self.config.joinpath("config.json").read_bytes(), config)
        self.assertEqual(self.unit.read_bytes(), unit)
        self.assertEqual(len(self.api_calls), 1)
        self.assertTrue(self.state()["active"])
        self.assertFalse(any(call[0] in ("apt-get", "useradd", "groupadd", "userdel", "groupdel") for call in self.state()["calls"][before:]))
        self.assertEqual(sorted(path.name for path in self.app.iterdir()), [".managed", "agent.py"])
        self.assertEqual(list(self.base.joinpath("work").iterdir()), [])

    def test_upgrade_restart_failure_or_failed_health_restores_code_and_registration(self):
        self.assertEqual(self.run_installer().returncode, 0)
        old = SOURCE.read_bytes() + b"\n# old code kept after failure\n"
        self.app.joinpath("agent.py").write_bytes(old)
        config = self.config.joinpath("config.json").read_bytes()
        for failure in ("AGENT_TEST_FAIL_UPGRADE", "AGENT_TEST_UPGRADE_INACTIVE"):
            with self.subTest(failure=failure):
                state = self.state()
                state["restarts"] = 0
                self.state_path.write_text(json.dumps(state))
                result = self.run_installer(upgrade=True, extra_env={failure: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.app.joinpath("agent.py").read_bytes(), old)
                self.assertEqual(self.config.joinpath("config.json").read_bytes(), config)
                self.assertTrue(self.state()["active"])
                self.assertEqual(self.state()["restarts"], 2)
                self.assertEqual(len(self.api_calls), 1)
                self.assertEqual(sorted(path.name for path in self.app.iterdir()), [".managed", "agent.py"])
                self.assertEqual(list(self.base.joinpath("work").iterdir()), [])

    def test_upgrade_rejects_checksum_foreign_server_and_probe_config_before_replacement(self):
        self.assertEqual(self.run_installer().returncode, 0)
        old = self.app.joinpath("agent.py").read_bytes() + b"\n# unchanged\n"
        self.app.joinpath("agent.py").write_bytes(old)
        path = self.config / "config.json"
        original = path.read_bytes()
        for change in ("checksum", "server", "probe"):
            with self.subTest(change=change):
                if change == "probe":
                    value = json.loads(original)
                    value["role"] = "probe"
                    path.write_text(json.dumps(value))
                before = len(self.state()["calls"])
                result = self.run_installer(upgrade=True,
                    extra_env={"AGENT_TEST_BAD_SHA": "1"} if change == "checksum" else None,
                    server="http://127.0.0.1:1" if change == "server" else None)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.app.joinpath("agent.py").read_bytes(), old)
                self.assertFalse(any(call[0] == "systemctl" and call[1] in ("restart", "stop") for call in self.state()["calls"][before:]))
                self.assertEqual(len(self.api_calls), 1)
                path.write_bytes(original)

    def test_upgrade_requires_unchanged_owned_markers_unit_and_no_unexpected_data(self):
        self.assertEqual(self.run_installer().returncode, 0)
        for path in (self.app / ".managed", self.unit, self.config / "foreign.json"):
            with self.subTest(path=path):
                original = path.read_bytes() if path.exists() else None
                path.write_bytes((original or b"") + b"foreign data\n")
                before = len(self.state()["calls"])
                result = self.run_installer(upgrade=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(call[0] in ("curl", "apt-get", "systemctl") for call in self.state()["calls"][before:]))
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)

    def test_upgrade_rejects_group_writable_owned_path_before_download(self):
        self.assertEqual(self.run_installer().returncode, 0)
        for path in (self.app, self.config / "config.json", self.unit):
            with self.subTest(path=path):
                previous = path.stat().st_mode & 0o777
                path.chmod(previous | 0o020)
                before = len(self.state()["calls"])
                result = self.run_installer(upgrade=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(call[0] in ("curl", "systemctl") for call in self.state()["calls"][before:]))
                path.chmod(previous)
if __name__ == "__main__":
    unittest.main()
