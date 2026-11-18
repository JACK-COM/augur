"""What a backend, a request or a planted file can no longer do, each an attack the 0.6.1
audit reproduced: follow a redirect with the key, print a terminal escape, plant a formula
in the ledger, pick its own model or checkpoint, truncate a file through the lock, or crash
past the unavailable contract."""
import csv
import http.client
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from tests.harness import AugurCase, unavailable

from augur import ask, main  # noqa: E402


class Hardening(AugurCase):
    def write(self, backends):
        (Path(self.home) / "augur.json").write_text(json.dumps({"backends": backends}))

    def script(self, body):
        p = Path(self.home) / "s.py"
        p.write_text("import json, sys\nsys.stdin.read()\n" + body)
        return [sys.executable, str(p)]

    def test_a_redirect_is_not_followed(self):
        hits = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                hits.append((self.path, self.headers.get("Authorization")))
                self.send_response(307)
                self.send_header("Location", f"http://localhost:{port}/stolen")
                self.end_headers()

        srv = HTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.write({"r": {"type": "systemone", "url": f"http://127.0.0.1:{port}/v1", "model": "m"}})
        self.web.stop()                           # the real client, against a real server
        self.assertTrue(unavailable(lambda: ask("x", self.fruit, backend="r"), "redirects to"))
        self.assertEqual([h[0] for h in hits], ["/v1"], "the redirect was followed")

    def test_remote_text_reaches_no_terminal_raw(self):
        evil = "\x1b]0;pwned\x07ok"
        self.write({"c": {"type": "command", "command": self.script(
            f"print(json.dumps({{'model': {evil!r}, 'answers': {{'fruit': {{'type': 'noul', 'noul': 0.9}}}}}}))")}})
        r = ask("x", self.fruit, backend="c", caller="=HYPERLINK(\"http://x\")")
        self.assertNotIn("\x1b", r["model"])
        rows = list(csv.reader(open(Path(self.home) / "usage.csv")))
        self.assertTrue(rows[-1][2].startswith("'="), rows[-1])
        self.assertNotIn("\x1b", rows[-1][3])
        self.write({"c": {"type": "command", "command": self.script(f"sys.stderr.write({evil!r}); sys.exit(1)")}})
        try:
            ask("x", self.fruit, backend="c")
        except Exception as e:  # noqa: AugurUnavailable, by its message
            self.assertNotIn("\x1b", str(e))

    def test_a_request_picks_no_model_its_entry_does_not_allow(self):
        self.write({"c": {"type": "command", "model": "own", "models": ["other"], "command": self.script(
            "print(json.dumps({'answers': {'fruit': {'type': 'noul', 'noul': 0.9}}}))")}})
        self.assertTrue(unavailable(lambda: ask("x", self.fruit, backend="c", model="attacker/repo"), "does not allow"))
        ask("x", self.fruit, backend="c", model="other")
        ask("x", self.fruit, backend="c", model="own")

    def test_the_lock_truncates_nothing_through_a_link(self):
        target = Path(self.home) / "precious"
        target.write_text("keep me")
        (Path(self.home) / "augur.json.lock").symlink_to(target)
        main(["configure", "backend", "jev", "--default"])
        self.assertEqual(target.read_text(), "keep me")

    @mock.patch("time.sleep", lambda s: None)
    def test_a_cut_reply_or_a_bad_manifest_is_unavailable(self):
        self.stub.answer = lambda *a: (_ for _ in ()).throw(http.client.IncompleteRead(b"x"))
        self.assertTrue(unavailable(lambda: ask("x", self.fruit), "retries exhausted"))
        (Path(self.home) / "augur.json").write_text("[]")
        self.assertTrue(unavailable(lambda: ask("x", self.fruit), "not an object"))
        self.write({"jev": ["url"]})
        self.assertTrue(unavailable(lambda: ask("x", self.fruit), "not an object"))

    def test_a_noul_answer_carries_a_noul(self):
        self.write({"c": {"type": "command", "command": self.script(
            "print(json.dumps({'answers': {'fruit': {'type': 'noul'}}}))")}})
        self.assertTrue(unavailable(lambda: ask("x", self.fruit, backend="c"), "no `noul`"))
