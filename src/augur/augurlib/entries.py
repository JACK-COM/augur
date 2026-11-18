"""The user's backend entries: listing, showing, building, saving and removing them in
augur.json, each new entry asked one real question before it is saved.
"""
import json
import os
import re
import shlex
import sys
import time

import _guard as guard

from . import backends
from . import client
from .backends import common as btype
from . import errors
from . import fsio
from . import keys
from . import manifest
from . import paths


# Flags that become entry keys, by the key each writes; the schema in BACKEND_TYPES checks them.
ENTRY_FLAGS = backends.entry_flags()


NAME_CHARS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
NAME_RULE = "a name takes letters, digits, '.', '_' and '-', starting with a letter or digit"


PROBE_TIMEOUT = 300             # the first call after Ollama loads a model can take minutes; never written


def _list_backends():
    m, user, shipped = manifest._manifest(), manifest._user_manifest().get("backends") or {}, manifest._defaults()["backends"]
    rows = [(("*" if n == m["backend"] else " ") + " " + n, manifest._type(c) or "?", manifest._origin(n, user, shipped),
             guard.relay(backends.TYPES[manifest._type(c)].describe(c), 160)) for n, c in sorted(m["backends"].items())]
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


def default_refused(name, cfg, t):
    """None when NAME may become the default; else the exit code, after saying why. A default
    that sends text off this machine receives every later call that names no backend, so a
    person chooses it at a terminal, as they confirmed the entry itself."""
    if not backends.TYPES[t].away(cfg) or manifest._manifest()["backend"] == name:
        return None
    if guard.person_confirms(f"Make {name} the default? Every call that names no backend then sends its text to "
                             f"{btype.host(cfg.get('url') or '')}.", [], "make it the default"):
        return None
    print(f"{name} is not the default: one that sends text off this machine is chosen by a person at a terminal. "
          f"Run it yourself:\n  augur configure backend {shlex.quote(name)} --default", file=sys.stderr)
    return 1


def _set_default(user, name):
    """Point `backend` at NAME in USER (augur.json's contents, written by the caller)."""
    user["backend"] = name
    if os.environ.get("AUGUR_BACKEND", name) != name:
        print(f"AUGUR_BACKEND={os.environ['AUGUR_BACKEND']} in this environment still wins over the default")


def _build_entry(name, given, replace=False):
    """The entry augur.json would hold for NAME, the merged entry Augur would run, its type,
    and what saving it would switch on that a person must confirm (the type's `gates`).
    `given` merges over the user's present entry by the manifest's own rule, or replaces it
    when `replace`. Raises ValueError naming the first fault."""
    if not NAME_CHARS.fullmatch(name):
        raise ValueError(NAME_RULE)
    if "url" in given:
        btype.check_url(given["url"])
    for k in ("keychain_service", "env_key"):
        if k in given and not btype.VAR_CHARS.fullmatch(str(given[k])):
            raise ValueError(f"{k} {given[k]!r}: letters, digits and '_', not starting with a digit")
    shipped = manifest._defaults()["backends"]
    have = (manifest._user_manifest().get("backends") or {}).get(name) or {}
    entry = dict(given) if replace else manifest._merged(have, given, shipped.get(name))
    found = manifest._entry_findings({"backends": {name: entry}}, shipped)
    if found:
        raise ValueError("; ".join(f.split(": ", 1)[-1] for f in found))
    run = manifest._merged(shipped.get(name) or {}, entry)
    before = manifest._merged(shipped.get(name) or {}, have) if (have or name in shipped) else {}
    try:
        t = manifest._resolve(name, run)
    except errors.AugurUnavailable as e:
        raise ValueError(str(e)) from None
    return entry, run, t, backends.TYPES[t].gates(run, before)


def _save_backend(name, given, *, replace=False, make_default=False, force=False, store_key=False, confirm=None):
    """Verify, then write: the entry is saved only when it answers one real question, unless
    `force` (or, guided, the user) says to save it anyway. `confirm(prompt) -> bool` asks the
    user; None means a script is driving and the flags decide."""
    try:
        entry, run, t, gated = _build_entry(name, given, replace)
    except (ValueError, errors.AugurUnavailable) as e:
        print(f"{name}: {e}", file=sys.stderr)
        return 2
    if gated:
        # Nothing runs and nothing is sent before a person says so: the probe below would
        # already run the command or send the key.
        if not guard.person_confirms(f"Save backend {name}? Every call to it will then:", gated, "save it"):
            print(f"{name} not saved: a backend that runs a program, sends text off this machine or sends a key "
                  f"is set by a person at a terminal. Run it yourself:\n  {command_line(name, given, make_default)}",
                  file=sys.stderr)
            return 1
        if run.get("keychain_service") or run.get("env_key"):
            entry["key_host"] = run["key_host"] = btype.host(run.get("url") or "")
    if store_key:
        code = keys._store_key(name, run)
        if code:
            return code
    print(f"asking {name} one question" + (" (a model loading for the first time can take a minute)"
                                           if backends.TYPES[t].cold_start(run)
                                           else ""), flush=True)
    began = time.monotonic()
    ok, reason = client._probe(name, run, t, caller="configure", timeout=PROBE_TIMEOUT)
    took = time.monotonic() - began
    if ok and took > 45 and not run.get("timeout") and "timeout" in backends.TYPES[t].schema["properties"]:
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
    if make_default:
        code = default_refused(name, run, t)
        if code:
            return code
    current = manifest._manifest()["backend"]
    if not make_default and confirm and current != name:
        make_default = confirm(f"Make {name} the default backend? It is {current} now.")
    with fsio._manifest_lock():                  # read under the lock: another run may have written since
        user = manifest._user_manifest()
        if not replace:                     # merge over the entry as it is now, keeping a same-name run's keys
            entry = {**manifest._merged(user["backends"].get(name) or {}, given, manifest._defaults()["backends"].get(name)),
                     **({"key_host": entry["key_host"]} if "key_host" in entry else {})}
        user["backends"][name] = entry
        if make_default:
            _set_default(user, name)
        path = fsio._write_json(paths.manifest(), user)
    print(f"saved {name}, a {t} backend{'' if ok else ' that has not answered'}"
          f"{', as the default' if make_default else ''} ({path})")
    print(f"before a threshold means anything on it, measure it:  augur calibrate items.json --backend {name} "
          f"--out run-{name}/")
    return 0


def command_line(name, given, make_default=False):
    """The `augur configure backend` line that would save `given`, quoted for a shell, for a
    person to run at a terminal."""
    words = ["augur", "configure", "backend", name]
    for key, value in given.items():
        flag = "--type" if key == "type" else btype.FLAGS[key].get("flag", "--" + key.replace("_", "-"))
        words += [flag, str(value) if not isinstance(value, list) else shlex.join(value)]
    # relayed: a person copies this line, and an escape or bidi character in it could hide what it runs
    return guard.relay(shlex.join(words + (["--default"] if make_default else [])), 2000)
