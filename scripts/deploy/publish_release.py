#!/usr/bin/env python3
"""Publish a validated static Web package from the current GitHub main commit."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


PACKAGE_NAME = "xiaowork-watch-web.tar.gz"
CHECKSUM_NAME = PACKAGE_NAME + ".sha256"
API_ROOT = "https://api.github.com"
# Listed as supported at https://docs.github.com/en/rest/about-the-rest-api/api-versions.
API_VERSION = "2026-03-10"
CHUNK_SIZE = 1024 * 1024


class PublishError(Exception):
    """An expected, safely printable publication failure."""


class HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise PublishError("Refusing a non-HTTPS GitHub redirect.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class Artifact:
    path: Path
    name: str
    size: int
    sha256: str
    content_type: str


def checksum_from_text(text: str) -> str:
    pattern = r"([0-9a-fA-F]{64})[ \t]+\*?" + re.escape(PACKAGE_NAME) + r"[ \t]*(?:\r?\n)?"
    match = re.fullmatch(pattern, text)
    if not match:
        raise PublishError("Checksum file must contain one SHA-256 entry for " + PACKAGE_NAME + ".")
    return match.group(1).lower()


def hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def prepare_artifacts(package: Path, checksum: Path) -> tuple[Artifact, Artifact]:
    if not package.is_file() or not checksum.is_file():
        raise PublishError("Package and checksum arguments must be existing regular files.")
    with checksum.open("rb") as stream:
        checksum_bytes = stream.read(4097)
    if len(checksum_bytes) > 4096:
        raise PublishError("Checksum file is unexpectedly large.")
    try:
        expected = checksum_from_text(checksum_bytes.decode("ascii"))
    except UnicodeError:
        raise PublishError("Checksum file must be ASCII text.") from None
    size, actual = hash_file(package)
    if size == 0 or actual != expected:
        raise PublishError("Package SHA-256 does not match its checksum, or package is empty.")
    checksum_size = len(checksum_bytes)
    checksum_digest = hashlib.sha256(checksum_bytes).hexdigest()
    return (
        Artifact(package, PACKAGE_NAME, size, actual, "application/gzip"),
        Artifact(checksum, CHECKSUM_NAME, checksum_size, checksum_digest, "text/plain"),
    )


def metadata_matches(asset: dict, artifact: Artifact) -> bool | None:
    """Return None when content must be downloaded to establish its digest."""
    if asset.get("state") != "uploaded" or asset.get("size") != artifact.size:
        return False
    digest = asset.get("digest")
    if isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        return digest[7:].lower() == artifact.sha256
    return None


def validate_identity(repository: str, sha: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise PublishError("GITHUB_REPOSITORY must have the owner/repository form.")
    if any(part in {".", ".."} for part in repository.split("/")):
        raise PublishError("GITHUB_REPOSITORY contains an invalid path component.")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise PublishError("GITHUB_SHA must be a full lowercase 40-character commit SHA.")


class GitHub:
    def __init__(self, token: str, repository: str):
        self.token = token
        self.repo_path = "/repos/" + "/".join(urllib.parse.quote(part, safe="") for part in repository.split("/"))
        self.opener = urllib.request.build_opener(HTTPSRedirectHandler())

    def _request(self, method: str, url: str, *, data=None, content_type=None, content_length=None, accept="application/vnd.github+json"):
        headers = {
            "Accept": accept,
            "User-Agent": "xiaowork-watch-release-publisher",
            "X-GitHub-Api-Version": API_VERSION,
        }
        if content_type:
            headers["Content-Type"] = content_type
        if content_length is not None:
            headers["Content-Length"] = str(content_length)
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        # urllib does not copy unredirected headers to redirected asset downloads.
        request.add_unredirected_header("Authorization", "Bearer " + self.token)
        return request

    def json(self, method: str, path: str, payload=None, *, missing_ok=False):
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = self._request(method, API_ROOT + self.repo_path + path, data=data, content_type="application/json" if data else None)
        try:
            with self.opener.open(request, timeout=45) as response:
                raw = response.read(8 * CHUNK_SIZE + 1)
            if len(raw) > 8 * CHUNK_SIZE:
                raise PublishError("GitHub JSON response exceeded the allowed size.")
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if missing_ok and exc.code == 404:
                return None
            raise PublishError("GitHub API request failed with HTTP " + str(exc.code) + ".") from None
        except (urllib.error.URLError, TimeoutError):
            raise PublishError("Unable to reach GitHub API; no publication was confirmed.") from None
        except (ValueError, UnicodeError):
            raise PublishError("GitHub API returned invalid JSON.") from None

    def upload(self, release: dict, artifact: Artifact) -> dict:
        base = str(release.get("upload_url", "")).split("{", 1)[0]
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme != "https" or parsed.netloc != "uploads.github.com" or not re.fullmatch(r"/repos/[^/]+/[^/]+/releases/[0-9]+/assets", parsed.path) or parsed.query or parsed.fragment:
            raise PublishError("GitHub supplied an unexpected asset upload URL.")
        url = base + "?" + urllib.parse.urlencode({"name": artifact.name})
        try:
            with artifact.path.open("rb") as stream:
                request = self._request("POST", url, data=stream, content_type=artifact.content_type, content_length=artifact.size)
                with self.opener.open(request, timeout=120) as response:
                    result = json.loads(response.read(2 * CHUNK_SIZE))
            if not isinstance(result, dict):
                raise PublishError("GitHub returned invalid upload metadata.")
            return result
        except urllib.error.HTTPError as exc:
            raise PublishError("Asset upload failed with HTTP " + str(exc.code) + ".") from None
        except (urllib.error.URLError, TimeoutError):
            raise PublishError("Asset upload failed to reach GitHub; draft can be resumed.") from None
        except (ValueError, UnicodeError):
            raise PublishError("GitHub returned invalid upload metadata.") from None

    def asset_digest(self, asset_id: int, expected_size: int) -> tuple[int, str]:
        request = self._request("GET", API_ROOT + self.repo_path + "/releases/assets/" + str(asset_id), accept="application/octet-stream")
        digest = hashlib.sha256()
        size = 0
        try:
            with self.opener.open(request, timeout=120) as response:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > expected_size:
                        return size, ""
                    digest.update(chunk)
            return size, digest.hexdigest()
        except urllib.error.HTTPError as exc:
            raise PublishError("Existing asset verification failed with HTTP " + str(exc.code) + ".") from None
        except (urllib.error.URLError, TimeoutError):
            raise PublishError("Unable to download the existing asset for verification.") from None


def current_main(api: GitHub, sha: str) -> bool:
    ref = api.json("GET", "/git/ref/heads/main")
    if not isinstance(ref, dict) or not isinstance(ref.get("object"), dict) or ref["object"].get("type") != "commit":
        raise PublishError("GitHub main reference has an unexpected format.")
    if ref["object"].get("sha") != sha:
        print("Skipping release publication: this commit is no longer the current main HEAD.")
        return False
    return True


def find_release(api: GitHub, tag: str) -> dict | None:
    release = api.json("GET", "/releases/tags/" + urllib.parse.quote(tag, safe=""), missing_ok=True)
    if release is not None:
        return release
    # The by-tag endpoint is documented for published releases; authenticated
    # listing also finds a leftover draft when the tag does not exist yet.
    page = 1
    while True:
        releases = api.json("GET", "/releases?per_page=100&page=" + str(page))
        if not isinstance(releases, list):
            raise PublishError("GitHub release listing has an unexpected format.")
        matches = [release for release in releases if isinstance(release, dict) and release.get("tag_name") == tag]
        if len(matches) > 1:
            raise PublishError("Multiple releases use the requested tag; refusing to modify them.")
        if matches:
            return matches[0]
        if len(releases) < 100:
            return None
        page += 1


def validate_release(api: GitHub, release: dict, tag: str, sha: str) -> None:
    if not isinstance(release, dict) or type(release.get("id")) is not int or release.get("tag_name") != tag or type(release.get("draft")) is not bool:
        raise PublishError("Existing release metadata does not match the requested tag.")
    if release.get("prerelease") is not False:
        raise PublishError("Existing release is a prerelease; refusing to change its publication semantics.")
    ref = api.json("GET", "/git/ref/tags/" + urllib.parse.quote(tag, safe=""), missing_ok=True)
    if ref is None:
        if release["draft"] and release.get("target_commitish") == sha:
            return
        raise PublishError("Release tag is missing or its draft target commit is incorrect.")
    if not isinstance(ref, dict) or not isinstance(ref.get("object"), dict):
        raise PublishError("Release tag reference has an unexpected format.")
    obj = ref["object"]
    for _ in range(8):
        if obj.get("type") == "commit":
            if obj.get("sha") != sha:
                raise PublishError("Release tag points to a different commit; refusing to modify it.")
            return
        if obj.get("type") != "tag" or not re.fullmatch(r"[0-9a-f]{40}", str(obj.get("sha", ""))):
            break
        annotated = api.json("GET", "/git/tags/" + obj["sha"])
        if not isinstance(annotated, dict) or not isinstance(annotated.get("object"), dict):
            raise PublishError("Annotated tag has an unexpected format.")
        obj = annotated["object"]
    raise PublishError("Release tag could not be resolved to the requested commit.")


def list_assets(api: GitHub, release_id: int) -> list[dict]:
    assets = []
    page = 1
    while True:
        batch = api.json("GET", "/releases/" + str(release_id) + "/assets?per_page=100&page=" + str(page))
        if not isinstance(batch, list) or not all(isinstance(asset, dict) for asset in batch):
            raise PublishError("GitHub asset listing has an unexpected format.")
        assets.extend(batch)
        if len(batch) < 100:
            return assets
        page += 1


def matching_asset(assets: list[dict], artifact: Artifact) -> dict | None:
    matches = [asset for asset in assets if asset.get("name") == artifact.name]
    if len(matches) > 1:
        raise PublishError("Duplicate release assets found for " + artifact.name + ".")
    return matches[0] if matches else None


def asset_matches(api: GitHub, asset: dict, artifact: Artifact) -> bool:
    match = metadata_matches(asset, artifact)
    if match is not None:
        return match
    if type(asset.get("id")) is not int:
        raise PublishError("Existing asset has an invalid identifier.")
    size, digest = api.asset_digest(asset["id"], artifact.size)
    return size == artifact.size and digest == artifact.sha256


def ensure_assets(api: GitHub, release: dict, artifacts: tuple[Artifact, Artifact]) -> None:
    existing = list_assets(api, release["id"])
    # Validate all published assets before any mutation. Published assets are
    # immutable here, even when the repository has not enabled immutable releases.
    if not release["draft"]:
        for artifact in artifacts:
            asset = matching_asset(existing, artifact)
            if asset is None or not asset_matches(api, asset, artifact):
                raise PublishError("Published release asset is missing or differs: " + artifact.name + ". Refusing to overwrite it.")
        print("Verified existing published release assets; no assets were modified.")
        return
    for artifact in artifacts:
        asset = matching_asset(existing, artifact)
        if asset is not None and asset_matches(api, asset, artifact):
            print("Reusing verified draft asset: " + artifact.name)
            continue
        if asset is not None:
            if type(asset.get("id")) is not int:
                raise PublishError("Draft asset has an invalid identifier.")
            api.json("DELETE", "/releases/assets/" + str(asset["id"]))
            print("Removed mismatched draft asset: " + artifact.name)
        uploaded = api.upload(release, artifact)
        if uploaded.get("name") != artifact.name or not asset_matches(api, uploaded, artifact):
            raise PublishError("Uploaded asset failed size or SHA-256 verification: " + artifact.name + ".")
        print("Uploaded and verified draft asset: " + artifact.name)
    final_assets = list_assets(api, release["id"])
    for artifact in artifacts:
        asset = matching_asset(final_assets, artifact)
        if asset is None or not asset_matches(api, asset, artifact):
            raise PublishError("Final release asset verification failed: " + artifact.name + ".")


def publish(api: GitHub, sha: str, artifacts: tuple[Artifact, Artifact]) -> bool:
    if not current_main(api, sha):
        return False
    tag = "web-" + sha
    release = find_release(api, tag)
    if release is None:
        if not current_main(api, sha):
            return False
        release = api.json("POST", "/releases", {
            "tag_name": tag,
            "target_commitish": sha,
            "name": "Web prototype " + sha[:12],
            "body": "Static Web prototype package. This release does not include a monitoring backend or a Linux probe.",
            "draft": True,
            "prerelease": False,
            "make_latest": "false",
        })
        print("Created unpublished draft release: " + tag)
    validate_release(api, release, tag, sha)
    ensure_assets(api, release, artifacts)
    if not current_main(api, sha):
        return False
    if not release["draft"]:
        latest = api.json("GET", "/releases/latest", missing_ok=True)
        if isinstance(latest, dict) and latest.get("tag_name") == tag:
            print("Verified release is already latest: " + tag)
            return True
        # The lookup above is an additional network round trip, so recheck main.
        if not current_main(api, sha):
            return False
    result = api.json("PATCH", "/releases/" + str(release["id"]), {"draft": False, "make_latest": "true"})
    if not isinstance(result, dict) or result.get("tag_name") != tag or result.get("draft") is not False or result.get("prerelease") is not False:
        raise PublishError("GitHub did not confirm the requested published release state.")
    latest = api.json("GET", "/releases/latest")
    if not isinstance(latest, dict) or latest.get("tag_name") != tag:
        raise PublishError("Release was published, but GitHub did not confirm it as latest.")
    print("Published validated Web prototype release as latest: " + tag)
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("checksum", type=Path)
    args = parser.parse_args(argv)
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        print("Skipping release publication: only refs/heads/main is eligible.")
        return 0
    try:
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        sha = os.environ.get("GITHUB_SHA", "")
        validate_identity(repository, sha)
        token = os.environ.get("GH_TOKEN", "")
        if not token:
            raise PublishError("GH_TOKEN is required to publish release assets.")
        api = GitHub(token, repository)
        if not current_main(api, sha):
            return 0
        artifacts = prepare_artifacts(args.package, args.checksum)
        publish(api, sha, artifacts)
        return 0
    except PublishError as exc:
        print("Release publication failed: " + str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("Release publication failed: unable to read a local artifact or complete network I/O.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
