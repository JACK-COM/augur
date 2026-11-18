"""Findings of the first blind red-team round and the second convention audit, each a case
that failed before its fix: waits past their timeout, replies and inputs that crashed past
the exit codes, strings printed raw, gates an entry change could step around."""
import contextlib
import io
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from tests.harness import AugurCase, unavailable

from augur import ask, main  # noqa: E402
from augurlib.backends import systemone  # noqa: E402
from augurlib.calibrate import calibrate  # noqa: E402


class Round1(AugurCase):
    def write(self, backends, **top):
        (Path(self.home) / "augur.json").write_text(json.dumps({"backends": backends, **top}))

    def script(self, body):
        p = Path(self.home) / f"s{abs(hash(body))}.py"
        p.write_text("import json, sys\nsys.stdin.read()\n" + body)
        return [sys.executable, str(p)]

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

    def test_a_dripping_server_is_cut_at_its_timeout(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                for _ in range(200):
                    try:
                        self.wfile.write(b" ")
                        self.wfile.flush()
                    except OSError:
                        return
                    time.sleep(0.05)
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.web.stop()
        self.write({"d": {"type": "systemone", "url": f"http://127.0.0.1:{srv.server_address[1]}/v1", "model": "m",
                          "timeout": 1}})
        t0 = time.monotonic()
        self.assertTrue(unavailable(lambda: ask("x", self.fruit, backend="d", retries=0), "deadline"))
        self.assertLess(time.monotonic() - t0, 4)

    def test_a_laya_worker_that_never_ends_its_line_is_killed(self):
        py = Path(self.home) / "py"
        py.write_text("#!/bin/sh\nprintf '{\"answers\": '\nsleep 30\n")
        py.chmod(0o755)
        self.write({"lay": {"type": "laya", "python": str(py), "checkpoint": "c"}})
        t0 = time.monotonic()
        self.assertTrue(unavailable(lambda: ask("x", self.fruit, backend="lay", timeout=1), "no whole answer"))
        self.assertLess(time.monotonic() - t0, 4)

    def test_hostile_replies_are_unavailable(self):
        for body, why in (("print('[' * 200000)", "not JSON"),
                          ("print(json.dumps({'answers': {'fruit': {'type': 'noul', 'noul': 0.5, 'probabilities': [1]}}}))", "not an object"),
                          ("print(json.dumps({'answers': {'fruit': '\\x1b]0;x\\x07'}}))", "not an object"),
                          ("sys.stdout.write('x' * (2 << 20))", "more than")):
            self.write({"c": {"type": "command", "command": self.script(body)}})
            try:
                ask("x", self.fruit, backend="c")
                self.fail(body)
            except Exception as e:  # noqa: AugurUnavailable, by its message
                self.assertIn(why, str(e), body)
                self.assertNotIn("\x1b", str(e))
        self.write({"c": {"type": "command", "command": self.script(
            "print('{\"answers\": {\"fruit\": {\"type\": \"noul\", \"noul\": 0.5}}, \"usage\": {\"input_tokens\": Infinity}}')")}})
        self.assertEqual(ask("x", self.fruit, backend="c")["usage"]["input_tokens"], 0)

    def test_bad_inputs_exit_2_never_a_traceback(self):
        q = Path(self.home) / "q.json"
        for content, argv in (("5", ["ask", "-q", str(q), "-t", "x"]),
                              ("[" * 100000, ["ask", "--request", str(q)]),
                              ("[]", ["calibrate", str(q)]),
                              ("{}", ["calibrate", str(q)])):
            q.write_text(content)
            code, out = self.cli(argv)
            self.assertEqual(code, 2, f"{argv}: {out}")
            self.assertNotIn("Traceback", out)
        code, out = self.cli(["score", "--workers", "0"])
        self.assertEqual(code, 2, out)
        code, out = self.cli(["score"], "{\"questions\": 5, \"text\": \"x\"}\n")
        self.assertEqual(code, 2, out)
        self.assertIn("line 1", out)
        q.write_text(json.dumps({"questions": self.fruit, "items": [{"id": "a", "text": "x", "labels": {}}]}))
        code, out = self.cli(["calibrate", str(q), "--resume"])
        self.assertEqual(code, 2, out)

    def test_retry_after_from_a_server_never_breaks_the_sleep(self):
        for v in ("nan", "-3", "inf", "Wed, 21 Oct 2015 07:28:00 GMT", "7"):
            d = systemone.backoff(0, v)
            self.assertTrue(0 <= d <= systemone.RETRY_AFTER_CAP, v)

    def test_an_old_keyed_entry_is_confirmed_once_and_then_works(self):
        self.write({"k": {"type": "systemone", "url": "https://h.example/v1", "model": "m", "env_key": "TYPESAFE_API_KEY"}})
        self.assertTrue(unavailable(lambda: ask("x", self.fruit, backend="k"), "confirmed for no host"))
        code, out = self.cli(["configure", "backend", "k", "--url", "https://h.example/v1"])
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads((Path(self.home) / "augur.json").read_text())["backends"]["k"]["key_host"], "h.example:443")

    def test_a_laya_checkpoint_and_an_off_machine_default_need_a_person(self):
        self.person.stop()
        py = Path(self.home) / "py"
        py.write_text("#!/bin/sh\nexit 1\n")
        py.chmod(0o755)
        self.write({"lay": {"type": "laya", "python": str(py), "checkpoint": "good/one"},
                    "local": {"type": "systemone", "url": "http://localhost:1/v1", "model": "m"}}, backend="local")
        code, out = self.cli(["configure", "backend", "lay", "--checkpoint", "/tmp/evil.pt", "--force"])
        self.assertEqual(code, 1, out)
        code, out = self.cli(["configure", "backend", "jev", "--default"])
        self.assertEqual(code, 1, out)
        self.assertNotIn('"backend": "jev"', (Path(self.home) / "augur.json").read_text())

    def test_strings_an_agent_saved_print_without_their_escapes(self):
        self.write({"ok": {"type": "systemone", "url": "http://localhost:1/v1", "model": "m\x1b]0;PWNED\x07‮"}})
        code, out = self.cli(["configure", "backend", "--list"])
        self.assertNotIn("\x1b", out)
        self.assertNotIn("‮", out)
        code, out = self.cli(["configure", "backend", "u", "--type", "systemone", "--url", "http://127.0.0.1:1/\x1b]0;X\x07"])
        self.assertEqual(code, 2, out)

    def test_a_local_server_takes_any_model(self):
        self.write({"local": {"type": "systemone", "url": "http://localhost:1/v1", "model": "m"}})
        ask("banana", self.fruit, backend="local", model="another:7b")

    def test_a_torn_or_stale_journal_is_asked_again(self):
        out = Path(self.home) / "run"
        spec = {"questions": self.fruit, "items": [{"id": str(i), "text": "banana", "labels": {"fruit": True}}
                                                   for i in range(3)]}
        calibrate(spec, out_dir=str(out), workers=1, log=lambda *a: None)
        journal = out / "results-jev.jsonl"
        journal.write_text(journal.read_text()[:-5])                     # the last row cut short
        n = len(self.stub.calls)
        calibrate(spec, out_dir=str(out), workers=1, resume=True, log=lambda *a: None)
        self.assertEqual(len(self.stub.calls) - n, 1, "the torn row was not asked again")
        spec["items"][0]["text"] = "stone"                               # an edited item
        n = len(self.stub.calls)
        calibrate(spec, out_dir=str(out), workers=1, resume=True, log=lambda *a: None)
        self.assertEqual(len(self.stub.calls) - n, 1, "a stale row was reused")
