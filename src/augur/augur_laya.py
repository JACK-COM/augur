#!/usr/bin/env python3
"""augur_laya: the Laya worker behind augur.py's `laya` backend.

Runs INSIDE a virtualenv that holds `laya` and `torch`; augur.py itself stays stdlib
and talks to this process over JSON lines. One request per line on stdin:

    {"state": <str|dict|list>, "questions": {...}, "checkpoint": "convaiinnovations/laya",
     "device": null, "head_max_len": 512}

One response per line on stdout, in augur's shape:

    {"model": "laya:convaiinnovations/laya", "answers": {qid: {...}}, "usage": {"input_tokens": n},
     "device": "mps", "truncated": false}

or {"error": "<one line>"}. The checkpoint loads once per process and is reused; a
second checkpoint loads beside it. Laya's own answer dicts pass through unchanged apart
from the keys augur reads (`noul`, `choice`, `confidence`, `score`, `probabilities`),
so a caller sees the same fields whichever backend answered.

`head_max_len` is raised from the checkpoint's default because a `noul` whose true and
false criteria are each a paragraph exceeds the 192-token head budget on the base
checkpoint and the agent refuses the question rather than truncating it. `truncated`
reports whether the state plus instructions overran the encoder context (512 on the
base checkpoint, 1,024 on typed-decisions), which Laya cuts silently.

    augur_laya.py --check      loads the default checkpoint and answers one known Noul
"""
import json
import sys

DEFAULT_CHECKPOINT = "convaiinnovations/laya"
DEFAULT_HEAD = 512

_agents = {}


def _agent(checkpoint, device):
    key = (checkpoint, device)
    if key not in _agents:
        import laya  # noqa: deferred so a missing package is one clear error line
        _agents[key] = laya.load(checkpoint, device=device)
    return _agents[key]


def _state_tokens(agent, state):
    text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
    try:
        return len(agent.tok(text, add_special_tokens=False)["input_ids"])
    except Exception:  # noqa: a token count is bookkeeping, never a reason to fail the answer
        return -1


def answer(req):
    checkpoint = req.get("checkpoint") or DEFAULT_CHECKPOINT
    agent = _agent(checkpoint, req.get("device"))
    head = int(req.get("head_max_len") or DEFAULT_HEAD)
    if agent.cfg.get("head_max_len", 0) < head:
        agent.cfg["head_max_len"] = head
    state, questions = req["state"], req["questions"]
    res = agent.system_one(state, questions)
    max_len = int(agent.cfg.get("max_len", 512))
    n_state = _state_tokens(agent, state)
    usage = res.get("usage") or {}
    tokens = int(usage.get("input_tokens") or usage.get("total_tokens") or 0)
    return {
        "model": f"laya:{checkpoint}",
        "answers": res.get("answers", {}),
        "usage": {"input_tokens": tokens if tokens else max(n_state, 0)},
        "device": str(agent.device),
        "truncated": n_state >= 0 and n_state + 64 > max_len,  # instructions and markers take the rest
        "max_len": max_len,
        "state_tokens": n_state,
    }


def _check():
    q = {"fruit": {"type": "noul", "instructions": "Is the word in `text` the name of a fruit?",
                   "criteria": {"true": "a fruit", "false": "not a fruit"}},
         "fish": {"type": "noul", "instructions": "Is the word in `text` the name of a fish?",
                  "criteria": {"true": "a fish", "false": "not a fish"}}}
    r = answer({"state": {"text": "banana"}, "questions": q})
    a = r["answers"]
    print(json.dumps({"model": r["model"], "device": r["device"], "fruit": a["fruit"].get("noul"),
                      "fish": a["fish"].get("noul"), "raw_fruit": a["fruit"]}))
    return 0


def main(argv):
    if "--check" in argv:
        return _check()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            out = answer(json.loads(line))
        except Exception as e:  # noqa: one error line per request keeps the pipe alive
            out = {"error": f"{type(e).__name__}: {e}"[:400]}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
