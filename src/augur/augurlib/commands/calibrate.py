"""`augur calibrate`: measure a backend on a labelled items file."""
import argparse
import json

from .. import calibrate as measure
from .. import client, errors, request
from .base import Verb, bad_input, count, unavailable


def runs(v: str) -> int:
    """argparse type: one to twenty repeats; each is a billed call per item."""
    n = count(v)
    if n > 20:
        raise argparse.ArgumentTypeError("--runs takes at most 20; each run is a billed call per item")
    return n


def args(c: argparse.ArgumentParser) -> None:
    c.add_argument("items", help="JSON file of questions and labelled items")
    c.add_argument("--runs", type=runs, default=1, metavar="N", help="repeats per item (default 1, at most 20)")
    c.add_argument("--limit", type=int, metavar="N", help="first N items only")
    c.add_argument("--questions", metavar="a,b", help="comma-separated subset of the file's questions")
    c.add_argument("--workers", type=count, metavar="N",
                   help="parallel calls (default: the entry's `workers`, else 1 for a server on this machine, else 6)")
    c.add_argument("--out", metavar="DIR", help="directory for the answers as they land and the per-item means")
    c.add_argument("--resume", action="store_true", help="with --out, ask only what an interrupted run left unasked")
    c.add_argument("--compare", metavar="FILE", help="a prior run's per-item means JSON")


def run(a: argparse.Namespace) -> int:
    try:
        spec = measure.check_spec(request.load(a.items, "the items file"))
        prev = request.load(a.compare, "--compare") if a.compare else None
        if a.resume and not a.out:
            raise ValueError("--resume continues the run in --out DIR; name the directory")
    except (OSError, ValueError) as e:
        return bad_input(e)
    ok, reason = client.available(a.backend)
    if not ok:
        return unavailable(reason)
    try:
        measure.calibrate(spec, backend=a.backend, runs=a.runs, limit=a.limit,
                          questions=[x.strip() for x in a.questions.split(",")] if a.questions else None,
                          workers=a.workers, out_dir=a.out, compare=a.compare, resume=a.resume, previous=prev)
    except errors.AugurUnavailable as e:
        return unavailable(e)
    except (ValueError, OSError) as e:
        return bad_input(e)
    return 0


VERBS = (Verb("calibrate", "measure a backend on a labelled items file", run, args, backend=True),)
