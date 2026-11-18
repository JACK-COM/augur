"""Augur's half of panoply-lib's scorer contract: accept its request, answer at its path."""
import contextlib
import io
import json
import sys
import unittest

from tests.harness import AugurCase

from augur import main  # noqa: E402


class Contract(AugurCase):
    def test_scorer_contract(self):
        stub = self.stub
        import _scorer_contract as contract
        stub.answer = lambda state, qid, q: 0.95 if "banana" in json.dumps(state) and qid in ("fruit",) else 0.05
        saved_stdin, sys.stdin = sys.stdin, io.StringIO(json.dumps(contract.REQUEST))
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = main(["ask", "--request", "-", "--caller", "grille"])
        finally:
            sys.stdin = saved_stdin
        try:
            p = contract.probability(json.loads(out.getvalue()))
            self.assertTrue(code == 0 and 0 <= p <= 1, f"scorer contract: exit {code}, probability {p!r}")
        except (KeyError, TypeError, ValueError) as e:
            self.fail(f"scorer contract: the reply lacks {'.'.join(contract.REPLY_PATH)} ({e!r})")


if __name__ == "__main__":
    unittest.main()
