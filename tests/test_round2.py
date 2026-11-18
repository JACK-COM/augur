"""Findings of the second blind red-team round, each a case that failed before its fix."""
import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

from tests.harness import AugurCase, unavailable

from augur import ask, main  # noqa: E402
from augurlib.calibrate import calibrate  # noqa: E402


class Round2(AugurCase):
    def write(self, backends, **top):
        (Path(self.home) / "augur.json").write_text(json.dumps({"backends": backends, **top}))

    def saved(self):
        return json.loads((Path(self.home) / "augur.json").read_text())

    def cli(self, argv, stdin=""):
        o, saved = io.StringIO(), sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(o), contextlib.redirect_stderr(o):
                try:
                    code = main(argv)
                except SystemExit as e:
                    code = e.code
        finally:
            sys.stdin = saved
        return code, o.getvalue()

    def local(self):
        return {"type": "systemone", "url": "http://localhost:1/v1", "model": "m"}

    def test_the_environment_never_becomes_the_saved_default(self):
        self.person.stop()
        self.write({"local": self.local()}, backend="local")
        with mock.patch.dict(os.environ, {"AUGUR_BACKEND": "jev"}):
            code, out = self.cli(["configure", "backend", "jev", "--default"])
        self.assertEqual(code, 1, out)
        self.assertEqual(self.saved()["backend"], "local")

    def test_a_remove_that_leaves_an_off_machine_default_needs_a_person(self):
        self.person.stop()
        self.write({"local": self.local()}, backend="local")
        code, out = self.cli(["configure", "backend", "--remove", "local"])
        self.assertEqual(code, 1, out)
        self.assertIn("local", self.saved()["backends"])
        self.write({"jev": {"url": "http://localhost:1/v1"}}, backend="jev")
        code, out = self.cli(["configure", "backend", "--remove", "jev"])
        self.assertEqual(code, 1, out)

    def test_a_laya_checkpoint_needs_a_person_with_no_interpreter_set(self):
        self.person.stop()
        code, out = self.cli(["configure", "backend", "laya", "--python", "", "--checkpoint", "evil/ckpt", "--force"])
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("evil", (Path(self.home) / "augur.json").read_text() if (Path(self.home) / "augur.json").exists() else "")

    def test_a_script_that_never_reads_stdin_is_cut_at_its_timeout(self):
        self.write({"c": {"type": "command", "command": [sys.executable, "-c", "import time; time.sleep(300)"], "timeout": 1}})
        t0 = time.monotonic()
        self.assertTrue(unavailable(lambda: ask("x" * 400_000, self.fruit, backend="c"), "no answer"))
        self.assertLess(time.monotonic() - t0, 5)

    def test_a_long_retry_after_waits_no_longer_than_the_timeout(self):
        slept = []
        self.stub.script = [503] * 6
        with mock.patch("time.sleep", lambda s: slept.append(s)):
            ask("x", self.fruit, timeout=2) if False else unavailable(lambda: ask("x", self.fruit, timeout=2), "503")
        self.assertTrue(slept and max(slept) <= 2, slept)

    def test_hostile_files_are_bad_input_never_a_traceback(self):
        out = Path(self.home) / "run"
        out.mkdir()
        (out / "results-jev.jsonl").write_text('{"id": [1]}\n{"id": "a", "values": [1], "fp": "x"}\n')
        spec = {"questions": self.fruit, "items": [{"id": "a", "text": "banana", "labels": {"fruit": True}, "class": 1},
                                                    {"id": "b", "text": "stone", "labels": {"fruit": False}, "class": "x"}]}
        calibrate(spec, out_dir=str(out), resume=True, workers=1, log=lambda *a: None, previous={"a": 5})
        shape = Path(self.home) / "shape.json"
        shape.write_text('{"p10": "x"}')
        code, o = self.cli(["score", "--against", str(shape)], json.dumps({"questions": self.fruit, "text": "x"}) + "\n")
        self.assertEqual(code, 2, o)
        (Path(self.home) / "augur.json").write_text("[" * 100000)
        code, o = self.cli(["check"])
        self.assertNotIn("Traceback", o)
        self.assertNotEqual(code, 0)

    def test_a_reply_key_prints_without_its_escapes(self):
        self.stub.answer = lambda *a: 0.5
        script = Path(self.home) / "s.py"
        script.write_text("import json,sys; sys.stdin.read(); print(json.dumps({'answers': {'fruit': {'type': 'choice', "
                          "'probabilities': {'\\x1b]0;PWNED\\x07': 2}}}}))")
        self.write({"c": {"type": "command", "command": [sys.executable, str(script)]}})
        try:
            ask("x", self.fruit, backend="c")
            self.fail("accepted")
        except Exception as e:  # noqa: AugurUnavailable, by its message
            self.assertNotIn("\x1b", str(e))
