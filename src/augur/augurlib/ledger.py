"""The usage ledger: one CSV row per call, appended, never read on the hot path.
"""
import csv
from datetime import datetime, timezone

from . import paths


def _log(backend, caller, model, nq, tokens, price):
    try:
        ledger = paths.home() / "usage.csv"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if f.tell() == 0:  # header under the same append handle; a concurrent second header is tolerated
                w.writerow(["ts", "backend", "caller", "model", "questions", "input_tokens", "usd"])
            w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), backend, caller, model, nq,
                        tokens, f"{tokens * price / 1e6:.6f}"])
    except OSError:
        pass  # the ledger is bookkeeping, never a reason to fail a call
