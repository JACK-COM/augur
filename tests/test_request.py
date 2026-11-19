"""A whole call as one JSON object: `ask --request`, and the faults `parse_request` names."""
import contextlib
import csv
import io
import json
import sys
import unittest
from pathlib import Path

from tests.harness import AugurCase

from augur import main  # noqa: E402
from augurlib.request import parse_request  # noqa: E402


class Request(AugurCase):
    def test_request_piped_and_from_file(self):
        home, stub, fruit = self.home, self.stub, self.fruit
        req = {"questions": fruit, "text": "banana", "subject": "lunch", "caller": "piped"}
        saved_stdin, sys.stdin = sys.stdin, io.StringIO(json.dumps(req))
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = main(["ask", "--request", "-", "--caller", "flagged"])
        finally:
            sys.stdin = saved_stdin
        piped = json.loads(out.getvalue()) if code == 0 else {}
        self.assertTrue(piped.get("answers", {}).get("fruit", {}).get("noul", 0) > 0.9
                        and stub.calls[-1]["state"]["subject"] == "lunch", f"--request -: exit {code}")
        with open(Path(home) / "usage.csv") as f:
            self.assertTrue(list(csv.reader(f))[-1][2] == "flagged", "--request: a flag did not win over the request")
        with open(Path(home) / "call.json", "w") as f:
            json.dump(req, f)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            codes = (main(["ask", "--request", str(Path(home) / "call.json")]),
                     main(["ask", "--request", str(Path(home) / "call.json"), "-q", "x.json"]),
                     main(["ask"]))
        self.assertTrue(codes == (0, 2, 2), f"--request FILE, -q beside --request, no input: exits {codes}")

    def test_request_faults(self):
        fruit = self.fruit
        for bad, fragment in (([], "not a JSON object"), ({"questions": fruit, "text": "x", "subjcet": "y"}, "'subjcet'"),
                              ({"questions": fruit}, "exactly one"), ({"questions": fruit, "text": "x", "state": {}}, "exactly one"),
                              ({"questions": {}, "text": "x"}, "non-empty"), ({"questions": fruit, "text": 3}, "`text`"),
                              ({"questions": fruit, "text": "x", "siblings": "a,b"}, "`siblings`"),
                              ({"questions": fruit, "text": "x", "caller": ""}, "`caller`")):
            try:
                parse_request(bad)
                self.fail(f"request accepted: {bad}")
            except ValueError as e:
                self.assertTrue(fragment in str(e), f"request {bad}: {e}")


if __name__ == "__main__":
    unittest.main()
