# GENERATED from panoply-lib/web.py (aec4985) by sync.sh: edit the source and rerun sync.sh, never this copy.
"""web: the one HTTP client the Panoply's pieces use, bounded so a hostile or slow server
cannot hold a piece, redirect it somewhere it was not sent, or walk it into this machine's
own network.

- Proxies come from the environment only. urllib's default also asks macOS's
  SystemConfiguration, and after that lookup on a remote plain-http request every child the
  process starts died of SIGSEGV before exec (measured under Python 3.9 and 3.14).
- Redirects: `follow=None` makes any 3xx a `Redirected` error; `follow="same-origin"`
  follows a hop that keeps scheme, host and port, and raises `Redirected` for any other, so
  the caller reports where it leads and a fresh request passes the caller's own checks;
  `follow="any"` is for a download whose bytes are checked by digest. A hop to another
  origin never carries `Authorization` or a cookie.
- `guard_private=True` resolves the host once, refuses it when any address is not public,
  and connects to the address it checked, so DNS rebinding has nothing to swap. Through a
  proxy the proxy connects, so only the name's addresses are checked, before the request.
- One `Deadline` spans connect, every read and every hop; `read` stops at a byte cap. A
  socket timeout alone counts per operation, which a server dripping a byte at a time holds
  open for as long as it likes. PANOPLY_DEADLINE (epoch seconds) passes one budget from a
  caller to the child processes it starts.
"""
import http.client
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional, Tuple
from urllib.parse import urlparse

import importlib

# guard's copy beside this one: inside a piece installed as a package, a piece's flat
# copy, or panoply-lib itself. By name, so a type checker reading this file alone passes.
guard: Any = (importlib.import_module("._guard", __package__) if __package__ else None)
if guard is None:
    try:
        guard = importlib.import_module("_guard")
    except ImportError:
        guard = importlib.import_module("guard")

MAX_AHEAD = 900          # a PANOPLY_DEADLINE further ahead than this is ignored


class Redirected(urllib.error.URLError):
    """A redirect the policy would not follow; `url` is where it leads."""

    def __init__(self, url: str, code: int, why: str):
        super().__init__(f"HTTP {code} redirects to {guard.relay(url, 300)} ({why})")
        self.url, self.code = url, code


class Inward(urllib.error.URLError):
    """A host that resolves into this machine's own or a private network."""


class Deadline:
    """A wall-clock budget, monotonic. `seconds` None means only PANOPLY_DEADLINE, if set,
    bounds it; both set, the sooner wins."""

    def __init__(self, seconds: Optional[float] = None):
        now = time.monotonic()
        ends = [now + seconds] if seconds is not None else []
        raw = os.environ.get("PANOPLY_DEADLINE")
        if raw:
            try:
                ahead = float(raw) - time.time()
            except ValueError:
                ahead = -1
            if 0 < ahead <= MAX_AHEAD:
                ends.append(now + ahead)
        self.end = min(ends) if ends else None

    def left(self) -> Optional[float]:
        return None if self.end is None else max(0.0, self.end - time.monotonic())

    def check(self, what: str = "the work") -> None:
        if self.end is not None and time.monotonic() >= self.end:
            raise TimeoutError(f"{what} ran past its deadline")

    def cap(self, timeout: float) -> float:
        """`timeout`, or what remains when that is sooner; never zero, which urllib reads as non-blocking."""
        left = self.left()
        return timeout if left is None else max(0.05, min(timeout, left))

    def env(self) -> dict:
        """PANOPLY_DEADLINE for a child process, so it shares this budget."""
        left = self.left()
        return {} if left is None else {"PANOPLY_DEADLINE": f"{time.time() + left:.3f}"}


def origin(url: str) -> Tuple[str, str, Optional[int]]:
    u = urlparse(url)
    default = {"http": 80, "https": 443}.get(u.scheme)
    try:
        port = u.port or default
    except ValueError:
        port = None
    return u.scheme, (u.hostname or "").lower(), port


def same_site(old: str, new: str) -> bool:
    """The same origin, or the same host moving from http to https on the default ports:
    an upgrade, which carries nothing anywhere new."""
    a, b = origin(old), origin(new)
    return a == b or (a[1] == b[1] and (a[0], a[2], b[0], b[2]) == ("http", 80, "https", 443))


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, follow: Optional[str], hops: int):
        self.follow = follow
        self.max_redirections = hops  # type: ignore[misc]  # urllib reads it per instance

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        newurl = urllib.parse.urljoin(req.full_url, newurl)
        if urlparse(newurl).scheme not in ("http", "https"):
            # ftp: would go through urllib's own handler, past every guard on this client
            raise Redirected(newurl, code, "not http or https")
        if self.follow not in ("same-origin", "any"):
            raise Redirected(newurl, code, "this request follows no redirect")
        if self.follow == "same-origin" and not same_site(req.full_url, newurl):
            raise Redirected(newurl, code, "another origin; fetch that URL itself")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and origin(newurl) != origin(req.full_url):
            for h in ("Authorization", "Proxy-Authorization", "Cookie"):
                new.headers.pop(h, None)
                new.unredirected_hdrs.pop(h, None)
        return new


def _pinned(host: str):  # type: ignore[no-untyped-def]
    """socket.create_connection, to the one public address `host` resolved to."""
    def connect(address, timeout=None, source_address=None):  # type: ignore[no-untyped-def]  # http.client always passes a timeout
        ip = guard.resolve_public(address[0], address[1])
        if ip is None:
            raise Inward(f"{guard.relay(host, 120)} resolves into this machine's own or a private network")
        return socket.create_connection((ip, address[1]), timeout, source_address)
    return connect


def _watched(make, deadline: Optional[Deadline]):  # type: ignore[no-untyped-def]
    """`make` (a create_connection), with the socket shut down when `deadline` passes: a
    server dripping its headers or an error body a byte at a time defeats every per-read
    timeout, and only closing the socket under the blocked read ends it."""
    def connect(address, timeout=None, source_address=None):  # type: ignore[no-untyped-def]
        sock = make(address, timeout, source_address)
        left = deadline.left() if deadline else None
        if left is not None:
            def cut() -> None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            t = threading.Timer(left, cut)
            t.daemon = True
            t.start()
        return sock
    return connect


def _connection(base: Any, pin: bool, deadline: Optional[Deadline]) -> Any:
    class Conn(base):  # type: ignore[valid-type,misc]
        def __init__(self, *a: Any, **k: Any):
            super().__init__(*a, **k)
            # pinned: the one public address the host resolved to (TLS still verifies the name)
            self._create_connection = _watched(_pinned(self.host) if pin else socket.create_connection, deadline)
    return Conn


def _proxied(req: urllib.request.Request) -> bool:
    return bool(getattr(req, "_tunnel_host", None)) or req.has_proxy()


def _precheck(req: urllib.request.Request) -> None:
    host = urlparse(req.full_url).hostname or ""
    if not guard.public_address(host):
        raise Inward(f"{guard.relay(host, 120)} resolves into this machine's own or a private network")


def _unwrap(open_: Any) -> Any:
    """do_open wraps every OSError in a URLError; give an Inward back as itself."""
    try:
        return open_()
    except urllib.error.URLError as e:
        if isinstance(e.reason, Inward):
            raise e.reason from None
        raise


class _GuardHTTP(urllib.request.HTTPHandler):
    def __init__(self, pin: bool, deadline: Optional[Deadline]):
        super().__init__()
        self.pin, self.deadline = pin, deadline

    def http_open(self, req):  # type: ignore[no-untyped-def]
        pin = self.pin and not _proxied(req)
        if self.pin and not pin:
            _precheck(req)
        return _unwrap(lambda: self.do_open(_connection(http.client.HTTPConnection, pin, self.deadline), req))


class _GuardHTTPS(urllib.request.HTTPSHandler):
    def __init__(self, pin: bool, deadline: Optional[Deadline]):
        super().__init__()
        self.pin, self.deadline = pin, deadline

    def https_open(self, req):  # type: ignore[no-untyped-def]
        pin = self.pin and not _proxied(req)
        if self.pin and not pin:
            _precheck(req)
        return _unwrap(lambda: self.do_open(_connection(http.client.HTTPSConnection, pin, self.deadline), req,
                                            context=getattr(self, "_context", None)))


def opener(follow: Optional[str] = None, guard_private: bool = False, hops: int = 8,
           *handlers: Any, direct: bool = False, deadline: Optional[Deadline] = None) -> urllib.request.OpenerDirector:
    """An opener with environment proxies (none when `direct`), the redirect policy, with
    `guard_private` connections only to public addresses, and with `deadline` every socket
    shut down when it passes. `handlers` are added after."""
    chain: list = [urllib.request.ProxyHandler({} if direct else urllib.request.getproxies_environment()),
                   _Redirects(follow, hops)]
    if guard_private or deadline:
        chain += [_GuardHTTP(guard_private, deadline), _GuardHTTPS(guard_private, deadline)]
    return urllib.request.build_opener(*chain, *handlers)


def open_url(req: Any, timeout: float, deadline: Optional[Deadline] = None, follow: Optional[str] = None,
             guard_private: bool = False, hops: int = 8) -> Any:
    """Open `req` (a URL or Request) under the policy, the connect bounded by the deadline."""
    t = deadline.cap(timeout) if deadline else timeout
    if deadline:
        deadline.check("the request")
    url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
    # a loopback host is reached directly: a proxy the environment names would carry this
    # machine's own traffic, and any key on it, to another one
    return opener(follow, guard_private, hops, direct=guard.is_loopback(url), deadline=deadline).open(req, timeout=t)


def read(resp: Any, cap: int, deadline: Optional[Deadline] = None, chunk: int = 65536) -> bytes:
    """The body, at most `cap` bytes (more raises ValueError), checking the deadline after
    every chunk the socket hands over. read1 returns what has arrived rather than waiting
    for a full chunk, so a drip trips the deadline within one socket timeout."""
    out = bytearray()
    while True:
        if deadline:
            deadline.check("reading the response")
        try:
            part = resp.read1(chunk) if hasattr(resp, "read1") else resp.read(chunk)
        except (OSError, http.client.HTTPException):
            if deadline:
                deadline.check("reading the response")
            raise
        if not part:
            if deadline:
                deadline.check("reading the response")   # the socket was cut at the deadline, not ended
            return bytes(out)
        out += part
        if len(out) > cap:
            raise ValueError(f"response passed {cap} bytes")


def _selftest() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from unittest import mock

    seen: list = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def do_GET(self) -> None:
            seen.append((self.path, self.headers.get("Authorization")))
            if self.path == "/same":
                self.send_response(302); self.send_header("Location", "/end"); self.end_headers()
            elif self.path == "/away":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{port}/end"); self.end_headers()
            elif self.path == "/hdrip":
                self.wfile.write(b"HTTP/1.1 200 OK\r\nX-A: ")
                for _ in range(100):
                    try:
                        self.wfile.write(b"a"); self.wfile.flush()
                    except OSError:
                        return
                    time.sleep(0.05)
            elif self.path == "/drip":
                self.send_response(200); self.send_header("Content-Length", "100"); self.end_headers()
                for _ in range(100):
                    try:
                        self.wfile.write(b"x"); self.wfile.flush()
                    except OSError:
                        return
                    time.sleep(0.05)
            else:
                body = b"x" * (5000 if self.path == "/big" else 3)
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    with mock.patch.dict(os.environ, {"PANOPLY_DEADLINE": "", "http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"}):
        req = urllib.request.Request(base + "/same", headers={"Authorization": "Bearer s"})
        assert read(open_url(req, 5, follow="same-origin"), 100) == b"xxx"
        assert seen[-1] == ("/end", "Bearer s"), seen   # same origin: the same server
        req = urllib.request.Request(base + "/away", headers={"Authorization": "Bearer s"})
        assert read(open_url(req, 5, follow="any"), 100) == b"xxx"
        assert seen[-1] == ("/end", None), seen        # another origin: the key stays behind
        for follow in (None, "same-origin"):
            try:
                open_url(base + "/away", 5, follow=follow)
                raise AssertionError("cross-origin redirect followed")
            except Redirected as e:
                assert e.url == f"http://localhost:{port}/end" and e.code == 302
        try:
            read(open_url(base + "/big", 5), 1000)
            raise AssertionError("cap ignored")
        except ValueError:
            pass
        d = Deadline(0.6)
        t0 = time.monotonic()
        try:
            read(open_url(base + "/drip", 5, deadline=d), 1000, d)
            raise AssertionError("drip outlived the deadline")
        except TimeoutError:
            assert time.monotonic() - t0 < 2, "deadline overshot"
        d = Deadline(0.6)
        t0 = time.monotonic()
        try:
            read(open_url(base + "/hdrip", 5, deadline=d), 1000, d)
            raise AssertionError("a header drip outlived the deadline")
        except (TimeoutError, OSError, http.client.HTTPException, urllib.error.URLError):
            assert time.monotonic() - t0 < 2, "header drip overshot the deadline"
        try:
            open_url(base + "/end", 5, guard_private=True)
            raise AssertionError("loopback reached under guard_private")
        except Inward:
            pass
        assert origin("https://H.example/x") == origin("https://h.example:443/y")
        assert origin("http://h.example/") != origin("https://h.example/")
        assert same_site("http://h.example/a", "https://h.example/b")
        assert not same_site("https://h.example/a", "http://h.example/b")      # never down
        assert not same_site("http://h.example/a", "https://h.example:8443/b")
    with mock.patch.dict(os.environ, {"PANOPLY_DEADLINE": str(time.time() + 5)}):
        assert 4 < (Deadline(60).left() or 0) <= 5 and 4 < (Deadline().left() or 0) <= 5
    for raw in (str(time.time() - 5), str(time.time() + 5000), "soon"):
        with mock.patch.dict(os.environ, {"PANOPLY_DEADLINE": raw}):
            assert Deadline().left() is None, raw
    srv.shutdown()
    print("web selftest ok")


if __name__ == "__main__":
    _selftest()
