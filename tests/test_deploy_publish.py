"""Offline publication tests; compatible with scratch and repository test paths.

Run directly or use: python -m unittest discover -s tests -p test_deploy_publish.py
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import urllib.error


def load_publisher():
    test_path = Path(__file__).resolve()
    candidates = (
        test_path.with_name("publish_release.py"),
        test_path.parent.parent / "scripts" / "deploy" / "publish_release.py",
    )
    source_path = next((path for path in candidates if path.is_file()), None)
    if source_path is None:
        raise RuntimeError("Cannot find the deployment release publisher.")
    spec = importlib.util.spec_from_file_location("xiaowork_watch_deploy_publish", source_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publisher = load_publisher()
SHA = "a" * 40
TAG = "web-" + SHA
INFO = publisher.ReleaseInfo("0.4.3", "monitoring-server", SHA)


class MemoryPath:
    """Read-only, in-memory file substitute; no temporary files are created."""

    def __init__(self, content):
        self.content = content

    def is_file(self):
        return True

    def open(self, mode):
        if mode != "rb":
            raise AssertionError("Only read-only binary access is expected.")
        return io.BytesIO(self.content)


def make_artifacts():
    package = b"offline-package-content"
    checksum = (hashlib.sha256(package).hexdigest() + "  " + publisher.PACKAGE_NAME + "\r\n").encode("ascii")
    return publisher.prepare_artifacts(MemoryPath(package), MemoryPath(checksum))


def make_release_archive(metadata=None, *, content=None, name="release.json", duplicate=False, symlink=False):
    if content is None:
        metadata = metadata if metadata is not None else {"schema": 1, "version": INFO.version, "kind": INFO.kind, "commit": INFO.commit}
        content = json.dumps(metadata).encode("utf-8")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for _ in range(2 if duplicate else 1):
            entry = tarfile.TarInfo(name)
            if symlink:
                entry.type = tarfile.SYMTYPE
                entry.linkname = "elsewhere.json"
                archive.addfile(entry)
            else:
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
    return buffer.getvalue()


def asset_metadata(artifact, identifier, *, include_digest=True):
    result = {"id": identifier, "name": artifact.name, "size": artifact.size, "state": "uploaded", "label": artifact.label}
    if include_digest:
        result["digest"] = "sha256:" + artifact.sha256
    return result


class FakeGitHub:
    """Record all mutations while emulating a repository without network I/O."""

    def __init__(self, artifacts, *, existing=False, published=False, stale_on=None, missing_checksum=False, mismatched=False, no_digest=False, already_latest=False, by_tag_missing=False, wrong_tag_target=False, omit_final_checksum=False, corrupt_upload=False):
        self.artifacts = artifacts
        self.mutations = []
        self.requests = []
        self.main_checks = 0
        self.stale_on = stale_on
        self.digest_checks = 0
        self.release = None
        self.assets = []
        self.latest_tag = TAG if already_latest else None
        self.by_tag_missing = by_tag_missing
        self.wrong_tag_target = wrong_tag_target
        self.omit_final_checksum = omit_final_checksum
        self.corrupt_upload = corrupt_upload
        if existing:
            self.release = {
                "id": 13, "tag_name": TAG, "target_commitish": SHA,
                "draft": not published, "prerelease": False,
                "upload_url": "https://uploads.github.com/repos/owner/repo/releases/13/assets{?name,label}",
                **publisher.release_presentation(INFO),
            }
            self.assets = [asset_metadata(artifact, index + 1, include_digest=not no_digest) for index, artifact in enumerate(publisher.labeled_artifacts(artifacts, INFO))]
            if missing_checksum:
                self.assets = self.assets[:1]
            if mismatched:
                self.assets[0]["digest"] = "sha256:" + "d" * 64

    def json(self, method, path, payload=None, *, missing_ok=False):
        self.requests.append((method, path))
        if path == "/git/ref/heads/main":
            self.main_checks += 1
            current = "f" * 40 if self.stale_on and self.main_checks >= self.stale_on else SHA
            return {"object": {"type": "commit", "sha": current}}
        if path.startswith("/releases/tags/"):
            return None if self.by_tag_missing else self.release
        if path.startswith("/releases?per_page="):
            return [self.release] if self.release else []
        if method == "POST" and path == "/releases":
            if payload["draft"] is not True or payload["make_latest"] != "false" or payload["target_commitish"] != SHA:
                raise AssertionError("New release must remain a draft at the exact source commit.")
            self.mutations.append(("create", payload.copy()))
            self.release = dict(payload, id=13, upload_url="https://uploads.github.com/repos/owner/repo/releases/13/assets{?name,label}")
            return self.release.copy()
        if path.startswith("/git/ref/tags/"):
            if self.wrong_tag_target:
                return {"object": {"type": "commit", "sha": "f" * 40}}
            return None if self.release["draft"] else {"object": {"type": "commit", "sha": SHA}}
        if method == "GET" and path.startswith("/releases/13/assets?"):
            assets = self.assets
            if self.omit_final_checksum and any(kind == "upload" for kind, _ in self.mutations):
                assets = [asset for asset in assets if asset["name"] != publisher.CHECKSUM_NAME]
            return [asset.copy() for asset in assets]
        if method == "DELETE" and path.startswith("/releases/assets/"):
            identifier = int(path.rsplit("/", 1)[-1])
            self.mutations.append(("delete", identifier))
            self.assets = [asset for asset in self.assets if asset["id"] != identifier]
            return None
        if method == "PATCH" and path.startswith("/releases/assets/"):
            if set(payload) != {"label"}:
                raise AssertionError("Only asset display labels may be modified.")
            identifier = int(path.rsplit("/", 1)[-1])
            asset = next(asset for asset in self.assets if asset["id"] == identifier)
            self.mutations.append(("label", (identifier, payload.copy())))
            asset.update(payload)
            return asset.copy()
        if path == "/releases/latest":
            return {"tag_name": self.latest_tag} if self.latest_tag else None
        if method == "PATCH" and path == "/releases/13":
            if set(payload) - {"name", "body", "draft", "make_latest"}:
                raise AssertionError("Unexpected publication mutation.")
            publishing = "draft" in payload
            if publishing and (payload["draft"] is not False or payload.get("make_latest") != "true"):
                raise AssertionError("Unexpected publication semantics.")
            self.mutations.append(("publish" if publishing else "presentation", payload.copy()))
            self.release.update(payload)
            if publishing:
                self.latest_tag = TAG
            return self.release.copy()
        raise AssertionError((method, path, payload))

    def upload(self, release, artifact):
        if release["draft"] is not True:
            raise AssertionError("Published assets must never be uploaded again.")
        self.mutations.append(("upload", artifact.name))
        uploaded = asset_metadata(artifact, len(self.assets) + 10)
        if self.corrupt_upload:
            uploaded["digest"] = "sha256:" + "d" * 64
        self.assets.append(uploaded)
        return uploaded.copy()

    def asset_digest(self, asset_id, expected_size):
        self.digest_checks += 1
        asset = next(asset for asset in self.assets if asset["id"] == asset_id)
        artifact = next(artifact for artifact in self.artifacts if artifact.name == asset["name"])
        return artifact.size, artifact.sha256


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.artifacts = make_artifacts()
        self.stdout = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.stdout)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def fake(self, **kwargs):
        return FakeGitHub(self.artifacts, **kwargs)

    def kinds(self, api):
        return [kind for kind, _ in api.mutations]

    def test_new_release_uploads_all_assets_before_publication(self):
        api = self.fake()
        self.assertTrue(publisher.publish(api, SHA, self.artifacts, INFO))
        self.assertEqual(self.kinds(api), ["create", "upload", "upload", "publish"])
        self.assertFalse(api.release["draft"])

    def test_stale_main_skips_without_any_mutation(self):
        api = self.fake(stale_on=1)
        self.assertFalse(publisher.publish(api, SHA, self.artifacts, INFO))
        self.assertEqual(api.mutations, [])
        self.assertIn("no longer the current main HEAD", self.stdout.getvalue())

    def test_main_changes_before_creation_skips_empty_draft(self):
        api = self.fake(stale_on=2)
        self.assertFalse(publisher.publish(api, SHA, self.artifacts, INFO))
        self.assertEqual(api.mutations, [])

    def test_main_changes_before_publication_leaves_draft(self):
        api = self.fake(stale_on=3)
        self.assertFalse(publisher.publish(api, SHA, self.artifacts, INFO))
        self.assertTrue(api.release["draft"])
        self.assertNotIn("publish", self.kinds(api))

    def test_main_changes_after_asset_labels_leaves_existing_draft_unpublished(self):
        api = self.fake(existing=True, stale_on=3)
        for asset in api.assets:
            asset["label"] = None
        self.assertFalse(publisher.publish(api, SHA, self.artifacts, INFO))
        self.assertEqual(self.kinds(api), ["label", "label"])
        self.assertTrue(api.release["draft"])
        self.assertIsNone(api.latest_tag)

    def test_main_changes_after_published_asset_labels_does_not_change_release_or_latest(self):
        api = self.fake(existing=True, published=True, stale_on=4)
        api.release.update(name="Old display", body="Old description")
        for asset in api.assets:
            asset["label"] = None
        self.assertFalse(publisher.publish(api, SHA, self.artifacts, INFO))
        self.assertEqual(self.kinds(api), ["label", "label"])
        self.assertEqual(api.release["name"], "Old display")
        self.assertIsNone(api.latest_tag)

    def test_draft_assets_are_reused(self):
        api = self.fake(existing=True)
        self.assertTrue(publisher.publish(api, SHA, self.artifacts, INFO))
        self.assertEqual(self.kinds(api), ["publish"])

    def test_leftover_draft_is_found_in_authenticated_listing(self):
        api = self.fake(existing=True, by_tag_missing=True)
        self.assertTrue(publisher.publish(api, SHA, self.artifacts, INFO))
        self.assertEqual(self.kinds(api), ["publish"])

    def test_mismatched_draft_asset_is_repaired(self):
        api = self.fake(existing=True, mismatched=True)
        self.assertTrue(publisher.publish(api, SHA, self.artifacts, INFO))
        self.assertEqual(self.kinds(api), ["delete", "upload", "publish"])

    def test_missing_draft_asset_is_uploaded(self):
        api = self.fake(existing=True, missing_checksum=True)
        self.assertTrue(publisher.publish(api, SHA, self.artifacts, INFO))
        self.assertEqual(self.kinds(api), ["upload", "publish"])

    def test_missing_final_asset_prevents_publication(self):
        api = self.fake(omit_final_checksum=True)
        with self.assertRaises(publisher.PublishError):
            publisher.publish(api, SHA, self.artifacts, INFO)
        self.assertTrue(api.release["draft"])
        self.assertNotIn("publish", self.kinds(api))

    def test_corrupt_uploaded_asset_prevents_publication(self):
        api = self.fake(corrupt_upload=True)
        with self.assertRaises(publisher.PublishError):
            publisher.publish(api, SHA, self.artifacts, INFO)
        self.assertTrue(api.release["draft"])
        self.assertNotIn("publish", self.kinds(api))

    def test_matching_published_assets_are_never_overwritten(self):
        api = self.fake(existing=True, published=True)
        self.assertTrue(publisher.publish(api, SHA, self.artifacts, INFO))
        self.assertEqual(self.kinds(api), ["publish"])

    def test_already_latest_published_release_is_unchanged(self):
        api = self.fake(existing=True, published=True, already_latest=True)
        self.assertTrue(publisher.publish(api, SHA, self.artifacts, INFO))
        self.assertEqual(api.mutations, [])

    def test_new_release_title_and_upload_labels_show_actual_version(self):
        api = self.fake()
        self.assertTrue(publisher.publish(api, SHA, self.artifacts, INFO))
        self.assertEqual(api.release["name"], "xiaowork Watch v0.4.3（监控系统）")
        self.assertIn("网站监控主控", api.release["body"])
        self.assertEqual(api.release["tag_name"], TAG)
        self.assertEqual([asset["name"] for asset in api.assets], [publisher.PACKAGE_NAME, publisher.CHECKSUM_NAME])
        self.assertEqual([asset["label"] for asset in api.assets], ["v0.4.3 · 完整安装包", "v0.4.3 · SHA-256 校验文件"])

    def test_published_latest_legacy_presentation_is_updated_without_replacing_bytes(self):
        api = self.fake(existing=True, published=True, already_latest=True)
        api.release.update(name="Web prototype " + SHA[:12], body="Old prototype description")
        for asset in api.assets:
            asset["label"] = None
        original = [(asset["id"], asset["name"], asset["size"], asset["digest"]) for asset in api.assets]
        self.assertTrue(publisher.publish(api, SHA, self.artifacts, INFO))
        self.assertEqual(self.kinds(api), ["label", "label", "presentation"])
        self.assertEqual([(asset["id"], asset["name"], asset["size"], asset["digest"]) for asset in api.assets], original)
        self.assertFalse(api.release["draft"])
        self.assertEqual(api.latest_tag, TAG)
        self.assertTrue(publisher.publish(api, SHA, self.artifacts, INFO))
        self.assertEqual(self.kinds(api), ["label", "label", "presentation"])

    def test_changed_labels_are_not_written_until_all_published_bytes_match(self):
        api = self.fake(existing=True, published=True, already_latest=True)
        api.assets[0]["label"] = None
        api.assets[1]["digest"] = "sha256:" + "d" * 64
        with self.assertRaises(publisher.PublishError):
            publisher.publish(api, SHA, self.artifacts, INFO)
        self.assertEqual(api.mutations, [])

    def test_malformed_release_info_is_rejected_before_network_or_mutation(self):
        for info in [publisher.ReleaseInfo("bad", INFO.kind, SHA), publisher.ReleaseInfo(INFO.version, "unknown", SHA), publisher.ReleaseInfo(INFO.version, INFO.kind, "f" * 40)]:
            with self.subTest(info=info):
                api = self.fake()
                with self.assertRaises(publisher.PublishError):
                    publisher.publish(api, SHA, self.artifacts, info)
                self.assertEqual(api.requests, [])
                self.assertEqual(api.mutations, [])

    def test_mismatched_published_asset_fails_without_mutation(self):
        api = self.fake(existing=True, published=True, mismatched=True)
        with self.assertRaises(publisher.PublishError):
            publisher.publish(api, SHA, self.artifacts, INFO)
        self.assertEqual(api.mutations, [])

    def test_missing_published_asset_fails_without_mutation(self):
        api = self.fake(existing=True, published=True, missing_checksum=True)
        with self.assertRaises(publisher.PublishError):
            publisher.publish(api, SHA, self.artifacts, INFO)
        self.assertEqual(api.mutations, [])

    def test_missing_api_digest_requires_content_verification(self):
        api = self.fake(existing=True, published=True, no_digest=True)
        self.assertTrue(publisher.publish(api, SHA, self.artifacts, INFO))
        self.assertEqual(api.digest_checks, 2)
        self.assertEqual(self.kinds(api), ["publish"])

    def test_wrong_tag_target_fails_without_mutation(self):
        api = self.fake(existing=True, published=True, wrong_tag_target=True)
        with self.assertRaises(publisher.PublishError):
            publisher.publish(api, SHA, self.artifacts, INFO)
        self.assertEqual(api.mutations, [])

    def test_stale_published_rerun_does_not_change_latest(self):
        api = self.fake(existing=True, published=True, stale_on=3)
        self.assertFalse(publisher.publish(api, SHA, self.artifacts, INFO))
        self.assertEqual(api.mutations, [])

    def test_cli_stale_main_returns_zero_before_local_file_reads(self):
        api = self.fake(stale_on=1)
        environment = {"GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY": "owner/repo", "GITHUB_SHA": SHA, "GH_TOKEN": "synthetic-test-token"}
        with mock.patch.object(publisher.os, "environ", environment), mock.patch.object(publisher, "GitHub", return_value=api), mock.patch.object(publisher, "prepare_artifacts") as prepare:
            self.assertEqual(publisher.main(["nonexistent-package", "nonexistent-checksum"]), 0)
            prepare.assert_not_called()
        self.assertEqual(api.mutations, [])

    def test_cli_non_main_returns_zero_without_network(self):
        with mock.patch.object(publisher.os, "environ", {"GITHUB_REF": "refs/heads/feature"}), mock.patch.object(publisher, "GitHub") as api:
            self.assertEqual(publisher.main(["missing-package", "missing-checksum"]), 0)
            api.assert_not_called()


class ArtifactTests(unittest.TestCase):
    def test_checksum_supports_text_and_binary_markers(self):
        self.assertEqual(publisher.checksum_from_text("b" * 64 + "  " + publisher.PACKAGE_NAME + "\n"), "b" * 64)
        self.assertEqual(publisher.checksum_from_text("B" * 64 + " *" + publisher.PACKAGE_NAME + "\r\n"), "b" * 64)

    def test_checksum_rejects_bad_hash_name_and_multiple_entries(self):
        bad_entries = ["", "b" * 63 + "  " + publisher.PACKAGE_NAME, "b" * 64 + "  ../other.tar.gz\n", "b" * 64 + "  " + publisher.PACKAGE_NAME + "\n\n"]
        for text in bad_entries:
            with self.subTest(text_length=len(text)), self.assertRaises(publisher.PublishError):
                publisher.checksum_from_text(text)

    def test_artifact_hashes_use_exact_file_bytes(self):
        artifacts = make_artifacts()
        for artifact in artifacts:
            self.assertEqual(artifact.size, len(artifact.path.content))
            self.assertEqual(artifact.sha256, hashlib.sha256(artifact.path.content).hexdigest())

    def test_mismatched_empty_or_oversize_inputs_are_rejected(self):
        artifacts = make_artifacts()
        for package, checksum in [(b"", artifacts[1].path.content), (b"different", artifacts[1].path.content), (artifacts[0].path.content, b"x" * 4097), (artifacts[0].path.content, b"\xff")]:
            with self.subTest(package_size=len(package), checksum_size=len(checksum)), self.assertRaises(publisher.PublishError):
                publisher.prepare_artifacts(MemoryPath(package), MemoryPath(checksum))

    def test_repository_and_commit_validation(self):
        publisher.validate_identity("xiaowork-dev/xiaowork-watch", SHA)
        for repository, commit in [("../repo", SHA), ("owner/..", SHA), ("a/b/c", SHA), ("owner/repo", "short")]:
            with self.subTest(repository=repository), self.assertRaises(publisher.PublishError):
                publisher.validate_identity(repository, commit)


class ReleaseInfoTests(unittest.TestCase):
    def test_real_archive_metadata_is_read_without_extraction(self):
        self.assertEqual(publisher.read_release_info(MemoryPath(make_release_archive()), SHA), INFO)

    def test_prototype_metadata_and_presentation_are_distinct(self):
        info = publisher.ReleaseInfo("0.2.6", "frontend-prototype", SHA)
        metadata = {"schema": 1, "version": info.version, "kind": info.kind, "commit": SHA}
        self.assertEqual(publisher.read_release_info(MemoryPath(make_release_archive(metadata)), SHA), info)
        presentation = publisher.release_presentation(info, "## 0.2.6-prototype — 2026-10-02\n\n- 原型修复。\n\n## v0.4.3\n\n- 其他版本。\n")
        self.assertIn("v0.2.6（前端原型）", presentation["name"])
        self.assertIn("原型修复", presentation["body"])
        self.assertNotIn("其他版本", presentation["body"])
        self.assertIn("尚未接入真实监控后端", presentation["body"])

    def test_current_changelog_only_includes_packaged_version_section(self):
        presentation = publisher.release_presentation(INFO, "# Changelog\n\n## v0.4.3 — 2026-10-02\n\n- 本次发布整理。\n\n## v0.4.2 — 2026-10-02\n\n- 旧版修复。\n")
        self.assertIn("本次发布整理", presentation["body"])
        self.assertNotIn("旧版修复", presentation["body"])
        self.assertIn("源代码提交：`" + SHA, presentation["body"])

    def test_bad_metadata_is_rejected(self):
        valid = {"schema": 1, "version": INFO.version, "kind": INFO.kind, "commit": SHA}
        cases = [None, [], {}, dict(valid, schema=True), dict(valid, schema=2), dict(valid, version="<script>"), dict(valid, version=None), dict(valid, kind="unknown"), dict(valid, commit="f" * 40), dict(valid, commit="short")]
        for metadata in cases:
            with self.subTest(metadata=metadata), self.assertRaises(publisher.PublishError):
                content = json.dumps(metadata).encode("utf-8")
                publisher.read_release_info(MemoryPath(make_release_archive(content=content)), SHA)

    def test_missing_duplicate_link_or_oversized_metadata_is_rejected(self):
        cases = [make_release_archive(name="other.json"), make_release_archive(duplicate=True), make_release_archive(symlink=True), make_release_archive(content=b"x" * 65537)]
        for content in cases:
            with self.subTest(size=len(content)), self.assertRaises(publisher.PublishError):
                publisher.read_release_info(MemoryPath(content), SHA)

    def test_invalid_json_encoding_and_duplicate_json_keys_are_rejected(self):
        cases = [b"\xff", b"{", b'{"schema":1,"schema":1,"version":"0.4.3","kind":"monitoring-server","commit":"' + SHA.encode("ascii") + b'"}']
        for content in cases:
            with self.subTest(content=content), self.assertRaises(publisher.PublishError):
                publisher.read_release_info(MemoryPath(make_release_archive(content=content)), SHA)

    def test_corrupt_archive_is_rejected(self):
        with self.assertRaises(publisher.PublishError):
            publisher.read_release_info(MemoryPath(b"not a gzip archive"), SHA)

    def test_cli_invalid_packaged_metadata_never_mutates_github(self):
        environment = {"GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY": "owner/repo", "GITHUB_SHA": SHA, "GH_TOKEN": "synthetic-test-token"}
        api = FakeGitHub(make_artifacts())
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / publisher.PACKAGE_NAME
            package.write_bytes(make_release_archive({"schema": 1, "version": INFO.version, "kind": INFO.kind, "commit": "f" * 40}))
            checksum = package.with_name(publisher.CHECKSUM_NAME)
            checksum.write_text(hashlib.sha256(package.read_bytes()).hexdigest() + "  " + publisher.PACKAGE_NAME + "\n", encoding="ascii")
            with mock.patch.object(publisher.os, "environ", environment), mock.patch.object(publisher, "GitHub", return_value=api), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(publisher.main([str(package), str(checksum)]), 1)
        self.assertEqual(api.mutations, [])

    def test_cli_uses_archive_version_not_working_tree_package_json(self):
        environment = {"GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY": "owner/repo", "GITHUB_SHA": SHA, "GH_TOKEN": "synthetic-test-token"}
        api = FakeGitHub(make_artifacts())
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / publisher.PACKAGE_NAME
            package.write_bytes(make_release_archive({"schema": 1, "version": "9.8.7", "kind": "frontend-prototype", "commit": SHA}))
            checksum = package.with_name(publisher.CHECKSUM_NAME)
            checksum.write_text(hashlib.sha256(package.read_bytes()).hexdigest() + "  " + publisher.PACKAGE_NAME + "\n", encoding="ascii")
            with mock.patch.object(publisher.os, "environ", environment), mock.patch.object(publisher, "GitHub", return_value=api), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(publisher.main([str(package), str(checksum)]), 0)
        self.assertEqual(api.release["name"], "xiaowork Watch v9.8.7（前端原型）")
        self.assertTrue(all(asset["label"].startswith("v9.8.7") for asset in api.assets))


class TransportTests(unittest.TestCase):
    def test_upload_adds_version_label_without_changing_asset_filename(self):
        client = publisher.GitHub("synthetic-test-token", "owner/repo")
        artifact = publisher.labeled_artifacts(make_artifacts(), INFO)[0]
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(asset_metadata(artifact, 1)).encode("utf-8")
        with mock.patch.object(client.opener, "open", return_value=response) as opened:
            client.upload({"upload_url": "https://uploads.github.com/repos/owner/repo/releases/13/assets{?name,label}"}, artifact)
        query = publisher.urllib.parse.parse_qs(publisher.urllib.parse.urlsplit(opened.call_args.args[0].full_url).query)
        self.assertEqual(query, {"name": [publisher.PACKAGE_NAME], "label": ["v0.4.3 · 完整安装包"]})

    def test_authorization_is_not_forwarded_to_asset_redirect(self):
        client = publisher.GitHub("synthetic-test-token", "owner/repo")
        request = client._request("GET", "https://api.github.com/example")
        redirected = publisher.HTTPSRedirectHandler().redirect_request(request, None, 302, "redirect", {}, "https://release-assets.githubusercontent.com/example")
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_https_downgrade_redirect_is_rejected(self):
        client = publisher.GitHub("synthetic-test-token", "owner/repo")
        request = client._request("GET", "https://api.github.com/example")
        with self.assertRaises(publisher.PublishError):
            publisher.HTTPSRedirectHandler().redirect_request(request, None, 302, "redirect", {}, "http://example.com/file")

    def test_error_messages_do_not_include_token_or_server_response(self):
        token = "synthetic-test-token"
        client = publisher.GitHub(token, "owner/repo")
        error = urllib.error.HTTPError("https://api.github.com/example", 403, token, {}, io.BytesIO(token.encode("ascii")))
        with mock.patch.object(client.opener, "open", side_effect=error), self.assertRaises(publisher.PublishError) as caught:
            client.json("GET", "/git/ref/heads/main")
        self.assertNotIn(token, str(caught.exception))
        self.assertIn("403", str(caught.exception))

    def test_upload_url_is_restricted_before_opening_a_file(self):
        client = publisher.GitHub("synthetic-test-token", "owner/repo")
        artifact = make_artifacts()[0]
        for url in ["https://example.com/repos/a/b/releases/1/assets", "http://uploads.github.com/repos/a/b/releases/1/assets", "https://uploads.github.com@evil.example/repos/a/b/releases/1/assets"]:
            with self.subTest(url=url), mock.patch.object(client.opener, "open") as opened, self.assertRaises(publisher.PublishError):
                client.upload({"upload_url": url}, artifact)
            opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
