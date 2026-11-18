"""batch: the re-ask near the threshold, the cap, the id rule."""
import unittest

from tests.harness import AugurCase

from augur import BatchTooLarge, MAX_BATCH, batch, noul  # noqa: E402


class Batch(AugurCase):
    def test_batch(self):
        stub = self.stub
        def qs(id_, path):
            return {"long": noul(f"Is the text at `{path}` longer than three words?")}
        stub.answer = lambda state, qid, q: 0.55 if "items" in state else 0.9
        n = len(stub.calls)
        out = batch({"a": "one two three four", "b": "one"}, qs, threshold=0.5)
        self.assertTrue(set(out) == {"a", "b"} and all(out[i]["long"].get("reasked") for i in out), "batch: re-ask near threshold")
        self.assertTrue(len(stub.calls) == n + 3 and stub.calls[n + 1]["state"]["text"] == "one two three four",
                        "batch: re-ask not asked in isolation")
        try:
            batch({str(i): "x" for i in range(MAX_BATCH + 1)}, qs)
            self.fail("batch: cap not enforced")
        except BatchTooLarge:
            pass
        try:
            batch({"a__b": "x"}, qs)
            self.fail("batch: '__' in an id accepted")
        except ValueError:
            pass


if __name__ == "__main__":
    unittest.main()
