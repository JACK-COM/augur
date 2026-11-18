"""Configuring a backend in a terminal, by questions: an Ollama model (pulled if missing),
a hosted API, laya, or a command.
"""
import getpass
import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import entries
from . import errors
from . import manifest
from . import paths


def _prompt(text, default=None):
    a = input(f"{text}{f' [{default}]' if default else ''}: ").strip()
    return a or default


def _yes(text, default=False):
    a = input(f"{text} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return a in ("y", "yes") if a else default


def _choose(text, options):
    print(text)
    for i, o in enumerate(options, 1):
        print(f"  {i}) {o}")
    while True:
        a = input("> ").strip()
        if a.isdigit() and 1 <= int(a) <= len(options):
            return int(a) - 1
        print(f"enter a number from 1 to {len(options)}")


def _ollama_base():
    h = (os.environ.get("OLLAMA_HOST") or "localhost:11434").rstrip("/")
    h = h if "://" in h else "http://" + h
    return h.replace("://0.0.0.0", "://localhost")      # a bind-all address, which a client cannot dial


def _get_json(url, timeout=10):
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
        return json.load(r)


def _ollama_models(base):
    """Ollama's pulled models, each a dict with a string `name`. Raises ValueError on another shape."""
    models = _get_json(base + "/api/tags").get("models") or []
    if not isinstance(models, list) or not all(isinstance(m, dict) and isinstance(m.get("name"), str) for m in models):
        raise ValueError("/api/tags: not a list of models")
    return models


def _ollama_pull(base, model):
    """Pull through Ollama's API, printing its progress on one line."""
    req = urllib.request.Request(base + "/api/pull", data=json.dumps({"model": model}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            for line in r:
                if not line.strip():
                    continue
                ev = json.loads(line)
                if ev.get("error"):
                    raise errors.AugurUnavailable(ev["error"])
                total, done = ev.get("total"), ev.get("completed")
                msg = ev.get("status", "") + (f" {done * 100 // total}%" if total and done else "")
                print("\r" + msg[:72].ljust(72), end="", flush=True)
    except urllib.error.HTTPError as e:
        raise errors.AugurUnavailable(f"HTTP {e.code}: {e.read().decode(errors='ignore')[:300]}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise errors.AugurUnavailable(f"pull: {e}") from e
    finally:
        print()


def _others(n):
    return "" if not n else " (1 other model is not)" if n == 1 else f" ({n} other models are not)"


def _exact_tag(tag, models):
    """`:latest` moves when the library updates, and a threshold calibrated on it moves too;
    another local tag of the same digest names the build itself."""
    if not tag.endswith(":latest"):
        return tag
    digest = next((m.get("digest") for m in models if m.get("name") == tag), None)
    return next((m["name"] for m in models if digest and m.get("digest") == digest and m["name"] != tag), tag)


def _guided_ollama():
    """-> (settings, suggested name), or None after saying why not."""
    base = _ollama_base()
    try:
        v = str(_get_json(base + "/api/version")["version"])
        models = _ollama_models(base)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"Ollama is not answering at {base} ({e}). Start it (open the app, or run `ollama serve`) and "
              f"run this again; https://ollama.com/download has it.", file=sys.stderr)
        return None
    if tuple(int(x) for x in re.findall(r"\d+", v)[:2]) < entries.OLLAMA_MIN:
        print(f"Ollama {v} is too old: decision models need {'.'.join(map(str, entries.OLLAMA_MIN))} or later. "
              f"Upgrade it and run this again.", file=sys.stderr)
        return None
    # Ollama marks a decision model with a `decision` capability, so the menu is the server's
    # word, never a list of names here; a server listing no capabilities shows every model.
    shown = [m for m in models if "decision" in (m.get("capabilities") or ["decision"])]
    i = len(shown)
    if shown:
        others = len(models) - len(shown)
        i = _choose(f"Ollama {v} has these decision models{_others(others)}. "
                    f"Which one?", [f"{m['name']}  ({(m.get('size') or 0) / 1e9:.1f} GB)" for m in shown]
                    + ["another, pulled by name"])
    if i == len(shown):
        tag = _prompt("Model to pull, by its tag on ollama.com/library (a sized tag pins the build)")
        if not tag:
            return None
        tag = tag if ":" in tag else tag + ":latest"
        try:
            _ollama_pull(base, tag)
            models = _ollama_models(base)
        except (errors.AugurUnavailable, OSError, ValueError, KeyError, TypeError) as e:
            print(f"pull {tag}: {e}", file=sys.stderr)
            return None
    else:
        tag = shown[i]["name"]
    exact = _exact_tag(tag, models)
    if exact.endswith(":latest"):
        print(f"{exact} moves when the library updates, and a calibrated threshold moves with it. To pin it, "
              f"`ollama cp {exact} {exact[:-7]}:<tag>` with the sized tag ollama.com/library lists, then run this again.")
    suggested = re.sub(r"[^A-Za-z0-9._-]", "-", exact.split(":")[0].rsplit("/", 1)[-1]).lstrip("._-")
    return {"type": "systemone", "url": base + "/v1/systemone", "model": exact, "price_per_mtok": 0.0}, suggested or "ollama"


def _guided_hosted():
    url = _prompt("The System One endpoint, e.g. https://api.example.com/v1/systemone")
    try:
        entries._check_url(url or "")
    except ValueError as e:
        print(e, file=sys.stderr)
        return None
    s = {"type": "systemone", "url": url}
    model = _prompt("The model to ask; pin a version where the host lists one (blank: the host's default)")
    if model:
        s["model"] = model
    price = _prompt("USD per million input tokens, for the usage ledger", "0")
    try:
        s["price_per_mtok"] = entries._price(price)
    except Exception:  # noqa: argparse's error type; any bad price records 0
        print(f"{price!r} is not a number; the ledger records 0")
        s["price_per_mtok"] = 0.0
    host = urllib.parse.urlparse(url).hostname or "hosted"
    parts = host.split(".")
    return s, parts[-2] if len(parts) > 1 else parts[0]


def _guided_laya():
    venv_py = paths.LAYA_VENV / "bin" / "python"
    py = _prompt("The python of a virtualenv holding laya and torch (`augur help install` makes one)",
                 str(venv_py) if venv_py.exists() else None)
    if not py:
        return None
    s = {"type": "laya", "python": py}
    ck = _prompt("Checkpoint; a fine-tune goes here", manifest._defaults()["backends"]["laya"]["checkpoint"])
    if ck != manifest._defaults()["backends"]["laya"]["checkpoint"]:
        s["checkpoint"] = ck
    return s, "laya"


def _guided_command():
    c = _prompt("The command. It reads one `augur ask --request` object on stdin and prints "
                "{answers}; `augur ask -h` shows the shape")
    if not c:
        return None
    s = {"type": "command", "command": c}
    model = _prompt("A model name to send with every request (blank: none)")
    if model:
        s["model"] = model
    try:
        return s, Path(shlex.split(c)[0]).stem
    except (ValueError, IndexError):        # an unbalanced quote: the check below names the fault
        return s, "script"


def _guided_backend(name=None):
    """Add or replace a backend in a terminal, by the same verify-then-write as the flags."""
    try:
        kind = _choose("Where does the model run?", ["Ollama, on this machine (no key, no bill)",
                                                     "a hosted System One API, with an API key",
                                                     "Laya, in a Python virtualenv",
                                                     "a script of your own"])
        got = (_guided_ollama, _guided_hosted, _guided_laya, _guided_command)[kind]()
        if got is None:
            return 3 if kind == 0 else 2
        settings, suggested = got
        while True:
            name = name or _prompt("A name for this backend", suggested)
            if name and entries.NAME_CHARS.fullmatch(name):
                break
            print(entries.NAME_RULE)
            name = None
        known = manifest._manifest()["backends"]
        shipped = manifest._defaults()["backends"].get(name)
        if shipped and shipped.get("type") == settings["type"]:
            left = sorted(set(shipped) - set(settings) - {"type"})
            print(f"{name} is shipped; what you leave out keeps its shipped value ({', '.join(left)})")
        if name in known and not _yes(f"{name} exists ({manifest._type(known[name])}, {entries._describe(known[name], manifest._type(known[name]))}). "
                                      f"Replace it?"):
            print("augur.json unchanged")
            return 1
        store = False
        if kind == 1:
            var = _prompt("Where to keep the key: a keychain service and environment variable name",
                          re.sub(r"\W", "_", name).upper() + "_API_KEY")
            while not entries.VAR_CHARS.fullmatch(var or ""):
                var = _prompt("Letters, digits and '_', not starting with a digit")
            settings["env_key"] = var
            if sys.platform == "darwin":
                settings["keychain_service"] = var
                store = _yes(f"Store the key in the keychain as {var} now? `security` prompts for it.", True)
            else:
                k = getpass.getpass(f"Paste the key to check {name} now (not echoed, not saved): ")
                if k:
                    os.environ[var] = k
                print(f"To keep it, add this to your shell profile:  export {var}=<your key>")
        return entries._save_backend(name, settings, replace=True, store_key=store, confirm=_yes)
    except (EOFError, KeyboardInterrupt):
        print("\naugur.json unchanged")
        return 1
