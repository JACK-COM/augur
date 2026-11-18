"""`augur ask`: one call, from flags and files or from one request object, printed as JSON."""
import argparse
import json
import sys

from .. import client, errors, request
from .base import Verb, bad_input, unavailable

REQUEST_CAP = 8 << 20        # characters; every one of them is billed


def args(a: argparse.ArgumentParser) -> None:
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


def run(a: argparse.Namespace) -> int:
    # a flag given beside --request wins over the request's own field
    flags = {"subject": a.subject, "model": a.model, "caller": a.caller,
             "siblings": [x.strip() for x in a.siblings.split(",") if x.strip()] if a.siblings else None}
    try:
        if a.request is not None:
            if a.questions is not None:
                raise ValueError("--request carries the questions; drop -q")
            if a.request == "-":
                raw = sys.stdin.read(REQUEST_CAP + 1)
            else:
                with open(a.request) as f:
                    raw = f.read(REQUEST_CAP + 1)
            if len(raw) > REQUEST_CAP:
                raise ValueError(f"the request passes {REQUEST_CAP // (1 << 20)} MB")
            req = json.loads(raw)
            state, questions, kw = request._request(req)
            kw.update({k: v for k, v in flags.items() if v is not None})
        else:
            if a.questions is None or (a.text is None and a.state is None):
                raise ValueError("give -q with -t or -s, or the whole call with --request")
            with open(a.questions) as f:
                questions = json.load(f)
            if a.text is not None:
                state = a.text
            else:
                with open(a.state) as f:
                    state = json.load(f)
            kw = flags
    except (OSError, ValueError) as e:
        return bad_input(e)
    try:
        r = client.ask(state, questions, subject=kw["subject"], siblings=kw["siblings"],
                       backend=a.backend, model=kw["model"], caller=kw["caller"] or "cli")
    except errors.AugurUnavailable as e:
        return unavailable(e)
    print(json.dumps(r, indent=1))
    return 0


VERBS = (Verb("ask", "one call; prints the response JSON", run, args, backend=True),)
