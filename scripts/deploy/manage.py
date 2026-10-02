#!/usr/bin/env python3
"""Install the static frontend prototype; this does not deploy an API or agent.

Python 3.8+, standard library only. The installer owns Nginx/systemd setup.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import time
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORY = "xiaowork-dev/xiaowork-watch"
API_BASE = "https://api.github.com/repos/" + REPOSITORY + "/releases"
MARKER = ".xiaowork-watch-managed"
MARKER_VALUE = "xiaowork-watch-managed-v1"
ARCHIVE_NAME = "xiaowork-watch-web.tar.gz"
CHECKSUM_NAME = ARCHIVE_NAME + ".sha256"
MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_MEMBERS = 10000
TAG_RE = re.compile(r"web-([0-9a-f]{40})\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")


class DeploymentError(Exception):
    pass


def _regular(path):
    return path.exists() and not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)


def _atomic_json(path, value):
    if path.is_symlink():
        raise DeploymentError("Refusing a symlink at " + str(path))
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json_file(path):
    if not _regular(path) or path.stat().st_size > 1024 * 1024:
        raise DeploymentError("Missing or invalid JSON file: " + str(path))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise DeploymentError("Invalid JSON: " + str(path)) from error
    if not isinstance(value, dict):
        raise DeploymentError("Expected a JSON object: " + str(path))
    return value


def _allowed_remote(url, api=False):
    parsed = urlsplit(url)
    hosts = ("api.github.com",) if api else (
        "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com")
    return (parsed.scheme == "https" and parsed.netloc in hosts
            and not parsed.username and not parsed.password)


class _RemoteRedirect(HTTPRedirectHandler):
    def __init__(self, api):
        self.api = api

    def redirect_request(self, request, response, code, message, headers, newurl):
        if not _allowed_remote(newurl, api=self.api):
            raise DeploymentError("Unexpected download redirect")
        return super().redirect_request(request, response, code, message, headers, newurl)


class _LocalRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        parsed = urlsplit(newurl)
        if (parsed.scheme not in ("http", "https")
                or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                or parsed.username or parsed.password):
            raise DeploymentError("Health endpoint redirected outside localhost")
        return super().redirect_request(request, response, code, message, headers, newurl)


def _open_health(request):
    return build_opener(_LocalRedirect()).open(request, timeout=10)


def _fetch(url, limit, api=False):
    if not _allowed_remote(url, api=api):
        raise DeploymentError("Unexpected download URL")
    request = Request(url, headers={"User-Agent": "xiaowork-watch-deployer",
                                  "Accept": "application/vnd.github+json" if api else "application/octet-stream"})
    try:
        with build_opener(_RemoteRedirect(api)).open(request, timeout=30) as response:
            if not _allowed_remote(response.geturl(), api=api):
                raise DeploymentError("Unexpected download redirect")
            chunks, count = [], 0
            while True:
                chunk = response.read(min(65536, limit - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                if count > limit:
                    raise DeploymentError("Download exceeds size limit")
                chunks.append(chunk)
            return b"".join(chunks)
    except DeploymentError:
        raise
    except Exception as error:
        raise DeploymentError("GitHub download failed: " + str(error)) from error


def _release(tag=None):
    if tag is not None and not TAG_RE.fullmatch(tag):
        raise DeploymentError("Invalid release tag")
    endpoint = API_BASE + ("/tags/" + quote(tag, safe="") if tag else "/latest")
    try:
        release = json.loads(_fetch(endpoint, 2 * 1024 * 1024, api=True))
    except (ValueError, UnicodeError) as error:
        raise DeploymentError("Invalid GitHub release response") from error
    if not isinstance(release, dict):
        raise DeploymentError("Invalid GitHub release response")
    found = TAG_RE.fullmatch(str(release.get("tag_name", "")))
    if (not found or release.get("draft") is not False
            or release.get("prerelease") is not False
            or (tag is not None and release["tag_name"] != tag)):
        raise DeploymentError("Expected a published web-fullSHA release")
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        raise DeploymentError("Invalid release assets")
    selected = {}
    for name in (ARCHIVE_NAME, CHECKSUM_NAME):
        matches = [item for item in assets if isinstance(item, dict) and item.get("name") == name]
        expected = "https://github.com/" + REPOSITORY + "/releases/download/" + release["tag_name"] + "/" + name
        if (len(matches) != 1 or matches[0].get("state") != "uploaded"
                or matches[0].get("browser_download_url") != expected):
            raise DeploymentError("Missing or unexpected release asset: " + name)
        selected[name] = matches[0]
    return found.group(1), selected


def _checksum(raw):
    try:
        text = raw.decode("ascii").strip()
    except UnicodeError as error:
        raise DeploymentError("Invalid checksum file") from error
    matched = re.fullmatch(r"([0-9a-fA-F]{64})(?:[ \t]+\*?" + re.escape(ARCHIVE_NAME) + r")?", text)
    if not matched:
        raise DeploymentError("Invalid checksum file")
    return matched.group(1).lower()


def _health_host(value):
    if value == "":
        return ""
    if not isinstance(value, str) or len(value) > 253:
        raise DeploymentError("healthHost must be an empty string or a safe hostname")
    labels = value.split(".")
    if not all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", part) for part in labels):
        raise DeploymentError("healthHost must be a hostname without port, whitespace or path")
    return value


def _member_path(name):
    # Permit tar's customary './' prefix, but never normalize away '..'.
    if not isinstance(name, str) or len(name) > 1024 or "\\" in name or any(ord(c) < 32 for c in name):
        raise DeploymentError("Invalid archive path")
    if name.startswith("/") or re.match(r"^[a-zA-Z]:", name):
        raise DeploymentError("Absolute archive path")
    parts = name.split("/")
    if ".." in parts:
        raise DeploymentError("Archive path traversal")
    normalized = PurePosixPath(name)
    if not normalized.parts:
        return None
    if any(len(part) > 255 for part in normalized.parts):
        raise DeploymentError("Archive filename is too long")
    return normalized


def _public_directory(path, root):
    """Create every directory below root with readable traversal permissions."""
    parts = path.relative_to(root).parts
    for length in range(len(parts) + 1):
        directory = root.joinpath(*parts[:length])
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise DeploymentError("Unsafe public directory: " + str(directory))
        directory.mkdir(exist_ok=True)
        os.chmod(str(directory), 0o755)


def _extract(archive, destination):
    seen, members, expanded, count = set(), [], 0, 0
    try:
        with tarfile.open(str(archive), "r:gz") as source:
            for item in source:
                count += 1
                if count > MAX_MEMBERS:
                    raise DeploymentError("Archive has too many entries")
                if not (item.isdir() or item.isreg()) or getattr(item, "sparse", None):
                    raise DeploymentError("Archive links and special files are forbidden")
                path = _member_path(item.name)
                if path is None:
                    if not item.isdir():
                        raise DeploymentError("Archive root must be a directory")
                    continue
                key = str(path)
                if key in seen:
                    raise DeploymentError("Duplicate archive entry")
                seen.add(key)
                if item.size < 0 or item.size > MAX_FILE:
                    raise DeploymentError("Archive file exceeds size limit")
                if item.isdir() and item.size:
                    raise DeploymentError("Archive directory has unexpected data")
                expanded += item.size
                if expanded > MAX_EXPANDED:
                    raise DeploymentError("Archive exceeds expanded size limit")
                members.append((item, path))
            for item, relative in members:
                target = destination.joinpath(*relative.parts)
                if item.isdir():
                    _public_directory(target, destination)
                    continue
                _public_directory(target.parent, destination)
                with source.extractfile(item) as input_stream, target.open("xb") as output:
                    remaining = item.size
                    while remaining:
                        data = input_stream.read(min(65536, remaining))
                        if not data:
                            raise DeploymentError("Truncated archive file")
                        output.write(data)
                        remaining -= len(data)
                os.chmod(str(target), 0o644)
    except DeploymentError:
        raise
    except (tarfile.TarError, OSError, EOFError) as error:
        raise DeploymentError("Cannot safely extract archive: " + str(error)) from error


def _validate_release(directory, sha):
    if directory.is_symlink() or not directory.is_dir():
        raise DeploymentError("Release directory is missing or unsafe")
    for name in ("index.html", ".deploy/manage.py"):
        if not _regular(directory / name) or (directory / name).stat().st_size == 0:
            raise DeploymentError("Release is missing " + name)
    if (directory / ".deploy").is_symlink():
        raise DeploymentError("Unsafe release manager directory")
    metadata = _json_file(directory / "release.json")
    if (type(metadata.get("schema")) is not int or metadata["schema"] != 1
            or metadata.get("commit") != sha or metadata.get("kind") != "frontend-prototype"
            or not isinstance(metadata.get("version"), str) or not metadata["version"]):
        raise DeploymentError("Release metadata does not match its tag")
    if (directory / "assets").is_symlink() or not (directory / "assets").is_dir():
        raise DeploymentError("Release is missing its assets directory")
    return metadata


@contextlib.contextmanager
def _locked(path):
    if path.is_symlink():
        raise DeploymentError("Unsafe lock file")
    with path.open("a+b") as handle:
        if os.name == "posix":
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        else:
            # Enables fixture tests on Windows; production uses flock above.
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class Manager:
    def __init__(self, root):
        candidate = Path(root).absolute()
        if candidate.is_symlink() or not candidate.is_dir():
            raise DeploymentError("Root must be an existing managed directory")
        self.root = candidate.resolve()
        marker = self.root / MARKER
        if not _regular(marker) or marker.read_text(encoding="ascii").strip() != MARKER_VALUE:
            raise DeploymentError("Managed-root marker is missing or invalid")
        self.releases = self.root / "releases"
        self.shared = self.root / "shared" / "assets"
        for directory in (self.releases, self.root / "shared", self.shared):
            if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                raise DeploymentError("Unsafe deployment directory: " + str(directory))
            directory.mkdir(exist_ok=True)
            os.chmod(str(directory), 0o755)

    def _config(self):
        config = _json_file(self.root / "config.json")
        if not isinstance(config.get("autoUpdate", True), bool):
            raise DeploymentError("autoUpdate must be a boolean")
        health = config.get("healthUrl", "")
        if not isinstance(health, str):
            raise DeploymentError("healthUrl must be a string")
        _health_host(config.get("healthHost", ""))
        if health:
            parsed = urlsplit(health)
            if (parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                    or parsed.username or parsed.password or parsed.fragment):
                raise DeploymentError("healthUrl must point to the local Nginx release.json")
        return config

    def _pointed_sha(self, name):
        link = self.root / name
        if not link.is_symlink():
            if link.exists():
                raise DeploymentError("Refusing a non-symlink " + name)
            return None
        target = link.resolve()
        if target.parent != self.releases or not SHA_RE.fullmatch(target.name):
            raise DeploymentError("Unsafe " + name + " link")
        _validate_release(target, target.name)
        return target.name

    def _switch(self, name, sha):
        link = self.root / name
        if link.exists() and not link.is_symlink():
            raise DeploymentError("Refusing to replace a non-symlink " + name)
        if sha is None:
            if link.is_symlink():
                link.unlink()
            return
        if not SHA_RE.fullmatch(sha):
            raise DeploymentError("Invalid release SHA")
        _validate_release(self.releases / sha, sha)
        temporary = self.root / ("." + name + "-" + str(os.getpid()))
        if temporary.exists() or temporary.is_symlink():
            raise DeploymentError("Temporary link already exists")
        try:
            temporary.symlink_to(Path("releases") / sha, target_is_directory=True)
            os.replace(str(temporary), str(link))
        finally:
            if temporary.is_symlink():
                temporary.unlink()

    def _assets(self, release):
        # Old asset URLs remain reachable after current changes. Never replace
        # an existing URL with different bytes, including hash collisions.
        for source in (release / "assets").rglob("*"):
            if source.is_symlink():
                raise DeploymentError("Release assets contain a symlink")
            if source.is_dir():
                continue
            if not _regular(source):
                raise DeploymentError("Release assets contain a special file")
            relative = source.relative_to(release / "assets")
            destination = self.shared / relative
            parent = self.shared
            for part in relative.parts[:-1]:
                parent = parent / part
                if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                    raise DeploymentError("Unsafe shared asset directory")
                parent.mkdir(exist_ok=True)
                os.chmod(str(parent), 0o755)
            if destination.is_symlink():
                raise DeploymentError("Unsafe shared asset")
            if destination.exists():
                if not _regular(destination) or source.read_bytes() != destination.read_bytes():
                    raise DeploymentError("Different content uses an existing asset URL")
                continue
            fd, temporary = tempfile.mkstemp(prefix=".asset-", dir=str(destination.parent))
            try:
                with os.fdopen(fd, "wb") as output, source.open("rb") as input_stream:
                    shutil.copyfileobj(input_stream, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary, 0o644)
                os.replace(temporary, str(destination))
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def _prepare(self, sha, assets):
        existing = self.releases / sha
        if existing.exists() or existing.is_symlink():
            _validate_release(existing, sha)
            return existing
        archive = _fetch(assets[ARCHIVE_NAME]["browser_download_url"], MAX_DOWNLOAD)
        checksum = _checksum(_fetch(assets[CHECKSUM_NAME]["browser_download_url"], 4096))
        actual = hashlib.sha256(archive).hexdigest()
        digest = assets[ARCHIVE_NAME].get("digest")
        if actual != checksum or (digest is not None and digest != "sha256:" + actual):
            raise DeploymentError("Archive SHA256 verification failed")
        with tempfile.TemporaryDirectory(prefix=".download-", dir=str(self.root)) as working:
            working = Path(working)
            archive_path, stage = working / "web.tar.gz", working / "stage"
            archive_path.write_bytes(archive)
            stage.mkdir()
            os.chmod(str(stage), 0o755)
            _extract(archive_path, stage)
            _validate_release(stage, sha)
            stage.rename(existing)
        return existing

    def _health(self, sha, config):
        address = config.get("healthUrl", "")
        host = _health_host(config.get("healthHost", ""))
        if not address:
            return
        last_error = None
        for attempt in range(3):
            try:
                separator = "&" if "?" in address else "?"
                headers = {"Cache-Control": "no-cache"}
                if host:
                    headers["Host"] = host
                request = Request(address + separator + "_deploy=" + sha, headers=headers)
                with _open_health(request) as response:
                    # Do not follow a local health endpoint to a remote host.
                    endpoint = urlsplit(response.geturl())
                    if endpoint.hostname not in ("127.0.0.1", "localhost", "::1"):
                        raise DeploymentError("Health endpoint redirected outside localhost")
                    raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise DeploymentError("Health response is too large")
                value = json.loads(raw)
                if (type(value.get("schema")) is int and value["schema"] == 1
                        and value.get("commit") == sha and value.get("kind") == "frontend-prototype"):
                    return
                last_error = "Nginx served a different release"
            except Exception as error:
                last_error = str(error)
            if attempt < 2:
                time.sleep(0.5)
        raise DeploymentError("Release health check failed: " + str(last_error))

    def _record(self, current, previous):
        _atomic_json(self.root / "installed.json", {
            "current": current, "previous": previous, "kind": "frontend-prototype",
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

    def _activate(self, sha, config, resume=False):
        old, previous = self._pointed_sha("current"), self._pointed_sha("previous")
        old_config = dict(config)
        config_written = False
        try:
            self._switch("current", sha)
            self._health(sha, config)
            if old and old != sha:
                self._switch("previous", old)
            if resume:
                config["autoUpdate"] = True
                _atomic_json(self.root / "config.json", config)
                config_written = True
            self._record(sha, old if old and old != sha else previous)
        except Exception:
            self._switch("current", old)
            self._switch("previous", previous)
            if config_written:
                _atomic_json(self.root / "config.json", old_config)
            raise

    def install_or_update(self, automatic=False, tag=None):
        config = self._config()
        if automatic and not config.get("autoUpdate", True):
            return {"status": "paused", "current": self._pointed_sha("current")}
        sha, assets = _release(tag)
        release = self._prepare(sha, assets)
        self._assets(release)
        self._activate(sha, config, resume=not automatic)
        return {"status": "installed", "current": sha, "autoUpdate": config.get("autoUpdate", True)}

    def rollback(self):
        config = self._config()
        current, previous = self._pointed_sha("current"), self._pointed_sha("previous")
        if not current or not previous or current == previous:
            raise DeploymentError("No previous release is available")
        config["autoUpdate"] = False
        _atomic_json(self.root / "config.json", config)
        self._assets(self.releases / previous)
        try:
            self._switch("current", previous)
            self._health(previous, config)
            self._switch("previous", current)
            self._record(previous, current)
        except Exception:
            self._switch("current", current)
            self._switch("previous", previous)
            raise
        return {"status": "rolled-back", "current": previous, "autoUpdate": False}

    def status(self):
        config = self._config()
        return {"status": "installed" if self._pointed_sha("current") else "not-installed",
                "current": self._pointed_sha("current"), "previous": self._pointed_sha("previous"),
                "autoUpdate": config.get("autoUpdate", True), "kind": "frontend-prototype"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/opt/xiaowork-watch")
    commands = parser.add_subparsers(dest="command", required=True)
    installer = commands.add_parser("install")
    installer.add_argument("--release-tag", help="Pin initial installation to web-fullSHA")
    updater = commands.add_parser("update")
    updater.add_argument("--automatic", action="store_true", help="Respect rollback's update pause")
    commands.add_parser("rollback")
    commands.add_parser("status")
    args = parser.parse_args(argv)
    try:
        manager = Manager(args.root)
        with _locked(manager.root / ".deploy.lock"):
            if args.command == "install":
                result = manager.install_or_update(tag=args.release_tag)
            elif args.command == "update":
                result = manager.install_or_update(automatic=args.automatic)
            elif args.command == "rollback":
                result = manager.rollback()
            else:
                result = manager.status()
        print(json.dumps(result, sort_keys=True))
        return 0
    except (DeploymentError, OSError, ValueError) as error:
        print("Deployment failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
