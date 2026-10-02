"""Offline purge fixtures. All system commands are mocked; only temp data is deleted."""
import contextlib
from dataclasses import fields
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "console.py"
if not SOURCE.exists():
    SOURCE = HERE.parent / "scripts" / "deploy" / "console.py"
SPEC = importlib.util.spec_from_file_location("deploy_uninstall_console", str(SOURCE))
console = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = console
SPEC.loader.exec_module(console)
SHA, OLD_SHA = "a" * 40, "b" * 40


class Manager:
    def __init__(self, root):
        self.root = root
    def _config(self):
        return json.loads((self.root / "config.json").read_text(encoding="utf-8"))
    def _pointed_sha(self, name):
        path = self.root / name
        if not path.is_symlink():
            if path.exists():
                raise console.ConsoleError("unexpected regular deployment pointer")
            return None
        target = path.resolve()
        if target.parent != self.root / "releases" or target.name not in (SHA, OLD_SHA):
            raise console.ConsoleError("unsafe deployment pointer")
        return target.name


class UninstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root, self.system = self.base / "install", self.base / "system"
        self.root.mkdir()
        self.system.mkdir()
        self.manager = Manager(self.root)
        self.paths = console.Paths(**{field.name: self.system / field.name for field in fields(console.Paths)})
        self.calls, self.locked_now, self.nginx_state = [], False, "active"
        self.timer_enabled, self.timer_active = True, True
        for name, content in ((".xiaowork-watch-managed", console.MARKER_VALUE),
                              (".installation-complete", console.MARKER_VALUE), (".deploy.lock", ""),
                              ("installed.json", json.dumps({"kind": "frontend-prototype"})),
                              ("config.json", json.dumps({"autoUpdate": True, "httpsEnabled": True,
                               "proxyDomain": "watch.example.com", "tlsDomain": "watch.example.com",
                               "healthUrl": "http://127.0.0.1:8088/release.json"}))):
            (self.root / name).write_text(content, encoding="utf-8")
        for sha in (SHA, OLD_SHA):
            release = self.root / "releases" / sha
            (release / ".deploy").mkdir(parents=True)
            (release / "release.json").write_text(json.dumps({"schema": 1, "commit": sha,
                  "kind": "frontend-prototype", "version": "0.2.3"}), encoding="utf-8")
            for name in ("index.html", ".deploy/manage.py", ".deploy/console.py"):
                (release / name).write_text("owned fixture", encoding="utf-8")
        assets = self.root / "shared" / "assets"
        assets.mkdir(parents=True)
        (assets / "old-hash.js").write_text("retained history", encoding="utf-8")
        folders = console._tls_directories(self.manager, create=True)
        self.certificates = folders["certs"]
        archive = self.certificates / "archive" / console.TLS_NAME
        live = self.certificates / "live" / console.TLS_NAME
        archive.mkdir(parents=True)
        live.mkdir(parents=True)
        for name in ("cert", "chain", "fullchain", "privkey"):
            target = archive / (name + "1.pem")
            target.write_text("certificate fixture", encoding="utf-8")
            if os.name == "posix":
                (live / (name + ".pem")).symlink_to(Path("../../archive") / console.TLS_NAME / target.name)
            else:
                (live / (name + ".pem")).write_text("Windows fixture", encoding="utf-8")
        if os.name == "posix":
            (self.root / "current").symlink_to(Path("releases") / SHA)
            (self.root / "previous").symlink_to(Path("releases") / OLD_SHA)
            (self.root / "control").symlink_to(Path("releases") / SHA / ".deploy")
        for field in fields(console.Paths):
            getattr(self.paths, field.name).write_text(console._header(self.manager) + "owned fixture\n", encoding="utf-8")
        self.foreign = self.system / "other-site.conf"
        self.foreign.write_text("unrelated site", encoding="utf-8")

    @contextlib.contextmanager
    def locked(self, path):
        self.assertEqual(path, self.root / ".deploy.lock")
        self.assertFalse(self.locked_now)
        self.locked_now = True
        try:
            yield
        finally:
            self.locked_now = False

    def command(self, arguments, **kwargs):
        self.assertTrue(self.locked_now)
        self.calls.append(arguments)
        if arguments[:2] == ["systemctl", "show"]:
            state = self.nginx_state if arguments[2] == "nginx.service" else ("active" if self.timer_active else "inactive")
            key = "UnitFileState" if "--property=UnitFileState" in arguments else "ActiveState"
            value = ("enabled" if self.timer_enabled else "disabled") if key == "UnitFileState" else state
            return "LoadState=loaded\n" + key + "=" + value + "\nwarning: unrelated diagnostic\n"
        return ""

    @contextlib.contextmanager
    def runtime(self, command=None):
        with patch.object(console, "_run", side_effect=command or self.command):
            yield

    def uninstall(self, purge=True, confirm=True):
        return console.uninstall(self.manager, confirm=confirm, paths=self.paths, locked=self.locked, purge=purge)

    def assert_entries_removed(self):
        for field in fields(console.Paths):
            self.assertFalse(getattr(self.paths, field.name).exists())
        self.assertEqual(self.foreign.read_text(encoding="utf-8"), "unrelated site")

    def test_purge_clears_all_owned_data_after_removing_entries(self):
        original_remove = shutil.rmtree
        seen = []
        def remove(path, *args, **kwargs):
            candidate = Path(path)
            self.assertTrue(self.locked_now)
            self.assertFalse(self.root.exists())
            self.assertEqual(candidate.parent, self.root.parent)
            self.assertTrue(candidate.name.startswith(".xiaowork-watch-purge-"))
            self.assert_entries_removed()
            seen.append(candidate)
            return original_remove(path, *args, **kwargs)
        with self.runtime(), patch.object(console.shutil, "rmtree", side_effect=remove):
            result = self.uninstall()
        self.assertFalse(result["dataRetained"])
        self.assertFalse(self.root.exists())
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].exists())
        self.assertFalse(any(args[0] in ("apt", "apt-get", "rm") or "certbot.timer" in args for args in self.calls))

    def test_default_uninstall_still_retains_all_history_and_certificates(self):
        with self.runtime():
            result = console.uninstall(self.manager, confirm=True, paths=self.paths, locked=self.locked)
        self.assertTrue(result["dataRetained"])
        self.assertTrue((self.root / "releases" / OLD_SHA / "index.html").exists())
        self.assertTrue((self.certificates / "archive" / console.TLS_NAME / "privkey1.pem").exists())
        self.assertTrue((self.root / "config.json").exists())
        self.assertTrue((self.root / ".xiaowork-watch-managed").exists())
        self.assertFalse((self.root / "installed.json").exists())
        self.assert_entries_removed()

    def test_purge_inspects_transient_update_files_only_after_acquiring_lock(self):
        transient = self.root / ".download-running-update"
        transient.write_text("in-progress update", encoding="utf-8")

        @contextlib.contextmanager
        def finishing_update(path):
            self.assertTrue(transient.exists())
            transient.unlink()
            with self.locked(path):
                yield

        with self.runtime():
            result = console.uninstall(self.manager, confirm=True, paths=self.paths,
                                       locked=finishing_update, purge=True)
        self.assertFalse(result["dataRetained"])
        self.assertFalse(self.root.exists())
        self.assert_entries_removed()

    def test_unconfirmed_purge_does_not_lock_or_change_anything(self):
        with patch.object(console, "_lock") as lock, patch.object(console, "_run") as run:
            self.assertEqual(self.uninstall(confirm=False)["status"], "cancelled")
        lock.assert_not_called()
        run.assert_not_called()
        self.assertTrue(self.root.exists())

    def test_purge_rejects_invalid_marker_and_unknown_content_before_commands(self):
        marker = self.root / ".xiaowork-watch-managed"
        for target, content in ((marker, "other-owner"), (self.root / "manual-project.txt", "manual"),
                                (self.root / "shared" / "manual-certs", "manual")):
            with self.subTest(target=target):
                previous = target.read_text(encoding="utf-8") if target.exists() else None
                target.write_text(content, encoding="utf-8")
                with self.runtime(), self.assertRaises(console.ConsoleError):
                    self.uninstall()
                self.assertEqual(self.calls, [])
                self.assertTrue(self.paths.wrapper.exists())
                if previous is None:
                    target.unlink()
                else:
                    target.write_text(previous, encoding="utf-8")

    def test_protected_root_paths_are_rejected_without_opening_a_lock(self):
        for root in (Path(Path.cwd().anchor), Path("/opt"), Path("/etc"), Path.home(), Path("relative")):
            with self.subTest(root=root), patch.object(console, "_lock") as lock:
                with self.assertRaises(console.ConsoleError):
                    console.uninstall(Manager(root), confirm=True, paths=self.paths, locked=self.locked, purge=True)
                lock.assert_not_called()

    def test_foreign_global_entry_blocks_purge_without_changing_data(self):
        self.paths.tls_timer.write_text("foreign unit", encoding="utf-8")
        with self.runtime(), self.assertRaises(console.ConsoleError):
            self.uninstall()
        self.assertEqual(self.calls, [])
        self.assertTrue(self.root.exists())
        self.assertEqual(self.paths.tls_timer.read_text(encoding="utf-8"), "foreign unit")

    def test_invalid_historical_release_is_preserved(self):
        metadata = self.root / "releases" / OLD_SHA / "release.json"
        metadata.write_text(json.dumps({"schema": 1, "commit": SHA, "kind": "frontend-prototype"}), encoding="utf-8")
        with self.runtime(), self.assertRaises(console.ConsoleError):
            self.uninstall()
        self.assertEqual(self.calls, [])
        self.assertTrue(metadata.exists())

    def test_offline_nginx_purge_checks_config_without_reloading(self):
        self.nginx_state = "inactive"
        with self.runtime():
            result = self.uninstall()
        self.assertFalse(result["dataRetained"])
        self.assertIn(["nginx", "-t"], self.calls)
        self.assertNotIn(["systemctl", "reload", "nginx"], self.calls)
        self.assertEqual(sum(args[:3] == ["systemctl", "show", "nginx.service"] for args in self.calls), 1)
        self.assert_entries_removed()

    def test_offline_nginx_failure_restores_entries_without_reload(self):
        self.nginx_state = "inactive"
        previous = (self.root / "config.json").read_bytes()
        def command(arguments, **kwargs):
            result = self.command(arguments, **kwargs)
            if arguments == ["nginx", "-t"] and not self.paths.nginx_site.exists():
                raise console.ConsoleError("invalid other site")
            return result
        with self.runtime(command), self.assertRaises(console.ConsoleError):
            self.uninstall()
        self.assertTrue(self.root.exists())
        self.assertTrue(self.paths.nginx_site.exists())
        self.assertEqual((self.root / "config.json").read_bytes(), previous)
        self.assertNotIn(["systemctl", "reload", "nginx"], self.calls)

    def test_rename_failure_rolls_back_entries_and_config(self):
        previous = (self.root / "config.json").read_bytes()
        with self.runtime(), patch.object(console.os, "rename", side_effect=OSError("cannot move")), self.assertRaises(OSError):
            self.uninstall()
        for field in fields(console.Paths):
            self.assertTrue(getattr(self.paths, field.name).exists())
        self.assertEqual((self.root / "config.json").read_bytes(), previous)

    def test_partial_data_cleanup_failure_reports_residue_and_keeps_entries_removed(self):
        residues = []
        def remove(path):
            candidate = Path(path)
            (candidate / "releases" / OLD_SHA / "index.html").unlink()
            residues.append(candidate)
            raise PermissionError("simulated partial cleanup failure")
        with self.runtime(), patch.object(console.shutil, "rmtree", side_effect=remove), self.assertRaises(console.ConsoleError) as failure:
            self.uninstall()
        self.assertIn("网站入口和服务已卸载", str(failure.exception))
        self.assertIn(str(residues[0]), str(failure.exception))
        self.assertFalse(self.root.exists())
        self.assertTrue(residues[0].exists())
        self.assert_entries_removed()

    def test_systemd_structured_state_ignores_stderr_noise_and_recognizes_states(self):
        for query, state, expected in (("is-enabled", "enabled", True), ("is-enabled", "linked", False),
                ("is-enabled", "alias", False), ("is-enabled", "static", False), ("is-active", "inactive", False),
                ("is-active", "failed", False), ("is-active", "reloading", True), ("is-active", "activating", False),
                ("is-active", "deactivating", False)):
            key = "UnitFileState" if query == "is-enabled" else "ActiveState"
            result = subprocess.CompletedProcess([], 0, "LoadState=loaded\n" + key + "=" + state + "\n", "diagnostic warning\n")
            with self.subTest(state=state), patch.object(console.subprocess, "run", return_value=result):
                self.assertEqual(console._unit_state("fixture.timer", query), expected)

    def test_systemd_missing_invalid_properties_and_bus_errors(self):
        with patch.object(console, "_run", return_value="LoadState=not-found\n"):
            self.assertFalse(console._unit_state("missing.timer", "is-active"))
        for value in ("ActiveState=active\n", "LoadState=loaded\n", "LoadState=loaded\nActiveState=nonsense\n",
                      "LoadState=loaded\nActiveState=active\nActiveState=inactive\n"):
            with self.subTest(value=value), patch.object(console, "_run", return_value=value), self.assertRaises(console.ConsoleError):
                console._unit_state("fixture.timer", "is-active")
        error = subprocess.CalledProcessError(1, ["systemctl"], output="", stderr="Failed to connect to bus: Permission denied")
        with patch.object(console.subprocess, "run", side_effect=error), self.assertRaises(console.ConsoleError):
            console._unit_state("fixture.timer", "is-active")

    @unittest.skipUnless(os.name == "posix", "POSIX parent permissions require Linux fixtures")
    def test_writable_parent_is_rejected_before_removing_entries(self):
        previous = self.base.stat().st_mode & 0o7777
        try:
            self.base.chmod(0o777)
            with self.runtime(), self.assertRaises(console.ConsoleError):
                self.uninstall()
            self.assertEqual(self.calls, [])
            self.assertTrue(self.paths.wrapper.exists())
            self.assertTrue(self.root.exists())
        finally:
            self.base.chmod(previous)

    @unittest.skipUnless(os.name == "posix", "POSIX ownership requires Linux fixtures")
    def test_untrusted_parent_owner_is_rejected_before_removing_entries(self):
        original = Path.lstat
        def lstat(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == self.base:
                values = list(result)
                values[4] = max(1, os.geteuid()) + 10000
                return os.stat_result(values)
            return result
        with patch.object(Path, "lstat", lstat), self.runtime(), self.assertRaises(console.ConsoleError):
            self.uninstall()
        self.assertEqual(self.calls, [])
        self.assertTrue(self.paths.wrapper.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX trusted sticky parent semantics require Linux")
    def test_root_owned_sticky_parent_is_allowed(self):
        original = Path.lstat
        def lstat(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == self.base:
                values = list(result)
                values[0] = stat.S_IFDIR | 0o1777
                values[4] = 0
                return os.stat_result(values)
            return result
        with patch.object(Path, "lstat", lstat):
            console._purge_parent_security(self.root)

    @unittest.skipUnless(os.name == "posix", "Directory symlinks require Linux fixtures")
    def test_external_child_symlink_is_rejected_and_external_data_is_untouched(self):
        outside = self.base / "unrelated-project"
        outside.mkdir()
        data = outside / "keep.txt"
        data.write_text("keep", encoding="utf-8")
        (self.root / "shared/assets/external").symlink_to(outside)
        with self.runtime(), self.assertRaises(console.ConsoleError):
            self.uninstall()
        self.assertEqual(self.calls, [])
        self.assertEqual(data.read_text(encoding="utf-8"), "keep")

    @unittest.skipUnless(os.name == "posix", "FD-based Linux deletion and symlinks")
    def test_internal_directory_link_is_unlinked_without_walking_it(self):
        (self.root / "shared/assets/old-release").symlink_to(self.root / "releases" / OLD_SHA)
        with self.runtime():
            self.uninstall()
        self.assertFalse(self.root.exists())
        self.assert_entries_removed()

    @unittest.skipUnless(os.name == "posix", "Special file fixtures require Linux")
    def test_special_child_file_is_rejected_before_commands(self):
        os.mkfifo(str(self.root / "shared/assets/pipe"))
        with self.runtime(), self.assertRaises(console.ConsoleError):
            self.uninstall()
        self.assertEqual(self.calls, [])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux mountinfo contains bind mounts")
    def test_same_device_bind_mount_is_rejected(self):
        mount = self.root / "shared/assets"
        line = "20 10 8:1 / " + str(mount).replace(" ", "\\040") + " rw - ext4 /dev/mock rw\n"
        original = Path.read_text
        def read(path, *args, **kwargs):
            return line if str(path) == "/proc/self/mountinfo" else original(path, *args, **kwargs)
        with patch.object(Path, "read_text", read), self.runtime(), self.assertRaises(console.ConsoleError):
            self.uninstall()
        self.assertEqual(self.calls, [])

    def assert_stale_lock_waiter_rejected(self, linked_root=False):
        result = self.base / "waiter-result.json"
        script = """
import importlib.util, json, pathlib, sys
spec = importlib.util.spec_from_file_location('waiter_console', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
try:
    with module._default_locked(pathlib.Path(sys.argv[2]) / '.deploy.lock'):
        value = {'entered': True}
except Exception as error:
    value = {'entered': False, 'error': str(error)}
pathlib.Path(sys.argv[3]).write_text(json.dumps(value), encoding='utf-8')
"""
        process = None
        try:
            with console._default_locked(self.root / ".deploy.lock"):
                process = subprocess.Popen([sys.executable, "-B", "-c", script, str(SOURCE), str(self.root), str(result)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                self.assertIn("等待", process.stdout.readline())
                tombstone = self.base / "held-old-install"
                os.rename(str(self.root), str(tombstone))
                if linked_root:
                    self.root.symlink_to(tombstone, target_is_directory=True)
                else:
                    self.root.mkdir()
                    (self.root / ".xiaowork-watch-managed").write_text(console.MARKER_VALUE, encoding="utf-8")
                    (self.root / ".deploy.lock").write_text("", encoding="utf-8")
            _, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertFalse(json.loads(result.read_text(encoding="utf-8"))["entered"])
        finally:
            if process and process.poll() is None:
                process.kill()
                process.communicate()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Real flock waiter regression requires Linux")
    def test_stale_lock_waiter_rejects_a_reinstalled_root(self):
        self.assert_stale_lock_waiter_rejected()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Real flock and directory links require Linux")
    def test_stale_lock_waiter_rejects_link_back_to_old_tree(self):
        self.assert_stale_lock_waiter_rejected(linked_root=True)


if __name__ == "__main__":
    unittest.main()
