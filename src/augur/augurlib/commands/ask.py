"""`augur ask`: one call, from flags and files or from one request object, printed as JSON."""
import argparse
import json
import sys

from .. import client, errors, request
from .base import Verb, bad_input, unavailable


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
            req = request.load(a.request, "the request")
            state, questions, kw = request.parse_request(req)
            kw.update({k: v for k, v in flags.items() if v is not None})
        else:
            if a.questions is None or (a.text is None and a.state is None):
                raise ValueError("give -q with -t or -s, or the whole call with --request")
            questions = request.load(a.questions, "the questions file")
            request.questions_ok(questions)
            if a.text is not None:
                state = a.text
            else:
                state = request.load(a.state, "the state file")
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
