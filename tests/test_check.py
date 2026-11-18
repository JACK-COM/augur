"""`configure check` and `check --live`: the configured items and the spec faults."""
import contextlib
import io
import json
import unittest
from pathlib import Path

from tests.harness import AugurCase

from augur import CHECK_MAX, _check_file, _check_spec, choice, main  # noqa: E402


class Check(AugurCase):
    def test_configure_check_and_live(self):
        home, stub, fruit = self.home, self.stub, self.fruit
        stub.answer = lambda state, qid, q: 0.95 if "banana" in json.dumps(state) and qid in ("fruit",) else 0.05
        items = Path(home) / "mine.json"
        items.write_text(json.dumps({"questions": fruit, "items": [
            {"id": "b", "text": "banana", "labels": {"fruit": True}},
            {"id": "c", "text": "carrot", "labels": {"fruit": False}}]}))
        quiet = contextlib.redirect_stdout(io.StringIO())
        with quiet, contextlib.redirect_stderr(io.StringIO()):
            codes = [main(["configure", "check", str(items)]), main(["check", "--live"])]
            items.write_text(json.dumps({"questions": fruit, "items": [
                {"id": "c", "text": "carrot", "labels": {"fruit": True}}]}))
            codes += [main(["check", "--live"]),                        # the copy, not the edited original
                      main(["configure", "check", str(items)]), main(["check", "--live"])]
            items.write_text(json.dumps({"questions": fruit, "items": [
                {"id": str(i), "text": "x", "labels": {"fruit": True}} for i in range(CHECK_MAX + 1)]}))
            codes += [main(["configure", "check", str(items)]),
                      main(["configure", "check", str(items), "--threshold", "1"])]
            items.write_text(json.dumps({"questions": fruit, "items": [{"id": "b", "text": "banana",
                                                                         "labels": {"frut": True}}]}))
            codes += [main(["configure", "check", str(items)]), main(["configure", "check", "--reset"]),
                      main(["check", "--live"])]
        self.assertTrue(codes == [0, 0, 0, 0, 1, 2, 2, 2, 0, 0], f"configure check / check --live exits: {codes}")
        items.write_text(json.dumps({"questions": fruit, "items": [{"id": "b", "text": "banana",
                                                                     "labels": {"fruit": True}}]}))
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown), contextlib.redirect_stderr(io.StringIO()):
            codes = [main(["configure", "check"]), main(["configure", "check", str(items), "--threshold", "0.4"]),
                     main(["configure", "check"]), main(["configure", "check", str(items), "--reset"]),
                     main(["configure", "check", "--threshold", "0.4"])]
            _check_file().write_text(json.dumps({"threshold": 0.5, "questions": fruit, "items": [
                {"id": "b", "text": "banana", "labels": {"fruit": True}, "siblings": 5}]}))
            codes += [main(["check", "--live"]), main(["configure", "check", "--reset"])]
        self.assertTrue(codes == [0, 0, 0, 2, 2, 2, 0], f"configure check view, flag combinations, a hand-broken copy: {codes}")
        self.assertTrue("built-in" in shown.getvalue() and "1 items from" in shown.getvalue() and "threshold 0.4" in shown.getvalue(),
                        "configure check with no file did not show the built-in pair, then the configured items")
        one = [{"id": "b", "text": "banana", "labels": {"fruit": True}}]
        for spec, t, fragment in (([], 0.5, "not a JSON object"), ({"questions": {}, "items": one}, 0.5, "`questions`"),
                                  ({"questions": fruit, "items": one}, 1.0, "strictly between"),
                                  ({"questions": fruit, "items": one * 2}, 0.5, "appears twice"),
                                  ({"questions": fruit, "items": [{"id": "b", "text": "", "labels": {"fruit": True}}]},
                                   0.5, "`text`"),
                                  ({"questions": fruit, "items": [{**one[0], "subject": 3}]}, 0.5, "`subject`"),
                                  ({"questions": fruit, "items": [{**one[0], "siblings": "a,b"}]}, 0.5, "`siblings`"),
                                  ({"questions": {"kind": choice("Which?", {"a": None})},
                                    "items": [{"id": "b", "text": "x", "labels": {"kind": True}}]}, 0.5, "nouls only")):
            try:
                _check_spec(spec, t)
                self.fail(f"_check_spec accepted a spec lacking {fragment}")
            except ValueError as e:
                self.assertTrue(fragment in str(e), f"_check_spec: {e!r} does not name {fragment}")
        self.assertTrue(not _check_file().exists(), "--reset left check.json behind")


if __name__ == "__main__":
    unittest.main()
