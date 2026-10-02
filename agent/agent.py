#!/usr/bin/env python3
"""Outbound Linux heartbeat / ICMP probe agent, using only Python's stdlib."""
import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HEARTBEAT_SECONDS = 30
MAX_TASKS = 10
MAX_PENDING = 100
HTTP_TIMEOUT = 8
MAX_RESPONSE = 65536
PING_COUNT = 5
PING_TIMEOUT = 8
_V4_DENIED = tuple(ipaddress.ip_network(value) for value in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15",
    "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "168.63.129.16/32"))


class AgentError(ValueError):
    pass


class ApiError(AgentError):
    def __init__(self, message="Control server unavailable", status=None):
        super().__init__(message)
        self.status = status


def _identifier(value):
    if type(value) is int and 0 < value <= 9007199254740991:
        return value
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        return value
    raise AgentError("Invalid agent or job identifier")


def _secret(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", value):
        raise AgentError("Invalid enrollment token or agent credential")
    return value


def server_url(value, development=False):
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\s\x00-\x1f\x7f]", value):
        raise AgentError("Invalid control server URL")
    try:
        parsed = urllib.parse.urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError as error:
        raise AgentError("Invalid control server URL") from error
    if (not host or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")
            or parsed.scheme not in ("https", "http") or (port is not None and not 1 <= port <= 65535)):
        raise AgentError("Server must be an HTTPS origin without credentials or a path")
    host = host.lower()
    try:
        address = ipaddress.ip_address(host)
        loopback = address.is_loopback
    except ValueError:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host):
            raise AgentError("Invalid control server hostname")
        loopback = host == "localhost"
    if parsed.scheme == "http" and not (development is True and loopback):
        raise AgentError("HTTP is allowed only for explicit localhost development")
    authority = "[" + host + "]" if ":" in host else host
    if port is not None:
        authority += ":" + str(port)
    return parsed.scheme + "://" + authority


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise ApiError("Control server redirects are forbidden", status=code)


class Client:
    def __init__(self, server, credential=None, development=False, opener=None):
        self.server = server_url(server, development)
        self.credential = _secret(credential) if credential is not None else None
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def post(self, endpoint, body):
        if endpoint not in ("/api/agent/enroll", "/api/agent/heartbeat", "/api/agent/results"):
            raise AgentError("Unknown agent API endpoint")
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "xiaowork-watch-agent/1"}
        if self.credential is not None:
            headers["Authorization"] = "Bearer " + self.credential
        request = urllib.request.Request(self.server + endpoint,
                                         data=json.dumps(body, allow_nan=False).encode("utf-8"),
                                         headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=HTTP_TIMEOUT) as response:
                if response.geturl() != request.full_url:
                    raise ApiError("Control server redirects are forbidden")
                if response.status != 200:
                    raise ApiError("Control server rejected the request", status=response.status)
                raw = response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as error:
            # Do not log a response body: it may reflect a credential or token.
            status = error.code
            error.close()
            raise ApiError("Control server rejected the request", status=status) from None
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise ApiError() from None
        if len(raw) > MAX_RESPONSE:
            raise ApiError("Control server response is too large")
        try:
            def invalid_constant(value):
                raise ValueError("Non-finite JSON")
            envelope = json.loads(raw.decode("utf-8"), parse_constant=invalid_constant)
        except (UnicodeError, ValueError):
            raise ApiError("Invalid control server response") from None
        if (not isinstance(envelope, dict) or type(envelope.get("code")) is not int
                or envelope["code"] != 0 or not isinstance(envelope.get("data"), dict)):
            raise ApiError("Control server rejected the request")
        return envelope["data"]


def validate_config(config):
    if not isinstance(config, dict) or type(config.get("schema")) is not int or config["schema"] != 1:
        raise AgentError("Unsupported agent configuration")
    if type(config.get("development", False)) is not bool:
        raise AgentError("Invalid development setting")
    if config.get("role") not in ("vps", "probe"):
        raise AgentError("Invalid agent role")
    if config.get("heartbeatSeconds") != HEARTBEAT_SECONDS or type(config.get("heartbeatSeconds")) is not int:
        raise AgentError("Invalid heartbeat interval")
    return {"schema": 1, "server": server_url(config.get("server"), config.get("development", False)),
            "credential": _secret(config.get("credential")), "role": config["role"],
            "id": _identifier(config.get("id")), "heartbeatSeconds": HEARTBEAT_SECONDS,
            "development": config.get("development", False)}


def read_config(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise AgentError("Agent configuration must be a small regular file")
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o027:
        raise AgentError("Agent configuration must not be world-readable or group-writable")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError):
        raise AgentError("Invalid agent configuration") from None
    return validate_config(config)


def enroll(server, token, destination, development=False, client=None):
    normalized = server_url(server, development)
    token = _secret(token)
    destination = Path(destination).absolute()
    if (destination.parent.resolve() != destination.parent or destination.is_symlink()
            or destination.exists() or not destination.parent.is_dir()):
        raise AgentError("Enrollment requires a new configuration file in a regular directory")
    response = (client or Client(normalized, development=development)).post("/api/agent/enroll", {"token": token})
    config = validate_config(dict(response, schema=1, server=normalized, development=development))
    descriptor = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(config, output, ensure_ascii=True, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        destination.unlink()
        raise
    return config


def public_target(value, allow_loopback=False):
    if not isinstance(value, str) or len(value) > 45 or "%" in value:
        raise AgentError("Probe target must be a fixed public IP address")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise AgentError("Probe target must be a fixed public IP address") from None
    if allow_loopback and address.is_loopback:
        return address
    if (not address.is_global or address.is_multicast or address.is_unspecified
            or address.is_reserved or getattr(address, "ipv4_mapped", None) is not None):
        raise AgentError("Probe target must be a fixed public IP address")
    if address.version == 4:
        denied = any(address in network for network in _V4_DENIED)
    else:
        denied = (address not in ipaddress.ip_network("2000::/3")
                  or address in ipaddress.ip_network("2001::/23")
                  or address in ipaddress.ip_network("2001:db8::/32"))
    if denied:
        raise AgentError("Probe target must be a fixed public IP address")
    return address


def validate_task(task, allow_loopback=False):
    if not isinstance(task, dict):
        raise AgentError("Invalid probe task")
    _identifier(task.get("id"))
    _identifier(task.get("hostId"))
    if type(task.get("sent")) is not int or task["sent"] != PING_COUNT:
        raise AgentError("Probe task must request exactly five packets")
    if type(task.get("timeoutSeconds")) is not int or not 5 <= task["timeoutSeconds"] <= PING_TIMEOUT:
        raise AgentError("Invalid probe task timeout")
    address = public_target(task.get("target"), allow_loopback)
    return {"id": task["id"], "hostId": task["hostId"], "target": str(address),
            "sent": PING_COUNT, "timeoutSeconds": task["timeoutSeconds"]}


def _failure(job_id, message, sent=0):
    return {"jobId": job_id, "sent": sent, "received": 0, "avgRttMs": None,
            "status": "ERROR", "error": message}


def parse_ping(job_id, output, returncode):
    """Use measured summary values, including partial reply success (exit 1)."""
    counts = re.findall(r"(?m)^\s*(\d+) packets transmitted,\s*(\d+) (?:packets )?received(?:,.*)?$", output)
    if len(counts) != 1:
        return _failure(job_id, "ping did not provide packet statistics")
    sent, received = map(int, counts[0])
    if not 1 <= sent <= PING_COUNT or not 0 <= received <= sent:
        return _failure(job_id, "ping reported invalid packet statistics")
    if returncode not in (0, 1):
        return _failure(job_id, "ping execution failed (exit " + str(returncode) + ")", sent)
    if received == 0:
        return {"jobId": job_id, "sent": sent, "received": 0, "avgRttMs": None, "status": "TIMEOUT"}
    rtt = re.findall(r"(?m)^\s*(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = "
                     r"[0-9.]+/([0-9.]+)/[0-9.]+/[0-9.]+ ms\s*$", output)
    try:
        average = float(rtt[0]) if len(rtt) == 1 else float("nan")
    except ValueError:
        average = float("nan")
    if not math.isfinite(average) or not 0 <= average <= 60000:
        return _failure(job_id, "ping did not provide valid RTT statistics", sent)
    return {"jobId": job_id, "sent": sent, "received": received, "avgRttMs": average, "status": "OK"}


def execute_ping(task, allow_loopback=False, runner=subprocess.run):
    try:
        task = validate_task(task, allow_loopback)
    except AgentError:
        # The controller's validation normally prevents this; never ping an
        # unsafe target merely because it arrived over an authenticated API.
        return _failure(_identifier(task.get("id")), "Unsafe or invalid probe task")
    address = ipaddress.ip_address(task["target"])
    arguments = ["/usr/bin/ping", "-4" if address.version == 4 else "-6", "-n", "-q",
                 "-c", str(task["sent"]), "-i", "1", "-W", "2", "-w", str(task["timeoutSeconds"]),
                 "--", task["target"]]
    environment = os.environ.copy()
    environment.update(LC_ALL="C", LANG="C", IPUTILS_PING_PTR_LOOKUP="0")
    try:
        result = runner(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                        env=environment, timeout=task["timeoutSeconds"] + 2, check=False)
    except subprocess.TimeoutExpired:
        return _failure(task["id"], "ping process exceeded its deadline; statistics unavailable")
    except OSError:
        return _failure(task["id"], "ping could not start; check installation and ICMP permission")
    output = (result.stdout + b"\n" + result.stderr).decode("ascii", errors="replace")
    return parse_ping(task["id"], output, result.returncode)


def heartbeat_tasks(response, role):
    if (not isinstance(response, dict) or response.get("role") != role
            or type(response.get("heartbeatSeconds")) is not int or response["heartbeatSeconds"] != HEARTBEAT_SECONDS):
        raise AgentError("Invalid heartbeat response")
    tasks = response.get("tasks")
    if not isinstance(tasks, list) or len(tasks) > MAX_TASKS or (role == "vps" and tasks):
        raise AgentError("Invalid heartbeat task list")
    seen = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise AgentError("Invalid probe task")
        job_id = _identifier(task.get("id"))
        if job_id in seen:
            raise AgentError("Duplicate probe job identifier")
        seen.add(job_id)
    return tasks


class Agent:
    def __init__(self, config, client=None, ping=execute_ping):
        self.config = validate_config(config)
        self.client = client or Client(self.config["server"], self.config["credential"], self.config["development"])
        self.ping = ping
        self.results, self.pending = OrderedDict(), OrderedDict()

    def _submit(self, result):
        try:
            self.client.post("/api/agent/results", result)
            return True
        except ApiError as error:
            # An expired/revoked job can never be accepted again. Otherwise
            # retain the exact measured result and retry without another ping.
            return error.status in (400, 404, 409, 410)

    def step(self):
        tasks = heartbeat_tasks(self.client.post("/api/agent/heartbeat", {}), self.config["role"])
        new = [task for task in tasks if task["id"] not in self.results]
        if new:
            # Ten bounded subprocesses run concurrently, so a full batch takes
            # at most each ping's 8-second deadline plus 2-second kill margin.
            with ThreadPoolExecutor(max_workers=MAX_TASKS) as pool:
                measured = list(pool.map(self.ping, new, timeout=12))
            for result in measured:
                self.results[result["jobId"]] = result
        for task in tasks:
            self.pending[task["id"]] = self.results[task["id"]]
        while len(self.results) > MAX_PENDING:
            old, _ = self.results.popitem(last=False)
            self.pending.pop(old, None)
        while len(self.pending) > MAX_PENDING:
            self.pending.popitem(last=False)
        batch = list(self.pending.items())[:MAX_TASKS]
        if batch:
            with ThreadPoolExecutor(max_workers=MAX_TASKS) as pool:
                accepted = list(pool.map(self._submit, [result for _, result in batch], timeout=HTTP_TIMEOUT + 2))
            for (job_id, _), completed in zip(batch, accepted):
                if completed:
                    self.pending.pop(job_id, None)
        return len(new)

    def run(self, stop):
        retry = 5
        while not stop.is_set():
            started = time.monotonic()
            try:
                self.step()
                retry = 5
                wait = max(1, HEARTBEAT_SECONDS - (time.monotonic() - started))
            except (AgentError, OSError, TimeoutError, FutureTimeoutError):
                # Fixed text prevents an API error body from leaking secrets.
                print("[agent] Control connection or task failed; retrying.", file=sys.stderr, flush=True)
                wait, retry = retry, min(HEARTBEAT_SECONDS, retry * 2)
            stop.wait(wait)


def main(arguments=None):
    parser = argparse.ArgumentParser(description="xiaowork Watch outbound Linux agent")
    parser.add_argument("--config", required=True)
    parser.add_argument("--enroll", action="store_true")
    parser.add_argument("--server")
    parser.add_argument("--token")
    parser.add_argument("--development", action="store_true")
    options = parser.parse_args(arguments)
    try:
        if options.enroll:
            enroll(options.server, options.token, options.config, options.development)
            print("Agent enrollment completed.")
            return 0
        if options.server is not None or options.token is not None or options.development:
            raise AgentError("Enrollment options require --enroll")
        agent = Agent(read_config(options.config))
        stop = threading.Event()
        for name in ("SIGINT", "SIGTERM"):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
        agent.run(stop)
        return 0
    except (AgentError, OSError):
        print("Agent setup failed; check server URL, enrollment token, configuration and permissions.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
