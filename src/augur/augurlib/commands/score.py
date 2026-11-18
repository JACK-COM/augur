"""`augur score`: many requests as JSON lines on stdin, one reply per line on stdout, through
one process, one key read and the calibrate pool, with no labels and no AUROC."""
import argparse
import json
import statistics as st
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, List, Optional

from .. import calibrate, client, errors, manifest, request
from .ask import REQUEST_CAP
from .base import Verb, bad_input, unavailable


def args(s: argparse.ArgumentParser) -> None:
    s.add_argument("--workers", type=int, metavar="N",
                   help="parallel calls (default: the entry's `workers`, else 1 for a server on this machine, else 6)")
    s.add_argument("--caller", help="the name the usage ledger books the cost under (default score)")
    s.add_argument("--against", metavar="FILE", help="a calibrate run's shape-<backend>.json: warn when these texts "
                   "are far longer or shorter than the ones the threshold was measured on")


def drift(texts: List[Any], shape: dict) -> Optional[str]:
    """A warning when the median text asked lies outside half the calibrated p10 to twice its
    p90: a threshold measured on sentences does not transfer to passages."""
    if not texts or not shape:
        return None
    m = st.median(len(t if isinstance(t, str) else json.dumps(t)) for t in texts)
    lo, hi = shape.get("p10", 0) / 2, shape.get("p90", 0) * 2
    if lo <= m <= hi:
        return None
    return (f"these texts run to a median of {m:.0f} characters, against {shape.get('median')} in the calibration "
            f"set (p10 {shape.get('p10')}, p90 {shape.get('p90')}); its threshold may not hold here")


def run(a: argparse.Namespace) -> int:
    try:
        reqs = []
        for n, line in enumerate(sys.stdin, 1):
            if not line.strip():
                continue
            if len(line) > REQUEST_CAP:
                raise ValueError(f"line {n}: the request passes {REQUEST_CAP // (1 << 20)} MB")
            reqs.append(request._request(json.loads(line)))
        shape = json.loads(Path(a.against).read_text(encoding="utf-8")) if a.against else {}
        name, cfg, t = manifest._backend(a.backend)
    except errors.AugurUnavailable as e:
        return unavailable(e)
    except (OSError, ValueError) as e:
        return bad_input(e)
    warning = drift([state for state, _, _ in reqs], shape)
    if warning:
        print(f"augur: {warning}", file=sys.stderr)

    def one(r: tuple) -> dict:
        state, questions, kw = r
        try:
            return client.ask(state, questions, subject=kw["subject"], siblings=kw["siblings"], backend=name,
                              model=kw["model"], caller=a.caller or kw["caller"] or "score")
        except (errors.AugurUnavailable, ValueError) as e:
            return {"error": str(e)}

    with ThreadPoolExecutor(max_workers=calibrate.workers_for(name, cfg, t, a.workers)) as ex:
        replies = list(ex.map(one, reqs))
    for r in replies:
        print(json.dumps(r))
    return 3 if any("error" in r for r in replies) else 0


VERBS = (Verb("score", "many requests as JSON lines on stdin; one reply per line", run, args, backend=True),)
