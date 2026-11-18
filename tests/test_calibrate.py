"""calibrate and its AUROC."""
import unittest

from tests.harness import AugurCase

from augur import calibrate  # noqa: E402
from augurlib.calibrate import _auroc  # noqa: E402


class Calibrate(AugurCase):
    def test_calibrate(self):
        stub, fruit = self.stub, self.fruit
        self.assertTrue(abs(_auroc([0.9, 0.8], [0.1, 0.8]) - 0.875) < 1e-9, "auroc")
        stub.answer = lambda state, qid, q: 0.9 if "yes" in state["text"] else 0.1
        spec = {"name": "selftest", "questions": fruit,
                "items": [{"id": "p", "text": "yes", "labels": {"fruit": True}},
                          {"id": "n", "text": "no", "labels": {"fruit": False}}]}
        lines = []
        means = calibrate(spec, runs=2, workers=1, log=lines.append)
        self.assertTrue(means == {"p": {"fruit": 0.9}, "n": {"fruit": 0.1}}, f"calibrate means: {means}")
        self.assertTrue(any("AUROC 1.000" in ln for ln in lines), "calibrate: AUROC line")


if __name__ == "__main__":
    unittest.main()
