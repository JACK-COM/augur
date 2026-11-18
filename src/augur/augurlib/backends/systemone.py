"""The systemone backend: TypeSafe's System One API over HTTP, which Jev's host and Ollama
0.35+ (at /v1/systemone) both serve, so any such model is a `url` and a `model`.
"""
import http.client
import json
import math
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, List, Optional, Tuple

import _guard as guard
import _web as web

from .. import errors
from . import common

RETRY_AFTER_CAP = 30.0       # never let a report-only instrument sleep for as long as a server asks
REPLY_CAP = 1 << 20          # an answer to typed questions is small; a larger body is not one
OLLAMA_MIN = (0, 35)         # the first Ollama release serving /v1/systemone


def key(cfg: dict) -> Optional[str]:
    """The API key, or None for a server that takes none (an entry naming neither
    `keychain_service` nor `env_key`, as Ollama's does). Keychain first, then environment."""
    service, env = cfg.get("keychain_service"), cfg.get("env_key")
    if not service and not env:
        return None
    if service:
        found = keychain(service)
        if found:
            return found
    k = os.environ.get(env, "").strip() if env else ""
    if k:
        return k
    raise errors.NoKey("no key: " + " and ".join(
        ([f"keychain service {service} empty"] if service else []) + ([f"{env} unset"] if env else [])))


def backoff(attempt: int, retry_after: Optional[str]) -> float:
    """Seconds before the next try: what the server asked, capped, else doubling from one
    second with jitter, so parallel callers that failed together do not retry together."""
    import random
    try:
        asked = float(retry_after) if retry_after else None
    except ValueError:                        # an HTTP-date Retry-After is legal; back off on the schedule
        asked = None
    if asked is not None and math.isfinite(asked) and asked >= 0:
        return min(asked, RETRY_AFTER_CAP)
    return min(RETRY_AFTER_CAP, 2.0 ** attempt) * random.uniform(0.5, 1.0)


_KEYS: dict = {}
_KEYS_LOCK = threading.Lock()


def keychain(service: str) -> Optional[str]:
    """The keychain item's secret, read once per process: each read is a `security` run,
    which was nearly all of an ask's own cost. One reader at a time, so a pool of workers
    raises one keychain prompt, not one each; only a found key is kept, so a locked keychain
    is asked again. `forget()` after storing a new one."""
    with _KEYS_LOCK:
        if service not in _KEYS:
            found = _read_keychain(service)
            if found:
                _KEYS[service] = found
        return _KEYS.get(service)


def _read_keychain(service: str) -> Optional[str]:
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def forget() -> None:
    with _KEYS_LOCK:
        _KEYS.clear()


def headers(cfg: dict) -> dict:
    """The request headers, the key included only for the host a person confirmed it for
    (`key_host`, written when they did) and only over https off this machine."""
    k = key(cfg)
    if k:
        url = cfg["url"]
        if cfg.get("key_host") != common.host(url):
            raise errors.AugurUnavailable(f"the key was confirmed for {cfg.get('key_host') or 'no host'}, not "
                                          f"{common.host(url)}; a person at a terminal confirms the new host with  "
                                          f"augur configure backend <name> --url {url}")
        if common.off_machine(url) and urllib.parse.urlparse(url).scheme != "https":
            raise errors.AugurUnavailable(f"{url} is not https, so the key would cross the network readable")
    return {"Content-Type": "application/json", **({"Authorization": f"Bearer {k}"} if k else {})}


def gates(new: dict, old: dict) -> List[str]:
    url, keyed = new.get("url") or "", bool(new.get("keychain_service") or new.get("env_key"))
    out = []
    if common.off_machine(url):
        if urllib.parse.urlparse(url).scheme != "https":
            raise ValueError(f"--url {url}: a backend off this machine is reached over https")
        if url != old.get("url"):
            out.append(f"send every text Augur asks it to {common.host(url)}")
    moved = (any(new.get(k) != old.get(k) for k in ("url", "keychain_service", "env_key"))
             or new.get("key_host") != common.host(url))   # a key never confirmed for this host
    if keyed and moved:
        where = " and ".join(([f"keychain service {new['keychain_service']}"] if new.get("keychain_service") else [])
                             + ([f"environment variable {new['env_key']}"] if new.get("env_key") else []))
        out.append(f"send the key in {where} to {common.host(url)}")
    return out


def ask(cfg: dict, state: Any, questions: dict, model: Optional[str], timeout: float, retries: int) -> dict:
    model = model or cfg.get("model")
    timeout = cfg.get("timeout") or timeout
    body = json.dumps({"state": state, **({"model": model} if model else {}), "questions": questions}).encode()
    req = urllib.request.Request(cfg["url"], data=body, headers=headers(cfg))
    for attempt in range(retries + 1):
        last = attempt == retries
        try:
            # no redirect: a 3xx would carry the request, key included, somewhere not confirmed
            # one deadline over connect and every read: a socket timeout alone counts per read,
            # which a server dripping a byte at a time holds open as long as it likes
            limit = web.Deadline(timeout)
            with web.open_url(req, timeout, deadline=limit, follow=None) as r:
                resp = common.parse(web.read(r, REPLY_CAP, limit))
        except urllib.error.HTTPError as e:
            ra = e.headers.get("retry-after") if e.headers else None
            if (e.code == 429 or 500 <= e.code < 600) and not last:
                e.close()
                time.sleep(min(backoff(attempt, ra), timeout))
                continue
            detail = guard.relay(e.read(4096).decode(errors="ignore"), 300)
            e.close()
            raise errors.AugurUnavailable(f"HTTP {e.code}{' (retries exhausted)' if e.code == 429 or e.code >= 500 else ''}: "
                                          f"{detail}") from e
        except (http.client.HTTPException, ConnectionError) as e:
            # cut mid-reply or reset: asked again, as a 5xx is; a timeout is not, since the
            # server may still be answering (and billing) the first one
            if not last:
                time.sleep(backoff(attempt, None))
                continue
            raise errors.AugurUnavailable(f"network: {guard.relay(e, 300)} (retries exhausted)") from e
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
            # ValueError covers a non-JSON or oversized body, TimeoutError the deadline
            raise errors.AugurUnavailable(f"network or body: {guard.relay(e, 300)}") from e
        return common.reply(resp, model or "?")
    raise errors.AugurUnavailable("unreachable: retry loop exhausted without a response")


def describe(cfg: dict) -> str:
    return f"{cfg.get('model') or '(the server default)'} at {cfg.get('url')}"


def leftovers(cfg: dict) -> List[str]:
    s = cfg.get("keychain_service")
    return [f"  a key:        security delete-generic-password -a \"$USER\" -s {shlex.quote(s)}"] if s else []


# ---- guided: Ollama on this machine, or a hosted API with a key

def _ollama_base() -> str:
    h = (os.environ.get("OLLAMA_HOST") or "localhost:11434").rstrip("/")
    h = h if "://" in h else "http://" + h
    return h.replace("://0.0.0.0", "://localhost")      # a bind-all address, which a client cannot dial


def _get_json(url: str, timeout: float = 10) -> Any:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
        return json.load(r)


def _ollama_models(base: str) -> list:
    """Ollama's pulled models, each a dict with a string `name`. Raises ValueError on another shape."""
    models = _get_json(base + "/api/tags").get("models") or []
    if not isinstance(models, list) or not all(isinstance(m, dict) and isinstance(m.get("name"), str) for m in models):
        raise ValueError("/api/tags: not a list of models")
    return models


def _ollama_pull(base: str, model: str) -> None:
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
                    raise errors.AugurUnavailable(guard.relay(ev["error"], 300))
                total, done = ev.get("total"), ev.get("completed")
                msg = ev.get("status", "") + (f" {done * 100 // total}%" if total and done else "")
                print("\r" + guard.relay(msg, 72).ljust(72), end="", flush=True)
    except urllib.error.HTTPError as e:
        raise errors.AugurUnavailable(f"HTTP {e.code}: {guard.relay(e.read(4096).decode(errors='ignore'), 300)}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise errors.AugurUnavailable(f"pull: {e}") from e
    finally:
        print()


def _others(n: int) -> str:
    return "" if not n else " (1 other model is not)" if n == 1 else f" ({n} other models are not)"


def _exact_tag(tag: str, models: list) -> str:
    """`:latest` moves when the library updates, and a threshold calibrated on it moves too;
    another local tag of the same digest names the build itself."""
    if not tag.endswith(":latest"):
        return tag
    digest = next((m.get("digest") for m in models if m.get("name") == tag), None)
    return next((m["name"] for m in models if digest and m.get("digest") == digest and m["name"] != tag), tag)


def guided_ollama(ui: Any, shipped: Optional[dict]) -> Optional[Tuple[dict, str]]:
    base = _ollama_base()
    try:
        v = guard.relay(_get_json(base + "/api/version")["version"], 40)
        models = _ollama_models(base)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"Ollama is not answering at {base} ({e}). Start it (open the app, or run `ollama serve`) and "
              f"run this again; https://ollama.com/download has it.", file=sys.stderr)
        return None
    if tuple(int(x) for x in re.findall(r"\d+", v)[:2]) < OLLAMA_MIN:
        print(f"Ollama {v} is too old: decision models need {'.'.join(map(str, OLLAMA_MIN))} or later. "
              f"Upgrade it and run this again.", file=sys.stderr)
        return None
    # Ollama marks a decision model with a `decision` capability, so the menu is the server's
    # word, never a list of names here; a server listing no capabilities shows every model.
    shown = [m for m in models if "decision" in (m.get("capabilities") or ["decision"])]
    i = len(shown)
    if shown:
        others = len(models) - len(shown)
        i = ui.choose(f"Ollama {v} has these decision models{_others(others)}. "
                      f"Which one?", [f"{guard.relay(m['name'], 80)}  ({(m.get('size') or 0) / 1e9:.1f} GB)" for m in shown]
                      + ["another, pulled by name"])
    if i == len(shown):
        tag = ui.prompt("Model to pull, by its tag on ollama.com/library (a sized tag pins the build)")
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


def guided_hosted(ui: Any, shipped: Optional[dict]) -> Optional[Tuple[dict, str]]:
    url = ui.prompt("The System One endpoint, e.g. https://api.example.com/v1/systemone")
    try:
        common.check_url(url or "")
    except ValueError as e:
        print(e, file=sys.stderr)
        return None
    s: dict = {"type": "systemone", "url": url}
    model = ui.prompt("The model to ask; pin a version where the host lists one (blank: the host's default)")
    if model:
        s["model"] = model
    price = ui.prompt("USD per million input tokens, for the usage ledger", "0")
    try:
        s["price_per_mtok"] = common.price(price)
    except Exception:  # noqa: argparse's error type; any bad price records 0
        print(f"{price!r} is not a number; the ledger records 0")
        s["price_per_mtok"] = 0.0
    host = urllib.parse.urlparse(url).hostname or "hosted"
    parts = host.split(".")
    return s, parts[-2] if len(parts) > 1 else parts[0]


def hosted_key(ui: Any, name: str, settings: dict) -> bool:
    """Where a hosted entry's key lives, asked once its name is known; True to store it now."""
    import getpass
    var = ui.prompt("Where to keep the key: a keychain service and environment variable name",
                    re.sub(r"\W", "_", name).upper() + "_API_KEY")
    while not common.VAR_CHARS.fullmatch(var or ""):
        var = ui.prompt("Letters, digits and '_', not starting with a digit")
    settings["env_key"] = var
    if sys.platform == "darwin":
        settings["keychain_service"] = var
        return bool(ui.yes(f"Store the key in the keychain as {var} now? `security` prompts for it.", True))
    k = getpass.getpass(f"Paste the key to check {name} now (not echoed, not saved): ")
    if k:
        os.environ[var] = k
    print(f"To keep it, add this to your shell profile:  export {var}=<your key>")
    return False


TYPE = common.BackendType(
    name="systemone",
    schema={"type": "object", "additionalProperties": False, "required": ["url"], "properties": {
        "type": common.TYPE,
        "url": {"type": "string", "description": "The System One endpoint, e.g. http://localhost:11434/v1/systemone."},
        "model": {"type": "string", "description": "The model to ask; pin a version, since a threshold does not transfer."},
        "keychain_service": {"type": "string", "description": "macOS keychain service holding the API key. "
                             "Omit it and `env_key` for a server that takes no key."},
        "env_key": {"type": "string", "description": "Environment variable holding the API key."},
        "key_host": {"type": "string", "description": "host:port the key may be sent to, written by "
                     "`augur configure backend` when a person confirms it; any other host gets no key."},
        "models": common.MODELS,
        "workers": common.WORKERS,
        "timeout": common.TIMEOUT,
        "price_per_mtok": common.PRICE}},
    ask=ask, describe=describe, flags=("url", "model", "keychain_service", "env_key"),
    kinds=(common.Kind("Ollama, on this machine (no key, no bill)", guided_ollama, declined=3),
           common.Kind("a hosted System One API, with an API key", guided_hosted, hosted_key)),
    cold_start=lambda cfg: not (cfg.get("keychain_service") or cfg.get("env_key")),
    away=lambda cfg: common.off_machine(cfg.get("url") or ""),
    # a keyless server on this machine (Ollama) serves whatever models it has pulled
    any_model=lambda cfg: not common.off_machine(cfg.get("url") or "") and not (cfg.get("keychain_service") or cfg.get("env_key")),
    pool=lambda cfg: 6 if common.off_machine(cfg.get("url") or "") else 1,
    gates=gates,
    leftovers=leftovers,
)
