"""The client API: ask a backend typed questions about a text, one call or a batch with a
re-ask near the threshold, and probe whether a backend answers.
"""
import uuid

from . import backends
from . import errors
from . import ledger
from . import manifest
from . import qtypes


MAX_BATCH = 20


REASK_BAND = 0.2


def available(backend=None):
    """Positive check for the backend. Returns (ok, reason)."""
    try:
        name, cfg, t = manifest._backend(backend)
    except errors.AugurUnavailable as e:
        return False, str(e)
    return _probe(name, cfg, t)


def _probe(name, cfg, t, caller="check", timeout=60):
    """`available` for a resolved entry, saved or not. -> (ok, reason)"""
    if backends.ADAPTERS[t][1]:
        return backends.ADAPTERS[t][1](cfg)
    try:          # a backend that starts is not yet one that answers
        r = _answer(name, cfg, t, {"text": "banana", "uid": uuid.uuid4().hex},
                    {"fruit": qtypes.noul("Is the word in `text` the name of a fruit?")}, None, timeout, 0, caller)
        return True, f"{r['model']} answers"
    except (errors.AugurUnavailable, ValueError, OSError) as e:
        return False, str(e)


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
    name, cfg, t = manifest._backend(backend)
    return _answer(name, cfg, t, state, questions, model, timeout, retries, caller)


def _answer(name, cfg, t, state, questions, model, timeout, retries, caller):
    """One checked, ledgered call to a resolved entry."""
    try:
        resp = backends.ADAPTERS[t][0](cfg, state, questions, model, timeout, retries)
    except errors.NoKey as e:
        raise errors.NoKey(f"{e}; `augur configure backend {name} --store-key` stores one") from None
    qtypes._check_answers(resp["answers"])
    resp["backend"] = name
    ledger._log(name, caller, resp["model"], len(questions), resp["usage"]["input_tokens"], float(cfg.get("price_per_mtok") or 0))
    return resp


def batch(items, make_questions, *, threshold=None, backend=None, model=None, caller="cli", **kw):
    """items: {id: text}. make_questions(id, path) -> {qname: question} where `path` is the
    state path to that item's text (e.g. "items.foo"), which the question should reference.
    Returns {id: {qname: answer}}. Caps at MAX_BATCH. With `threshold`, any Noul within
    REASK_BAND of it is re-asked in isolation and the isolated answer replaces it."""
    if len(items) > MAX_BATCH:
        raise errors.BatchTooLarge(f"{len(items)} items; cap is {MAX_BATCH} (scores drift past that)")
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
            raise errors.AugurUnavailable(f"unexpected answer key {k!r}")
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
