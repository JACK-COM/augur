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

Backends, chosen by `augur.json` in the Augur home (`~/.augur`, or `$AUGUR_HOME`), then
the `AUGUR_BACKEND` environment variable, then the `backend=` argument:

  jev    TypeSafe's System One API, model pinned (an alias moves without notice and a
         tuned threshold moves with it). Key: macOS keychain service `TYPESAFE_API_KEY`
         (`security add-generic-password -a "$USER" -s TYPESAFE_API_KEY -w <key> -U`),
         `TYPESAFE_API_KEY` in the environment as fallback. Never in a repo, never printed.
  laya   Convai's open-weight ModernBERT decision model, run locally through
         `augur_laya.py` inside a virtualenv holding `laya` and `torch`; the manifest
         names that interpreter. Free and fast, and by its makers' own card a base to
         fine-tune rather than a zero-shot engine: calibrate before trusting a threshold.

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

    augur check [--backend B] [--live]           positive check; --live asks two known Nouls
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
import json
import math
import os
import select
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.2.1"                          # the one home: pyproject.toml and the formula's test read it

HERE = Path(__file__).resolve().parent
# A package manager owns the command and moves this file on every upgrade: Homebrew's
# wrapper sets AUGUR_PACKAGED, a `uv tool` or pip install enters through `cli()`. So the
# manifest and the usage ledger live in the Augur home, never beside this file.
PACKAGED = os.environ.get("AUGUR_PACKAGED") == "1"
MAX_BATCH = 20
REASK_BAND = 0.2
RETRY_AFTER_CAP = 30.0       # never let a report-only instrument sleep for as long as a server asks

DEFAULT_MANIFEST = {
    "backend": "jev",
    "backends": {
        "jev": {
            "url": "https://api.typesafe.ai/v1/systemone",
            "models_url": "https://api.typesafe.ai/v1/models",
            "model": "jev-1.13.0",          # pinned; see the docstring
            "price_per_mtok": 0.042,        # USD per million input tokens, docs.typesafe.ai/models, 2026-09-17
            "keychain_service": "TYPESAFE_API_KEY",
            "env_key": "TYPESAFE_API_KEY",
        },
        "laya": {
            "python": "",                   # interpreter of a venv holding laya and torch; empty = unavailable
            "checkpoint": "convaiinnovations/laya",
            "device": None,                 # None lets laya pick cuda, mps, cpu in that order
            "head_max_len": 512,
            "price_per_mtok": 0.0,
        },
    },
}


_STR = {"type": "string"}
_PRICE = {"type": "number", "description": "USD per million input tokens, for the usage ledger."}
MANIFEST_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "augur.schema.json",
    "title": "Augur manifest (augur.json)",
    "description": "Augur's settings. Every key is optional and a missing key takes the default; "
                   "settings merge per backend.",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "$schema": {"type": "string", "description": "Editor hint only; ignored by Augur."},
        "backend": {"type": "string", "description": "The default backend: jev or laya. "
                    "AUGUR_BACKEND and --backend override it."},
        "backends": {
            "type": "object",
            "description": "Per-backend settings, merged over the defaults.",
            "properties": {
                "jev": {"type": "object", "additionalProperties": False, "properties": {
                    "url": _STR, "models_url": _STR,
                    "model": {"type": "string", "description": "Pinned model version; a threshold does not transfer."},
                    "price_per_mtok": _PRICE,
                    "keychain_service": {"type": "string", "description": "macOS keychain service holding the key."},
                    "env_key": {"type": "string", "description": "Environment variable holding the key."}}},
                "laya": {"type": "object", "additionalProperties": False, "properties": {
                    "python": {"type": "string", "description": "Interpreter of a venv holding laya and torch."},
                    "checkpoint": {"type": "string", "description": "Checkpoint to load; a fine-tune goes here."},
                    "device": {"type": ["string", "null"], "description": "cuda, mps or cpu; null lets laya pick."},
                    "head_max_len": {"type": "integer"},
                    "price_per_mtok": _PRICE}},
            },
            "additionalProperties": {"type": "object"},
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


def manifest_findings():
    """Structural problems in the manifest, as lines; [] when clean or absent."""
    found = _schema_lib().validate_file(_home() / "augur.json", MANIFEST_SCHEMA)
    # the shared checker anchors every finding on the word "manifest"; name the file instead
    return ["augur.json" + (f[len("manifest"):] if f.startswith("manifest") else ": " + f) for f in found]


class AugurUnavailable(RuntimeError):
    pass


class BatchTooLarge(ValueError):
    pass


# ---- manifest ----------------------------------------------------------------

def _home():
    """Read per call, so a caller (or the selftest) can point AUGUR_HOME elsewhere."""
    return Path(os.environ.get("AUGUR_HOME") or Path.home() / ".augur").expanduser()


def _manifest():
    m = json.loads(json.dumps(DEFAULT_MANIFEST))
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
            m["backends"].setdefault(name, {}).update(cfg or {})
    if os.environ.get("AUGUR_BACKEND"):
        m["backend"] = os.environ["AUGUR_BACKEND"]
    return m


def _backend(name=None):
    m = _manifest()
    name = name or m["backend"]
    if name not in m["backends"]:
        raise AugurUnavailable(f"unknown backend {name!r}; manifest knows {sorted(m['backends'])}")
    return name, m["backends"][name]


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


# ---- backend: jev (TypeSafe System One) ----------------------------------------

def _jev_key(cfg):
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", cfg["keychain_service"], "-w"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    k = os.environ.get(cfg["env_key"], "").strip()
    if k:
        return k
    raise AugurUnavailable(f"no key: keychain service {cfg['keychain_service']} empty and {cfg['env_key']} unset")


def _jev_available(cfg):
    try:
        k = _jev_key(cfg)
        req = urllib.request.Request(cfg["models_url"], headers={"Authorization": f"Bearer {k}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            names = [str(m.get("name", "?")) if isinstance(m, dict) else str(m)
                     for m in json.load(r).get("models", [])]
        return True, ", ".join(names) or "models endpoint answered"
    except AugurUnavailable as e:
        return False, str(e)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"models endpoint: {e}"


def _jev_ask(cfg, state, questions, model, timeout, retries):
    model = model or cfg["model"]
    body = json.dumps({"state": state, "model": model, "questions": questions}).encode()
    req = urllib.request.Request(cfg["url"], data=body, headers={
        "Authorization": f"Bearer {_jev_key(cfg)}", "Content-Type": "application/json"})
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
        return {"model": resp.get("model", model), "answers": resp["answers"],
                "usage": {"input_tokens": int(usage.get("input_tokens") or 0)}}
    raise AugurUnavailable("unreachable: retry loop exhausted without a response")


# ---- backend: laya (local worker) ----------------------------------------------

_laya_proc = None
_laya_lock = threading.Lock()


def _laya_worker(cfg):
    global _laya_proc
    if _laya_proc is not None and _laya_proc.poll() is None:
        return _laya_proc
    py = os.path.expanduser(cfg.get("python") or "")
    if not py or not os.path.exists(py):
        raise AugurUnavailable("laya: manifest `backends.laya.python` does not name an interpreter with laya installed")
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


BACKENDS = {"jev": (_jev_ask, _jev_available), "laya": (_laya_ask, _laya_available)}


# ---- public API --------------------------------------------------------------

def available(backend=None):
    """Positive check for the backend. Returns (ok, reason)."""
    try:
        name, cfg = _backend(backend)
    except AugurUnavailable as e:
        return False, str(e)
    return BACKENDS[name][1](cfg)


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
    name, cfg = _backend(backend)
    resp = BACKENDS[name][0](cfg, state, questions, model, timeout, retries)
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
    name, cfg = _backend(backend)
    qs = {k: v for k, v in spec["questions"].items() if not questions or k in questions}
    items = spec["items"][:limit] if limit else spec["items"]
    if name == "laya":
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

def _live_check(backend):
    """Two Nouls with known answers, one real call: the backend answers AND answers right."""
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


class _Stub:
    """A stand-in for the jev endpoint, installed over urllib.request.urlopen. `answer`
    maps (state, qid, question) to a noul; `script` is a queue of HTTP errors to raise
    before answering."""

    def __init__(self):
        import io
        self.io = io
        self.calls = []
        self.script = []
        self.answer = lambda state, qid, q: 0.95 if "banana" in json.dumps(state) and "fruit" in q["instructions"] else 0.05

    def __call__(self, req, timeout=None):
        if req.full_url.endswith("/models"):
            return self.io.BytesIO(json.dumps({"models": [{"name": "stub-1"}]}).encode())
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
            expect(ok and "stub-1" in reason, f"check: {reason}")
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
    import shutil
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
            if input("proceed? [y/N] ").strip().lower() not in ("y", "yes"):
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
    print(f"  the key:      security delete-generic-password -a \"$USER\" -s {DEFAULT_MANIFEST['backends']['jev']['keychain_service']}")
    if _home().is_dir():
        print(f"  the home:     rm -r {_home()}   (augur.json, its schema and the usage ledger)")
    try:
        py = (_manifest().get("backends") or {}).get("laya", {}).get("python")
    except AugurUnavailable:
        py = None
    if py and not str(Path(py).expanduser()).startswith(str(LAYA_VENV)):
        print(f"  a laya interpreter the manifest names elsewhere: {py}")
    if PACKAGED:
        print("  the command:  brew uninstall jack-com/panoply/augur   (or `uv tool uninstall augur`)")
    else:
        print(f"  the files:    rm {' '.join(str(HERE / n) for n in ('augur.py', 'augur_laya.py', 'INSTALL-augur.md'))}")
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
  augur check --live                           two known sentences, two known answers, one real call
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
  augur check --live                           ask two known Nouls and check the answers, one billed call

Exit 0 and `ok:` when the backend answers; exit 3 and `unavailable: <reason>` otherwise.
A misspelled or mistyped manifest key is printed first, prefixed `augur.json:`, and never changes the exit.
""",
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
    common.add_argument("--backend", metavar="jev|laya", help="default from augur.json or AUGUR_BACKEND")
    p = argparse.ArgumentParser(prog="augur", description=__doc__.split("\n\n")[0], epilog=EXAMPLES[None], **fmt)
    p.add_argument("--version", action="version", version=f"augur {__version__}")
    sub = p.add_subparsers(dest="cmd", title="commands", metavar="<command>")

    def cmd(name, help, **kw):
        return sub.add_parser(name, help=help, description=help, epilog=EXAMPLES[name], **fmt, **kw)
    k = cmd("check", "positive check for the backend", parents=[common])
    k.add_argument("--live", action="store_true", help="ask two known Nouls and check the answers")
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
