"""The systemone backend: TypeSafe's System One API over HTTP, which Jev's host and Ollama
0.35+ (at /v1/systemone) both serve, so any such model is a `url` and a `model`.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, List, Optional, Tuple

from .. import errors
from . import common

RETRY_AFTER_CAP = 30.0       # never let a report-only instrument sleep for as long as a server asks
OLLAMA_MIN = (0, 35)         # the first Ollama release serving /v1/systemone


def key(cfg: dict) -> Optional[str]:
    """The API key, or None for a server that takes none (an entry naming neither
    `keychain_service` nor `env_key`, as Ollama's does). Keychain first, then environment."""
    service, env = cfg.get("keychain_service"), cfg.get("env_key")
    if not service and not env:
        return None
    if service:
        try:
            out = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                                 capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    k = os.environ.get(env, "").strip() if env else ""
    if k:
        return k
    raise errors.NoKey("no key: " + " and ".join(
        ([f"keychain service {service} empty"] if service else []) + ([f"{env} unset"] if env else [])))


def headers(cfg: dict) -> dict:
    k = key(cfg)
    return {"Content-Type": "application/json", **({"Authorization": f"Bearer {k}"} if k else {})}


def ask(cfg: dict, state: Any, questions: dict, model: Optional[str], timeout: float, retries: int) -> dict:
    model = model or cfg.get("model")
    timeout = cfg.get("timeout") or timeout
    body = json.dumps({"state": state, **({"model": model} if model else {}), "questions": questions}).encode()
    req = urllib.request.Request(cfg["url"], data=body, headers=headers(cfg))
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                ra = e.headers.get("retry-after")
                e.close()
                try:
                    delay = min(float(ra), RETRY_AFTER_CAP) if ra else 1.5 * (attempt + 1)
                except ValueError:  # an HTTP-date Retry-After is legal; back off on the schedule instead
                    delay = 1.5 * (attempt + 1)
                time.sleep(delay)
                continue
            detail = e.read().decode(errors="ignore")[:300]
            e.close()
            raise errors.AugurUnavailable(f"HTTP {e.code}{' (rate limited, retries exhausted)' if e.code == 429 else ''}: {detail}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:  # ValueError covers a non-JSON body
            raise errors.AugurUnavailable(f"network or body: {e}") from e
        return common.reply(resp, model or "?")
    raise errors.AugurUnavailable("unreachable: retry loop exhausted without a response")


def describe(cfg: dict) -> str:
    return f"{cfg.get('model') or '(the server default)'} at {cfg.get('url')}"


def leftovers(cfg: dict) -> List[str]:
    s = cfg.get("keychain_service")
    return [f"  a key:        security delete-generic-password -a \"$USER\" -s {s}"] if s else []


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
        v = str(_get_json(base + "/api/version")["version"])
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
                      f"Which one?", [f"{m['name']}  ({(m.get('size') or 0) / 1e9:.1f} GB)" for m in shown]
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
        "timeout": common.TIMEOUT,
        "price_per_mtok": common.PRICE}},
    ask=ask, describe=describe, flags=("url", "model", "keychain_service", "env_key"),
    kinds=(common.Kind("Ollama, on this machine (no key, no bill)", guided_ollama),
           common.Kind("a hosted System One API, with an API key", guided_hosted, hosted_key)),
    cold_start=lambda cfg: not (cfg.get("keychain_service") or cfg.get("env_key")),
    leftovers=leftovers,
)
