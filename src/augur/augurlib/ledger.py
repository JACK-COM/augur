"""The usage ledger: one CSV row per call, appended, never read on the hot path.
"""
import csv
import os
from datetime import datetime, timezone

import _guard as guard

from . import paths


def cell(v: object) -> str:
    """A ledger cell that is only text: controls made spaces, and a leading = + - @ quoted,
    since a spreadsheet opening the ledger would otherwise run it as a formula."""
    s = guard.relay(v, 200)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def log(backend, caller, model, nq, tokens, price):
    try:
        ledger = paths.home() / "usage.csv"
        ledger.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(ledger, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with open(fd, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if f.tell() == 0:  # header under the same append handle; a concurrent second header is tolerated
                w.writerow(["ts", "backend", "caller", "model", "questions", "input_tokens", "usd"])
            w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), cell(backend), cell(caller), cell(model), nq,
                        tokens, f"{tokens * price / 1e6:.6f}"])
    except OSError:
        pass  # the ledger is bookkeeping, never a reason to fail a call
