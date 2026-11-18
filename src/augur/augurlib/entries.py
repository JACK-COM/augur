"""The user's backend entries: listing, showing, building, saving and removing them in
augur.json, each new entry asked one real question before it is saved.
"""
import json
import math
import os
import re
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import client
from . import errors
from . import fsio
from . import keys
from . import manifest
from . import paths


# Flags that become entry keys, by the key each writes; the schema in BACKEND_TYPES checks them.
ENTRY_FLAGS = ("type", "url", "model", "keychain_service", "env_key", "timeout", "price_per_mtok",
               "python", "checkpoint", "device", "command")


NAME_CHARS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


NAME_RULE = "a name takes letters, digits, '.', '_' and '-', starting with a letter or digit"


VAR_CHARS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


OLLAMA_MIN = (0, 35)            # the first Ollama release serving /v1/systemone


PROBE_TIMEOUT = 300             # the first call after Ollama loads a model can take minutes; never written


def _describe(cfg, t):
    if t == "systemone":
        return f"{cfg.get('model') or '(the server default)'} at {cfg.get('url')}"
    if t == "laya":
        return f"{cfg.get('checkpoint')} via {cfg.get('python') or '(no interpreter named)'}"
    c = cfg.get("command", "")
    return c if isinstance(c, str) else shlex.join(str(a) for a in c)


def _list_backends():
    m, user, shipped = manifest._manifest(), manifest._user_manifest().get("backends") or {}, manifest._defaults()["backends"]
    rows = [(("*" if n == m["backend"] else " ") + " " + n, manifest._type(c) or "?", manifest._origin(n, user, shipped),
             _describe(c, manifest._type(c))) for n, c in sorted(m["backends"].items())]
    w = [max(len(r[i]) for r in rows) for i in range(3)]
    for r in rows:
        print(f"{r[0]:<{w[0]}}  {r[1]:<{w[1]}}  {r[2]:<{w[2]}}  {r[3]}")
    print("\n* the default. `augur configure backend` adds one, `augur configure backend NAME --default`\n"
          "makes one the default, and `--remove NAME` takes yours away.")
    return 0


def _show_backend(name):
    """One backend as Augur runs it, for a program or a reader without a terminal."""
    m, user, shipped = manifest._manifest(), manifest._user_manifest().get("backends") or {}, manifest._defaults()["backends"]
    if name not in m["backends"]:
        print(f"no backend named {name!r}; `augur configure backend --list` shows them", file=sys.stderr)
        return 2
    cfg = m["backends"][name]
    print(json.dumps({"name": name, "type": manifest._type(cfg), "origin": manifest._origin(name, user, shipped),
                      "default": name == m["backend"], "settings": cfg}, indent=2))
    return 0


def _remove_backend(name):
    with fsio._manifest_lock():
        return _remove_locked(name)


def _remove_locked(name):
    user = manifest._user_manifest()
    shipped = manifest._defaults()["backends"]
    b = user.get("backends") or {}
    if name not in b:
        if name in shipped:
            print(f"{name} runs on the shipped settings; there are none of yours to remove")
            return 0
        print(f"no backend named {name!r} in {paths.home() / 'augur.json'}", file=sys.stderr)
        return 2
    del b[name]
    said = f"removed {name}"
    if name in shipped:
        said += f"; {name} runs on the shipped settings again"
    elif user.get("backend") == name:
        del user["backend"]
        said += f"; it was the default, which is {manifest._defaults()['backend']} again"
    print(f"{said} ({fsio._write_json(paths.home() / 'augur.json', user)})")
    return 0


def _check_url(url):
    """A key belongs in the keychain or the environment, where Augur never prints it; a URL is
    printed by --list, so one carrying a password or a query string is refused."""
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError(f"--url {url!r} is not an http(s) address")
    if u.username or u.password or u.query:
        raise ValueError("--url may not carry a username, password or query string; give the key with "
                         "--key-service or --key-env, and Augur sends it as a header")


def _set_default(user, name):
    """Point `backend` at NAME in USER (augur.json's contents, written by the caller)."""
    user["backend"] = name
    if os.environ.get("AUGUR_BACKEND", name) != name:
        print(f"AUGUR_BACKEND={os.environ['AUGUR_BACKEND']} in this environment still wins over the default")


def _build_entry(name, given, replace=False):
    """The entry augur.json would hold for NAME, and the merged entry Augur would run.
    `given` merges over the user's present entry by the manifest's own rule, or replaces it
    when `replace`. Raises ValueError naming the first fault."""
    if not NAME_CHARS.fullmatch(name):
        raise ValueError(NAME_RULE)
    if "url" in given:
        _check_url(given["url"])
    shipped = manifest._defaults()["backends"]
    have = (manifest._user_manifest().get("backends") or {}).get(name) or {}
    entry = dict(given) if replace else manifest._merged(have, given, shipped.get(name))
    found = manifest._entry_findings({"backends": {name: entry}}, shipped)
    if found:
        raise ValueError("; ".join(f.split(": ", 1)[-1] for f in found))
    run = manifest._merged(shipped.get(name) or {}, entry)
    try:
        return entry, run, manifest._resolve(name, run)
    except errors.AugurUnavailable as e:
        raise ValueError(str(e)) from None


def _save_backend(name, given, *, replace=False, make_default=False, force=False, store_key=False, confirm=None):
    """Verify, then write: the entry is saved only when it answers one real question, unless
    `force` (or, guided, the user) says to save it anyway. `confirm(prompt) -> bool` asks the
    user; None means a script is driving and the flags decide."""
    try:
        entry, run, t = _build_entry(name, given, replace)
    except (ValueError, errors.AugurUnavailable) as e:
        print(f"{name}: {e}", file=sys.stderr)
        return 2
    if store_key:
        code = keys._store_key(name, run)
        if code:
            return code
    print(f"asking {name} one question" + (" (a model loading for the first time can take a minute)"
                                           if t == "systemone" and not (run.get("keychain_service") or run.get("env_key"))
                                           else ""), flush=True)
    began = time.monotonic()
    ok, reason = client._probe(name, run, t, caller="configure", timeout=PROBE_TIMEOUT)
    took = time.monotonic() - began
    if ok and took > 45 and not run.get("timeout") and t != "laya":
        print(f"that took {took:.0f}s; calls wait 60s, so if a cold model times out later, "
              f"`augur configure backend {name} --timeout 180` raises it")
    if ok:
        print(f"ok: {reason}")
    else:
        print(f"unavailable: {reason}", file=sys.stderr)
        if not (force or (confirm and confirm(f"{name} did not answer. Save it anyway?"))):
            print(f"{name} not saved; fix that and run this again, or add --force to save it unverified",
                  file=sys.stderr)
            return 3
    current = manifest._manifest()["backend"]
    if not make_default and confirm and current != name:
        make_default = confirm(f"Make {name} the default backend? It is {current} now.")
    with fsio._manifest_lock():                  # read under the lock: another run may have written since
        user = manifest._user_manifest()
        if not replace:                     # merge over the entry as it is now, keeping a same-name run's keys
            entry = manifest._merged(user["backends"].get(name) or {}, given, manifest._defaults()["backends"].get(name))
        user["backends"][name] = entry
        if make_default:
            _set_default(user, name)
        path = fsio._write_json(paths.manifest(), user)
    print(f"saved {name}, a {t} backend{'' if ok else ' that has not answered'}"
          f"{', as the default' if make_default else ''} ({path})")
    print(f"before a threshold means anything on it, measure it:  augur calibrate items.json --backend {name} "
          f"--out run-{name}/")
    return 0


def _positive(v):
    """argparse type: a finite number above zero, so `nan` never reaches the JSON and `0` never
    reads as unset."""
    import argparse
    try:
        f = float(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{v!r} is not a number")
    if not (math.isfinite(f) and f > 0):
        raise argparse.ArgumentTypeError(f"{v!r} must be a finite number above zero")
    return f


def _price(v):
    """argparse type: a finite price, zero for a backend with no bill."""
    return 0.0 if v.strip() in ("0", "0.0") else _positive(v)
