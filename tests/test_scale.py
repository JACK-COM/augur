"""Calibrate keeps what an interrupted run paid for and resumes it, asks a failed item again,
sizes its pool to the server, and computes AUROC by rank sum; `augur score` answers many
requests through one process and warns when its texts are far from the calibrated lengths."""
import contextlib
import io
import json
import random
import sys
from pathlib import Path
from unittest import mock

from tests.harness import AugurCase

from augur import main  # noqa: E402
from augurlib.calibrate import _auroc, calibrate, workers_for  # noqa: E402
from augurlib.commands.score import drift  # noqa: E402


def brute(pos, neg):
    return sum(1 if p > q else 0.5 if p == q else 0 for p in pos for q in neg) / (len(pos) * len(neg))


class Scale(AugurCase):
    def spec(self, n=6):
        return {"questions": self.fruit, "items": [
            {"id": str(i), "text": "banana" if i % 2 else "stone", "labels": {"fruit": bool(i % 2)}} for i in range(n)]}

    def test_auroc_by_rank_sum_matches_the_pairwise_count(self):
        rng = random.Random(7)
        for _ in range(50):
            pos = [rng.choice([0.1, 0.2, 0.5, 0.9]) for _ in range(rng.randint(1, 30))]
            neg = [rng.choice([0.1, 0.2, 0.5, 0.9]) for _ in range(rng.randint(1, 30))]
            self.assertAlmostEqual(_auroc(pos, neg), brute(pos, neg))

    def test_an_interrupted_run_resumes_where_it_stopped(self):
        import http.client
        out = Path(self.home) / "run"
        asked, down = [], [True]

        def answer(state, qid, q):
            if down[0] and state["text"] == "stone":
                raise http.client.IncompleteRead(b"")          # the server dies on these
            asked.append(state["text"])
            return 0.9 if state["text"] == "banana" else 0.1
        self.stub.answer = answer
        with mock.patch("time.sleep", lambda s: None):
            calibrate(self.spec(), out_dir=str(out), workers=1, log=lambda *a: None)
        journal = out / "results-jev.jsonl"
        self.assertEqual(len(journal.read_text().splitlines()), 3, "the journal holds what was answered")
        asked.clear()
        down[0] = False
        means = calibrate(self.spec(), out_dir=str(out), workers=1, resume=True, log=lambda *a: None)
        self.assertEqual(asked, ["stone"] * 3, "resume asked what the journal already held")
        self.assertTrue(all(m["fruit"] is not None for m in means.values()))

    def test_a_failed_item_is_asked_once_more(self):
        with mock.patch("time.sleep", lambda s: None):
            self.stub.script = [500] * 6                 # one item's first try: every retry
            means = calibrate(self.spec(2), workers=1, log=lambda *a: None)
        self.assertTrue(all(m["fruit"] is not None for m in means.values()))

    def test_the_pool_fits_the_server(self):
        self.assertEqual(workers_for({"url": "http://localhost:11434/v1"}, "systemone", None), 1)
        self.assertEqual(workers_for({"url": "https://api.example/v1"}, "systemone", None), 6)
        self.assertEqual(workers_for({"url": "https://api.example/v1", "workers": 12}, "systemone", None), 12)
        self.assertEqual(workers_for({}, "laya", 8), 1)

    def test_score_answers_many_in_order(self):
        reqs = [{"questions": self.fruit, "text": t} for t in ("banana", "stone")] + \
               [{"questions": self.fruit, "text": "x", "model": "not-allowed"}]
        o, saved = io.StringIO(), sys.stdin
        sys.stdin = io.StringIO("\n".join(json.dumps(r) for r in reqs) + "\n")
        try:
            with contextlib.redirect_stdout(o), contextlib.redirect_stderr(io.StringIO()):
                code = main(["score", "--workers", "2"])
        finally:
            sys.stdin = saved
        lines = [json.loads(x) for x in o.getvalue().splitlines()]
        self.assertEqual(code, 3)
        self.assertGreater(lines[0]["answers"]["fruit"]["noul"], 0.9)
        self.assertLess(lines[1]["answers"]["fruit"]["noul"], 0.1)
        self.assertIn("does not allow", lines[2]["error"])

    def test_texts_far_from_the_calibrated_lengths_draw_a_warning(self):
        shape = {"p10": 40, "median": 80, "p90": 160}
        self.assertIsNone(drift(["x" * 90] * 3, shape))
        self.assertIn("may not hold", drift(["x" * 2000] * 3, shape))
