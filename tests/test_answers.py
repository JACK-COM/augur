"""The answer checks and the 429 retry."""
import unittest
from unittest import mock

from tests.harness import AugurCase, unavailable

from augur import ask  # noqa: E402


class Answers(AugurCase):
    def test_answer_not_a_probability(self):
        stub, fruit = self.stub, self.fruit
        for bad in (float("nan"), 1.2):
            stub.answer = lambda state, qid, q, bad=bad: bad
            self.assertTrue(unavailable(lambda: ask("x", fruit), "not a probability"), f"answer {bad} accepted")

    @mock.patch("time.sleep", lambda s: None)
    def test_retry(self):
        stub, fruit = self.stub, self.fruit
        stub.answer = lambda state, qid, q: 0.9
        stub.script = [429, 429]
        self.assertTrue(ask("x", fruit)["answers"]["fruit"]["noul"] == 0.9, "429: not retried to an answer")
        stub.script = [500]
        self.assertTrue(unavailable(lambda: ask("x", fruit), "HTTP 500"), "500: not reported")


if __name__ == "__main__":
    unittest.main()
