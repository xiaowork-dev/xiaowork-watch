"""Offline console fixtures; copy unchanged to tests/test_deploy_console.py."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "console.py"
if not SOURCE.exists():
    SOURCE = HERE.parent / "scripts" / "deploy" / "console.py"
SPEC = importlib.util.spec_from_file_location("deploy_console", str(SOURCE))
console = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = console
SPEC.loader.exec_module(console)


class FakeManager:
    def __init__(self, root):
        self.root = root
    def _config(self):
        return json.loads((self.root / "config.json").read_text(encoding="utf-8"))
    def _pointed_sha(self, name):
        path = self.root / name
        if not path.is_symlink():
            if path.exists():
                raise ValueError("unsafe pointer")
            return None
        target = path.resolve()
        if target.parent != self.root / "releases" or len(target.name) != 40 or not target.is_dir():
            raise ValueError("outside release pointer")
        return target.name
    def status(self):
        return {"current": self._pointed_sha("current"), "autoUpdate": self._config()["autoUpdate"]}
    def install_or_update(self):
        return {"status": "updated"}
    def rollback(self):
        return {"status": "rolled-back"}


class Terminal:
    def __init__(self, inputs):
        self.inputs = io.StringIO(inputs)
        self.output = io.StringIO()
    def write(self, text):
        return self.output.write(text)
    def flush(self):
        pass
    def readline(self):
        return self.inputs.readline()
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.root = base / "install"
        self.system = base / "system"
        self.root.mkdir()
        self.system.mkdir()
        self.manager = FakeManager(self.root)
        self.paths = console.Paths(**{name: self.system / name for name in (
            "nginx_site", "nginx_proxy", "wrapper", "service", "timer", "tls_service", "tls_timer")})
        self.locked_paths = []
        self.inside_lock = False
        self.write_config()
        (self.root / ".xiaowork-watch-managed").write_text(console.MARKER_VALUE, encoding="utf-8")
        (self.root / ".installation-complete").write_text(console.MARKER_VALUE, encoding="utf-8")
        (self.root / "installed.json").write_text(json.dumps({"kind": "frontend-prototype"}), encoding="utf-8")
        for name in ("releases", "shared"):
            (self.root / name).mkdir()
            (self.root / name / "keep.data").write_text("retained", encoding="utf-8")
        self.write_owned_files()

    @contextlib.contextmanager
    def locked(self, path):
        self.assertFalse(self.inside_lock, "Menu mutations must not nest deployment locks")
        self.locked_paths.append(path)
        self.inside_lock = True
        try:
            yield
        finally:
            self.inside_lock = False

    def write_config(self, **changes):
        value = {"healthUrl": "http://127.0.0.1:8088/release.json", "healthHost": "", "autoUpdate": True}
        value.update(changes)
        (self.root / "config.json").write_text(json.dumps(value), encoding="utf-8")

    def write_owned_files(self):
        root = str(self.root)
        site = ("server {\n    root " + root + "/current;\n    location /assets/ {\n"
                "        alias " + root + "/shared/assets/;\n    }\n}\n")
        wrapper = '#!/bin/sh\nexec /usr/bin/python3 "' + root + '/current/.deploy/manage.py" --root "' + root + '" "$@"\n'
        for path, text in ((self.paths.nginx_site, site), (self.paths.wrapper, wrapper),
                           (self.paths.service, console.OLD_SERVICE), (self.paths.timer, console.OLD_TIMER)):
            path.write_text(console._header(self.manager) + text, encoding="utf-8")

    def proxy(self, domain="watch.example.com"):
        return console.configure_proxy(self.manager, domain, paths=self.paths, locked=self.locked)

    def test_proxy_is_http_loopback_and_owned(self):
        self.write_config(healthHost="original.example.com")
        with patch.object(console, "_run", return_value="") as commands:
            result = self.proxy()
        conf = self.paths.nginx_proxy.read_text(encoding="utf-8")
        self.assertIn("server_name watch.example.com;", conf)
        self.assertIn("proxy_pass http://127.0.0.1:8088;", conf)
        self.assertIn("proxy_set_header Host original.example.com;", conf)
        self.assertIn(console.ROOT_PREFIX + str(self.root), conf)
        self.assertFalse(result["https"])
        self.assertEqual(self.manager._config()["proxyDomain"], "watch.example.com")
        self.assertEqual(commands.call_args_list[0].args[0], ["nginx", "-t"])
        self.assertEqual(self.locked_paths, [self.root / ".deploy.lock"])

    def test_domain_injection_never_writes_or_runs_nginx(self):
        for domain in ("x; include /tmp/evil", "x\nserver {}", "https://example.com", "a:80", "a/b", "_", "a..com", "a" * 64 + ".com"):
            with self.subTest(domain=domain), patch.object(console, "_run") as commands:
                with self.assertRaises(console.ConsoleError):
                    self.proxy(domain)
                commands.assert_not_called()
                self.assertFalse(self.paths.nginx_proxy.exists())

    def test_proxy_prevents_upstream_port_80_loop(self):
        self.write_config(healthUrl="http://127.0.0.1:80/release.json")
        with patch.object(console, "_run") as commands, self.assertRaises(console.ConsoleError):
            self.proxy()
        commands.assert_not_called()

    def test_proxy_nginx_validation_failure_restores_previous_conf(self):
        original = console._header(self.manager) + "server { listen 80; server_name old.example.com; }\n"
        self.paths.nginx_proxy.write_text(original, encoding="utf-8")
        def fail_first(arguments):
            if fail_first.first:
                fail_first.first = False
                raise console.ConsoleError("simulated nginx -t failure")
            return ""
        fail_first.first = True
        with patch.object(console, "_run", side_effect=fail_first), self.assertRaises(console.ConsoleError):
            self.proxy()
        self.assertEqual(self.paths.nginx_proxy.read_text(encoding="utf-8"), original)
        self.assertNotIn("proxyDomain", self.manager._config())

    def test_proxy_reload_failure_removes_new_conf(self):
        def failure(arguments):
            if arguments == ["systemctl", "reload", "nginx"]:
                raise console.ConsoleError("simulated reload failure")
            return ""
        with patch.object(console, "_run", side_effect=failure), self.assertRaises(console.ConsoleError):
            self.proxy()
        self.assertFalse(self.paths.nginx_proxy.exists())

    def test_conflicting_domain_warning_cannot_report_proxy_success(self):
        with patch.object(console, "_run", return_value="nginx: [warn] conflicting server name watch.example.com"), self.assertRaises(console.ConsoleError):
            self.proxy()
        self.assertFalse(self.paths.nginx_proxy.exists())
        self.assertNotIn("proxyDomain", self.manager._config())

    def test_unmanaged_proxy_conf_is_untouched(self):
        original = "server { listen 80; server_name another.example.com; }"
        self.paths.nginx_proxy.write_text(original, encoding="utf-8")
        with patch.object(console, "_run") as commands, self.assertRaises(console.ConsoleError):
            self.proxy()
        commands.assert_not_called()
        self.assertEqual(self.paths.nginx_proxy.read_text(encoding="utf-8"), original)

    def test_conf_marked_for_another_root_is_not_legacy_owned(self):
        text = self.paths.nginx_site.read_text(encoding="utf-8").replace(
            console.ROOT_PREFIX + str(self.root), console.ROOT_PREFIX + "/some/other/root")
        self.paths.nginx_site.write_text(text, encoding="utf-8")
        with patch.object(console, "_run") as commands, self.assertRaises(console.ConsoleError):
            self.proxy()
        commands.assert_not_called()

    def test_auto_update_preserves_other_config_and_uses_lock(self):
        self.write_config(extraSetting="keep")
        console.set_auto_update(self.manager, False, locked=self.locked)
        self.assertFalse(self.manager._config()["autoUpdate"])
        self.assertEqual(self.manager._config()["extraSetting"], "keep")
        self.assertEqual(self.locked_paths, [self.root / ".deploy.lock"])
        with self.assertRaises(console.ConsoleError):
            console.set_auto_update(self.manager, "false", locked=self.locked)

    def test_uninstall_unconfirmed_has_no_side_effect(self):
        before = self.manager._config()
        with patch.object(console, "_run") as commands:
            result = console.uninstall(self.manager, paths=self.paths, locked=self.locked)
        self.assertEqual(result["status"], "cancelled")
        commands.assert_not_called()
        self.assertEqual(self.manager._config(), before)
        self.assertTrue(self.paths.wrapper.exists())

    def test_uninstall_only_removes_owned_entries_and_retains_data(self):
        unrelated = self.system / "another-site.conf"
        unrelated.write_text("manual website", encoding="utf-8")
        manual = self.root / "manual-project"
        manual.mkdir()
        (manual / "keep.txt").write_text("manual source", encoding="utf-8")
        with patch.object(console, "_run", return_value="") as commands:
            result = console.uninstall(self.manager, True, paths=self.paths, locked=self.locked)
        self.assertEqual(result["status"], "uninstalled")
        for path in (self.paths.nginx_site, self.paths.wrapper, self.paths.service, self.paths.timer,
                     self.root / "installed.json", self.root / ".installation-complete"):
            self.assertFalse(path.exists(), str(path))
        for path in (self.root / "releases/keep.data", self.root / "shared/keep.data", self.root / "config.json",
                     self.root / ".xiaowork-watch-managed", manual / "keep.txt", unrelated):
            self.assertTrue(path.exists(), str(path))
        self.assertFalse(self.manager._config()["autoUpdate"])
        calls = [item.args[0] for item in commands.call_args_list]
        self.assertIn(["systemctl", "disable", "--now", "xiaowork-watch-update.timer"], calls)
        self.assertIn(["systemctl", "stop", "xiaowork-watch-update.service"], calls)
        self.assertFalse(any("apt" in " ".join(item) for item in calls))
        self.assertFalse(any(item[:2] == ["systemctl", "stop"] and "nginx" in item for item in calls))

    def test_uninstall_full_precheck_preserves_foreign_unit(self):
        original = "[Service]\nExecStart=/another/manual/project\n"
        self.paths.service.write_text(original, encoding="utf-8")
        with patch.object(console, "_run") as commands, self.assertRaises(console.ConsoleError):
            console.uninstall(self.manager, True, paths=self.paths, locked=self.locked)
        commands.assert_not_called()
        self.assertEqual(self.paths.service.read_text(encoding="utf-8"), original)
        self.assertTrue(self.manager._config()["autoUpdate"])
        self.assertTrue((self.root / "installed.json").exists())

    def test_uninstall_invalid_completion_aborts_before_any_changes(self):
        (self.root / ".installation-complete").write_text("foreign marker", encoding="utf-8")
        with patch.object(console, "_run") as commands, self.assertRaises(console.ConsoleError):
            console.uninstall(self.manager, True, paths=self.paths, locked=self.locked)
        commands.assert_not_called()
        self.assertTrue(self.paths.wrapper.exists())

    def test_uninstall_non_symlink_current_aborts_full_precheck(self):
        (self.root / "current").mkdir()
        (self.root / "current/keep.txt").write_text("manual source", encoding="utf-8")
        with patch.object(console, "_run") as commands, self.assertRaises(ValueError):
            console.uninstall(self.manager, True, paths=self.paths, locked=self.locked)
        commands.assert_not_called()
        self.assertEqual((self.root / "current/keep.txt").read_text(encoding="utf-8"), "manual source")
        self.assertTrue(self.manager._config()["autoUpdate"])

    def test_control_refuses_unmanaged_regular_file(self):
        (self.root / "control").write_text("manual data", encoding="utf-8")
        before = self.paths.wrapper.read_text(encoding="utf-8")
        with self.assertRaises(console.ConsoleError):
            console.ensure_control_entry(self.manager, paths=self.paths, locked=self.locked)
        self.assertEqual((self.root / "control").read_text(encoding="utf-8"), "manual data")
        self.assertEqual(self.paths.wrapper.read_text(encoding="utf-8"), before)

    def test_legacy_templates_are_owned_only_with_matching_root_wrapper(self):
        for path in (self.paths.nginx_site, self.paths.wrapper, self.paths.service, self.paths.timer):
            text = path.read_text(encoding="utf-8").replace(console._header(self.manager), "")
            if path == self.paths.nginx_site:
                text = console.OWNER + "\n" + text
            path.write_text(text, encoding="utf-8")
        for path, kind in ((self.paths.nginx_site, "site"), (self.paths.wrapper, "wrapper"),
                           (self.paths.service, "service"), (self.paths.timer, "timer")):
            self.assertTrue(console._owned(path, self.manager, kind, self.paths), kind)
        self.paths.wrapper.write_text("#!/bin/sh\nexec /other/app\n", encoding="utf-8")
        self.assertFalse(console._owned(self.paths.service, self.manager, "service", self.paths))
        self.assertFalse(console._owned(self.paths.timer, self.manager, "timer", self.paths))

    def test_no_tty_returns_without_mutation_or_stdin_read(self):
        with patch.object(console, "_open_terminal", side_effect=OSError("no tty")), patch.object(
                console, "uninstall") as remove, patch.object(console, "configure_proxy") as proxy, patch(
                "sys.stderr", new=io.StringIO()):
            self.assertEqual(console.run_menu(self.manager, self.locked, paths=self.paths), 1)
        remove.assert_not_called()
        proxy.assert_not_called()
        self.assertEqual(self.locked_paths, [])

    def test_menu_error_remains_in_loop_and_does_not_hold_idle_lock(self):
        terminal = Terminal("2\ninvalid/domain\n1\n0\n")
        with patch.object(console, "_open_terminal", return_value=terminal):
            self.assertEqual(console.run_menu(self.manager, self.locked, paths=self.paths), 0)
        self.assertIn("操作失败", terminal.output.getvalue())
        self.assertIn('自动更新：开启', terminal.output.getvalue())
        self.assertEqual(self.locked_paths, [])

    def test_menu_uninstall_requires_both_confirmations_and_exits_after_removal(self):
        terminal = Terminal("7\n7\nNO\n7\n7\nUNINSTALL\n")
        with patch.object(console, "_open_terminal", return_value=terminal), patch.object(console, "_run", return_value=""):
            self.assertEqual(console.run_menu(self.manager, self.locked, paths=self.paths), 0)
        self.assertIn("已取消卸载", terminal.output.getvalue())
        self.assertIn("卸载完成", terminal.output.getvalue())
        self.assertEqual(len(self.locked_paths), 1)
        self.assertFalse(self.paths.wrapper.exists())

    @unittest.skipUnless(os.name == "posix", "Real symbolic-link ownership requires Linux")
    def test_uninstall_rejects_symlink_record_before_system_changes(self):
        record = self.root / "installed.json"
        record.unlink()
        record.symlink_to(self.system / "service")
        with patch.object(console, "_run") as commands, self.assertRaises(console.ConsoleError):
            console.uninstall(self.manager, True, paths=self.paths, locked=self.locked)
        commands.assert_not_called()
        self.assertTrue(self.manager._config()["autoUpdate"])

    @unittest.skipUnless(os.name == "posix", "Real symbolic-link removal requires Linux")
    def test_uninstall_removes_pointers_but_keeps_both_release_directories(self):
        for name, sha in (("current", "a" * 40), ("previous", "b" * 40)):
            (self.root / "releases" / sha).mkdir()
            (self.root / name).symlink_to(Path("releases") / sha, target_is_directory=True)
        with patch.object(console, "_run", return_value=""):
            console.uninstall(self.manager, True, paths=self.paths, locked=self.locked)
        for name in ("current", "previous"):
            self.assertFalse((self.root / name).is_symlink())
        for sha in ("a" * 40, "b" * 40):
            self.assertTrue((self.root / "releases" / sha).is_dir())

    def create_control_release(self, sha, has_console=True):
        target = self.root / "releases" / sha / ".deploy"
        target.mkdir(parents=True)
        (target / "manage.py").write_text("# manager\n", encoding="utf-8")
        if has_console:
            (target / "console.py").write_text(SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
        return target

    @unittest.skipUnless(os.name == "posix", "Stable control-link regression requires Linux")
    def test_control_menu_entry_survives_rollback_to_v021_without_console(self):
        new, old = "a" * 40, "b" * 40
        target = self.create_control_release(new)
        self.create_control_release(old, has_console=False)
        current = self.root / "current"
        current.symlink_to(Path("releases") / new, target_is_directory=True)
        self.assertTrue(console.ensure_control_entry(self.manager, paths=self.paths, locked=self.locked))
        self.assertEqual((self.root / "control").resolve(), target)
        self.assertIn("/control/manage.py", self.paths.wrapper.read_text(encoding="utf-8"))
        current.unlink()
        current.symlink_to(Path("releases") / old, target_is_directory=True)
        self.assertTrue(console.ensure_control_entry(self.manager, paths=self.paths, locked=self.locked))
        self.assertEqual((self.root / "control").resolve(), target)
        reopened = importlib.util.spec_from_file_location("reopened_control_console", str(self.root / "control/console.py"))
        module = importlib.util.module_from_spec(reopened)
        sys.modules[reopened.name] = module
        reopened.loader.exec_module(module)
        terminal = Terminal("1\n0\n")
        with patch.object(module, "_open_terminal", return_value=terminal):
            self.assertEqual(module.run_menu(self.manager, self.locked, paths=self.paths), 0)
        self.assertIn(old[:12], terminal.output.getvalue())
        self.assertEqual((self.root / "control").resolve(), target)

    @unittest.skipUnless(os.name == "posix", "Control refresh requires Linux symlinks")
    def test_control_refresh_follows_successful_new_version_only(self):
        old, new = "a" * 40, "b" * 40
        old_target = self.create_control_release(old)
        new_target = self.create_control_release(new)
        current = self.root / "current"
        current.symlink_to(Path("releases") / old, target_is_directory=True)
        console.ensure_control_entry(self.manager, paths=self.paths, locked=self.locked)
        current.unlink()
        current.symlink_to(Path("releases") / new, target_is_directory=True)
        console.ensure_control_entry(self.manager, paths=self.paths, locked=self.locked)
        self.assertEqual((self.root / "control").resolve(), old_target)
        console.ensure_control_entry(self.manager, paths=self.paths, locked=self.locked, refresh=True)
        self.assertEqual((self.root / "control").resolve(), new_target)
        with patch.object(console, "_run", return_value=""):
            console.uninstall(self.manager, True, paths=self.paths, locked=self.locked)
        self.assertFalse((self.root / "control").is_symlink())
        self.assertTrue(old_target.is_dir())
        self.assertTrue(new_target.is_dir())

    @unittest.skipUnless(os.name == "posix", "Legacy current-link regression requires Linux")
    def test_old_current_without_console_does_not_create_control_or_change_wrapper(self):
        old = "a" * 40
        self.create_control_release(old, has_console=False)
        (self.root / "current").symlink_to(Path("releases") / old, target_is_directory=True)
        before = self.paths.wrapper.read_text(encoding="utf-8")
        self.assertFalse(console.ensure_control_entry(self.manager, paths=self.paths, locked=self.locked))
        self.assertFalse((self.root / "control").exists())
        self.assertEqual(self.paths.wrapper.read_text(encoding="utf-8"), before)

    @unittest.skipUnless(os.name == "posix", "Control-link ownership requires Linux")
    def test_control_outside_releases_aborts_before_wrapper_change_or_uninstall(self):
        (self.root / "control").symlink_to(self.system, target_is_directory=True)
        before = self.paths.wrapper.read_text(encoding="utf-8")
        with self.assertRaises(console.ConsoleError):
            console.ensure_control_entry(self.manager, paths=self.paths, locked=self.locked)
        with patch.object(console, "_run") as commands, self.assertRaises(console.ConsoleError):
            console.uninstall(self.manager, True, paths=self.paths, locked=self.locked)
        commands.assert_not_called()
        self.assertEqual(self.paths.wrapper.read_text(encoding="utf-8"), before)

    @unittest.skipUnless(os.name == "posix", "Real tty read/write requires Linux")
    def test_open_terminal_supports_nonseekable_tty_without_stdin(self):
        import builtins
        import pty
        master, slave = pty.openpty()
        device = os.ttyname(slave)
        original_open = builtins.open
        def open_tty(path, *args, **kwargs):
            self.assertEqual(path, "/dev/tty")
            return original_open(device, *args, **kwargs)
        try:
            with patch.object(console, "open", side_effect=open_tty, create=True):
                with console._open_terminal() as terminal:
                    terminal.write("menu ready\n")
                    terminal.flush()
                    os.write(master, b"0\n")
                    self.assertEqual(terminal.readline().strip(), "0")
        finally:
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
