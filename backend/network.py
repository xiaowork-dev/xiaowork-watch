"""Bounded, DNS-pinned HTTP checks. No proxies or unvalidated redirects."""
import concurrent.futures
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit


class TargetError(ValueError):
    def __init__(self, message, kind="SSRF_BLOCKED"):
        super().__init__(message)
        self.kind = kind


_DNS_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="watch-dns")
_DNS_SLOTS = threading.BoundedSemaphore(4)
_V4_DENIED = tuple(ipaddress.ip_network(value) for value in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15",
    "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "168.63.129.16/32"))


def public_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if (not address.is_global or address.is_multicast or address.is_unspecified or address.is_reserved
            or address.is_loopback or address.is_link_local):
        return False
    if address.version == 4:
        return not any(address in network for network in _V4_DENIED)
    return (address in ipaddress.ip_network("2000::/3")
            and address not in ipaddress.ip_network("2001::/23")
            and address not in ipaddress.ip_network("2001:db8::/32"))


def host_name(value):
    if not isinstance(value, str) or not value or len(value) > 253 or "%" in value:
        raise TargetError("请输入有效 IP 或主机名。", "INVALID_TARGET")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    try:
        hostname = value.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as error:
        raise TargetError("主机名无效。", "INVALID_TARGET") from error
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
               for part in hostname.split(".")):
        raise TargetError("主机名不能包含协议、端口、路径或特殊字符。", "INVALID_TARGET")
    return hostname


def resolve_target(host, port=80, allow_private=False, deadline=None):
    hostname = host_name(host)
    deadline = deadline if deadline is not None else time.monotonic() + 3
    try:
        numeric = ipaddress.ip_address(hostname)
    except ValueError:
        numeric = None
    if numeric is not None:
        records = [(socket.AF_INET if numeric.version == 4 else socket.AF_INET6,
                    socket.SOCK_STREAM, 6, "", (hostname, port) if numeric.version == 4 else (hostname, port, 0, 0))]
    else:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not _DNS_SLOTS.acquire(blocking=False):
            raise TargetError("DNS 查询超时或已达到并发限制。", "TIMEOUT")
        future = _DNS_POOL.submit(socket.getaddrinfo, hostname, port, 0, socket.SOCK_STREAM)
        future.add_done_callback(lambda unused: _DNS_SLOTS.release())
        try:
            records = future.result(timeout=min(3, remaining))
        except concurrent.futures.TimeoutError as error:
            raise TargetError("DNS 查询超时。", "TIMEOUT") from error
        except OSError as error:
            raise TargetError("无法解析目标主机。", "DNS_ERROR") from error
    unique = []
    for family, unused_type, unused_proto, unused_name, address in records:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        if not allow_private and not public_ip(address[0]):
            raise TargetError("目标包含非公网 IP，已拒绝访问。")
        item = (family, address)
        if item not in unique:
            unique.append(item)
    if not unique:
        raise TargetError("目标没有可用的 IP 地址。", "DNS_ERROR")
    return unique[:8]


def url_target(value):
    if not isinstance(value, str) or len(value) > 1024 or any(character.isspace() for character in value):
        raise TargetError("请输入不超过 1024 字符的 HTTP/HTTPS URL。", "INVALID_URL")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.port == 0):
            raise ValueError()
        hostname = host_name(parsed.hostname)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, UnicodeError) as error:
        raise TargetError("URL 仅支持 HTTP/HTTPS，不能包含用户凭据。", "INVALID_URL") from error
    authority = "[" + hostname + "]" if ":" in hostname else hostname
    if parsed.port is not None:
        authority += ":" + str(port)
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~")
    normalized = urlunsplit((parsed.scheme, authority, path, query, ""))
    return normalized, parsed.scheme, hostname, port, path + ("?" + query if query else ""), authority


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, hostname, port, records, deadline, tls=False):
        super().__init__(hostname, port, timeout=max(0.01, deadline - time.monotonic()))
        self.records, self.deadline, self.tls = records, deadline, tls
        self.held_socket = None

    def expire(self):
        if self.held_socket is not None:
            try:
                self.held_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def connect(self):
        last_error = None
        for family, address in self.records:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("request deadline exceeded")
            connection = socket.socket(family, socket.SOCK_STREAM)
            try:
                connection.settimeout(min(2, remaining))
                self.held_socket = connection
                connection.connect(address)
                connection.settimeout(max(0.01, self.deadline - time.monotonic()))
                if self.tls:
                    connection = ssl.create_default_context().wrap_socket(connection, server_hostname=self.host)
                    self.held_socket = connection
                self.sock = connection
                return
            except ssl.SSLError:
                connection.close()
                raise
            except OSError as error:
                connection.close()
                last_error = error
        raise last_error or OSError("No usable address")


def check_http(url, method="GET", timeout_ms=5000, allow_private=False):
    start = time.monotonic()
    deadline = start + timeout_ms / 1000.0
    code = None
    kind, message = None, None
    try:
        for redirect in range(6):
            normalized, scheme, hostname, port, path, authority = url_target(url)
            records = resolve_target(hostname, port, allow_private, deadline)
            connection = _PinnedHTTP(hostname, port, records, deadline, tls=scheme == "https")
            deadline_timer = threading.Timer(max(0, deadline - time.monotonic()), connection.expire)
            deadline_timer.daemon = True
            deadline_timer.start()
            response = None
            try:
                connection.request(method, path, headers={"Host": authority, "User-Agent": "xiaowork-watch/0.4.0",
                                   "Accept": "*/*", "Accept-Encoding": "identity", "Connection": "close"})
                response = connection.getresponse()
                if time.monotonic() >= deadline:
                    raise socket.timeout("request deadline exceeded")
                code = response.status
                if code in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise TargetError("重定向缺少 Location。", "REDIRECT_ERROR")
                    if redirect == 5:
                        raise TargetError("重定向超过 5 次。", "REDIRECT_LIMIT")
                    url = urljoin(normalized, location)
                    if code == 303 and method != "HEAD":
                        method = "GET"
                    continue
                # Availability and responseTimeMs describe the HTTP status/headers.
                # Never buffer an arbitrary remote body or mark large/streaming pages DOWN.
                # http.client itself bounds header lines and total header count.
                if not 200 <= code < 400:
                    kind, message = "HTTP_STATUS", "目标返回 HTTP " + str(code) + "。"
                break
            finally:
                deadline_timer.cancel()
                if response is not None:
                    response.close()
                connection.close()
    except TargetError as error:
        kind, message = error.kind, str(error)
    except ssl.SSLError:
        kind, message = "SSL_ERROR", "TLS 证书验证或握手失败。"
    except (TimeoutError, socket.timeout):
        kind, message = "TIMEOUT", "检测请求超时。"
    except (OSError, http.client.HTTPException, UnicodeError, ValueError):
        kind, message = ("TIMEOUT", "检测请求超时。") if time.monotonic() >= deadline else ("CONNECTION_ERROR", "无法完成目标 HTTP 请求。")
    return {"success": kind is None, "httpCode": code,
            "responseTimeMs": round((time.monotonic() - start) * 1000, 2),
            "errorType": kind, "errorMessage": message}
