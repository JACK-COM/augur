"""The question types a caller asks (noul, choice, score) and the check every answer passes
before it is returned.
"""
import math

from . import errors


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


def _check_answers(answers):
    """Refuse a non-finite or out-of-range probability. JSON parses NaN and Infinity, and a
    NaN fails every comparison, so a caller gating on `noul >= threshold` would pass it:
    an injection screen taking `max(best, nan)` keeps `best` and never withholds."""
    for qid, a in answers.items():
        if not isinstance(a, dict):
            raise errors.AugurUnavailable(f"answer {qid!r} is not an object: {str(a)[:100]}")
        if a.get("type") == "noul" and "noul" not in a:
            raise errors.AugurUnavailable(f"answer {qid!r} is a noul with no `noul`")
        probs = [(k, a[k]) for k in ("noul", "confidence") if k in a]
        probs += [(f"probabilities.{k}", v) for k, v in (a.get("probabilities") or {}).items()]
        for k, v in probs:
            if not (isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 1):
                raise errors.AugurUnavailable(f"answer {qid!r} has {k}={v!r}, not a probability")
        if "score" in a and not (isinstance(a["score"], (int, float)) and math.isfinite(a["score"])):
            raise errors.AugurUnavailable(f"answer {qid!r} has score={a['score']!r}, not a finite number")
