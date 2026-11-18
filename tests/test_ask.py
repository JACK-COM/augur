"""ask: the default backend's check, one call, the ledger row it books."""
import csv
import unittest
from pathlib import Path

from tests.harness import AugurCase

from augur import ask, available, manifest_findings  # noqa: E402


class Ask(AugurCase):
    def test_check_ask_and_ledger(self):
        home, stub, fruit = self.home, self.stub, self.fruit
        ok, reason = available()
        self.assertTrue(ok and reason == "jev-1.13.0 answers" and stub.calls[-1]["model"] == "jev-1.13.0",
                        f"check asks the pinned model a real question: {reason}")
        self.assertTrue(manifest_findings() == [], f"a clean manifest drew findings: {manifest_findings()}")

        r = ask("banana", fruit, subject="lunch", siblings=["apple"], caller="selftest")
        st = stub.calls[-1]["state"]
        self.assertTrue(r["answers"]["fruit"]["noul"] > 0.9 and r["backend"] == "jev", "ask: answer or backend")
        self.assertTrue(st["text"] == "banana" and st["subject"] == "lunch" and st["siblings"] == ["apple"] and st.get("uid"),
                        "ask: subject, siblings and uid not named in the state")
        with open(Path(home) / "usage.csv") as f:
            rows = list(csv.reader(f))
        self.assertTrue(rows[0][0] == "ts" and rows[-1][2] == "selftest" and rows[-1][6] == "0.001000",
                        f"ledger: {rows[-1] if rows else 'empty'}")


if __name__ == "__main__":
    unittest.main()
