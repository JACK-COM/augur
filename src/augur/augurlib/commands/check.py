"""`augur check`: a positive check of the backend, or with --live, known questions graded."""
import argparse

from .. import check as live
from .. import client, manifest
from .base import Verb


def args(k: argparse.ArgumentParser) -> None:
    k.add_argument("--live", action="store_true",
                   help="ask known questions and check the answers: yours from `augur configure check`, else two built-in")


def run(a: argparse.Namespace) -> int:
    for finding in manifest.manifest_findings():
        print(finding)
    if a.live:
        return live.live_check(a.backend)
    ok, reason = client.available(a.backend)
    print(("ok: " if ok else "unavailable: ") + reason)
    return 0 if ok else 3


VERBS = (Verb("check", "positive check for the backend", run, args, backend=True),)
