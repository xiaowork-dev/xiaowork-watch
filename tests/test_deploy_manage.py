"""Offline deployment tests. Copy to tests/test_deploy_manage.py unchanged."""
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import os
import subprocess
import sys
import time
import stat
import unittest
from unittest.mock import patch
import hashlib

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "manage.py"
if not SOURCE.exists():
    SOURCE = HERE.parent / "scripts" / "deploy" / "manage.py"
SPEC = importlib.util.spec_from_file_location("deploy_manage", str(SOURCE))
manage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage)

SHA_A, SHA_B = "a" * 40, "b" * 40


def archive_bytes(sha, extra=None, metadata_sha=None):
    files = {
        "index.html": b"<!doctype html><html>static prototype</html>",
        "release.json": json.dumps({"schema": 1, "commit": metadata_sha or sha,
                                    "version": "0.1.0", "kind": "frontend-prototype"}).encode(),
        ".deploy/manage.py": b"# deployment manager fixture\n",
        "assets/app-" + sha[0] + ".js": ("console.log('" + sha[0] + "')").encode(),
        "favicon.svg": b"<svg></svg>",
    }
    files.update(extra or {})
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
    return output.getvalue()


def release_fixture(sha):
    tag = "web-" + sha
    return {"tag_name": tag, "draft": False, "prerelease": False, "assets": [
        {"name": name, "state": "uploaded", "browser_download_url":
         "https://github.com/" + manage.REPOSITORY + "/releases/download/" + tag + "/" + name}
        for name in (manage.ARCHIVE_NAME, manage.CHECKSUM_NAME)]}


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / manage.MARKER).write_text(manage.MARKER_VALUE, encoding="ascii")
        self.write_config()
        self.manager = manage.Manager(self.root)

    def write_config(self, auto=True, health=""):
        (self.root / "config.json").write_text(json.dumps({"autoUpdate": auto, "healthUrl": health}), encoding="utf-8")

    def fetcher(self, sha, archive=None, bad_checksum=False, release=None):
        bundle = archive if archive is not None else archive_bytes(sha)
        digest = "0" * 64 if bad_checksum else hashlib.sha256(bundle).hexdigest()
        metadata = release or release_fixture(sha)
        def fetch(url, limit, api=False):
            if api:
                return json.dumps(metadata).encode()
            if url.endswith(manage.CHECKSUM_NAME):
                return (digest + "  " + manage.ARCHIVE_NAME + "\n").encode()
            if url.endswith(manage.ARCHIVE_NAME):
                return bundle
            raise AssertionError("Unexpected URL: " + url)
        return fetch

    def require_symlinks(self):
        link = self.root / "symlink-probe"
        try:
            link.symlink_to(self.root / manage.MARKER)
            link.unlink()
        except OSError as error:
            self.skipTest("Host cannot create symlinks: " + str(error))

    def deploy(self, sha, **options):
        self.require_symlinks()
        with patch.object(manage, "_fetch", side_effect=self.fetcher(sha, **options)):
            return self.manager.install_or_update()

    def test_marker_required_before_creating_directories(self):
        (self.root / manage.MARKER).unlink()
        other = self.root / "unmanaged"
        other.mkdir()
        with self.assertRaises(manage.DeploymentError):
            manage.Manager(other)
        self.assertFalse((other / "releases").exists())

    def test_checksum_failure_cannot_activate_or_record_install(self):
        with patch.object(manage, "_fetch", side_effect=self.fetcher(SHA_A, bad_checksum=True)):
            with self.assertRaises(manage.DeploymentError):
                self.manager.install_or_update()
        self.assertFalse((self.root / "current").exists())
        self.assertFalse((self.root / "installed.json").exists())
        self.assertFalse((self.root / "releases" / SHA_A).exists())

    def test_release_commit_mismatch_rejected(self):
        bundle = archive_bytes(SHA_A, metadata_sha=SHA_B)
        with patch.object(manage, "_fetch", side_effect=self.fetcher(SHA_A, archive=bundle)):
            with self.assertRaises(manage.DeploymentError):
                self.manager.install_or_update()
        self.assertFalse((self.root / "releases" / SHA_A).exists())

    def test_wrong_release_kind_rejected(self):
        bundle = archive_bytes(SHA_A, extra={"release.json": json.dumps({
            "commit": SHA_A, "version": "1", "kind": "backend"}).encode()})
        with patch.object(manage, "_fetch", side_effect=self.fetcher(SHA_A, archive=bundle)):
            with self.assertRaises(manage.DeploymentError):
                self.manager.install_or_update()

    def test_release_schema_must_be_integer_one(self):
        for schema in (None, 2, True, "1"):
            with self.subTest(schema=schema):
                bundle = archive_bytes(SHA_A, extra={"release.json": json.dumps({
                    "schema": schema, "commit": SHA_A, "version": "1", "kind": "frontend-prototype"}).encode()})
                with patch.object(manage, "_fetch", side_effect=self.fetcher(SHA_A, archive=bundle)):
                    with self.assertRaises(manage.DeploymentError):
                        self.manager.install_or_update()

    def test_wrong_asset_source_rejected_before_download(self):
        release = release_fixture(SHA_A)
        release["assets"][0]["browser_download_url"] = "https://evil.invalid/archive.tar.gz"
        fetch = self.fetcher(SHA_A, release=release)
        with patch.object(manage, "_fetch", side_effect=fetch) as network:
            with self.assertRaises(manage.DeploymentError):
                self.manager.install_or_update()
        self.assertEqual(network.call_count, 1)

    def test_full_sha_release_only(self):
        for tag in ("v1.0.0", "web-abcdef", "web-" + "a" * 40 + "/../x"):
            with self.subTest(tag=tag), self.assertRaises(manage.DeploymentError):
                manage._release(tag)

    def test_pinned_install_uses_tag_endpoint(self):
        self.require_symlinks()
        with patch.object(manage, "_fetch", side_effect=self.fetcher(SHA_A)) as network:
            self.manager.install_or_update(tag="web-" + SHA_A)
        self.assertEqual(network.call_args_list[0].args[0], manage.API_BASE + "/tags/web-" + SHA_A)

    def test_update_keeps_old_assets_at_same_public_paths(self):
        self.deploy(SHA_A)
        self.deploy(SHA_B)
        self.assertEqual(self.manager.status()["current"], SHA_B)
        self.assertEqual(self.manager.status()["previous"], SHA_A)
        for sha in (SHA_A, SHA_B):
            self.assertTrue((self.root / "shared" / "assets" / ("app-" + sha[0] + ".js")).is_file())

    def test_existing_asset_path_cannot_change_bytes(self):
        self.deploy(SHA_A)
        with patch.object(manage, "_fetch", side_effect=self.fetcher(SHA_B, archive=archive_bytes(
                SHA_B, extra={"assets/app-a.js": b"different"}))):
            with self.assertRaises(manage.DeploymentError):
                self.manager.install_or_update()
        self.assertEqual(self.manager.status()["current"], SHA_A)
        self.assertEqual((self.root / "shared/assets/app-a.js").read_bytes(), b"console.log('a')")

    def test_same_release_is_not_overwritten(self):
        self.deploy(SHA_A)
        original = (self.root / "releases" / SHA_A / "index.html").read_bytes()
        with patch.object(manage, "_fetch", side_effect=self.fetcher(SHA_A, archive=archive_bytes(
                SHA_A, extra={"index.html": b"changed"}))) as network:
            self.manager.install_or_update()
        self.assertEqual(network.call_count, 1)  # Metadata only; reuse immutable local release.
        self.assertEqual((self.root / "releases" / SHA_A / "index.html").read_bytes(), original)

    def test_rollback_pauses_timer_and_manual_update_resumes(self):
        self.deploy(SHA_A)
        self.deploy(SHA_B)
        self.manager.rollback()
        self.assertEqual(self.manager.status()["current"], SHA_A)
        self.assertFalse(self.manager.status()["autoUpdate"])
        with patch.object(manage, "_fetch", side_effect=AssertionError("No network while paused")):
            self.assertEqual(self.manager.install_or_update(automatic=True)["status"], "paused")
        self.deploy(SHA_B)
        self.assertEqual(self.manager.status()["current"], SHA_B)
        self.assertTrue(self.manager.status()["autoUpdate"])

    def test_failed_update_restores_current_previous_and_install_record(self):
        self.deploy(SHA_A)
        installed = (self.root / "installed.json").read_bytes()
        with patch.object(manage, "_fetch", side_effect=self.fetcher(SHA_B)), patch.object(
                self.manager, "_health", side_effect=manage.DeploymentError("Simulated Nginx failure")):
            with self.assertRaises(manage.DeploymentError):
                self.manager.install_or_update()
        self.assertEqual(self.manager.status()["current"], SHA_A)
        self.assertIsNone(self.manager.status()["previous"])
        self.assertEqual((self.root / "installed.json").read_bytes(), installed)

    def test_failed_first_install_has_no_installed_marker(self):
        self.require_symlinks()
        with patch.object(manage, "_fetch", side_effect=self.fetcher(SHA_A)), patch.object(
                self.manager, "_health", side_effect=manage.DeploymentError("Simulated Nginx failure")):
            with self.assertRaises(manage.DeploymentError):
                self.manager.install_or_update()
        self.assertEqual(self.manager.status()["status"], "not-installed")
        self.assertFalse((self.root / "installed.json").exists())

    def test_unmanaged_current_directory_is_not_removed(self):
        current = self.root / "current"
        current.mkdir()
        (current / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(manage.DeploymentError):
            self.manager._switch("current", SHA_A)
        self.assertEqual((current / "keep.txt").read_text(), "keep")

    def test_outside_current_symlink_is_rejected(self):
        self.require_symlinks()
        (self.root / "current").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(manage.DeploymentError):
            self.manager.status()


class ArchiveTests(unittest.TestCase):
    def extract_members(self, members):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path, destination = root / "test.tar.gz", root / "stage"
            destination.mkdir()
            with tarfile.open(str(archive_path), "w:gz") as archive:
                for item, content in members:
                    archive.addfile(item, io.BytesIO(content) if content is not None else None)
            manage._extract(archive_path, destination)

    def test_absolute_traversal_and_foreign_paths(self):
        for name in ("../escape", "/etc/x", "./../escape", "a/../../escape", "C:/x", "a\\b", "a\nb"):
            with self.subTest(name=name), self.assertRaises(manage.DeploymentError):
                manage._member_path(name)

    def test_links_and_special_files_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE):
            item = tarfile.TarInfo("unsafe")
            item.type = kind
            item.linkname = "../../etc"
            with self.subTest(kind=kind), self.assertRaises(manage.DeploymentError):
                self.extract_members([(item, None)])

    def test_duplicate_normalized_path_rejected(self):
        first, second = tarfile.TarInfo("a.txt"), tarfile.TarInfo("./a.txt")
        first.size = second.size = 1
        with self.assertRaises(manage.DeploymentError):
            self.extract_members([(first, b"a"), (second, b"b")])

    def test_file_parent_collision_rejected(self):
        first, second = tarfile.TarInfo("a"), tarfile.TarInfo("a/b")
        first.size = second.size = 1
        with self.assertRaises(manage.DeploymentError):
            self.extract_members([(first, b"a"), (second, b"b")])

    def test_size_limits(self):
        item = tarfile.TarInfo("large")
        item.size = 3
        with patch.object(manage, "MAX_FILE", 2), self.assertRaises(manage.DeploymentError):
            self.extract_members([(item, b"abc")])
        with patch.object(manage, "MAX_EXPANDED", 2), self.assertRaises(manage.DeploymentError):
            self.extract_members([(item, b"abc")])

    def test_root_directory_entries_count_toward_limit(self):
        item = tarfile.TarInfo("./")
        item.type = tarfile.DIRTYPE
        with patch.object(manage, "MAX_MEMBERS", 1), self.assertRaises(manage.DeploymentError):
            self.extract_members([(item, None), (item, None)])

    def test_checksum_filename_is_bound_to_expected_archive(self):
        with self.assertRaises(manage.DeploymentError):
            manage._checksum(("a" * 64 + " other.tar.gz").encode())
        self.assertEqual(manage._checksum(("A" * 64 + " *" + manage.ARCHIVE_NAME).encode()), "a" * 64)

    @unittest.skipUnless(os.name == "posix", "Directory permission semantics require Linux")
    def test_umask_077_still_creates_nginx_readable_release_and_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_umask = os.umask(0o077)
            try:
                (root / manage.MARKER).write_text(manage.MARKER_VALUE, encoding="ascii")
                manager = manage.Manager(root)
                archive_path, destination = root / "web.tar.gz", root / "stage"
                archive_path.write_bytes(archive_bytes(SHA_A, extra={
                    "assets/nested/deep/chunk.js": b"public chunk"}))
                destination.mkdir()
                manage._extract(archive_path, destination)
                manager._assets(destination)
                directories = [destination, destination / ".deploy", destination / "assets",
                               destination / "assets/nested", destination / "assets/nested/deep",
                               manager.releases, root / "shared", manager.shared,
                               manager.shared / "nested", manager.shared / "nested/deep"]
                for directory in directories:
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755, str(directory))
                files = [destination / "index.html", destination / ".deploy/manage.py",
                         destination / "assets/nested/deep/chunk.js",
                         manager.shared / "nested/deep/chunk.js"]
                for filename in files:
                    self.assertEqual(stat.S_IMODE(filename.stat().st_mode), 0o644, str(filename))
            finally:
                os.umask(old_umask)


class HttpTests(unittest.TestCase):
    class Response:
        def __init__(self, payload, address="http://127.0.0.1:8088/release.json"):
            self.payload, self.address = payload, address
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def geturl(self):
            return self.address
        def read(self, limit):
            return json.dumps(self.payload).encode()[:limit]

    def health_manager(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / manage.MARKER).write_text(manage.MARKER_VALUE, encoding="ascii")
        return manage.Manager(root)

    def test_health_requires_matching_commit_and_kind(self):
        manager = self.health_manager()
        config = {"healthUrl": "http://127.0.0.1:8088/release.json"}
        with patch.object(manage, "_open_health", return_value=self.Response({
                "schema": 1, "commit": SHA_A, "kind": "frontend-prototype"})):
            manager._health(SHA_A, config)
        with patch.object(manage, "_open_health", return_value=self.Response({
                "schema": 1, "commit": SHA_B, "kind": "frontend-prototype"})), patch.object(manage.time, "sleep"):
            with self.assertRaises(manage.DeploymentError):
                manager._health(SHA_A, config)

    def test_remote_redirect_rejected_before_following(self):
        redirect = manage._RemoteRedirect(False)
        request = manage.Request("https://github.com/example")
        for address in ("http://github.com/example", "https://evil.invalid/web.tar.gz", "http://127.0.0.1/private"):
            with self.subTest(address=address), self.assertRaises(manage.DeploymentError):
                redirect.redirect_request(request, None, 302, "Found", {}, address)

    def test_health_redirect_rejected_before_following(self):
        redirect = manage._LocalRedirect()
        request = manage.Request("http://127.0.0.1:8088/release.json")
        with self.assertRaises(manage.DeploymentError):
            redirect.redirect_request(request, None, 302, "Found", {}, "https://evil.invalid/data")

    def test_health_config_rejects_nonlocal_url(self):
        manager = self.health_manager()
        (manager.root / "config.json").write_text(json.dumps({
            "healthUrl": "http://remote.invalid/release.json", "autoUpdate": True}), encoding="utf-8")
        with self.assertRaises(manage.DeploymentError):
            manager._config()

    def test_health_request_uses_optional_host_without_changing_loopback_url(self):
        manager = self.health_manager()
        payload = {"schema": 1, "commit": SHA_A, "kind": "frontend-prototype"}
        config = {"healthUrl": "http://127.0.0.1:80/release.json", "healthHost": "watch.example.com"}
        with patch.object(manage, "_open_health", return_value=self.Response(payload)) as network:
            manager._health(SHA_A, config)
        request = network.call_args.args[0]
        self.assertEqual(request.get_header("Host"), "watch.example.com")
        self.assertTrue(request.full_url.startswith("http://127.0.0.1:80/release.json?"))
        config.pop("healthHost")
        with patch.object(manage, "_open_health", return_value=self.Response(payload)) as network:
            manager._health(SHA_A, config)
        self.assertIsNone(network.call_args.args[0].get_header("Host"))

    def test_health_host_rejects_header_injection_and_non_hostnames(self):
        manager = self.health_manager()
        for host in ("example.com\r\nX-Injected: value", "example.com\n", "example.com:80", " example.com",
                     "example.com/path", "bad_host", "-example.com", "example..com", "a" * 64 + ".com", None):
            with self.subTest(host=host):
                config = {"healthUrl": "http://127.0.0.1:80/release.json", "healthHost": host}
                (manager.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(manage.DeploymentError):
                    manager._config()
                with patch.object(manage, "_open_health") as network:
                    with self.assertRaises(manage.DeploymentError):
                        manager._health(SHA_A, config)
                    network.assert_not_called()


class LockTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "Production lock semantics require Linux flock")
    def test_two_processes_cannot_hold_deployment_lock_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "lock"
            code = ("import importlib.util,time; from pathlib import Path; "
                    "s=importlib.util.spec_from_file_location('manage'," + repr(str(SOURCE)) + "); "
                    "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                    "ctx=m._locked(Path(" + repr(str(lock)) + ")); "
                    "ctx.__enter__(); print('locked',flush=True); time.sleep(0.4); ctx.__exit__(None,None,None)")
            child = subprocess.Popen([sys.executable, "-B", "-c", code], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, universal_newlines=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                started = time.monotonic()
                with manage._locked(lock):
                    self.assertGreaterEqual(time.monotonic() - started, 0.25)
                self.assertEqual(child.wait(timeout=5), 0)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                child.stdout.close()
                child.stderr.close()


if __name__ == "__main__":
    unittest.main()
