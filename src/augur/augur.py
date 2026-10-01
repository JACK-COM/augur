#!/usr/bin/env python3
"""augur: typed questions to a decision model, calibrated probabilities back, no text.

A state (string, object or array) and a map of typed questions go to a backend that
answers every question against the same state and returns a probability (noul), a
choice with per-option probabilities (choice) or a position on ordered levels (score).
No generated text, no reasoning, no tools. Every backend reads the question literally,
so a rule it is asked to check has to be phrased as a literal yes-or-no with the
boundary cases written into the criteria; a rule that cannot be phrased that way is
one this instrument cannot see.

This file is a CLIENT and holds no questions. Each project keeps its question file
beside the rule it enforces, so the rule and its test move together. Three constraints
are built in and are not to be relaxed:

  * It informs, never blocks. A probability answers a proxy for the question a rule
    asks, and a proxy may nudge but must never refuse. Callers threshold and act; this
    file returns numbers.
  * Nothing that must succeed may depend on it. Network, key, rate limits and a local
    model's weights are all outside this file. `available()` is the positive check; on
    failure the CLI exits 3 with a one-line reason and the library raises `AugurUnavailable`.
  * A backend earns a threshold by a measured run on the project's own labelled items,
    never by a published number. `calibrate` is that run, and a threshold read from one
    backend does not transfer to another.

A backend is a named entry under `backends` with a `type`, the protocol it speaks. The
shipped `defaults.json` holds two entries in the shape a user writes, and the user's
`augur.json` in the Augur home (`~/.augur`, or `$AUGUR_HOME`) merges over it by the same
rule; the default backend comes from there, then `AUGUR_BACKEND`, then `backend=`. Types:

  systemone  TypeSafe's System One API, which Ollama also serves at /v1/systemone: a
             `url`, a `model`, and an API key only when the entry names where to find
             one (`keychain_service`, then `env_key`). The shipped `jev` entry is this type
             with the model pinned (an alias moves without notice and a tuned threshold
             moves with it) and its key in the macOS keychain service `TYPESAFE_API_KEY`
             (`augur configure backend jev --store-key`, where `security` prompts for it), the
             same variable in the environment as fallback. Never in a repo, never printed.
  laya       Convai's open-weight ModernBERT decision model, run locally through
             `augur_laya.py` inside a virtualenv holding `laya` and `torch`; the entry
             names that interpreter. Free and fast, and by its makers' own card a base to
             fine-tune rather than a zero-shot engine: calibrate before trusting a threshold.
  command    Any script: it gets one `ask --request`-shaped object on stdin ({questions,
             state, model?}) and prints {answers, model?, usage?}, so any model a script
             can wrap answers through Augur, and `augur ask --request -` is itself valid.
             An entry naming only a `command` is taken as this type.

An entry merges over the default of the same name unless it names a different type, which
replaces that default whole.

Three design facts measured with Jev on labelled product-copy sentences, which is why the
API has the shape it has:

  * The state must name the subject the question is about, or "this X" is undefined
    and a sentence pointing at a different X scores as one about the subject. `ask`
    takes `subject` and `siblings` and puts them in state as named fields.
  * Batching many items into one state moves individual scores by about 0.04 at five
    to twenty items and breaks at forty (a floor sentence scored 0.77). `batch` caps a
    call at MAX_BATCH items and re-asks anything within REASK_BAND of a threshold in
    isolation when the caller passes one.
  * Run-to-run standard deviation is 0.008 with a fresh `uid` in state, so a threshold
    read from one run is defensible; the uid is added by default.

Usage is appended per call to `usage.csv` in the Augur home (timestamp, backend, caller,
model, questions, input tokens, USD at the backend's price; a local backend logs zero).

    augur check [--backend B] [--live]           positive check; --live asks known questions
    augur configure backend [NAME] [--type T --url U --model M ...] [--list] [--remove NAME]
                                                 add or change a backend: guided in a terminal, by flags anywhere
    augur configure check [FILE] [--reset]       the questions --live asks: yours, or two built-in
    augur ask -q questions.json (-t "text" | -s state.json) [--subject S] [--siblings A,B]
    augur calibrate items.json [--backend B] [--runs N] [--limit N] [--out DIR]
                               [--compare means.json] [--questions a,b]
    augur selftest                               the client against a stand-in backend, offline
    augur help install                           the install steps an agent follows
    augur --help

Library:

    from augur import ask, batch, noul, choice, score, available, AugurUnavailable
    r = ask("Buy the Bravo for the altitude.", {"cond": noul("Does `text` recommend
        the aircraft named in `subject` to the reader on a condition?", true="...",
        false="...")}, subject="Mooney M20M Bravo", caller="copy_lint")
    r["answers"]["cond"]["noul"]  -> 0.92          r["backend"] -> "jev"

`calibrate` items file: {"name", "description", "questions": {qid: question},
"items": [{"id", "class", "text", "subject", "siblings", "labels": {qid: true|false|null}}]}.
A null label is reported as a distribution only. Every item is asked in isolation with
its subject and siblings named. The report per question: AUROC of labelled positives
against labelled negatives, the lowest positive and highest negative, per-class means,
and with `--runs` above one the per-item standard deviation. `--compare` prints the
mean absolute difference against a prior run's per-item means, which is how a backend
change or a client change is shown not to have moved the instrument.
"""
import csv
import getpass
import json
import math
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.5.0"                          # the one home: pyproject.toml and the formula's test read it

HERE = Path(__file__).resolve().parent
# A package manager owns the command and moves this file on every upgrade: Homebrew's
# wrapper sets AUGUR_PACKAGED, a `uv tool` or pip install enters through `cli()`. So the
# manifest and the usage ledger live in the Augur home, never beside this file.
PACKAGED = os.environ.get("AUGUR_PACKAGED") == "1"
MAX_BATCH = 20
REASK_BAND = 0.2
RETRY_AFTER_CAP = 30.0       # never let a report-only instrument sleep for as long as a server asks

# The shipped defaults are `defaults.json` beside this file, in the same shape as a user's
# augur.json and merged by the same rule, so a default backend is configuration, never code.
# There, jev's model is pinned (see the docstring) and its price is USD per million input
# tokens from docs.typesafe.ai/models on 2026-09-17; laya's empty `python` means unavailable
# until a user names a venv, and a null `device` lets laya pick cuda, mps, cpu in that order.
DEFAULTS_FILE = HERE / "defaults.json"

_STR = {"type": "string"}
_PRICE = {"type": "number", "description": "USD per million input tokens, for the usage ledger."}
_TIMEOUT = {"type": "number", "description": "Seconds to wait for an answer (default 60)."}
_TYPE = {"type": "string", "description": "The protocol this backend speaks: systemone, laya or command."}
# One schema per backend type. An entry is checked against the schema its `type` names;
# the shared checker has no oneOf, so the dispatch is Augur's (`_entry_findings`).
BACKEND_TYPES = {
    "systemone": {"type": "object", "additionalProperties": False, "required": ["url"], "properties": {
        "type": _TYPE,
        "url": {"type": "string", "description": "The System One endpoint, e.g. http://localhost:11434/v1/systemone."},
        "model": {"type": "string", "description": "The model to ask; pin a version, since a threshold does not transfer."},
        "keychain_service": {"type": "string", "description": "macOS keychain service holding the API key. "
                             "Omit it and `env_key` for a server that takes no key."},
        "env_key": {"type": "string", "description": "Environment variable holding the API key."},
        "timeout": _TIMEOUT,
        "price_per_mtok": _PRICE}},
    "laya": {"type": "object", "additionalProperties": False, "properties": {
        "type": _TYPE,
        "python": {"type": "string", "description": "Interpreter of a venv holding laya and torch."},
        "checkpoint": {"type": "string", "description": "Checkpoint to load; a fine-tune goes here."},
        "device": {"type": ["string", "null"], "description": "cuda, mps or cpu; null lets laya pick."},
        "head_max_len": {"type": "integer"},
        "price_per_mtok": _PRICE}},
    "command": {"type": "object", "additionalProperties": False, "required": ["command"], "properties": {
        "type": _TYPE,
        "command": {"type": ["array", "string"], "items": {"type": "string"},
                    "description": "The command: an argument list, or one string split as a shell would. "
                                   "It reads {questions, state, model?} on stdin and prints {answers, model?, usage?}."},
        "model": {"type": "string", "description": "Sent as `model` in every request; --model overrides it."},
        "timeout": _TIMEOUT,
        "price_per_mtok": _PRICE}},
}
# Every key any type takes: an entry whose type cannot be resolved is checked against this,
# so a misspelled key still draws a did-you-mean.
_ANY_TYPE = {"type": "object", "additionalProperties": False,
             "properties": {k: v for s in BACKEND_TYPES.values() for k, v in s["properties"].items()}}
MANIFEST_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "augur.schema.json",
    "title": "Augur manifest (augur.json)",
    "description": "Augur's settings, merged over the defaults Augur ships in the same shape. "
                   "Every key is optional; an entry merges over the default of the same name.",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "$schema": {"type": "string", "description": "Editor hint only; ignored by Augur."},
        "backend": {"type": "string", "description": "The default backend, by its name under `backends`. "
                    "AUGUR_BACKEND and --backend override it."},
        "backends": {
            "type": "object",
            "description": "Backends by name. Each has a `type`; jev and laya are shipped entries of the same shape.",
            # for editors only: Augur itself checks each entry against BACKEND_TYPES by its type
            "additionalProperties": {
                "type": "object",
                "properties": {"type": {"enum": sorted(BACKEND_TYPES)}},
                "allOf": [{"if": {"properties": {"type": {"const": t}}, "required": ["type"]}, "then": s}
                          for t, s in BACKEND_TYPES.items()]},
        },
    },
}


def _schema_lib():
    """The shared checker: a sibling module under Homebrew or a checkout, a package
    member under pip or uv."""
    try:
        from . import _manifest_schema
    except ImportError:
        import _manifest_schema
    return _manifest_schema


def _entry_findings(data, defaults):
    """Findings for each entry under `backends`, checked against the schema its type names."""
    out = []
    backends = data.get("backends") if isinstance(data, dict) else None
    for name, entry in (backends or {}).items() if isinstance(backends, dict) else ():
        if not isinstance(entry, dict):
            continue                         # the top-level check already named it
        where = f"backends.{name}"
        t = _type(entry, defaults.get(name))
        if t is None:
            out.append(f"{where}: names no `type`; give it one of {', '.join(sorted(BACKEND_TYPES))}")
            _schema_lib().check_value(entry, _ANY_TYPE, where, out)
        elif t not in BACKEND_TYPES:
            out.append(f"{where}: unknown type {t!r}; known: {', '.join(sorted(BACKEND_TYPES))}")
        else:
            schema = BACKEND_TYPES[t]
            if entry.get("type") in (None, (defaults.get(name) or {}).get("type")):
                schema = {**schema, "required": []}      # it merges over a default that supplies them
            _schema_lib().check_value(entry, schema, where, out)
    return out


def manifest_findings(path=None):
    """Structural problems in the manifest, as lines; [] when clean or absent. `path` checks
    another file of the same shape, which is how the selftest holds the shipped defaults to it."""
    p = Path(path) if path else _home() / "augur.json"
    found = _schema_lib().validate_file(p, MANIFEST_SCHEMA)
    if p.is_file() and not any(f.startswith("manifest does not parse") for f in found):
        defaults = {} if path else _defaults().get("backends", {})
        found += _entry_findings(json.loads(p.read_text()), defaults)
    # the shared checker anchors every finding on the word "manifest"; name the file instead
    return [p.name + (f[len("manifest"):] if f.startswith("manifest") else ": " + f) for f in found]


class AugurUnavailable(RuntimeError):
    pass


class NoKey(AugurUnavailable):
    """An entry names where its key lives and nothing is there."""


class BatchTooLarge(ValueError):
    pass


# ---- manifest ----------------------------------------------------------------

def _home():
    """Read per call, so a caller (or the selftest) can point AUGUR_HOME elsewhere."""
    return Path(os.environ.get("AUGUR_HOME") or Path.home() / ".augur").expanduser()


def _defaults():
    try:
        return json.loads(DEFAULTS_FILE.read_text())
    except (OSError, ValueError) as e:
        raise AugurUnavailable(f"{DEFAULTS_FILE}: the shipped defaults do not load ({e}); reinstall augur")


def _type(entry, default=None):
    """An entry's type: its own, else the default's of the same name, else `command` for a
    0.4 entry that names only a command. None when none of those resolves."""
    return entry.get("type") or (default or {}).get("type") or ("command" if entry.get("command") else None)


def _merged(base, cfg, default=None):
    """The manifest's one rule: `cfg` merges over `base`, unless it names a type other than
    base's (resolved through `default`), which replaces base whole so no key of the old type
    lingers."""
    if cfg.get("type") and cfg["type"] != _type(base, default):
        base = {}
    return {**base, **cfg}


def _manifest():
    """The shipped defaults, then augur.json over them by `_merged`'s rule. Then AUGUR_BACKEND."""
    m = _defaults()
    p = _home() / "augur.json"
    if p.exists():
        try:
            with open(p) as f:
                user = json.load(f)
        except ValueError as e:
            raise AugurUnavailable(f"{p}: not JSON ({e})")
        if "backend" in user:
            m["backend"] = user["backend"]
        for name, cfg in (user.get("backends") or {}).items():
            m["backends"][name] = _merged(m["backends"].get(name) or {}, cfg or {})
    if os.environ.get("AUGUR_BACKEND"):
        m["backend"] = os.environ["AUGUR_BACKEND"]
    return m


def _backend(name=None):
    """(name, merged entry, type) for the named or default backend, or AugurUnavailable."""
    m = _manifest()
    name = name or m["backend"]
    if name not in m["backends"]:
        raise AugurUnavailable(f"unknown backend {name!r}; manifest knows {sorted(m['backends'])}")
    cfg = m["backends"][name]
    return name, cfg, _resolve(name, cfg)


def _resolve(name, cfg):
    """A merged entry's type, once it names one Augur speaks and every key that type requires."""
    t = _type(cfg)
    if t is None:
        raise AugurUnavailable(f"backend {name!r} names no `type`; give it one of {', '.join(sorted(BACKEND_TYPES))}")
    if t not in BACKEND_TYPES:
        raise AugurUnavailable(f"backend {name!r} has unknown type {t!r}; known: {', '.join(sorted(BACKEND_TYPES))}")
    for k in BACKEND_TYPES[t].get("required", []):
        if not cfg.get(k):
            raise AugurUnavailable(f"backend {name!r} is a {t} backend with no `{k}`")
    return t


# ---- questions ---------------------------------------------------------------

def noul(instructions, true=None, false=None):
    q = {"type": "noul", "instructions": instructions}
    if true or false:
        q["criteria"] = {k: v for k, v in (("true", true), ("false", false)) if v}
    return q


def choice(instructions, options):
    """options: {name: description-or-None}. Include a no-match option when nothing may fit."""
    return {"type": "choice", "instructions": instructions, "criteria": dict(options)}


def score(instructions, levels):
    """levels: ordered list of concrete level descriptions, at least two."""
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


# ---- ledger ------------------------------------------------------------------

def _log(backend, caller, model, nq, tokens, price):
    try:
        ledger = _home() / "usage.csv"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a", newline="") as f:
            w = csv.writer(f)
            if f.tell() == 0:  # header under the same append handle; a concurrent second header is tolerated
                w.writerow(["ts", "backend", "caller", "model", "questions", "input_tokens", "usd"])
            w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), backend, caller, model, nq,
                        tokens, f"{tokens * price / 1e6:.6f}"])
    except OSError:
        pass  # the ledger is bookkeeping, never a reason to fail a call


# ---- backend type: systemone (the System One API: Jev, Ollama, any host) -----

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
    raise NoKey("no key: " + " and ".join(
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
            raise AugurUnavailable(f"HTTP {e.code}{' (rate limited, retries exhausted)' if e.code == 429 else ''}: {detail}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:  # ValueError covers a non-JSON body
            raise AugurUnavailable(f"network or body: {e}") from e
        if not isinstance(resp, dict) or not isinstance(resp.get("answers"), dict):
            raise AugurUnavailable(f"unexpected response shape: {str(resp)[:200]}")
        usage = resp.get("usage") or {}
        return {"model": resp.get("model") or model or "?", "answers": resp["answers"],
                "usage": {"input_tokens": int(usage.get("input_tokens") or 0)}}
    raise AugurUnavailable("unreachable: retry loop exhausted without a response")


# ---- backend type: laya (local worker) ---------------------------------------

_laya_proc = None
_laya_lock = threading.Lock()


def _laya_worker(cfg):
    global _laya_proc
    if _laya_proc is not None and _laya_proc.poll() is None:
        return _laya_proc
    py = os.path.expanduser(cfg.get("python") or "")
    if not py or not os.path.exists(py):
        raise AugurUnavailable("laya: the backend's `python` does not name an interpreter with laya installed")
    _laya_proc = subprocess.Popen([py, str(HERE / "augur_laya.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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
                raise AugurUnavailable(f"laya worker gave no answer in {timeout}s; killed")
            line = p.stdout.readline()
        except (OSError, ValueError) as e:  # OSError covers an interpreter Popen cannot start
            raise AugurUnavailable(f"laya worker: {e}") from e
    if not line:
        raise AugurUnavailable("laya worker exited without answering (missing package, bad checkpoint, or out of memory)")
    try:
        resp = json.loads(line)
    except ValueError as e:
        raise AugurUnavailable(f"laya worker sent a line that is not JSON: {line[:200]!r}") from e
    if isinstance(resp, dict) and "error" in resp:
        raise AugurUnavailable(f"laya: {resp['error']}")
    if not isinstance(resp, dict) or not isinstance(resp.get("answers"), dict):
        raise AugurUnavailable(f"unexpected response shape: {str(resp)[:200]}")
    usage = resp.get("usage") or {}
    resp["usage"] = {"input_tokens": int(usage.get("input_tokens") or 0)}
    resp.setdefault("model", model or cfg["checkpoint"])
    return resp


def _laya_available(cfg):
    try:
        r = _laya_ask(cfg, {"text": "banana"}, {"fruit": noul("Is the word in `text` the name of a fruit?")}, None, 60, 0)
        return True, f"{r['model']} on {r.get('device', '?')}, context {r.get('max_len', '?')}"
    except (AugurUnavailable, ValueError, OSError) as e:
        return False, str(e)


# ---- backend type: command (any script) --------------------------------------

def _command_argv(cfg):
    cmd = cfg["command"]
    try:
        argv = shlex.split(cmd) if isinstance(cmd, str) else [str(a) for a in cmd]
    except ValueError as e:                 # an unbalanced quote in a hand-written manifest
        raise AugurUnavailable(f"command backend: `command` does not parse ({e})") from e
    if not argv:
        raise AugurUnavailable("command backend: `command` is empty")
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
        raise AugurUnavailable(f"command {argv[0]} gave no answer in {wait}s")
    except OSError as e:
        raise AugurUnavailable(f"command {argv[0]}: {e}") from e
    if out.returncode != 0:
        why = (out.stderr.strip().splitlines() or ["no message"])[-1][:200]
        raise AugurUnavailable(f"command {argv[0]} exited {out.returncode}: {why}")
    try:
        resp = json.loads(out.stdout)
    except ValueError as e:
        raise AugurUnavailable(f"command {argv[0]} printed something that is not JSON: {out.stdout[:200]!r}") from e
    if not isinstance(resp, dict) or not isinstance(resp.get("answers"), dict):
        raise AugurUnavailable(f"unexpected response shape: {str(resp)[:200]}")
    usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
    return {"model": str(resp.get("model") or model or Path(argv[0]).name), "answers": resp["answers"],
            "usage": {"input_tokens": int(usage.get("input_tokens") or 0)}}


# (ask, available) by backend type; the name a backend has is the user's, never dispatched on.
# A None check means `available` asks one real question through `ask`, which is booked in the
# ledger: a models listing was dropped because it passed for a model the host had never
# loaded, and for a pinned version it did not list.
ADAPTERS = {"systemone": (_systemone_ask, None), "laya": (_laya_ask, _laya_available),
            "command": (_command_ask, None)}


# ---- public API --------------------------------------------------------------

def available(backend=None):
    """Positive check for the backend. Returns (ok, reason)."""
    try:
        name, cfg, t = _backend(backend)
    except AugurUnavailable as e:
        return False, str(e)
    return _probe(name, cfg, t)


def _probe(name, cfg, t, caller="check", timeout=60):
    """`available` for a resolved entry, saved or not. -> (ok, reason)"""
    if ADAPTERS[t][1]:
        return ADAPTERS[t][1](cfg)
    try:          # a backend that starts is not yet one that answers
        r = _answer(name, cfg, t, {"text": "banana", "uid": uuid.uuid4().hex},
                    {"fruit": noul("Is the word in `text` the name of a fruit?")}, None, timeout, 0, caller)
        return True, f"{r['model']} answers"
    except (AugurUnavailable, ValueError, OSError) as e:
        return False, str(e)


def _check_answers(answers):
    """Refuse a non-finite or out-of-range probability. JSON parses NaN and Infinity, and a
    NaN fails every comparison, so a caller gating on `noul >= threshold` would pass it:
    an injection screen taking `max(best, nan)` keeps `best` and never withholds."""
    for qid, a in answers.items():
        if not isinstance(a, dict):
            raise AugurUnavailable(f"answer {qid!r} is not an object: {str(a)[:100]}")
        probs = [(k, a[k]) for k in ("noul", "confidence") if k in a]
        probs += [(f"probabilities.{k}", v) for k, v in (a.get("probabilities") or {}).items()]
        for k, v in probs:
            if not (isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 1):
                raise AugurUnavailable(f"answer {qid!r} has {k}={v!r}, not a probability")
        if "score" in a and not (isinstance(a["score"], (int, float)) and math.isfinite(a["score"])):
            raise AugurUnavailable(f"answer {qid!r} has score={a['score']!r}, not a finite number")


def ask(state, questions, *, subject=None, siblings=None, backend=None, model=None, uid=True, caller="cli",
        timeout=60, retries=5):
    """One call. `state` is a string (stored under `text`) or a dict; `subject` and
    `siblings` are added as named fields. Returns {backend, model, answers, usage}."""
    if isinstance(state, (str, bytes)):
        state = {"text": state if isinstance(state, str) else state.decode()}
    elif not isinstance(state, dict):
        state = {"items": state}
    else:
        state = dict(state)
    if subject is not None:
        state["subject"] = subject
    if siblings:
        state["siblings"] = list(siblings)
    if uid:
        state["uid"] = uuid.uuid4().hex
    name, cfg, t = _backend(backend)
    return _answer(name, cfg, t, state, questions, model, timeout, retries, caller)


def _answer(name, cfg, t, state, questions, model, timeout, retries, caller):
    """One checked, ledgered call to a resolved entry."""
    try:
        resp = ADAPTERS[t][0](cfg, state, questions, model, timeout, retries)
    except NoKey as e:
        raise NoKey(f"{e}; `augur configure backend {name} --store-key` stores one") from None
    _check_answers(resp["answers"])
    resp["backend"] = name
    _log(name, caller, resp["model"], len(questions), resp["usage"]["input_tokens"], float(cfg.get("price_per_mtok") or 0))
    return resp


def batch(items, make_questions, *, threshold=None, backend=None, model=None, caller="cli", **kw):
    """items: {id: text}. make_questions(id, path) -> {qname: question} where `path` is the
    state path to that item's text (e.g. "items.foo"), which the question should reference.
    Returns {id: {qname: answer}}. Caps at MAX_BATCH. With `threshold`, any Noul within
    REASK_BAND of it is re-asked in isolation and the isolated answer replaces it."""
    if len(items) > MAX_BATCH:
        raise BatchTooLarge(f"{len(items)} items; cap is {MAX_BATCH} (scores drift past that)")
    if not items:
        return {}
    qs, key_of = {}, {}
    for id_ in items:
        if "__" in str(id_):
            raise ValueError(f"item id {id_!r} contains '__', which is the key separator")
        for qn, q in make_questions(id_, f"items.{id_}").items():
            k = f"{qn}__{id_}"
            qs[k] = q
            key_of[k] = (id_, qn)
    resp = ask({"items": dict(items)}, qs, backend=backend, model=model, caller=caller, **kw)
    out = {id_: {} for id_ in items}
    for k, ans in resp["answers"].items():
        if k not in key_of:
            raise AugurUnavailable(f"unexpected answer key {k!r}")
        id_, qn = key_of[k]
        out[id_][qn] = dict(ans)
    if threshold is not None:
        for id_, answers in out.items():
            near = [qn for qn, a in answers.items() if a.get("type") == "noul" and abs(a["noul"] - threshold) < REASK_BAND]
            if near:
                iso_q = {qn: q for qn, q in make_questions(id_, "text").items() if qn in near}
                iso = ask(items[id_], iso_q, backend=backend, model=model, caller=caller + ":reask", **kw)
                for qn in near:
                    out[id_][qn] = iso["answers"][qn]
                    out[id_][qn]["reasked"] = True
    return out


# ---- calibrate ---------------------------------------------------------------

def _auroc(pos, neg):
    """Probability a random positive outranks a random negative, ties half."""
    if not pos or not neg:
        return float("nan")
    n = 0.0
    for p in pos:
        for q in neg:
            n += 1 if p > q else 0.5 if p == q else 0
    return n / (len(pos) * len(neg))


def calibrate(spec, *, backend=None, runs=1, limit=None, questions=None, workers=6, out_dir=None, compare=None,
              caller="calibrate", log=print):
    """Ask every item in isolation, `runs` times, and report per question. Returns the per-item means."""
    import statistics as st
    from concurrent.futures import ThreadPoolExecutor
    name, cfg, t = _backend(backend)
    qs = {k: v for k, v in spec["questions"].items() if not questions or k in questions}
    items = spec["items"][:limit] if limit else spec["items"]
    if t == "laya":
        workers = 1  # one worker process, one device
    t0 = time.time()
    scores = {it["id"]: {q: [] for q in qs} for it in items}
    tokens = []
    errors = []

    def one(it):
        try:
            r = ask(it["text"], qs, subject=it.get("subject"), siblings=it.get("siblings"), backend=name, caller=caller)
        except AugurUnavailable as e:
            errors.append(f"{it['id']}: {e}")
            return
        tokens.append(r["usage"]["input_tokens"])  # append is atomic under threads, += is not
        for q in qs:
            a = r["answers"].get(q, {})
            v = a.get("noul") if a.get("type") == "noul" else a.get("score", a.get("confidence"))
            if v is not None:
                scores[it["id"]][q].append(float(v))

    for _ in range(runs):
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(one, items))
    elapsed = time.time() - t0
    means = {id_: {q: (st.mean(v) if v else None) for q, v in d.items()} for id_, d in scores.items()}
    log(f"{spec.get('name', 'calibration')} on backend {name}: {len(items)} items x {runs} run(s), "
        f"{len(qs)} question(s), {sum(tokens):,} input tokens, ${sum(tokens) * float(cfg.get('price_per_mtok') or 0) / 1e6:.4f}, "
        f"{elapsed:.0f}s{', ' + str(len(errors)) + ' errors' if errors else ''}")
    for e in errors[:5]:
        log("  error " + e)
    classes = sorted({it.get("class", "?") for it in items})
    for q in qs:
        log(f"\n[{q}]")
        pos = [means[it["id"]][q] for it in items if it["labels"].get(q) is True and means[it["id"]][q] is not None]
        neg = [means[it["id"]][q] for it in items if it["labels"].get(q) is False and means[it["id"]][q] is not None]
        if pos and neg:
            log(f"  AUROC {_auroc(pos, neg):.3f}   positives n={len(pos)} min {min(pos):.2f} mean {st.mean(pos):.2f}"
                f"   negatives n={len(neg)} max {max(neg):.2f} mean {st.mean(neg):.2f}")
        for c in classes:
            v = [means[it["id"]][q] for it in items if it.get("class") == c and means[it["id"]][q] is not None]
            if v:
                v.sort()
                log(f"  {c:10s} n={len(v):3d}  mean {st.mean(v):.2f}  min {v[0]:.2f}  med {v[len(v) // 2]:.2f}  max {v[-1]:.2f}")
        if runs > 1:
            sds = [st.pstdev(scores[it["id"]][q]) for it in items if len(scores[it["id"]][q]) > 1]
            if sds:
                log(f"  run-to-run sd: mean {st.mean(sds):.3f}  max {max(sds):.3f}  n>0.05 {sum(1 for s in sds if s > 0.05)}")
        if compare:
            prev = json.load(open(compare))
            d = [abs(means[i][q] - prev[i][q]) for i in means if i in prev and q in prev[i] and means[i][q] is not None and prev[i][q] is not None]
            if d:
                log(f"  vs {Path(compare).name}: mean |diff| {st.mean(d):.3f}  max {max(d):.2f}  n={len(d)}")
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(Path(out_dir) / f"means-{name}.json", "w") as f:
            json.dump(means, f, indent=1)
        log(f"\nper-item means: {Path(out_dir) / f'means-{name}.json'}")
    return means


# ---- CLI ---------------------------------------------------------------------

CHECK_MAX = 10          # a live check is a health probe, one billed call per item, never a calibration


def _write_json(path, obj, indent=2):
    """Write whole or not at all: a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, indent=indent) + "\n")
    tmp.replace(path)
    return path


def _check_file():
    return _home() / "check.json"


def _check_spec(spec, threshold=0.5):
    """A calibrate items file, checked for use as the live check -> the stored form.
    Raises ValueError naming the first fault."""
    if not isinstance(spec, dict):
        raise ValueError("the file is not a JSON object")
    qs, items = spec.get("questions"), spec.get("items")
    if not isinstance(qs, dict) or not qs or not all(isinstance(q, dict) for q in qs.values()):
        raise ValueError("`questions` must be a non-empty object of named questions (`augur ask -h` shows one)")
    if not isinstance(items, list) or not 1 <= len(items) <= CHECK_MAX:
        raise ValueError(f"`items` must list 1 to {CHECK_MAX} items; each is one billed call on every check")
    if not (isinstance(threshold, (int, float)) and not isinstance(threshold, bool) and 0 < threshold < 1):
        raise ValueError("the threshold must lie strictly between 0 and 1")
    seen, kept = set(), []
    for i, it in enumerate(items):
        where = f"items[{i}]"
        if not isinstance(it, dict) or not isinstance(it.get("id"), str) or not it["id"]:
            raise ValueError(f"{where} needs a string `id`")
        if it["id"] in seen:
            raise ValueError(f"{where}: id {it['id']!r} appears twice")
        seen.add(it["id"])
        if not isinstance(it.get("text"), str) or not it["text"]:
            raise ValueError(f"{where} needs a non-empty string `text`")
        labels = it.get("labels")
        if not isinstance(labels, dict) or not labels:
            raise ValueError(f"{where} needs `labels`, {{question: true or false}}")
        for q, v in labels.items():
            if q not in qs or not isinstance(v, bool):
                raise ValueError(f"{where}: label {q!r} must name a question and be true or false")
            if qs[q].get("type") != "noul":
                raise ValueError(f"{where}: label {q!r} names a {qs[q].get('type')!r} question; a live check "
                                 f"grades nouls only")
        if it.get("subject") is not None and not (isinstance(it["subject"], str) and it["subject"]):
            raise ValueError(f"{where}: `subject` must be a non-empty string")
        sib = it.get("siblings")
        if sib is not None and not (isinstance(sib, list) and all(isinstance(x, str) for x in sib)):
            raise ValueError(f"{where}: `siblings` must be a list of strings")
        kept.append({k: it[k] for k in ("id", "text", "labels", "subject", "siblings") if k in it})
    return {"threshold": threshold, "questions": qs, "items": kept}


def _load_check():
    """The configured live check, or None. Raises OSError or ValueError on a broken
    check.json, which a hand edit can make."""
    path = _check_file()
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    return _check_spec(raw, raw.get("threshold", 0.5) if isinstance(raw, dict) else 0.5)


def _configure_check(file, threshold, reset):
    path = _check_file()
    if reset and (file is not None or threshold is not None):
        print("--reset takes no file or threshold", file=sys.stderr)
        return 2
    if threshold is not None and file is None:
        print("--threshold applies to a file being configured: augur configure check FILE --threshold P",
              file=sys.stderr)
        return 2
    if reset:
        if path.exists():
            path.unlink()
            print(f"removed {path}; `augur check --live` asks its two built-in questions again")
        else:
            print("no live check configured; `augur check --live` asks its two built-in questions")
        return 0
    if file is None:
        try:
            spec = _load_check()
        except (OSError, ValueError) as e:
            print(f"{path}: {e}; `augur configure check FILE` replaces it, `--reset` removes it", file=sys.stderr)
            return 2
        print(f"live check: {len(spec['items'])} items from {path}, threshold {spec['threshold']}" if spec
              else "live check: the two built-in questions (fruit, fish)")
        return 0
    try:
        spec = _check_spec(json.loads(Path(file).read_text()), 0.5 if threshold is None else threshold)
    except (OSError, ValueError) as e:
        print(f"{file}: {e}", file=sys.stderr)
        return 2
    _write_json(path, spec, 1)
    print(f"live check: {len(spec['items'])} items, {len(spec['questions'])} question(s), threshold {spec['threshold']}, "
          f"copied to {path}; `augur check --live` asks them, {len(spec['items'])} billed call(s)")
    return 0


def _live_check(backend):
    """Known questions with known answers, asked for real: the backend answers AND answers
    right. The questions are the user's own from `augur configure check`, else two built-in
    Nouls in one call."""
    path = _check_file()
    try:
        spec = _load_check()
    except (OSError, ValueError) as e:
        print(f"{path}: {e}; `augur configure check FILE` replaces it, `--reset` removes it", file=sys.stderr)
        return 2
    if spec:
        t, wrong, tokens, r = spec["threshold"], [], 0, None
        for it in spec["items"]:
            qs = {q: spec["questions"][q] for q in it["labels"]}
            try:
                r = ask(it["text"], qs, subject=it.get("subject"), siblings=it.get("siblings"),
                        backend=backend, caller="check")
            except AugurUnavailable as e:
                print(f"unavailable: {e}", file=sys.stderr)
                return 3
            tokens += r["usage"]["input_tokens"]
            for q, want in it["labels"].items():
                a = r["answers"].get(q, {})
                got = a.get("noul") if a.get("type") == "noul" else None
                if got is None or (got >= t) != want:
                    wrong.append(f"  {it['id']}.{q}: expected {'at or above' if want else 'below'} {t}, got {got}")
        n = sum(len(it["labels"]) for it in spec["items"])
        print(f"{'ok' if not wrong else 'wrong'}: {n - len(wrong)} of {n} answers right at {t}  backend {r['backend']}  "
              f"model {r['model']}  tokens {tokens}  ({path})")
        for w in wrong:
            print(w)
        return 0 if not wrong else 1
    q = {"fruit": noul("Is the word in `text` the name of a fruit?", true="a fruit", false="not a fruit"),
         "fish": noul("Is the word in `text` the name of a fish?", true="a fish", false="not a fish")}
    try:
        r = ask("banana", q, backend=backend, caller="check")
    except AugurUnavailable as e:
        print(f"unavailable: {e}", file=sys.stderr)
        return 3
    a = r["answers"]
    ok = a["fruit"]["noul"] > 0.8 and a["fish"]["noul"] < 0.2
    print(f"{'ok' if ok else 'wrong'}: backend {r['backend']}  model {r['model']}  fruit {a['fruit']['noul']}  "
          f"fish {a['fish']['noul']}  tokens {r['usage']['input_tokens']}")
    return 0 if ok else 1


# ---- configure backend -------------------------------------------------------

# Flags that become entry keys, by the key each writes; the schema in BACKEND_TYPES checks them.
ENTRY_FLAGS = ("type", "url", "model", "keychain_service", "env_key", "timeout", "price_per_mtok",
               "python", "checkpoint", "device", "command")
NAME_CHARS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
NAME_RULE = "a name takes letters, digits, '.', '_' and '-', starting with a letter or digit"
VAR_CHARS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
OLLAMA_MIN = (0, 35)            # the first Ollama release serving /v1/systemone
PROBE_TIMEOUT = 300             # the first call after Ollama loads a model can take minutes; never written


def _user_manifest():
    """augur.json as the user wrote it, {} when absent. Raises ValueError when it does not parse."""
    p = _home() / "augur.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: not a JSON object")
    b = raw.setdefault("backends", {})
    if b is None:
        raw["backends"] = b = {}
    if not isinstance(b, dict) or not all(isinstance(e, dict) for e in b.values()):
        raise ValueError(f"{p}: `backends` must be an object of entries; `augur check` names the fault")
    return raw


def _origin(name, user, shipped):
    if name not in shipped:
        return "yours"
    if name not in user:
        return "shipped"
    same = _type(user[name], shipped[name]) == shipped[name].get("type")
    return "shipped, changed" if same else "yours, replaces shipped"


def _describe(cfg, t):
    if t == "systemone":
        return f"{cfg.get('model') or '(the server default)'} at {cfg.get('url')}"
    if t == "laya":
        return f"{cfg.get('checkpoint')} via {cfg.get('python') or '(no interpreter named)'}"
    c = cfg.get("command", "")
    return c if isinstance(c, str) else shlex.join(str(a) for a in c)


def _list_backends():
    m, user, shipped = _manifest(), _user_manifest().get("backends") or {}, _defaults()["backends"]
    rows = [(("*" if n == m["backend"] else " ") + " " + n, _type(c) or "?", _origin(n, user, shipped),
             _describe(c, _type(c))) for n, c in sorted(m["backends"].items())]
    w = [max(len(r[i]) for r in rows) for i in range(3)]
    for r in rows:
        print(f"{r[0]:<{w[0]}}  {r[1]:<{w[1]}}  {r[2]:<{w[2]}}  {r[3]}")
    print("\n* the default. `augur configure backend` adds one, `augur configure backend NAME --default`\n"
          "makes one the default, and `--remove NAME` takes yours away.")
    return 0


def _show_backend(name):
    """One backend as Augur runs it, for a program or a reader without a terminal."""
    m, user, shipped = _manifest(), _user_manifest().get("backends") or {}, _defaults()["backends"]
    if name not in m["backends"]:
        print(f"no backend named {name!r}; `augur configure backend --list` shows them", file=sys.stderr)
        return 2
    cfg = m["backends"][name]
    print(json.dumps({"name": name, "type": _type(cfg), "origin": _origin(name, user, shipped),
                      "default": name == m["backend"], "settings": cfg}, indent=2))
    return 0


def _remove_backend(name):
    user = _user_manifest()
    shipped = _defaults()["backends"]
    b = user.get("backends") or {}
    if name not in b:
        if name in shipped:
            print(f"{name} runs on the shipped settings; there are none of yours to remove")
            return 0
        print(f"no backend named {name!r} in {_home() / 'augur.json'}", file=sys.stderr)
        return 2
    del b[name]
    said = f"removed {name}"
    if name in shipped:
        said += f"; {name} runs on the shipped settings again"
    elif user.get("backend") == name:
        del user["backend"]
        said += f"; it was the default, which is {_defaults()['backend']} again"
    print(f"{said} ({_write_json(_home() / 'augur.json', user)})")
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
    shipped = _defaults()["backends"]
    have = (_user_manifest().get("backends") or {}).get(name) or {}
    entry = dict(given) if replace else _merged(have, given, shipped.get(name))
    found = _entry_findings({"backends": {name: entry}}, shipped)
    if found:
        raise ValueError("; ".join(f.split(": ", 1)[-1] for f in found))
    run = _merged(shipped.get(name) or {}, entry)
    try:
        return entry, run, _resolve(name, run)
    except AugurUnavailable as e:
        raise ValueError(str(e)) from None


def _store_key(name, cfg):
    """Put the key where the entry looks for it. On macOS `security` prompts for it itself, so
    the key is never an argument, never in shell history and never in Augur's output; Augur
    takes no key by flag at all. Elsewhere, say which variable to export."""
    service, env = cfg.get("keychain_service"), cfg.get("env_key")
    if not service and not env:
        print(f"{name} names no `keychain_service` or `env_key`, so it sends no key; give it --key-service "
              f"or --key-env first", file=sys.stderr)
        return 2
    if service and sys.platform == "darwin" and shutil.which("security"):
        if not sys.stdin.isatty():
            print(f"storing a key needs a terminal, where `security` prompts for it. Or run:\n"
                  f"  security add-generic-password -a \"$USER\" -s {service} -U -w", file=sys.stderr)
            return 2
        print(f"Paste the key at the prompt, then again to confirm; nothing echoes. "
              f"It goes in the keychain as {service}.")
        r = subprocess.run(["security", "add-generic-password", "-a", getpass.getuser(), "-s", service, "-U", "-w"])
        if r.returncode:
            print(f"security exited {r.returncode}; no key stored", file=sys.stderr)
            return 3
        return 0
    if not env:
        print(f"{name} keeps its key in the macOS keychain only; give it --key-env to read one from the "
              f"environment here", file=sys.stderr)
        return 2
    print(f"Add this to your shell profile, with your key, and open a new shell:\n  export {env}=<your key>")
    return 0


def _save_backend(name, given, *, replace=False, make_default=False, force=False, store_key=False, confirm=None):
    """Verify, then write: the entry is saved only when it answers one real question, unless
    `force` (or, guided, the user) says to save it anyway. `confirm(prompt) -> bool` asks the
    user; None means a script is driving and the flags decide."""
    try:
        entry, run, t = _build_entry(name, given, replace)
    except (ValueError, AugurUnavailable) as e:
        print(f"{name}: {e}", file=sys.stderr)
        return 2
    if store_key:
        code = _store_key(name, run)
        if code:
            return code
    print(f"asking {name} one question" + (" (a model loading for the first time can take a minute)"
                                           if t == "systemone" and not (run.get("keychain_service") or run.get("env_key"))
                                           else ""), flush=True)
    began = time.monotonic()
    ok, reason = _probe(name, run, t, caller="configure", timeout=PROBE_TIMEOUT)
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
    user = _user_manifest()
    user.setdefault("backends", {})[name] = entry
    current = _manifest()["backend"]
    if not make_default and confirm and current != name:
        make_default = confirm(f"Make {name} the default backend? It is {current} now.")
    if make_default:
        _set_default(user, name)
    print(f"saved {name}, a {t} backend{'' if ok else ' that has not answered'}"
          f"{', as the default' if make_default else ''} ({_write_json(_home() / 'augur.json', user)})")
    print(f"before a threshold means anything on it, measure it:  augur calibrate items.json --backend {name} "
          f"--out run-{name}/")
    return 0


# -- guided: the same save, its settings asked for in a terminal --

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
                    raise AugurUnavailable(ev["error"])
                total, done = ev.get("total"), ev.get("completed")
                msg = ev.get("status", "") + (f" {done * 100 // total}%" if total and done else "")
                print("\r" + msg[:72].ljust(72), end="", flush=True)
    except urllib.error.HTTPError as e:
        raise AugurUnavailable(f"HTTP {e.code}: {e.read().decode(errors='ignore')[:300]}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AugurUnavailable(f"pull: {e}") from e
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
        except (AugurUnavailable, OSError, ValueError, KeyError, TypeError) as e:
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
        _check_url(url or "")
    except ValueError as e:
        print(e, file=sys.stderr)
        return None
    s = {"type": "systemone", "url": url}
    model = _prompt("The model to ask; pin a version where the host lists one (blank: the host's default)")
    if model:
        s["model"] = model
    price = _prompt("USD per million input tokens, for the usage ledger", "0")
    try:
        s["price_per_mtok"] = _price(price)
    except Exception:  # noqa: argparse's error type; any bad price records 0
        print(f"{price!r} is not a number; the ledger records 0")
        s["price_per_mtok"] = 0.0
    host = urllib.parse.urlparse(url).hostname or "hosted"
    parts = host.split(".")
    return s, parts[-2] if len(parts) > 1 else parts[0]


def _guided_laya():
    venv_py = LAYA_VENV / "bin" / "python"
    py = _prompt("The python of a virtualenv holding laya and torch (`augur help install` makes one)",
                 str(venv_py) if venv_py.exists() else None)
    if not py:
        return None
    s = {"type": "laya", "python": py}
    ck = _prompt("Checkpoint; a fine-tune goes here", _defaults()["backends"]["laya"]["checkpoint"])
    if ck != _defaults()["backends"]["laya"]["checkpoint"]:
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
            if name and NAME_CHARS.fullmatch(name):
                break
            print(NAME_RULE)
            name = None
        known = _manifest()["backends"]
        shipped = _defaults()["backends"].get(name)
        if shipped and shipped.get("type") == settings["type"]:
            left = sorted(set(shipped) - set(settings) - {"type"})
            print(f"{name} is shipped; what you leave out keeps its shipped value ({', '.join(left)})")
        if name in known and not _yes(f"{name} exists ({_type(known[name])}, {_describe(known[name], _type(known[name]))}). "
                                      f"Replace it?"):
            print("augur.json unchanged")
            return 1
        store = False
        if kind == 1:
            var = _prompt("Where to keep the key: a keychain service and environment variable name",
                          re.sub(r"\W", "_", name).upper() + "_API_KEY")
            while not VAR_CHARS.fullmatch(var or ""):
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
        return _save_backend(name, settings, replace=True, store_key=store, confirm=_yes)
    except (EOFError, KeyboardInterrupt):
        print("\naugur.json unchanged")
        return 1


def _configure_backend(args):
    given = {k: getattr(args, k) for k in ENTRY_FLAGS if getattr(args, k) is not None}
    acted = bool(given or args.default or args.force or args.store_key)
    try:
        if args.list or args.remove is not None:
            if args.name or acted or (args.list and args.remove is not None):
                print("--list and --remove NAME stand alone", file=sys.stderr)
                return 2
            return _list_backends() if args.list else _remove_backend(args.remove)
        terminal = sys.stdin.isatty() and sys.stdout.isatty()
        if not acted:
            if terminal:
                return _guided_backend(args.name)
            return _show_backend(args.name) if args.name else _list_backends()
        if not args.name:
            print("name the backend: augur configure backend NAME --type ...", file=sys.stderr)
            return 2
        if given:
            return _save_backend(args.name, given, make_default=args.default, force=args.force,
                                 store_key=args.store_key)
        if args.name not in _manifest()["backends"]:
            print(f"no backend named {args.name!r}; `augur configure backend --list` shows them", file=sys.stderr)
            return 2
        name, cfg, t = _backend(args.name)          # an existing backend: a key, the default, or both
        if args.force:
            print("--force saves settings; give some, or drop it", file=sys.stderr)
            return 2
        if args.store_key:
            code = _store_key(name, cfg)
            if code:
                return code
            ok, reason = _probe(name, cfg, t, caller="configure", timeout=PROBE_TIMEOUT)
            print(("ok: " if ok else "unavailable: ") + reason)
            if not ok:
                return 3
        if args.default:
            user = _user_manifest()
            _set_default(user, name)
            print(f"{name} is the default ({_write_json(_home() / 'augur.json', user)}); "
                  f"`augur check` asks it a question")
        return 0
    except ValueError as e:
        print(f"augur.json: {e}", file=sys.stderr)
        return 2
    except AugurUnavailable as e:
        print(f"unavailable: {e}", file=sys.stderr)
        return 3
    except OSError as e:
        print(f"{e.filename or 'augur.json'}: {e.strerror or e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted; augur.json unchanged", file=sys.stderr)
        return 130


class _Stub:
    """A stand-in for the jev endpoint, installed over urllib.request.urlopen. `answer`
    maps (state, qid, question) to a noul; `script` is a queue of HTTP errors to raise
    before answering."""

    def __init__(self):
        import io
        self.io = io
        self.calls = []
        self.requests = []                  # (url, headers, timeout) per call, keys included
        self.script = []
        self.routes = {}                    # URL suffix -> body, for Ollama's own API
        self.answer = lambda state, qid, q: 0.95 if "banana" in json.dumps(state) and "fruit" in q["instructions"] else 0.05

    def __call__(self, req, timeout=None):
        self.requests.append((req.full_url, dict(req.header_items()), timeout))
        for suffix, reply in self.routes.items():
            if req.full_url.endswith(suffix):
                return self.io.BytesIO(reply if isinstance(reply, bytes) else json.dumps(reply).encode())
        body = json.loads(req.data)
        self.calls.append(body)
        if self.script:
            code = self.script.pop(0)
            from email.message import Message
            h = Message()
            h["retry-after"] = "0"
            raise urllib.error.HTTPError(req.full_url, code, "stub", h, self.io.BytesIO(b"stub error"))
        answers = {qid: {"type": "noul", "noul": self.answer(body["state"], qid, q)} for qid, q in body["questions"].items()}
        return self.io.BytesIO(json.dumps({"model": body["model"], "answers": answers,
                                           "usage": {"input_tokens": 1000}}).encode())


REQUEST_KEYS = {"questions", "text", "state", "subject", "siblings", "model", "caller"}


def _request(req):
    """A whole `ask` call as one JSON object -> ask()'s arguments. Raises ValueError naming
    the first fault; an unknown key is a fault, so a misspelled `subjcet` is never dropped."""
    if not isinstance(req, dict):
        raise ValueError("the request is not a JSON object")
    unknown = sorted(set(req) - REQUEST_KEYS)
    if unknown:
        raise ValueError(f"unknown request key {unknown[0]!r}; known: {', '.join(sorted(REQUEST_KEYS))}")
    if not isinstance(req.get("questions"), dict) or not req["questions"]:
        raise ValueError("`questions` must be a non-empty object of named questions")
    if ("text" in req) == ("state" in req):
        raise ValueError("give exactly one of `text` (a string) and `state` (an object)")
    if "text" in req and not isinstance(req["text"], str):
        raise ValueError("`text` must be a string")
    if "state" in req and not isinstance(req["state"], dict):
        raise ValueError("`state` must be an object")
    sib = req.get("siblings")
    if sib is not None and not (isinstance(sib, list) and all(isinstance(s, str) for s in sib)):
        raise ValueError("`siblings` must be a list of strings")
    for k in ("subject", "model", "caller"):
        if req.get(k) is not None and not (isinstance(req[k], str) and req[k]):
            raise ValueError(f"`{k}` must be a non-empty string")
    return (req["text"] if "text" in req else req["state"], req["questions"],
            {"subject": req.get("subject"), "siblings": sib, "model": req.get("model"), "caller": req.get("caller")})


def _selftest():
    """The client end to end against a stand-in backend: no network, no key, no model.
    Exercises the manifest, the ledger, the answer checks, retry, batch and re-ask,
    calibrate and the unavailable paths. ⚠ It patches urllib.request.urlopen and
    time.sleep process-wide: safe once per CLI process, never from two threads at once."""
    import tempfile
    saved_env = {k: os.environ.get(k) for k in ("AUGUR_HOME", "AUGUR_BACKEND", "TYPESAFE_API_KEY")}
    saved_open, saved_sleep = urllib.request.urlopen, time.sleep
    stub = _Stub()
    failures = []

    def expect(cond, what):
        if not cond:
            failures.append(what)

    def unavailable(fn, fragment):
        try:
            fn()
        except AugurUnavailable as e:
            return fragment in str(e)
        return False

    with tempfile.TemporaryDirectory() as home:
        try:
            os.environ["AUGUR_HOME"] = home
            os.environ["TYPESAFE_API_KEY"] = "selftest"
            os.environ.pop("AUGUR_BACKEND", None)
            # a keychain service nobody has, so the key comes from the environment
            with open(Path(home) / "augur.json", "w") as f:
                json.dump({"backends": {"jev": {"keychain_service": "augur-selftest-absent", "price_per_mtok": 1.0}}}, f)
            urllib.request.urlopen, time.sleep = stub, lambda s: None

            ok, reason = available()
            expect(ok and reason == "jev-1.13.0 answers" and stub.calls[-1]["model"] == "jev-1.13.0",
                   f"check asks the pinned model a real question: {reason}")
            expect(manifest_findings() == [], f"a clean manifest drew findings: {manifest_findings()}")

            fruit = {"fruit": noul("Is the word in `text` the name of a fruit?")}
            r = ask("banana", fruit, subject="lunch", siblings=["apple"], caller="selftest")
            st = stub.calls[-1]["state"]
            expect(r["answers"]["fruit"]["noul"] > 0.9 and r["backend"] == "jev", "ask: answer or backend")
            expect(st["text"] == "banana" and st["subject"] == "lunch" and st["siblings"] == ["apple"] and st.get("uid"),
                   "ask: subject, siblings and uid not named in the state")
            with open(Path(home) / "usage.csv") as f:
                rows = list(csv.reader(f))
            expect(rows[0][0] == "ts" and rows[-1][2] == "selftest" and rows[-1][6] == "0.001000",
                   f"ledger: {rows[-1] if rows else 'empty'}")

            import contextlib
            import io
            req = {"questions": fruit, "text": "banana", "subject": "lunch", "caller": "piped"}
            saved_stdin, sys.stdin = sys.stdin, io.StringIO(json.dumps(req))
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    code = main(["ask", "--request", "-", "--caller", "flagged"])
            finally:
                sys.stdin = saved_stdin
            piped = json.loads(out.getvalue()) if code == 0 else {}
            expect(piped.get("answers", {}).get("fruit", {}).get("noul", 0) > 0.9
                   and stub.calls[-1]["state"]["subject"] == "lunch", f"--request -: exit {code}")
            with open(Path(home) / "usage.csv") as f:
                expect(list(csv.reader(f))[-1][2] == "flagged", "--request: a flag did not win over the request")
            with open(Path(home) / "call.json", "w") as f:
                json.dump(req, f)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                codes = (main(["ask", "--request", str(Path(home) / "call.json")]),
                         main(["ask", "--request", str(Path(home) / "call.json"), "-q", "x.json"]),
                         main(["ask"]))
            expect(codes == (0, 2, 2), f"--request FILE, -q beside --request, no input: exits {codes}")
            for bad, fragment in (([], "not a JSON object"), ({"questions": fruit, "text": "x", "subjcet": "y"}, "'subjcet'"),
                                  ({"questions": fruit}, "exactly one"), ({"questions": fruit, "text": "x", "state": {}}, "exactly one"),
                                  ({"questions": {}, "text": "x"}, "non-empty"), ({"questions": fruit, "text": 3}, "`text`"),
                                  ({"questions": fruit, "text": "x", "siblings": "a,b"}, "`siblings`"),
                                  ({"questions": fruit, "text": "x", "caller": ""}, "`caller`")):
                try:
                    _request(bad)
                    expect(False, f"request accepted: {bad}")
                except ValueError as e:
                    expect(fragment in str(e), f"request {bad}: {e}")

            for bad in (float("nan"), 1.2):
                stub.answer = lambda state, qid, q, bad=bad: bad
                expect(unavailable(lambda: ask("x", fruit), "not a probability"), f"answer {bad} accepted")

            stub.answer = lambda state, qid, q: 0.9
            stub.script = [429, 429]
            expect(ask("x", fruit)["answers"]["fruit"]["noul"] == 0.9, "429: not retried to an answer")
            stub.script = [500]
            expect(unavailable(lambda: ask("x", fruit), "HTTP 500"), "500: not reported")

            def qs(id_, path):
                return {"long": noul(f"Is the text at `{path}` longer than three words?")}
            stub.answer = lambda state, qid, q: 0.55 if "items" in state else 0.9
            n = len(stub.calls)
            out = batch({"a": "one two three four", "b": "one"}, qs, threshold=0.5)
            expect(set(out) == {"a", "b"} and all(out[i]["long"].get("reasked") for i in out), "batch: re-ask near threshold")
            expect(len(stub.calls) == n + 3 and stub.calls[n + 1]["state"]["text"] == "one two three four",
                   "batch: re-ask not asked in isolation")
            try:
                batch({str(i): "x" for i in range(MAX_BATCH + 1)}, qs)
                expect(False, "batch: cap not enforced")
            except BatchTooLarge:
                pass
            try:
                batch({"a__b": "x"}, qs)
                expect(False, "batch: '__' in an id accepted")
            except ValueError:
                pass

            stub.answer = lambda state, qid, q: 0.95 if "banana" in json.dumps(state) and qid in ("fruit",) else 0.05
            items = Path(home) / "mine.json"
            items.write_text(json.dumps({"questions": fruit, "items": [
                {"id": "b", "text": "banana", "labels": {"fruit": True}},
                {"id": "c", "text": "carrot", "labels": {"fruit": False}}]}))
            quiet = contextlib.redirect_stdout(io.StringIO())
            with quiet, contextlib.redirect_stderr(io.StringIO()):
                codes = [main(["configure", "check", str(items)]), main(["check", "--live"])]
                items.write_text(json.dumps({"questions": fruit, "items": [
                    {"id": "c", "text": "carrot", "labels": {"fruit": True}}]}))
                codes += [main(["check", "--live"]),                        # the copy, not the edited original
                          main(["configure", "check", str(items)]), main(["check", "--live"])]
                items.write_text(json.dumps({"questions": fruit, "items": [
                    {"id": str(i), "text": "x", "labels": {"fruit": True}} for i in range(CHECK_MAX + 1)]}))
                codes += [main(["configure", "check", str(items)]),
                          main(["configure", "check", str(items), "--threshold", "1"])]
                items.write_text(json.dumps({"questions": fruit, "items": [{"id": "b", "text": "banana",
                                                                             "labels": {"frut": True}}]}))
                codes += [main(["configure", "check", str(items)]), main(["configure", "check", "--reset"]),
                          main(["check", "--live"])]
            expect(codes == [0, 0, 0, 0, 1, 2, 2, 2, 0, 0], f"configure check / check --live exits: {codes}")
            items.write_text(json.dumps({"questions": fruit, "items": [{"id": "b", "text": "banana",
                                                                         "labels": {"fruit": True}}]}))
            shown = io.StringIO()
            with contextlib.redirect_stdout(shown), contextlib.redirect_stderr(io.StringIO()):
                codes = [main(["configure", "check"]), main(["configure", "check", str(items), "--threshold", "0.4"]),
                         main(["configure", "check"]), main(["configure", "check", str(items), "--reset"]),
                         main(["configure", "check", "--threshold", "0.4"])]
                _check_file().write_text(json.dumps({"threshold": 0.5, "questions": fruit, "items": [
                    {"id": "b", "text": "banana", "labels": {"fruit": True}, "siblings": 5}]}))
                codes += [main(["check", "--live"]), main(["configure", "check", "--reset"])]
            expect(codes == [0, 0, 0, 2, 2, 2, 0], f"configure check view, flag combinations, a hand-broken copy: {codes}")
            expect("built-in" in shown.getvalue() and "1 items from" in shown.getvalue() and "threshold 0.4" in shown.getvalue(),
                   "configure check with no file did not show the built-in pair, then the configured items")
            one = [{"id": "b", "text": "banana", "labels": {"fruit": True}}]
            for spec, t, fragment in (([], 0.5, "not a JSON object"), ({"questions": {}, "items": one}, 0.5, "`questions`"),
                                      ({"questions": fruit, "items": one}, 1.0, "strictly between"),
                                      ({"questions": fruit, "items": one * 2}, 0.5, "appears twice"),
                                      ({"questions": fruit, "items": [{"id": "b", "text": "", "labels": {"fruit": True}}]},
                                       0.5, "`text`"),
                                      ({"questions": fruit, "items": [{**one[0], "subject": 3}]}, 0.5, "`subject`"),
                                      ({"questions": fruit, "items": [{**one[0], "siblings": "a,b"}]}, 0.5, "`siblings`"),
                                      ({"questions": {"kind": choice("Which?", {"a": None})},
                                        "items": [{"id": "b", "text": "x", "labels": {"kind": True}}]}, 0.5, "nouls only")):
                try:
                    _check_spec(spec, t)
                    expect(False, f"_check_spec accepted a spec lacking {fragment}")
                except ValueError as e:
                    expect(fragment in str(e), f"_check_spec: {e!r} does not name {fragment}")
            expect(not _check_file().exists(), "--reset left check.json behind")

            try:            # Augur's half of panoply-lib's scorer contract: accept its request, answer at its path
                from . import _scorer_contract as contract
            except ImportError:
                import _scorer_contract as contract
            saved_stdin, sys.stdin = sys.stdin, io.StringIO(json.dumps(contract.REQUEST))
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    code = main(["ask", "--request", "-", "--caller", "grille"])
            finally:
                sys.stdin = saved_stdin
            try:
                p = contract.probability(json.loads(out.getvalue()))
                expect(code == 0 and 0 <= p <= 1, f"scorer contract: exit {code}, probability {p!r}")
            except (KeyError, TypeError, ValueError) as e:
                expect(False, f"scorer contract: the reply lacks {'.'.join(contract.REPLY_PATH)} ({e!r})")

            expect(abs(_auroc([0.9, 0.8], [0.1, 0.8]) - 0.875) < 1e-9, "auroc")
            stub.answer = lambda state, qid, q: 0.9 if "yes" in state["text"] else 0.1
            spec = {"name": "selftest", "questions": fruit,
                    "items": [{"id": "p", "text": "yes", "labels": {"fruit": True}},
                              {"id": "n", "text": "no", "labels": {"fruit": False}}]}
            lines = []
            means = calibrate(spec, runs=2, workers=1, log=lines.append)
            expect(means == {"p": {"fruit": 0.9}, "n": {"fruit": 0.1}}, f"calibrate means: {means}")
            expect(any("AUROC 1.000" in ln for ln in lines), "calibrate: AUROC line")

            with open(Path(home) / "augur.json", "w") as f:
                json.dump({"backnd": "laya", "backends": {"laya": {"pyton": "/x", "device": None, "head_max_len": "512"}}}, f)
            found = " | ".join(manifest_findings())
            for w in ("unknown key 'backnd', did you mean 'backend'?", "unknown key 'pyton', did you mean 'python'?",
                      "head_max_len: expected integer"):
                expect(w in found, f"manifest: not caught: {w}")
            expect("device" not in found, "manifest: a null device was flagged")
            schema_path = write_schema()
            expect(json.loads(schema_path.read_text())["$id"] == "augur.schema.json", "schema: not written")

            # a command backend: a stand-in script, then `augur ask --request -` itself as the command
            stand = Path(home) / "stand.py"
            stand.write_text(
                "import json, sys\n"
                "mode = sys.argv[1]\n"
                "req = json.load(sys.stdin)\n"
                "open(sys.argv[2], 'w').write(json.dumps(req))\n"
                "if mode == 'fail':\n    sys.stderr.write('boom\\n'); sys.exit(3)\n"
                "if mode == 'prose':\n    print('I think it is a fruit'); sys.exit(0)\n"
                "if mode == 'slow':\n    import time; time.sleep(5)\n"
                "if mode == 'bytes':\n    sys.stdout.buffer.write(b'\\xff\\xfe'); sys.exit(0)\n"
                "p = float('nan') if mode == 'nan' else 0.97 if 'banana' in json.dumps(req['state']) else 0.03\n"
                "print(json.dumps({'answers': {q: {'type': 'noul', 'noul': p} for q in req['questions']},"
                " 'usage': {'input_tokens': 10}}))\n")
            seen = Path(home) / "seen.json"
            me = [sys.executable, str(HERE / "augur.py")]

            def cmd(mode):
                return [sys.executable, str(stand), mode, str(seen)]
            with open(Path(home) / "augur.json", "w") as f:
                json.dump({"backends": {
                    "stand": {"command": cmd("ok"), "price_per_mtok": 2.0},
                    "named": {"command": " ".join(cmd("ok")), "model": "m1"},
                    "fails": {"command": cmd("fail")}, "prose": {"command": cmd("prose")}, "nan": {"command": cmd("nan")},
                    "slow": {"command": cmd("slow"), "timeout": 0.5}, "bytes": {"command": cmd("bytes")},
                    "unquoted": {"command": "stand 'open"},
                    "chain": {"command": me + ["ask", "--request", "-", "--backend", "stand", "--caller", "inner"]},
                    "bare": {"price_per_mtok": 1.0}}}, f)
            expect(manifest_findings() == ["augur.json: backends.bare: names no `type`; give it one of command, laya, systemone"],
                   f"command backends: {manifest_findings()}")
            ok, reason = available("stand")
            expect(ok and "answers" in reason, f"command check: {reason}")
            r = ask("banana", fruit, backend="stand", caller="cmd")
            sent = json.loads(seen.read_text())
            expect(r["backend"] == "stand" and r["answers"]["fruit"]["noul"] == 0.97, f"command ask: {r}")
            expect(set(sent) == {"questions", "state"} and sent["state"]["text"] == "banana" and "fruit" in sent["questions"],
                   f"command request: {sent}")
            with open(Path(home) / "usage.csv") as f:
                row = list(csv.reader(f))[-1]
            expect(row[1:3] == ["stand", "cmd"] and row[5:] == ["10", "0.000020"], f"command ledger: {row}")
            r = ask("banana", fruit, backend="named")
            expect(json.loads(seen.read_text()).get("model") == "m1" and r["model"] == "m1",
                   "command: a string command or its model not honoured")
            r = ask("banana", fruit, backend="chain")
            expect(r["answers"]["fruit"]["noul"] == 0.97, f"augur ask --request - as a command: {r}")
            expect(_command_argv({"command": "~/x --flag"}) == [os.path.expanduser("~/x"), "--flag"], "command: ~ not expanded")
            for name, fragment in (("fails", "exited 3: boom"), ("prose", "not JSON"), ("nan", "not a probability"),
                                   ("bare", "names no `type`"), ("slow", "no answer in 0.5s"),
                                   ("bytes", "not JSON"), ("unquoted", "does not parse")):
                expect(unavailable(lambda: ask("banana", fruit, backend=name), fragment), f"command backend {name}")
            with open(Path(home) / "augur.json", "w") as f:
                json.dump({"backends": {"loose": {"comand": ["x"]}}}, f)
            found = " | ".join(manifest_findings())
            expect("unknown key 'comand', did you mean 'command'?" in found and "names no `type`" in found,
                   f"command manifest: {found}")

            # the shipped defaults are configuration of the user's shape: every entry typed, all clean
            expect(manifest_findings(DEFAULTS_FILE) == [], f"defaults.json: {manifest_findings(DEFAULTS_FILE)}")
            expect(all(e.get("type") in BACKEND_TYPES for e in _defaults()["backends"].values()),
                   "defaults.json: an entry names no known type")
            # a keyless systemone entry, as Ollama serves one; a type change replacing a default whole
            with open(Path(home) / "augur.json", "w") as f:
                json.dump({"backends": {
                    "local": {"type": "systemone", "url": "http://localhost:1/v1/systemone", "model": "nimble", "timeout": 7},
                    "jev": {"type": "command", "command": cmd("ok")},
                    "nourl": {"type": "systemone", "model": "m"}, "odd": {"type": "grpc"}}}, f)
            found = manifest_findings()
            expect(found == ["augur.json: backends.nourl: missing required key 'url'",
                             "augur.json: backends.odd: unknown type 'grpc'; known: command, laya, systemone"],
                   f"typed manifest: {found}")
            stub.answer = lambda state, qid, q: 0.95 if "banana" in json.dumps(state) else 0.05
            n = len(stub.requests)
            ok, reason = available("local")
            expect(ok and "nimble answers" in reason, f"systemone check asks a real question: {reason}")
            with open(Path(home) / "usage.csv") as f:
                row = list(csv.reader(f))[-1]
            expect(row[1:4] == ["local", "check", "nimble"], f"a check's question is not in the ledger: {row}")
            r = ask("banana", fruit, backend="local", caller="keyless")
            url, headers, wait = stub.requests[-1]
            expect(r["backend"] == "local" and r["model"] == "nimble" and stub.calls[-1]["model"] == "nimble",
                   f"keyless systemone ask: {r}")
            expect(url.endswith(":1/v1/systemone") and "Authorization" not in headers and wait == 7,
                   f"keyless systemone: url {url}, headers {sorted(headers)}, timeout {wait}")
            expect(all(u.startswith("http://localhost:1/") for u, _, _ in stub.requests[n:]), "keyless: a call left the entry's url")
            r = ask("banana", fruit, backend="jev")
            expect(r["answers"]["fruit"]["noul"] == 0.97 and "url" not in _backend("jev")[1],
                   "a type change did not replace the default whole")
            expect(unavailable(lambda: ask("x", fruit, backend="nourl"), "systemone backend with no `url`"), "systemone without url")
            expect(unavailable(lambda: ask("x", fruit, backend="odd"), "unknown type 'grpc'"), "unknown type accepted")
            with open(Path(home) / "augur.json", "w") as f:
                json.dump({"backends": {"hosted": {"type": "systemone", "url": "http://h/v1/systemone", "model": "m",
                                                   "env_key": "AUGUR_SELFTEST_ABSENT_KEY"}}}, f)
            expect(unavailable(lambda: ask("x", fruit, backend="hosted"), "AUGUR_SELFTEST_ABSENT_KEY unset"),
                   "a named key that is missing did not refuse")
            with open(Path(home) / "augur.json", "w") as f:
                json.dump({"backends": {"jev": {"models_url": "https://api.typesafe.ai/v1/models"}}}, f)
            expect([f.split(",")[0] for f in manifest_findings()] == ["augur.json: backends.jev: unknown key 'models_url'"],
                   f"a 0.4 models_url drew no finding: {manifest_findings()}")
            with open(Path(home) / "augur.json", "w") as f:
                json.dump({"backends": {"hosted": {"type": "systemone", "url": "http://h/v1/systemone", "model": "m",
                                                   "env_key": "AUGUR_SELFTEST_ABSENT_KEY"}}}, f)
            os.environ["AUGUR_SELFTEST_ABSENT_KEY"] = "k2"
            ask("banana", fruit, backend="hosted")
            os.environ.pop("AUGUR_SELFTEST_ABSENT_KEY")
            expect(stub.requests[-1][1].get("Authorization") == "Bearer k2", "a second hosted key was not sent")

            # configure backend: flags and guided prompts share one verify-then-write
            def run(argv, answers=""):
                o, saved_in = io.StringIO(), sys.stdin
                sys.stdin = io.StringIO(answers)          # never a terminal here, so never guided by accident
                try:
                    with contextlib.redirect_stdout(o), contextlib.redirect_stderr(o):
                        code = main(argv) if argv is not None else _guided_backend()
                finally:
                    sys.stdin = saved_in
                return code, o.getvalue()

            def saved():
                return json.loads((Path(home) / "augur.json").read_text())
            (Path(home) / "augur.json").write_text("{}")
            stub.answer = lambda state, qid, q: 0.95 if "banana" in json.dumps(state) else 0.05
            loc = ["configure", "backend", "loc", "--type", "systemone", "--url", "http://localhost:1/v1/systemone",
                   "--model", "m:1b", "--price", "0"]
            code, out = run(loc)
            expect(code == 0 and saved()["backends"]["loc"] == {"type": "systemone", "url": "http://localhost:1/v1/systemone",
                                                                "model": "m:1b", "price_per_mtok": 0.0},
                   f"configure backend by flags: exit {code}, {out}")
            with open(Path(home) / "usage.csv") as f:
                row = list(csv.reader(f))[-1]
            expect(row[1:4] == ["loc", "configure", "m:1b"] and stub.requests[-1][2] == PROBE_TIMEOUT,
                   f"configure's question: ledger {row}, timeout {stub.requests[-1][2]}")
            code, out = run(["configure", "backend", "loc", "--timeout", "9"])
            expect(code == 0 and saved()["backends"]["loc"]["model"] == "m:1b" and saved()["backends"]["loc"]["timeout"] == 9,
                   f"one flag did not merge over the entry: {saved()}")
            stub.script = [500]
            code, out = run(loc[:2] + ["fails"] + loc[3:])
            expect(code == 3 and "fails" not in saved()["backends"] and "not saved" in out, f"saved unverified: {code} {out}")
            stub.script = [500]
            code, out = run(loc[:2] + ["fails"] + loc[3:] + ["--force"])
            expect(code == 0 and "fails" in saved()["backends"] and "has not answered" in out, f"--force: {code} {out}")
            for argv, fragment in ((["configure", "backend", "-x", "--url", "http://h"], "error"),
                                   (["configure", "backend", "b@d", "--type", "command", "--command", "x"], "a name takes"),
                                   (["configure", "backend", "nu", "--type", "systemone", "--model", "m"], "'url'"),
                                   (["configure", "backend", "loc", "--command", "x"], "unknown key 'command'"),
                                   (["configure", "backend", "--type", "command", "--command", "x"], "name the backend"),
                                   (["configure", "backend", "--list", "--remove", "loc"], "stand alone"),
                                   (["configure", "backend", "jev", "--force"], "--force saves settings"),
                                   (["configure", "backend", "u", "--type", "systemone", "--url", "https://me:k@h/v1"], "username, password"),
                                   (["configure", "backend", "u", "--type", "systemone", "--url", "https://h/v1?key=k"], "query string"),
                                   (["configure", "backend", "loc", "--price", "nan"], "error"),
                                   (["configure", "backend", "loc", "--timeout", "0"], "error"),
                                   (["configure", "backend", "typo", "--default"], "no backend named 'typo'"),
                                   (["configure", "backend", "--remove", ""], "no backend named ''")):
                try:
                    code, out = run(argv)
                except SystemExit as e:                  # argparse refuses before main returns
                    code, out = e.code, "error"
                expect(code == 2 and fragment in out, f"configure backend {argv[2:]}: exit {code}, {out.strip()[-120:]}")
            code, out = run(["configure", "backend", "loc", "--type", "command", "--command", " ".join(cmd("ok"))])
            expect(code == 0 and saved()["backends"]["loc"] == {"type": "command", "command": " ".join(cmd("ok"))},
                   f"a new type did not replace the entry whole: {saved()['backends']['loc']}")
            code, out = run(["configure", "backend", "loc", "--default"])
            expect(code == 0 and saved()["backend"] == "loc" and _manifest()["backend"] == "loc", f"--default: {code} {out}")
            code, out = run(["configure", "backend", "jev", "--model", "jev-9"])
            expect(code == 0 and saved()["backends"]["jev"] == {"model": "jev-9"} and _backend("jev")[1]["url"].startswith("https"),
                   f"a shipped name's change is not a merge over it: {saved()['backends'].get('jev')}")
            code, out = run(["configure", "backend", "--list"])
            expect(code == 0 and "* loc" in out and "shipped, changed" in out and "yours" in out, f"--list: {out}")
            code, out = run(["configure", "backend", "jev"])
            expect(code == 0 and json.loads(out)["origin"] == "shipped, changed", f"show without a terminal: {out}")
            code, out = run(["configure", "backend", "--remove", "jev"])
            expect(code == 0 and "jev" not in saved()["backends"] and "shipped settings again" in out, f"--remove jev: {out}")
            code, out = run(["configure", "backend", "--remove", "loc"])
            expect(code == 0 and "backend" not in saved() and "jev again" in out, f"--remove the default: {out}")
            expect(run(["configure", "backend", "--remove", "jev"])[0] == 0 and run(["configure", "backend", "--remove", "zz"])[0] == 2,
                   "--remove on an untouched shipped name or an unknown one")
            (Path(home) / "augur.json").write_text('{"backends": null}')
            code, out = run(["configure", "backend", "--list"])
            expect(code == 0 and "jev" in out, f"--list over a null `backends`: {code} {out}")
            (Path(home) / "augur.json").write_text('{"backends": {"x": null}}')
            code, out = run(["configure", "backend", "--list"])
            expect(code == 2 and "`backends` must be an object" in out, f"--list over a null entry: {code} {out}")
            (Path(home) / "augur.json").write_text("{}")
            code, out = run(["configure", "backend", "hk", "--type", "systemone", "--url", "http://h/v1/systemone",
                             "--key-env", "AUGUR_SELFTEST_ABSENT_KEY", "--store-key"])
            expect(code == 3 and "export AUGUR_SELFTEST_ABSENT_KEY=<your key>" in out
                   and "`augur configure backend hk --store-key` stores one" in out, f"a missing key's remedy: {out}")
            if sys.platform == "darwin":
                code, out = run(["configure", "backend", "jev", "--store-key"])
                expect(code == 2 and "needs a terminal" in out, f"--store-key without a terminal: {out}")

            # guided, against a stand-in Ollama
            saved_host = os.environ.get("OLLAMA_HOST")
            os.environ["OLLAMA_HOST"] = "localhost:1"
            tags = {"models": [{"name": "nim:latest", "digest": "d1", "size": 9e9, "capabilities": ["decision"]},
                               {"name": "nim:9b-q8_0", "digest": "d1", "size": 9e9, "capabilities": ["decision"]},
                               {"name": "other:1b", "digest": "d2", "size": 1e9, "capabilities": ["completion"]},
                               {"name": "old:1b", "digest": "d3", "size": None, "capabilities": None}]}
            stub.routes = {"/api/version": {"version": "0.35.0"}, "/api/tags": tags,
                           "/api/pull": b'{"status": "pulling", "total": 4, "completed": 2}\n{"status": "success"}\n'}
            try:
                code, out = run(None, "1\n1\n\ny\n")
                expect(code == 0 and saved()["backends"]["nim"]["model"] == "nim:9b-q8_0" and saved()["backend"] == "nim"
                       and saved()["backends"]["nim"]["url"] == "http://localhost:1/v1/systemone"
                       and "other:1b" not in out and "1 other model is not" in out,
                       f"guided Ollama, latest resolved to its exact tag, made default: {code} {out[-300:]}")
                code, out = run(None, "1\n4\nfresh:2b\n\nn\n")
                expect(code == 0 and saved()["backends"]["fresh"]["model"] == "fresh:2b" and "success" in out,
                       f"guided Ollama pull: {code} {out[-300:]}")
                code, out = run(None, "1\n1\nnim\nn\n")
                expect(code == 1 and "augur.json unchanged" in out, f"guided: declining to replace: {code} {out[-200:]}")
                stub.routes["/api/pull"] = b'{"error": "pull model manifest: file does not exist"}\n'
                code, out = run(None, "1\n4\nnope:1b\n")
                expect(code == 3 and "file does not exist" in out, f"guided pull of a missing model: {code} {out[-200:]}")
                stub.routes["/api/tags"] = b"<html>proxy error</html>"
                code, out = run(None, "1\n")
                expect(code == 3 and "not answering" in out, f"guided, a tags reply that is not JSON: {code} {out[-200:]}")
                stub.routes["/api/tags"] = tags
                stub.routes["/api/version"] = {"version": "0.34.9"}
                code, out = run(None, "1\n")
                expect(code == 3 and "too old" in out, f"guided, an old Ollama: {code} {out[-200:]}")
                code, out = run(None, f"4\n{' '.join(cmd('ok'))}\n\nscr\nn\n")
                expect(code == 0 and saved()["backends"]["scr"]["type"] == "command", f"guided command: {code} {out[-200:]}")
                if sys.platform == "darwin":       # a hosted key: decline storing it, so no keychain is touched
                    code, out = run(None, "2\nhttps://api.acme.example/v1/systemone\nacme-2\n0.05\n\n\nn\nn\n")
                    expect(code == 3 and "acme" not in saved()["backends"] and "acme --store-key" in out,
                           f"guided hosted without its key: {code} {out[-300:]}")
                code, out = run(None, "")
                expect(code == 1 and "augur.json unchanged" in out, f"guided, input ends: {code} {out[-200:]}")
            finally:
                stub.routes = {}
                if saved_host is None:
                    os.environ.pop("OLLAMA_HOST", None)
                else:
                    os.environ["OLLAMA_HOST"] = saved_host
            (Path(home) / "augur.json").write_text("{}")

            ok, reason = available("laya")
            expect(not ok and "does not name an interpreter" in reason, f"laya without a venv: {reason}")
            expect(unavailable(lambda: ask("x", fruit, backend="nope"), "unknown backend"), "unknown backend accepted")
            with open(Path(home) / "augur.json", "w") as f:
                f.write("{not json")
            expect(unavailable(lambda: ask("x", fruit), "not JSON"), "broken manifest accepted")
            found = manifest_findings()
            expect(len(found) == 1 and found[0].startswith("augur.json does not parse: "), f"parse finding: {found}")
        except Exception as e:  # noqa: a selftest reports every failure as a line, never a traceback
            failures.append(f"{type(e).__name__}: {e}")
        finally:
            urllib.request.urlopen, time.sleep = saved_open, saved_sleep
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    for f in failures:
        print(f"FAIL {f}")
    print("selftest FAILED" if failures else "selftest ok")
    return 1 if failures else 0


def write_schema():
    return _schema_lib().write_schema(MANIFEST_SCHEMA, _home() / "augur.schema.json")


BIN = Path(os.environ.get("AUGUR_BIN", str(Path.home() / ".local" / "bin")))
LAYA_VENV = Path.home() / ".augur-laya"       # the venv INSTALL-augur.md tells a user to make


def _install():
    """Symlink `augur` into BIN for a source checkout. A link works from any shell, a
    subprocess and cron, where an alias reaches an interactive shell only."""
    if PACKAGED:
        print("augur is on PATH through its package manager; nothing to link")
        return 0
    me = HERE / "augur.py"
    me.chmod(me.stat().st_mode | 0o111)
    BIN.mkdir(parents=True, exist_ok=True)
    link = BIN / "augur"
    if link.is_symlink():
        link.unlink()
    elif link.exists():
        print(f"link: {link} exists and is not a symlink; leaving it. Remove it and rerun.")
        return 1
    link.symlink_to(me)
    print(f"link: {link} -> {me}")
    on_path = str(BIN) in os.environ.get("PATH", "").split(os.pathsep)
    if not on_path:
        print(f"PATH: {BIN} is not on it; add  export PATH=\"{BIN}:$PATH\"  to the shell profile")
    print("verify from another directory:  augur check")
    return 0 if on_path else 2


def _uninstall(yes, dry):
    link = BIN / "augur"
    links = [link] if not PACKAGED and link.is_symlink() else []
    venv = LAYA_VENV if LAYA_VENV.is_dir() else None
    print("uninstall will remove:")
    for ln in links:
        print(f"  link      {ln}")
    if venv:
        print(f"  directory {venv}  (the laya virtualenv)")
    if not links and not venv:
        print("  nothing it made")
    if dry:
        return 0
    if (links or venv) and not yes:
        if sys.stdin.isatty():
            if not _yes("proceed?"):
                print("nothing removed")
                return 1
        else:
            print("nothing removed; rerun with --yes")
            return 1
    for ln in links:
        ln.unlink()
        print(f"removed {ln}")
    if venv:
        shutil.rmtree(venv)
        print(f"removed {venv}")
    print("\nleft for you, if you want it gone completely:")
    try:
        backends = _manifest().get("backends") or {}
    except AugurUnavailable:
        backends = {}
    for service in sorted({c["keychain_service"] for c in backends.values()
                           if _type(c) == "systemone" and c.get("keychain_service")}):
        print(f"  a key:        security delete-generic-password -a \"$USER\" -s {service}")
    if _home().is_dir():
        print(f"  the home:     rm -r {_home()}   (augur.json, its schema and the usage ledger)")
    for py in sorted({c["python"] for c in backends.values() if _type(c) == "laya" and c.get("python")}):
        if not str(Path(py).expanduser()).startswith(str(LAYA_VENV)):
            print(f"  a laya interpreter the manifest names elsewhere: {py}")
    if PACKAGED:
        print("  the command:  brew uninstall jack-com/panoply/augur   (or `uv tool uninstall augur`)")
    else:
        print(f"  the files:    rm {' '.join(str(HERE / n) for n in ('augur.py', 'augur_laya.py', 'defaults.json', 'INSTALL-augur.md'))}")
    return 0


QUESTION_FILE = """A question file is a JSON object of named, typed questions. `noul` is a
yes-or-no with the boundary written into the criteria; `choice` names its
options; `score` lists ordered levels:

  {"buyer_named": {"type": "noul",
                   "instructions": "Does the sentence name who this aircraft is for?",
                   "criteria": {"true": "A buyer, mission or pilot profile is named.",
                                "false": "It describes the aircraft only, or says nothing about a buyer."}}}
"""

EXAMPLES = {
    None: """examples:
  augur check                                  the backend answers and the key is found
  augur check --live                           known questions, known answers, asked for real
  augur configure check items.json             make those questions your own
  augur check --backend laya                   the same for the local backend
  augur ask -q closers.json -t "A four-seat single for an owner who flies IFR."
  augur calibrate items.json --out run-a/
  augur selftest                               the client against a stand-in backend, offline
  augur schema                                 write the manifest schema for an editor
  augur help install                           the install steps, written for an agent to follow

`augur <command> -h` has that command's flags and examples. Library use:
`from augur import ask, noul`; `ask(state, {"q": noul(...)}, subject=..., caller=...)`
returns the same dict the CLI prints, and `batch(items, make_questions, threshold=...)`
runs up to 20 items in one call. The manifest and the usage ledger live in ~/.augur
(or $AUGUR_HOME).
""",
    "check": """examples:
  augur check                                  the default backend (augur.json or AUGUR_BACKEND)
  augur check --backend laya                   the local backend; needs the venv INSTALL-augur.md names
  augur check --backend nimble                 any backend named in augur.json, e.g. a systemone entry for Ollama
  augur check --live                          ask known questions and check the answers: your own from
                                               `augur configure check`, else two built-in Nouls in one call

Exit 0 and `ok:` when the backend answers; exit 3 and `unavailable: <reason>` otherwise.
A misspelled or mistyped manifest key is printed first, prefixed `augur.json:`, and never changes the exit.
""",
    "configure": """examples:
  augur configure backend                      add a backend, guided: Ollama, a hosted API, Laya or a script
  augur configure backend --list               every backend, its type, and which is the default
  augur configure check my-items.json          the questions `augur check --live` asks

`augur configure backend -h` and `augur configure check -h` have the flags.
""",
    "configure backend": """examples:
  augur configure backend                      guided, in a terminal: where the model runs, which model,
                                               then one real question before anything is saved
  augur configure backend nimble --type systemone --url http://localhost:11434/v1/systemone \\
      --model nimble:9b-q8_0 --price 0         the same by flags, for a script or an agent
  augur configure backend acme --type systemone --url https://api.acme.example/v1/systemone \\
      --model acme-2.1 --key-service ACME_API_KEY --key-env ACME_API_KEY --store-key
                                               a hosted API; `security` prompts for the key itself
  augur configure backend jev --store-key      store or replace jev's key, then ask it one question
  augur configure backend nimble --default     make an existing backend the default
  augur configure backend nimble --timeout 120 change one setting; the rest of the entry stays
  augur configure backend --list               every backend: type, shipped or yours, the default
  augur configure backend --remove nimble      remove yours; on a shipped name, its shipped settings return

A backend is saved only after it answers one question, booked in the ledger as caller
`configure`; --force saves one that does not. No flag takes an API key: --store-key hands
the prompt to macOS `security`, and elsewhere prints the variable to export.
""",
    "configure check": """examples:
  augur configure check my-items.json          copy a calibrate items file of up to %d items as the live check
  augur configure check my-items.json --threshold 0.3
                                               a label is right at or above 0.3 for true, below it for false
  augur configure check                        what `check --live` asks now
  augur configure check --reset                back to the two built-in questions

The file is copied to ~/.augur/check.json, so editing or deleting the original changes nothing.
Every item is one billed call on each `augur check --live`.
""" % CHECK_MAX,
    "schema": """examples:
  augur schema                                 write ~/.augur/augur.schema.json and say how to use it
""",
    "ask": QUESTION_FILE + """
examples:
  augur ask -q closers.json -t "A four-seat single for an owner who flies IFR."
                                               one state as a string; prints {backend, model, answers, usage}
  augur ask -q closers.json -s state.json --subject "Cessna 182" --siblings "Cessna 172,Piper Archer"
                                               a JSON state; subject and siblings are named in the state
                                               so the model knows which aircraft the sentence is about
  augur ask -q closers.json -t "..." --caller copy_lint
                                               the caller is what the usage ledger records the cost under
  echo '{"questions": {...}, "text": "..."}' | augur ask --request -
                                               the whole call as one JSON object on stdin, for a program
                                               that runs augur as a command; prints the same response

A request object holds `questions` and exactly one of `text` (a string) or `state` (an object),
and may add `subject`, `siblings` (a list), `model` and `caller`. Any other key is refused, and a
flag given beside --request wins over the request's field. Exit 2 is bad input, exit 3 an
unavailable backend; the answers are at `answers.<name>`, a `noul` as a probability from 0 to 1.
""",
    "calibrate": """examples:
  augur calibrate items.json --out run-a/
                                               every item's mean probability per question, AUROC against
                                               the labels, and the floor sentences' scores; the threshold
                                               a rule uses comes from this run, never from a published figure
  augur calibrate items.json --backend laya --out run-b/ --compare run-a/means-jev.json
                                               the same items on another backend, with the per-item
                                               difference against the first run
  augur calibrate items.json --limit 40 --questions cond,strict --workers 8
                                               a quick pass on a subset

An items file is {"questions": {name: question}, "items": [{"id": ..., "text": ..., "labels": {name: bool}}]};
`augur ask -h` shows the question shape.
""",
    "selftest": """examples:
  augur selftest                               no network, no key, no model; prints `selftest ok`
""",
    "help": """examples:
  augur help install                           the install steps, written for an agent to follow
""",
    "install": """examples:
  augur install                                link `augur` into ~/.local/bin, or $AUGUR_BIN (source checkout only)
""",
    "uninstall": """examples:
  augur uninstall --dry-run                    what removal would take, without taking it
  augur uninstall --yes                        remove what augur made; print what is left
""",
}


def main(argv):
    import argparse
    fmt = dict(formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--backend", metavar="NAME", help="default from augur.json or AUGUR_BACKEND")
    p = argparse.ArgumentParser(prog="augur", description=__doc__.split("\n\n")[0], epilog=EXAMPLES[None], **fmt)
    p.add_argument("-v", "-V", "--version", action="version", version=f"augur {__version__}")
    sub = p.add_subparsers(dest="cmd", title="commands", metavar="<command>")

    def cmd(name, help, **kw):
        return sub.add_parser(name, help=help, description=help, epilog=EXAMPLES[name], **fmt, **kw)
    k = cmd("check", "positive check for the backend", parents=[common])
    k.add_argument("--live", action="store_true",
                   help="ask known questions and check the answers: yours from `augur configure check`, else two built-in")
    f = cmd("configure", "set up a backend, or what `check --live` asks")
    fs = f.add_subparsers(dest="what", title="what", metavar="<what>", required=True)

    def part(name, help):
        return fs.add_parser(name, help=help, description=help, epilog=EXAMPLES[f"configure {name}"],
                             allow_abbrev=False, **fmt)
    fb = part("backend", "add, change, list or remove a backend")
    fb.add_argument("name", nargs="?", help="the backend's name; guided in a terminal when no flag follows")
    fb.add_argument("--type", choices=sorted(BACKEND_TYPES), help="the protocol it speaks")
    fb.add_argument("--url", help="systemone: the endpoint")
    fb.add_argument("--model", help="systemone or command: the model to ask; pin a version")
    fb.add_argument("--key-service", dest="keychain_service", metavar="NAME",
                    help="systemone: the macOS keychain service holding the key")
    fb.add_argument("--key-env", dest="env_key", metavar="VAR", help="systemone: the environment variable holding the key")
    fb.add_argument("--command", help="command: the script, as one string split as a shell would")
    fb.add_argument("--python", help="laya: the interpreter of a venv holding laya and torch")
    fb.add_argument("--checkpoint", help="laya: the checkpoint to load")
    fb.add_argument("--device", help="laya: cuda, mps or cpu")
    fb.add_argument("--timeout", type=_positive, metavar="S", help="seconds to wait for an answer")
    fb.add_argument("--price", type=_price, dest="price_per_mtok", metavar="USD",
                    help="USD per million input tokens, for the usage ledger")
    fb.add_argument("--default", action="store_true", help="make it the default backend")
    fb.add_argument("--store-key", action="store_true", help="prompt for its key through macOS `security`")
    fb.add_argument("--force", action="store_true", help="save it even when it does not answer")
    fb.add_argument("--list", action="store_true", help="every backend, its type and origin, and the default")
    fb.add_argument("--remove", metavar="NAME", help="remove your entry; a shipped name gets its shipped settings back")
    fc = part("check", "set what `check --live` asks")
    fc.add_argument("file", nargs="?", help="a calibrate items file (at most %d items) to copy as the live check" % CHECK_MAX)
    fc.add_argument("--threshold", type=float, metavar="P", help="a label is right when its answer is at or above P for "
                    "true and below it for false (default 0.5)")
    fc.add_argument("--reset", action="store_true", help="remove the live check; the built-in questions return")
    a = cmd("ask", "one call; prints the response JSON", parents=[common])
    a.add_argument("-q", "--questions", metavar="FILE", help="JSON file: {name: question}")
    g = a.add_mutually_exclusive_group()
    g.add_argument("-t", "--text", help="state as a string (stored under `text`)")
    g.add_argument("-s", "--state", metavar="FILE", help="JSON file with the state object")
    g.add_argument("-r", "--request", metavar="FILE",
                   help="the whole call as one JSON object; `-` reads it from stdin")
    a.add_argument("--subject", help="what the state is about, named to the model")
    a.add_argument("--siblings", metavar="A,B", help="comma-separated neighbours of the subject")
    a.add_argument("--model", help="override the backend's pinned model or checkpoint")
    a.add_argument("--caller", help="the name the usage ledger books the cost under (default cli)")
    c = cmd("calibrate", "measure a backend on a labelled items file", parents=[common])
    c.add_argument("items", help="JSON file of questions and labelled items")
    c.add_argument("--runs", type=int, default=1, metavar="N", help="repeats per item (default 1)")
    c.add_argument("--limit", type=int, metavar="N", help="first N items only")
    c.add_argument("--questions", metavar="a,b", help="comma-separated subset of the file's questions")
    c.add_argument("--workers", type=int, default=6, metavar="N", help="parallel calls (default 6)")
    c.add_argument("--out", metavar="DIR", help="directory for per-item means")
    c.add_argument("--compare", metavar="FILE", help="a prior run's per-item means JSON")
    cmd("selftest", "the client against a stand-in backend: no network, no key, no model")
    cmd("schema", "write the manifest schema where an editor can be pointed at it")
    h = cmd("help", "print a guide")
    h.add_argument("topic", choices=["install"])
    cmd("install", "put `augur` on PATH from a source checkout (a symlink in ~/.local/bin)")
    u = cmd("uninstall", "remove what augur made; print what is left")
    u.add_argument("--yes", action="store_true", help="remove without asking")
    u.add_argument("--dry-run", action="store_true", help="print what would be removed and stop")
    args = p.parse_args(argv)
    if args.cmd == "selftest":
        return _selftest()
    if args.cmd == "schema":
        path = write_schema()
        print(f"schema: {path}")
        print(f'editor: add "$schema": "{path}" to augur.json, or map augur.json to it in the editor\'s JSON schema settings')
        return 0
    if args.cmd == "help":
        print((HERE / "INSTALL-augur.md").read_text(), end="")
        return 0
    if args.cmd == "install":
        return _install()
    if args.cmd == "uninstall":
        return _uninstall(args.yes, args.dry_run)
    if args.cmd == "configure":
        if args.what == "backend":
            return _configure_backend(args)
        return _configure_check(args.file, args.threshold, args.reset)
    if args.cmd == "check":
        for finding in manifest_findings():
            print(finding)
        if args.live:
            return _live_check(args.backend)
        ok, reason = available(args.backend)
        print(("ok: " if ok else "unavailable: ") + reason)
        return 0 if ok else 3
    if args.cmd == "ask":
        # a flag given beside --request wins over the request's own field
        flags = {"subject": args.subject, "model": args.model, "caller": args.caller,
                 "siblings": [x.strip() for x in args.siblings.split(",") if x.strip()] if args.siblings else None}
        try:
            if args.request is not None:
                if args.questions is not None:
                    raise ValueError("--request carries the questions; drop -q")
                if args.request == "-":
                    req = json.load(sys.stdin)
                else:
                    with open(args.request) as f:
                        req = json.load(f)
                state, questions, kw = _request(req)
                kw.update({k: v for k, v in flags.items() if v is not None})
            else:
                if args.questions is None or (args.text is None and args.state is None):
                    raise ValueError("give -q with -t or -s, or the whole call with --request")
                with open(args.questions) as f:
                    questions = json.load(f)
                if args.text is not None:
                    state = args.text
                else:
                    with open(args.state) as f:
                        state = json.load(f)
                kw = flags
        except (OSError, ValueError) as e:
            print(f"bad input: {e}", file=sys.stderr)
            return 2
        try:
            r = ask(state, questions, subject=kw["subject"], siblings=kw["siblings"],
                    backend=args.backend, model=kw["model"], caller=kw["caller"] or "cli")
        except AugurUnavailable as e:
            print(f"unavailable: {e}", file=sys.stderr)
            return 3
        print(json.dumps(r, indent=1))
        return 0
    if args.cmd == "calibrate":
        try:
            with open(args.items) as f:
                spec = json.load(f)
        except (OSError, ValueError) as e:
            print(f"bad input: {e}", file=sys.stderr)
            return 2
        ok, reason = available(args.backend)
        if not ok:
            print(f"unavailable: {reason}", file=sys.stderr)
            return 3
        try:
            calibrate(spec, backend=args.backend, runs=args.runs, limit=args.limit,
                      questions=[x.strip() for x in args.questions.split(",")] if args.questions else None,
                      workers=args.workers, out_dir=args.out, compare=args.compare)
        except AugurUnavailable as e:
            print(f"unavailable: {e}", file=sys.stderr)
            return 3
        return 0
    p.print_help()
    return 2


def cli():
    """The entry point a `uv tool` or pip install puts on PATH."""
    global PACKAGED
    PACKAGED = True
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        sys.exit(0)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        sys.exit(0)
