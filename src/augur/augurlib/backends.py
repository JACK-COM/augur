"""The backends Augur asks, by protocol: systemone over HTTP, laya through a worker process
in its own venv, and command, a script fed the request on stdin.
"""
import json
import os
import select
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import errors
from . import paths
from . import qtypes


RETRY_AFTER_CAP = 30.0       # never let a report-only instrument sleep for as long as a server asks


def _systemone_key(cfg):
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


def _systemone_headers(cfg):
    k = _systemone_key(cfg)
    return {"Content-Type": "application/json", **({"Authorization": f"Bearer {k}"} if k else {})}


def _systemone_ask(cfg, state, questions, model, timeout, retries):
    model = model or cfg.get("model")
    timeout = cfg.get("timeout") or timeout
    body = json.dumps({"state": state, **({"model": model} if model else {}), "questions": questions}).encode()
    req = urllib.request.Request(cfg["url"], data=body, headers=_systemone_headers(cfg))
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
            raise errors.AugurUnavailable(f"HTTP {e.code}{' (rate limited, retries exhausted)' if e.code == 429 else ''}: {detail}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:  # ValueError covers a non-JSON body
            raise errors.AugurUnavailable(f"network or body: {e}") from e
        if not isinstance(resp, dict) or not isinstance(resp.get("answers"), dict):
            raise errors.AugurUnavailable(f"unexpected response shape: {str(resp)[:200]}")
        usage = resp.get("usage") or {}
        return {"model": resp.get("model") or model or "?", "answers": resp["answers"],
                "usage": {"input_tokens": int(usage.get("input_tokens") or 0)}}
    raise errors.AugurUnavailable("unreachable: retry loop exhausted without a response")


_laya_proc = None


_laya_lock = threading.Lock()


def _laya_worker(cfg):
    global _laya_proc
    if _laya_proc is not None and _laya_proc.poll() is None:
        return _laya_proc
    py = os.path.expanduser(cfg.get("python") or "")
    if not py or not os.path.exists(py):
        raise errors.AugurUnavailable("laya: the backend's `python` does not name an interpreter with laya installed")
    _laya_proc = subprocess.Popen([py, str(paths.HERE / "augur_laya.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1)
    return _laya_proc


def _laya_ask(cfg, state, questions, model, timeout, retries):
    """`retries` does not apply: a local worker that fails once fails the same way again.
    `timeout` bounds the wait for the answer line; past it the worker is killed, since a
    hung model load would otherwise hold the lock and every later caller behind it."""
    global _laya_proc
    req = {"state": state, "questions": questions, "checkpoint": model or cfg["checkpoint"],
           "device": cfg.get("device"), "head_max_len": cfg.get("head_max_len")}
    with _laya_lock:  # one model on one device answers one request at a time
        try:
            p = _laya_worker(cfg)
            p.stdin.write(json.dumps(req) + "\n")
            p.stdin.flush()
            ready, _, _ = select.select([p.stdout], [], [], timeout)
            if not ready:
                p.kill()
                _laya_proc = None
                raise errors.AugurUnavailable(f"laya worker gave no answer in {timeout}s; killed")
            line = p.stdout.readline()
        except (OSError, ValueError) as e:  # OSError covers an interpreter Popen cannot start
            raise errors.AugurUnavailable(f"laya worker: {e}") from e
    if not line:
        raise errors.AugurUnavailable("laya worker exited without answering (missing package, bad checkpoint, or out of memory)")
    try:
        resp = json.loads(line)
    except ValueError as e:
        raise errors.AugurUnavailable(f"laya worker sent a line that is not JSON: {line[:200]!r}") from e
    if isinstance(resp, dict) and "error" in resp:
        raise errors.AugurUnavailable(f"laya: {resp['error']}")
    if not isinstance(resp, dict) or not isinstance(resp.get("answers"), dict):
        raise errors.AugurUnavailable(f"unexpected response shape: {str(resp)[:200]}")
    usage = resp.get("usage") or {}
    resp["usage"] = {"input_tokens": int(usage.get("input_tokens") or 0)}
    resp.setdefault("model", model or cfg["checkpoint"])
    return resp


def _laya_available(cfg):
    try:
        r = _laya_ask(cfg, {"text": "banana"}, {"fruit": qtypes.noul("Is the word in `text` the name of a fruit?")}, None, 60, 0)
        return True, f"{r['model']} on {r.get('device', '?')}, context {r.get('max_len', '?')}"
    except (errors.AugurUnavailable, ValueError, OSError) as e:
        return False, str(e)


def _command_argv(cfg):
    cmd = cfg["command"]
    try:
        argv = shlex.split(cmd) if isinstance(cmd, str) else [str(a) for a in cmd]
    except ValueError as e:                 # an unbalanced quote in a hand-written manifest
        raise errors.AugurUnavailable(f"command backend: `command` does not parse ({e})") from e
    if not argv:
        raise errors.AugurUnavailable("command backend: `command` is empty")
    argv[0] = os.path.expanduser(argv[0])   # a manifest is not a shell: `~` would otherwise reach exec verbatim
    return argv


def _command_ask(cfg, state, questions, model, timeout, retries):
    """`retries` does not apply, as for laya: a command that fails once fails the same way
    again. The request is a valid `augur ask --request` object, so a wrapper can be tested
    by piping the same JSON to it by hand."""
    argv = _command_argv(cfg)
    model = model or cfg.get("model")
    wait = cfg.get("timeout") or timeout
    req = {"questions": questions, "state": state, **({"model": model} if model else {})}
    try:
        # errors="replace": a stray non-UTF-8 byte would otherwise raise past every except here
        out = subprocess.run(argv, input=json.dumps(req), capture_output=True, text=True,
                             errors="replace", timeout=wait)
    except subprocess.TimeoutExpired:
        raise errors.AugurUnavailable(f"command {argv[0]} gave no answer in {wait}s")
    except OSError as e:
        raise errors.AugurUnavailable(f"command {argv[0]}: {e}") from e
    if out.returncode != 0:
        why = (out.stderr.strip().splitlines() or ["no message"])[-1][:200]
        raise errors.AugurUnavailable(f"command {argv[0]} exited {out.returncode}: {why}")
    try:
        resp = json.loads(out.stdout)
    except ValueError as e:
        raise errors.AugurUnavailable(f"command {argv[0]} printed something that is not JSON: {out.stdout[:200]!r}") from e
    if not isinstance(resp, dict) or not isinstance(resp.get("answers"), dict):
        raise errors.AugurUnavailable(f"unexpected response shape: {str(resp)[:200]}")
    usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
    return {"model": str(resp.get("model") or model or Path(argv[0]).name), "answers": resp["answers"],
            "usage": {"input_tokens": int(usage.get("input_tokens") or 0)}}


# (ask, available) by backend type; the name a backend has is the user's, never dispatched on.
# A None check means `available` asks one real question through `ask`, which is booked in the
# ledger: a models listing was dropped because it passed for a model the host had never
# loaded, and for a pinned version it did not list.
ADAPTERS = {"systemone": (_systemone_ask, None), "laya": (_laya_ask, _laya_available),
            "command": (_command_ask, None)}
