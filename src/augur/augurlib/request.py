"""A whole ask call as one JSON object, the shape `augur ask --request` reads and Grille's
scorer contract sends.
"""
import json

from . import qtypes



REQUEST_KEYS = {"questions", "text", "state", "subject", "siblings", "model", "caller"}
REQUEST_CAP = 8 << 20        # characters in one file or line a verb reads; every one of them is billed


def load(source, what="the input", text=False):
    """JSON from a path (`-` is stdin), or from `source` itself when `text`, at most
    REQUEST_CAP characters. Raises ValueError naming `what` for anything else: too long,
    not JSON, or nested past parsing; OSError when the file does not open."""
    import sys
    if text:
        raw = source
    elif source == "-":
        raw = sys.stdin.read(REQUEST_CAP + 1)
    else:
        with open(source, encoding="utf-8") as f:
            raw = f.read(REQUEST_CAP + 1)
    if len(raw) > REQUEST_CAP:
        raise ValueError(f"{what} passes {REQUEST_CAP >> 20} MB")
    try:
        return json.loads(raw)
    except RecursionError:
        raise ValueError(f"{what} nests too deep to read") from None
    except ValueError as e:
        raise ValueError(f"{what} is not JSON ({e})") from None


def questions_ok(questions):
    """A question map: an object of named question objects, at most MAX_QUESTIONS. ValueError otherwise."""
    if not isinstance(questions, dict) or not questions or not all(isinstance(q, dict) for q in questions.values()):
        raise ValueError("questions must be a non-empty object of named question objects")
    if len(questions) > qtypes.MAX_QUESTIONS:
        raise ValueError(f"at most {qtypes.MAX_QUESTIONS} questions in one call, each read against the whole text")


def _request(req):
    """A whole `ask` call as one JSON object -> ask()'s arguments. Raises ValueError naming
    the first fault; an unknown key is a fault, so a misspelled `subjcet` is never dropped."""
    if not isinstance(req, dict):
        raise ValueError("the request is not a JSON object")
    unknown = sorted(set(req) - REQUEST_KEYS)
    if unknown:
        raise ValueError(f"unknown request key {unknown[0]!r}; known: {', '.join(sorted(REQUEST_KEYS))}")
    questions_ok(req.get("questions"))
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
