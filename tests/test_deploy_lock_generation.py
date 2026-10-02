"""Offline lock-generation and uninstall CLI tests; no system changes.

Run next to scratch manage.py or copy to tests/test_deploy_lock_generation.py.
The stale-waiter tests use spawned Linux processes with a five-second ceiling.
"""
import contextlib
import importlib.util
import io
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch


HERE = Path(__file__).resolve().parent
SOURCE = HERE / "manage.py"
if not SOURCE.exists():
    SOURCE = HERE.parent / "scripts" / "deploy" / "manage.py"


def _load_manager(source, name):
    specification = importlib.util.spec_from_file_location(name, str(source))
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


manage = _load_manager(SOURCE, "deploy_lock_generation_manager")


def _stale_waiter(source, lock_path, connection):
    """Tell the parent when the old fd is open and the blocking flock begins."""
    import fcntl
    module = _load_manager(Path(source), "deploy_lock_waiter_manager")
    original_flock = fcntl.flock

    def observed_flock(descriptor, operation):
        if operation == fcntl.LOCK_EX:
            connection.send(("waiting", None))
        return original_flock(descriptor, operation)

    try:
        with patch.object(fcntl, "flock", side_effect=observed_flock), contextlib.redirect_stdout(io.StringIO()):
            with module._locked(Path(lock_path)):
                connection.send(("entered", None))
                Path(lock_path).parent.joinpath("mutation.txt").write_text("stale operation", encoding="utf-8")
        connection.send(("completed", None))
    except module.DeploymentError as error:
        connection.send(("rejected", str(error)))
    except BaseException as error:
        connection.send(("unexpected-error", repr(error)))
    finally:
        connection.close()


class LockGenerationTests(unittest.TestCase):
    def test_generic_lock_without_install_marker_remains_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "generic-operation.lock"
            self.assertFalse((root / manage.MARKER).exists())
            for _ in range(2):
                with manage._locked(lock):
                    self.assertTrue(lock.is_file())
                    self.assertFalse((root / manage.MARKER).exists())

    def test_deployment_lock_without_marker_does_not_enter_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            entered = False
            with self.assertRaises(manage.DeploymentError):
                with manage._locked(Path(temporary) / ".deploy.lock"):
                    entered = True
            self.assertFalse(entered)

    def _assert_stale_waiter_rejected(self, replacement):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "installation"
            root.mkdir()
            (root / manage.MARKER).write_text(manage.MARKER_VALUE, encoding="ascii")
            lock = root / ".deploy.lock"
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=_stale_waiter, args=(str(SOURCE), str(lock), sender))
            started = False
            # Cleanup has at most one additional second, including kill fallback.
            deadline = time.monotonic() + 4.0
            replacement_snapshot = None
            replacement_identity = None
            retired = base / "retired-installation"
            try:
                with manage._locked(lock):
                    process.start()
                    started = True
                    sender.close()
                    self.assertTrue(receiver.poll(max(0, deadline - time.monotonic())), "Waiter did not reach the held lock")
                    self.assertEqual(receiver.recv()[0], "waiting")
                    if replacement == "marker-removed":
                        (root / manage.MARKER).unlink()
                    else:
                        root.rename(retired)
                        if replacement == "deleted":
                            # This fixture has exactly two owned files; no recursive deletion.
                            (retired / ".deploy.lock").unlink()
                            (retired / manage.MARKER).unlink()
                            retired.rmdir()
                        elif replacement == "symlink-to-retired":
                            root.symlink_to(retired, target_is_directory=True)
                        elif replacement == "recreated":
                            root.mkdir()
                            (root / manage.MARKER).write_text(manage.MARKER_VALUE, encoding="ascii")
                            (root / ".deploy.lock").write_bytes(b"replacement-lock")
                            (root / "config.json").write_text('{"keep":"new installation"}', encoding="utf-8")
                            replacement_snapshot = {path.name: path.read_bytes() for path in root.iterdir()}
                            identity = root.stat()
                            replacement_identity = (identity.st_dev, identity.st_ino)
                # Parent releases the old fd only after replacing/removing the root.
                self.assertTrue(receiver.poll(max(0, deadline - time.monotonic())), "Waiter did not finish after the old lock was released")
                event, detail = receiver.recv()
                self.assertEqual(event, "rejected", "Stale waiter entered mutation: " + str(detail))
                process.join(timeout=max(0, deadline - time.monotonic()))
                self.assertFalse(process.is_alive(), "Waiter failed to exit within the bounded timeout")
                self.assertEqual(process.exitcode, 0)
                self.assertFalse((root / "mutation.txt").exists())
                self.assertFalse((retired / "mutation.txt").exists())
                if replacement == "recreated":
                    self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, replacement_snapshot)
                    identity = root.stat()
                    self.assertEqual((identity.st_dev, identity.st_ino), replacement_identity)
                elif replacement in ("renamed", "deleted"):
                    self.assertFalse(root.exists(), "The old waiter recreated the removed installation")
            finally:
                sender.close()
                receiver.close()
                if started and process.is_alive():
                    process.terminate()
                    process.join(timeout=0.5)
                    if process.is_alive():
                        process.kill()
                        process.join(timeout=0.5)
                if started and not process.is_alive():
                    process.close()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Old-flock waiter regression requires Linux")
    def test_waiter_rejects_renamed_installation_before_mutation(self):
        self._assert_stale_waiter_rejected("renamed")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Old-flock waiter regression requires Linux")
    def test_waiter_rejects_deleted_installation_before_mutation(self):
        self._assert_stale_waiter_rejected("deleted")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Old-flock waiter regression requires Linux")
    def test_waiter_cannot_mutate_recreated_installation_with_new_marker_and_lock(self):
        self._assert_stale_waiter_rejected("recreated")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Old-flock waiter regression requires Linux")
    def test_waiter_rejects_removed_marker_even_when_root_and_lock_survive(self):
        self._assert_stale_waiter_rejected("marker-removed")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Old-flock symlink waiter regression requires Linux")
    def test_waiter_rejects_original_root_symlinked_back_to_retired_installation(self):
        self._assert_stale_waiter_rejected("symlink-to-retired")


class UninstallCliTests(unittest.TestCase):
    def _invoke(self, arguments):
        manager = types.SimpleNamespace(root=Path("/unused-offline-installation"))
        fake_console = types.ModuleType("xiaowork_watch_console")
        fake_console.ensure_control_entry = Mock(return_value=True)
        fake_console.run_uninstall_menu = Mock(return_value=23)
        fake_console.uninstall = Mock(return_value={"status": "uninstalled"})
        fake_loader = types.SimpleNamespace(exec_module=lambda module: None)
        specification = types.SimpleNamespace(name=fake_console.__name__, loader=fake_loader)
        with patch.object(manage, "Manager", return_value=manager), patch.object(manage, "_regular", return_value=True), patch.object(
                manage, "_locked") as locked, patch("importlib.util.spec_from_file_location", return_value=specification), patch(
                "importlib.util.module_from_spec", return_value=fake_console), patch.dict(sys.modules), contextlib.redirect_stdout(io.StringIO()):
            result = manage.main(["--root", str(manager.root)] + arguments)
        return result, manager, fake_console, locked

    def test_uninstall_without_confirm_uses_menu_including_purge_request(self):
        for arguments in (["uninstall"], ["uninstall", "--purge"]):
            with self.subTest(arguments=arguments):
                result, manager, console, locked = self._invoke(arguments)
                self.assertEqual(result, 23)
                console.run_uninstall_menu.assert_called_once_with(manager, locked)
                console.uninstall.assert_not_called()
                locked.assert_not_called()

    def test_confirmed_uninstall_defaults_to_preserving_data(self):
        result, manager, console, locked = self._invoke(["uninstall", "--confirm"])
        self.assertEqual(result, 0)
        console.uninstall.assert_called_once_with(manager, confirm=True, locked=locked, purge=False)
        console.run_uninstall_menu.assert_not_called()
        locked.assert_not_called()

    def test_confirmed_purge_is_forwarded_explicitly(self):
        result, manager, console, locked = self._invoke(["uninstall", "--confirm", "--purge"])
        self.assertEqual(result, 0)
        console.uninstall.assert_called_once_with(manager, confirm=True, locked=locked, purge=True)
        console.run_uninstall_menu.assert_not_called()
        locked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
