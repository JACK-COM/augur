"""`configure backend`: flags and guided prompts share one verify-then-write."""
import contextlib
import csv
import io
import json
import os
import sys
import unittest
from pathlib import Path

from tests.harness import AugurCase

from augur import main  # noqa: E402
from augurlib.entries import PROBE_TIMEOUT  # noqa: E402
from augurlib.guided import _guided_backend  # noqa: E402
from augurlib.manifest import _backend, _manifest  # noqa: E402


class Configure(AugurCase):
    def run_cli(self, argv, answers=""):
        o, saved_in = io.StringIO(), sys.stdin
        sys.stdin = io.StringIO(answers)          # never a terminal here, so never guided by accident
        try:
            with contextlib.redirect_stdout(o), contextlib.redirect_stderr(o):
                code = main(argv) if argv is not None else _guided_backend()
        finally:
            sys.stdin = saved_in
        return code, o.getvalue()

    def saved(self):
        return json.loads((Path(self.home) / "augur.json").read_text(encoding="utf-8"))

    def test_configure_backend_by_flags(self):
        home, stub = self.home, self.stub
        cmd, _ = self.stand()
        run, saved = self.run_cli, self.saved
        # configure backend: flags and guided prompts share one verify-then-write
        (Path(home) / "augur.json").unlink()          # a first backend, before augur.json exists
        stub.answer = lambda state, qid, q: 0.95 if "banana" in json.dumps(state) else 0.05
        loc = ["configure", "backend", "loc", "--type", "systemone", "--url", "http://localhost:1/v1/systemone",
               "--model", "m:1b", "--price", "0"]
        code, out = run(loc)
        self.assertTrue(code == 0 and saved()["backends"]["loc"] == {"type": "systemone", "url": "http://localhost:1/v1/systemone",
                                                            "model": "m:1b", "price_per_mtok": 0.0},
               f"configure backend by flags: exit {code}, {out}")
        with open(Path(home) / "usage.csv") as f:
            row = list(csv.reader(f))[-1]
        self.assertTrue(row[1:4] == ["loc", "configure", "m:1b"] and stub.requests[-1][2] == PROBE_TIMEOUT,
                        f"configure's question: ledger {row}, timeout {stub.requests[-1][2]}")
        code, out = run(["configure", "backend", "loc", "--timeout", "9"])
        self.assertTrue(code == 0 and saved()["backends"]["loc"]["model"] == "m:1b" and saved()["backends"]["loc"]["timeout"] == 9,
                        f"one flag did not merge over the entry: {saved()}")
        stub.script = [500]
        code, out = run(loc[:2] + ["fails"] + loc[3:])
        self.assertTrue(code == 3 and "fails" not in saved()["backends"] and "not saved" in out, f"saved unverified: {code} {out}")
        stub.script = [500]
        code, out = run(loc[:2] + ["fails"] + loc[3:] + ["--force"])
        self.assertTrue(code == 0 and "fails" in saved()["backends"] and "has not answered" in out, f"--force: {code} {out}")
        for argv, fragment in ((["configure", "backend", "-x", "--url", "http://h"], "error"),
                               (["configure", "backend", "b@d", "--type", "command", "--command", "x"], "a name takes"),
                               (["configure", "backend", "nu", "--type", "systemone", "--model", "m"], "'url'"),
                               (["configure", "backend", "loc", "--command", "x"], "unknown key 'command'"),
                               (["configure", "backend", "--type", "command", "--command", "x"], "name the backend"),
                               (["configure", "backend", "--list", "--remove", "loc"], "stand alone"),
                               (["configure", "backend", "jev", "--force"], "--force saves settings"),
                               (["configure", "backend", "u", "--type", "systemone", "--url", "https://me:k@h/v1"], "username, password"),
                               (["configure", "backend", "u", "--type", "systemone", "--url", "https://h/v1?key=k"], "query string"),
                               (["configure", "backend", "loc", "--price", "nan"], "error"),
                               (["configure", "backend", "loc", "--timeout", "0"], "error"),
                               (["configure", "backend", "typo", "--default"], "no backend named 'typo'"),
                               (["configure", "backend", "--remove", ""], "no backend named ''")):
            try:
                code, out = run(argv)
            except SystemExit as e:                  # argparse refuses before main returns
                code, out = e.code, "error"
            self.assertTrue(code == 2 and fragment in out, f"configure backend {argv[2:]}: exit {code}, {out.strip()[-120:]}")
        code, out = run(["configure", "backend", "loc", "--type", "command", "--command", " ".join(cmd("ok"))])
        self.assertTrue(code == 0 and saved()["backends"]["loc"] == {"type": "command", "command": " ".join(cmd("ok"))},
                        f"a new type did not replace the entry whole: {saved()['backends']['loc']}")
        code, out = run(["configure", "backend", "loc", "--default"])
        self.assertTrue(code == 0 and saved()["backend"] == "loc" and _manifest()["backend"] == "loc", f"--default: {code} {out}")
        code, out = run(["configure", "backend", "jev", "--model", "jev-9"])
        self.assertTrue(code == 0 and saved()["backends"]["jev"] == {"model": "jev-9"} and _backend("jev")[1]["url"].startswith("https"),
                        f"a shipped name's change is not a merge over it: {saved()['backends'].get('jev')}")
        code, out = run(["configure", "backend", "--list"])
        self.assertTrue(code == 0 and "* loc" in out and "shipped, changed" in out and "yours" in out, f"--list: {out}")
        code, out = run(["configure", "backend", "jev"])
        self.assertTrue(code == 0 and json.loads(out)["origin"] == "shipped, changed", f"show without a terminal: {out}")
        code, out = run(["configure", "backend", "--remove", "jev"])
        self.assertTrue(code == 0 and "jev" not in saved()["backends"] and "shipped settings again" in out, f"--remove jev: {out}")
        code, out = run(["configure", "backend", "--remove", "loc"])
        self.assertTrue(code == 0 and "backend" not in saved() and "jev again" in out, f"--remove the default: {out}")
        self.assertTrue(run(["configure", "backend", "--remove", "jev"])[0] == 0 and run(["configure", "backend", "--remove", "zz"])[0] == 2,
                        "--remove on an untouched shipped name or an unknown one")
        (Path(home) / "augur.json").write_text('{"backends": null}')
        code, out = run(["configure", "backend", "--list"])
        self.assertTrue(code == 0 and "jev" in out, f"--list over a null `backends`: {code} {out}")
        (Path(home) / "augur.json").write_text('{"backends": {"x": null}}')
        code, out = run(["configure", "backend", "--list"])
        self.assertTrue(code == 2 and "`backends` must be an object" in out, f"--list over a null entry: {code} {out}")
        (Path(home) / "augur.json").write_text("{}")
        code, out = run(["configure", "backend", "hk", "--type", "systemone", "--url", "http://h/v1/systemone",
                         "--key-env", "AUGUR_SELFTEST_ABSENT_KEY", "--store-key"])
        self.assertTrue(code == 3 and "export AUGUR_SELFTEST_ABSENT_KEY=<your key>" in out
                        and "`augur configure backend hk --store-key` stores one" in out, f"a missing key's remedy: {out}")
        if sys.platform == "darwin":
            code, out = run(["configure", "backend", "jev", "--store-key"])
            self.assertTrue(code == 2 and "needs a terminal" in out, f"--store-key without a terminal: {out}")

    def test_configure_backend_guided(self):
        home, stub = self.home, self.stub
        cmd, _ = self.stand()
        run, saved = self.run_cli, self.saved
        (Path(home) / "augur.json").write_text("{}")          # the state the flags section left
        stub.answer = lambda state, qid, q: 0.95 if "banana" in json.dumps(state) else 0.05
        # guided, against a stand-in Ollama
        saved_host = os.environ.get("OLLAMA_HOST")
        os.environ["OLLAMA_HOST"] = "localhost:1"
        tags = {"models": [{"name": "nim:latest", "digest": "d1", "size": 9e9, "capabilities": ["decision"]},
                           {"name": "nim:9b-q8_0", "digest": "d1", "size": 9e9, "capabilities": ["decision"]},
                           {"name": "other:1b", "digest": "d2", "size": 1e9, "capabilities": ["completion"]},
                           {"name": "old:1b", "digest": "d3", "size": None, "capabilities": None}]}
        stub.routes = {"/api/version": {"version": "0.35.0"}, "/api/tags": tags,
                       "/api/pull": b'{"status": "pulling", "total": 4, "completed": 2}\n{"status": "success"}\n'}
        try:
            code, out = run(None, "1\n1\n\ny\n")
            self.assertTrue(code == 0 and saved()["backends"]["nim"]["model"] == "nim:9b-q8_0" and saved()["backend"] == "nim"
                            and saved()["backends"]["nim"]["url"] == "http://localhost:1/v1/systemone"
                            and "other:1b" not in out and "1 other model is not" in out,
                            f"guided Ollama, latest resolved to its exact tag, made default: {code} {out[-300:]}")
            code, out = run(None, "1\n4\nfresh:2b\n\nn\n")
            self.assertTrue(code == 0 and saved()["backends"]["fresh"]["model"] == "fresh:2b" and "success" in out,
                            f"guided Ollama pull: {code} {out[-300:]}")
            code, out = run(None, "1\n1\nnim\nn\n")
            self.assertTrue(code == 1 and "augur.json unchanged" in out, f"guided: declining to replace: {code} {out[-200:]}")
            stub.routes["/api/pull"] = b'{"error": "pull model manifest: file does not exist"}\n'
            code, out = run(None, "1\n4\nnope:1b\n")
            self.assertTrue(code == 3 and "file does not exist" in out, f"guided pull of a missing model: {code} {out[-200:]}")
            stub.routes["/api/tags"] = b"<html>proxy error</html>"
            code, out = run(None, "1\n")
            self.assertTrue(code == 3 and "not answering" in out, f"guided, a tags reply that is not JSON: {code} {out[-200:]}")
            stub.routes["/api/tags"] = tags
            stub.routes["/api/version"] = {"version": "0.34.9"}
            code, out = run(None, "1\n")
            self.assertTrue(code == 3 and "too old" in out, f"guided, an old Ollama: {code} {out[-200:]}")
            code, out = run(None, f"4\n{' '.join(cmd('ok'))}\n\nscr\nn\n")
            self.assertTrue(code == 0 and saved()["backends"]["scr"]["type"] == "command", f"guided command: {code} {out[-200:]}")
            if sys.platform == "darwin":       # a hosted key: decline storing it, so no keychain is touched
                code, out = run(None, "2\nhttps://api.acme.example/v1/systemone\nacme-2\n0.05\n\n\nn\nn\n")
                self.assertTrue(code == 3 and "acme" not in saved()["backends"] and "acme --store-key" in out,
                                f"guided hosted without its key: {code} {out[-300:]}")
            code, out = run(None, "")
            self.assertTrue(code == 1 and "augur.json unchanged" in out, f"guided, input ends: {code} {out[-200:]}")
        finally:
            stub.routes = {}
            if saved_host is None:
                os.environ.pop("OLLAMA_HOST", None)
            else:
                os.environ["OLLAMA_HOST"] = saved_host
        (Path(home) / "augur.json").write_text("{}")


if __name__ == "__main__":
    unittest.main()
