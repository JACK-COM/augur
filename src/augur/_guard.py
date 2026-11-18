# GENERATED from panoply-lib/guard.py (b4a5413) by sync.sh: edit the source and rerun sync.sh, never this copy.
"""guard: the checks every Panoply piece makes before text reaches a terminal or a setting
turns a passive file active.

- `safe` and `relay` make text from a document, a server, a model or a store safe to
  print: control, format, bidi, private-use and line-separator characters become spaces,
  so an escape sequence cannot rewrite a terminal and a planted newline cannot pose as a
  fresh instruction.
- `person_confirms` is the one gate for a setting that runs code, sends text off this
  machine or sends a key: true only when a person at a terminal types `yes`.
- `is_loopback` and `public_address` decide whether a URL or host stays on this machine,
  or resolves into a private network.
- `guarded` lists the settings files no agent may write by tool or shell; Locket's guards
  read it, so a new piece's file is guarded by one line here.

The standard behind them: no passive file turns active without a human act, and no agent
gets a cheap verb that does it. Accepted residual: a faked terminal (`script`, a pty)
passes `person_confirms`; that is deliberate circumvention, of a piece with editing a
guarded file by an obfuscated shell write.
"""
import ipaddress
import os
import socket
import sys
import unicodedata
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

# Control, format (bidi overrides, zero-width marks, tag characters), surrogate,
# private-use, unassigned, and line and paragraph separators.
INVISIBLE = frozenset(("Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp"))


def safe(text: object) -> str:
    """`text` with every invisible character but newline and tab made a space."""
    keep = "\n\t"
    return "".join(c if c in keep or unicodedata.category(c) not in INVISIBLE else " " for c in str(text))


def relay(text: object, limit: int = 200, lines: bool = False) -> str:
    """`text` on one line: every invisible character a space, runs collapsed, cut to
    `limit` characters with an ellipsis. `lines` keeps the newlines the text had."""
    if lines:
        return "\n".join(relay(ln, limit) for ln in str(text)[:limit * 4].split("\n"))[:limit]
    flat = "".join(" " if unicodedata.category(c) in INVISIBLE else c for c in str(text)[:limit * 4])
    flat = " ".join(flat.split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


def person_confirms(question: str, details: List[str], act: str) -> bool:
    """True only when a person at a terminal types `yes` to `question`. A hook-run or
    piped agent has no terminal, so it gets False and nothing is written; the caller then
    prints the command for the person to run themselves."""
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return False
    say = lambda line: print(line, file=sys.stderr)     # one stream for the whole exchange
    say(relay(question, 2000, lines=True))
    for line in details:
        say(f"  {relay(line)}")
    sys.stderr.write(f"Type yes to {act}: ")
    sys.stderr.flush()
    try:
        return sys.stdin.readline().strip() == "yes"
    except (EOFError, OSError):
        return False


def is_loopback(url: str) -> bool:
    """Whether `url` names this machine by `localhost` or a loopback literal, so text sent
    there stays here. A name is not resolved: a setting is judged by what it says."""
    host = (urlparse(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _public(ip: str) -> bool:
    a = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


def resolve_public(host: str, port: Optional[int] = None) -> Optional[str]:
    """The first address `host` resolves to, when every address it resolves to is public;
    None when any is loopback, private, link-local, unspecified, reserved or multicast
    (IPv4-mapped IPv6 judged as its IPv4), or when it does not resolve. A caller connects
    to the address returned, so a second lookup cannot swap in another (DNS rebinding)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return None
    addrs = [str(i[4][0]) for i in infos]
    try:
        return addrs[0] if addrs and all(_public(a) for a in addrs) else None
    except ValueError:
        return None


def public_address(host: str) -> bool:
    """Whether every address `host` resolves to is public."""
    return resolve_public(host) is not None


def guarded() -> List[Path]:
    """The settings files that decide what a piece runs or where it sends text, at the
    paths the environment names when it names them. No agent may write these."""
    home = Path.home()
    env = lambda name, default: Path(os.environ.get(name) or default).expanduser()
    return [env("PANOPLY_CONFIG", home / ".panoply" / "config.json"),
            env("AUGUR_HOME", home / ".augur") / "augur.json",
            env("GRILLE_HOME", home / ".grille") / "grille.json"]


def _selftest() -> None:
    import io
    from unittest import mock
    assert safe("a\x1b]0;pwned\x07b‮c\td\n") == "a ]0;pwned b c\td\n"
    assert relay("one\n two\x00three", 9) == "one two …"
    assert relay("x" * 5, 5) == "xxxxx"
    assert relay("a\nb", lines=True) == "a\nb"
    assert is_loopback("http://127.0.0.1:11434") and is_loopback("http://LOCALHOST/")
    assert is_loopback("http://[::1]:1/") and not is_loopback("http://127.0.0.1.nip.io/")
    assert not is_loopback("https://example.com") and not is_loopback("not a url")
    for ip, want in (("127.0.0.1", False), ("10.1.2.3", False), ("169.254.169.254", False),
                     ("0.0.0.0", False), ("::ffff:127.0.0.1", False), ("fe80::1", False),
                     ("224.0.0.1", False), ("93.184.215.14", True), ("2606:4700::1", True)):
        assert _public(ip) is want, ip
    fake = lambda *a, **k: [(0, 0, 0, "", ("93.184.215.14", 0)), (0, 0, 0, "", ("10.0.0.1", 0))]
    with mock.patch.object(socket, "getaddrinfo", fake):
        assert resolve_public("mixed.example") is None       # one inward address is enough
    with mock.patch.object(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.215.14", 0))]):
        assert resolve_public("ok.example") == "93.184.215.14"
    def gai_fails(*a, **k):
        raise socket.gaierror("no such host")
    with mock.patch.object(socket, "getaddrinfo", gai_fails):
        assert not public_address("nowhere.invalid")
    with mock.patch.object(sys, "stdin", io.StringIO("yes\n")):   # a pipe is no person
        assert person_confirms("q?", [], "do it") is False
    with mock.patch.dict(os.environ, {"AUGUR_HOME": "/tmp/a", "GRILLE_HOME": "", "PANOPLY_CONFIG": "/tmp/p.json"}):
        g = guarded()
        assert g[0] == Path("/tmp/p.json") and g[1] == Path("/tmp/a/augur.json")
        assert g[2] == Path.home() / ".grille" / "grille.json"
    print("guard selftest ok")


if __name__ == "__main__":
    _selftest()
