"""Offline publication tests; compatible with scratch and repository test paths.

Run directly or use: python -m unittest discover -s tests -p test_deploy_publish.py
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import sys
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


def asset_metadata(artifact, identifier, *, include_digest=True):
    result = {"id": identifier, "name": artifact.name, "size": artifact.size, "state": "uploaded"}
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
            }
            self.assets = [asset_metadata(artifact, index + 1, include_digest=not no_digest) for index, artifact in enumerate(artifacts)]
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
        if path == "/releases/latest":
            return {"tag_name": self.latest_tag} if self.latest_tag else None
        if method == "PATCH" and path == "/releases/13":
            if payload != {"draft": False, "make_latest": "true"}:
                raise AssertionError("Unexpected publication mutation.")
            self.mutations.append(("publish", payload.copy()))
            self.release.update(payload)
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
        self.assertTrue(publisher.publish(api, SHA, self.artifacts))
        self.assertEqual(self.kinds(api), ["create", "upload", "upload", "publish"])
        self.assertFalse(api.release["draft"])

    def test_stale_main_skips_without_any_mutation(self):
        api = self.fake(stale_on=1)
        self.assertFalse(publisher.publish(api, SHA, self.artifacts))
        self.assertEqual(api.mutations, [])
        self.assertIn("no longer the current main HEAD", self.stdout.getvalue())

    def test_main_changes_before_creation_skips_empty_draft(self):
        api = self.fake(stale_on=2)
        self.assertFalse(publisher.publish(api, SHA, self.artifacts))
        self.assertEqual(api.mutations, [])

    def test_main_changes_before_publication_leaves_draft(self):
        api = self.fake(stale_on=3)
        self.assertFalse(publisher.publish(api, SHA, self.artifacts))
        self.assertTrue(api.release["draft"])
        self.assertNotIn("publish", self.kinds(api))

    def test_draft_assets_are_reused(self):
        api = self.fake(existing=True)
        self.assertTrue(publisher.publish(api, SHA, self.artifacts))
        self.assertEqual(self.kinds(api), ["publish"])

    def test_leftover_draft_is_found_in_authenticated_listing(self):
        api = self.fake(existing=True, by_tag_missing=True)
        self.assertTrue(publisher.publish(api, SHA, self.artifacts))
        self.assertEqual(self.kinds(api), ["publish"])

    def test_mismatched_draft_asset_is_repaired(self):
        api = self.fake(existing=True, mismatched=True)
        self.assertTrue(publisher.publish(api, SHA, self.artifacts))
        self.assertEqual(self.kinds(api), ["delete", "upload", "publish"])

    def test_missing_draft_asset_is_uploaded(self):
        api = self.fake(existing=True, missing_checksum=True)
        self.assertTrue(publisher.publish(api, SHA, self.artifacts))
        self.assertEqual(self.kinds(api), ["upload", "publish"])

    def test_missing_final_asset_prevents_publication(self):
        api = self.fake(omit_final_checksum=True)
        with self.assertRaises(publisher.PublishError):
            publisher.publish(api, SHA, self.artifacts)
        self.assertTrue(api.release["draft"])
        self.assertNotIn("publish", self.kinds(api))

    def test_corrupt_uploaded_asset_prevents_publication(self):
        api = self.fake(corrupt_upload=True)
        with self.assertRaises(publisher.PublishError):
            publisher.publish(api, SHA, self.artifacts)
        self.assertTrue(api.release["draft"])
        self.assertNotIn("publish", self.kinds(api))

    def test_matching_published_assets_are_never_overwritten(self):
        api = self.fake(existing=True, published=True)
        self.assertTrue(publisher.publish(api, SHA, self.artifacts))
        self.assertEqual(self.kinds(api), ["publish"])

    def test_already_latest_published_release_is_unchanged(self):
        api = self.fake(existing=True, published=True, already_latest=True)
        self.assertTrue(publisher.publish(api, SHA, self.artifacts))
        self.assertEqual(api.mutations, [])

    def test_mismatched_published_asset_fails_without_mutation(self):
        api = self.fake(existing=True, published=True, mismatched=True)
        with self.assertRaises(publisher.PublishError):
            publisher.publish(api, SHA, self.artifacts)
        self.assertEqual(api.mutations, [])

    def test_missing_published_asset_fails_without_mutation(self):
        api = self.fake(existing=True, published=True, missing_checksum=True)
        with self.assertRaises(publisher.PublishError):
            publisher.publish(api, SHA, self.artifacts)
        self.assertEqual(api.mutations, [])

    def test_missing_api_digest_requires_content_verification(self):
        api = self.fake(existing=True, published=True, no_digest=True)
        self.assertTrue(publisher.publish(api, SHA, self.artifacts))
        self.assertEqual(api.digest_checks, 2)
        self.assertEqual(self.kinds(api), ["publish"])

    def test_wrong_tag_target_fails_without_mutation(self):
        api = self.fake(existing=True, published=True, wrong_tag_target=True)
        with self.assertRaises(publisher.PublishError):
            publisher.publish(api, SHA, self.artifacts)
        self.assertEqual(api.mutations, [])

    def test_stale_published_rerun_does_not_change_latest(self):
        api = self.fake(existing=True, published=True, stale_on=3)
        self.assertFalse(publisher.publish(api, SHA, self.artifacts))
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


class TransportTests(unittest.TestCase):
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
