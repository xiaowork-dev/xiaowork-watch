"""Short-menu entry fixtures use only temporary files and mocked system commands."""
import contextlib
from dataclasses import fields
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parent.parent / "scripts" / "deploy" / "console.py"
SPEC = importlib.util.spec_from_file_location("deploy_shortcut_console", str(SOURCE))
console = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = console
SPEC.loader.exec_module(console)


class Manager:
    def __init__(self, root):
        self.root = root

    def _config(self):
        return json.loads((self.root / "config.json").read_text(encoding="utf-8"))

    def _pointed_sha(self, name):
        pointer = self.root / name
        return pointer.resolve().name if pointer.is_symlink() else None


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root, self.system = self.base / "install", self.base / "system"
        self.root.mkdir()
        self.system.mkdir()
        self.manager = Manager(self.root)
        self.paths = console.Paths(**{field.name: self.system / field.name for field in fields(console.Paths)})
        (self.root / "config.json").write_text(json.dumps({"autoUpdate": True}), encoding="utf-8")
        (self.root / ".xiaowork-watch-managed").write_text(console.MARKER_VALUE, encoding="utf-8")
        self.paths.wrapper.write_text('#!/bin/sh\n' + console._header(self.manager)
            + 'exec /usr/bin/python3 "' + str(self.root) + '/control/manage.py" --root "'
            + str(self.root) + '" "$@"\n', encoding="utf-8")

    @contextlib.contextmanager
    def locked(self, path):
        self.assertEqual(path, self.root / ".deploy.lock")
        yield

    def sync(self):
        return console._sync_shortcut(self.manager, self.paths)

    def uninstall(self):
        with patch.object(console, "_run", return_value=""), patch.object(console, "_unit_state", return_value=False):
            return console.uninstall(self.manager, confirm=True, paths=self.paths, locked=self.locked)

    def test_shortcut_forwards_all_arguments_and_has_exact_root_marker(self):
        self.assertTrue(self.sync())
        expected = ('#!/bin/sh\n' + console._header(self.manager)
                    + 'exec /usr/local/bin/xiaowork-watch "$@"\n')
        self.assertEqual(self.paths.shortcut.read_text(encoding="utf-8"), expected)
        self.assertTrue(console._owned(self.paths.shortcut, self.manager, "shortcut", self.paths))
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.paths.shortcut.stat().st_mode), 0o755)
        with patch.object(console, "_write") as write:
            self.assertTrue(self.sync())
        if os.name == "posix":
            write.assert_not_called()

    def test_removed_shortcut_is_recreated_for_existing_install(self):
        self.assertTrue(self.sync())
        self.paths.shortcut.unlink()
        self.assertTrue(self.sync())
        self.assertEqual(self.paths.shortcut.read_text(encoding="utf-8"), console._shortcut_text(self.manager))

    def test_new_shortcut_is_published_without_replace_or_temporary_file_residue(self):
        with patch.object(console.os, "replace") as replace:
            self.assertTrue(self.sync())
        replace.assert_not_called()
        self.assertEqual(self.paths.shortcut.read_text(encoding="utf-8"), console._shortcut_text(self.manager))
        self.assertEqual(list(self.system.glob(".xiaowork-watch-*")), [])

    def test_intervening_foreign_file_is_preserved_and_temporary_file_is_cleaned(self):
        original_link = console.os.link
        foreign = '#!/bin/sh\nexec /another/program "$@"\n'
        def claim_destination(source, destination):
            self.assertEqual(Path(destination), self.paths.shortcut)
            self.assertTrue(Path(source).is_file())
            self.paths.shortcut.write_text(foreign, encoding="utf-8")
            return original_link(source, destination)
        with patch.object(console.os, "link", side_effect=claim_destination), patch("sys.stderr", new=io.StringIO()) as error:
            self.assertFalse(self.sync())
        self.assertIn("sudo xiaowork-watch", error.getvalue())
        self.assertEqual(self.paths.shortcut.read_text(encoding="utf-8"), foreign)
        self.assertTrue(self.paths.wrapper.exists())
        self.assertEqual(list(self.system.glob(".xiaowork-watch-*")), [])

    @unittest.skipUnless(os.name == "posix", "Executable mode repair requires POSIX")
    def test_nonexecutable_owned_shortcut_is_repaired(self):
        self.assertTrue(self.sync())
        self.paths.shortcut.chmod(0o644)
        self.assertTrue(self.sync())
        self.assertEqual(stat.S_IMODE(self.paths.shortcut.stat().st_mode), 0o755)

    def test_foreign_shortcut_is_not_overwritten_or_removed(self):
        foreign = '#!/bin/sh\nexec /some/other/application "$@"\n'
        self.paths.shortcut.write_text(foreign, encoding="utf-8")
        with patch("sys.stderr", new=io.StringIO()) as error:
            self.assertFalse(self.sync())
        self.assertIn("sudo xiaowork-watch", error.getvalue())
        self.assertEqual(self.paths.shortcut.read_text(encoding="utf-8"), foreign)
        self.assertEqual(self.uninstall()["status"], "uninstalled")
        self.assertEqual(self.paths.shortcut.read_text(encoding="utf-8"), foreign)
        self.assertFalse(self.paths.wrapper.exists())

    def test_other_root_shortcut_is_not_removed_with_this_install(self):
        other = Manager(self.base / "another-install")
        foreign = console._shortcut_text(other)
        self.paths.shortcut.write_text(foreign, encoding="utf-8")
        with patch("sys.stderr", new=io.StringIO()):
            self.assertFalse(self.sync())
        self.assertEqual(self.uninstall()["status"], "uninstalled")
        self.assertEqual(self.paths.shortcut.read_text(encoding="utf-8"), foreign)

    def test_binary_shortcut_is_preserved_without_blocking_sync_or_uninstall(self):
        foreign = b"\x7fELF\xff\x00binary software"
        self.paths.shortcut.write_bytes(foreign)
        with patch("sys.stderr", new=io.StringIO()):
            self.assertFalse(self.sync())
        self.assertEqual(self.uninstall()["status"], "uninstalled")
        self.assertEqual(self.paths.shortcut.read_bytes(), foreign)

    def test_duplicate_root_marker_or_different_command_does_not_prove_ownership(self):
        for content in (console._shortcut_text(self.manager) + console.ROOT_PREFIX + str(self.root) + "\n",
                        console._shortcut_text(self.manager).replace("exec /usr/local/bin/xiaowork-watch", "exec /other/program")):
            with self.subTest(content=content):
                self.paths.shortcut.write_text(content, encoding="utf-8")
                self.assertFalse(console._owned(self.paths.shortcut, self.manager, "shortcut", self.paths))
                with patch("sys.stderr", new=io.StringIO()):
                    self.assertFalse(self.sync())
                self.assertEqual(self.paths.shortcut.read_text(encoding="utf-8"), content)

    def test_shortcut_install_failure_preserves_old_file_and_long_command(self):
        self.paths.shortcut.write_text(console._shortcut_text(self.manager), encoding="utf-8")
        before = self.paths.shortcut.read_bytes()
        with patch.object(console.stat, "S_IMODE", return_value=0o644), patch.object(console.os, "replace", side_effect=OSError("fixture replace failed")), patch("sys.stderr", new=io.StringIO()):
            self.assertFalse(self.sync())
        self.assertEqual(self.paths.shortcut.read_bytes(), before)
        self.assertTrue(self.paths.wrapper.exists())

    def test_owned_shortcut_is_removed_on_uninstall(self):
        self.assertTrue(self.sync())
        self.assertEqual(self.uninstall()["status"], "uninstalled")
        self.assertFalse(self.paths.shortcut.exists())

    def test_uninstall_failure_restores_original_shortcut(self):
        self.assertTrue(self.sync())
        before = self.paths.shortcut.read_bytes()
        self.paths.service.write_text(console._header(self.manager) + "[Service]\n", encoding="utf-8")
        def fail(arguments, **kwargs):
            if arguments == ["systemctl", "daemon-reload"]:
                raise console.ConsoleError("fixture reload failed")
            return ""
        with patch.object(console, "_run", side_effect=fail), patch.object(console, "_unit_state", return_value=False), self.assertRaises(console.ConsoleError):
            console.uninstall(self.manager, confirm=True, paths=self.paths, locked=self.locked)
        self.assertEqual(self.paths.shortcut.read_bytes(), before)
        self.assertTrue(self.paths.wrapper.exists())
        self.assertTrue(self.manager._config()["autoUpdate"])

    @unittest.skipUnless(os.name == "posix", "Symbolic-link preservation requires POSIX")
    def test_foreign_shortcut_symlink_and_its_target_are_preserved(self):
        target = self.system / "other-command"
        target.write_text("other software", encoding="utf-8")
        self.paths.shortcut.symlink_to(target)
        with patch("sys.stderr", new=io.StringIO()):
            self.assertFalse(self.sync())
        self.assertEqual(self.uninstall()["status"], "uninstalled")
        self.assertTrue(self.paths.shortcut.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "other software")

    @unittest.skipUnless(os.name == "posix", "Control-link refresh requires POSIX")
    def test_control_refresh_installs_shortcut_only_after_success(self):
        sha = "a" * 40
        release = self.root / "releases" / sha / ".deploy"
        release.mkdir(parents=True)
        for name in ("manage.py", "console.py"):
            (release / name).write_text("# fixture\n", encoding="utf-8")
        (self.root / "current").symlink_to(Path("releases") / sha, target_is_directory=True)
        self.assertTrue(console.ensure_control_entry(self.manager, paths=self.paths, locked=self.locked))
        self.assertTrue(console._owned(self.paths.shortcut, self.manager, "shortcut", self.paths))
        before = self.paths.shortcut.read_bytes()
        new_sha = "b" * 40
        new_release = self.root / "releases" / new_sha / ".deploy"
        new_release.mkdir(parents=True)
        for name in ("manage.py", "console.py"):
            (new_release / name).write_text("# fixture\n", encoding="utf-8")
        (self.root / "current").unlink()
        (self.root / "current").symlink_to(Path("releases") / new_sha, target_is_directory=True)
        self.paths.wrapper.write_text(self.paths.wrapper.read_text(encoding="utf-8") + "# trigger refresh\n", encoding="utf-8")
        original_write = console._write
        def fail_wrapper(path, text, mode=0o644):
            if path == self.paths.wrapper and "# trigger refresh" not in text:
                raise OSError("fixture wrapper repair failed")
            return original_write(path, text, mode)
        with patch.object(console, "_write", side_effect=fail_wrapper), self.assertRaises(OSError):
            console.ensure_control_entry(self.manager, paths=self.paths, locked=self.locked, refresh=True)
        self.assertEqual((self.root / "control").resolve(), release)
        self.assertEqual(self.paths.shortcut.read_bytes(), before)

    @unittest.skipUnless(os.name == "posix", "Existing management link requires POSIX")
    def test_existing_control_entry_repairs_shortcut_without_a_release_update(self):
        sha = "a" * 40
        release = self.root / "releases" / sha / ".deploy"
        release.mkdir(parents=True)
        for name in ("manage.py", "console.py"):
            (release / name).write_text("# fixture\n", encoding="utf-8")
        (self.root / "current").symlink_to(Path("releases") / sha, target_is_directory=True)
        (self.root / "control").symlink_to(Path("releases") / sha / ".deploy", target_is_directory=True)
        self.assertFalse(self.paths.shortcut.exists())
        # manage.py calls this before processing status/menu, with refresh=False.
        self.assertTrue(console.ensure_control_entry(self.manager, paths=self.paths, locked=self.locked))
        self.assertTrue(console._owned(self.paths.shortcut, self.manager, "shortcut", self.paths))
        self.assertEqual((self.root / "control").resolve(), release)


if __name__ == "__main__":
    unittest.main()
