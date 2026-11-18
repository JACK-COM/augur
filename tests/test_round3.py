"""Findings of the third blind red-team round (no high severity left), each failing before its fix."""
import json
import sys
import time
from pathlib import Path
from unittest import mock

from tests.harness import AugurCase, unavailable

from augur import ask  # noqa: E402
from augurlib.calibrate import check_spec  # noqa: E402


class Round3(AugurCase):
    def write(self, backends):
        (Path(self.home) / "augur.json").write_text(json.dumps({"backends": backends}))

    def test_a_laya_worker_that_never_reads_is_cut_at_its_timeout(self):
        py = Path(self.home) / "py"
        py.write_text("#!/bin/sh\nexec sleep 200\n")
        py.chmod(0o755)
        self.write({"lay": {"type": "laya", "python": str(py), "checkpoint": "c"}})
        t0 = time.monotonic()
        self.assertTrue(unavailable(lambda: ask("x" * 300_000, self.fruit, backend="lay", timeout=1), "no whole answer"))
        self.assertLess(time.monotonic() - t0, 5)

    def test_a_whole_call_ends_by_about_three_timeouts(self):
        slept = []
        self.stub.script = [503] * 6
        with mock.patch("time.sleep", lambda s: slept.append(s)):
            unavailable(lambda: ask("x", self.fruit, timeout=2), "503")
        self.assertLessEqual(sum(slept), 6.5, slept)

    def test_items_with_a_bad_subject_or_siblings_are_refused_first(self):
        for bad in ({"siblings": 5}, {"subject": 3}):
            with self.assertRaises(ValueError):
                check_spec({"questions": self.fruit, "items": [{"id": "a", "text": "hi", "labels": {}, **bad}]})

    def test_an_entry_of_the_wrong_types_is_unavailable(self):
        self.write({"c": {"type": "command", "command": [sys.executable, "-c", "print(1)"], "timeout": "5"}})
        self.assertTrue(unavailable(lambda: ask("x", self.fruit, backend="c"), "augur.json"))
        self.write({"c": {"type": "command", "command": [sys.executable, "-c", "print(1)"], "models": "pinned-model-v2"}})
        self.assertTrue(unavailable(lambda: ask("x", self.fruit, backend="c", model="ned"), "augur.json"))
        self.write({"c": {"type": ["command"], "command": "x"}})
        self.assertTrue(unavailable(lambda: ask("x", self.fruit, backend="c"), "type"))
