"""Backend types: command, typed and keyless systemone, and the unavailable paths."""
import csv
import json
import os
import sys
import unittest
from pathlib import Path

from tests.harness import AugurCase, unavailable

from augurlib import paths  # noqa: E402
from augur import ask, available, manifest_findings  # noqa: E402
from augurlib.backends.command import argv as _command_argv  # noqa: E402
from augurlib.manifest import BACKEND_TYPES, _backend, _defaults  # noqa: E402


class Backends(AugurCase):
    def test_command_backends(self):
        home, fruit = self.home, self.fruit
        cmd, seen = self.stand()
        # a command backend: a stand-in script, then `augur ask --request -` itself as the command
        me = [sys.executable, str(paths.HERE / "augur.py")]
        with open(Path(home) / "augur.json", "w") as f:
            json.dump({"backends": {
                "stand": {"command": cmd("ok"), "price_per_mtok": 2.0},
                "named": {"command": " ".join(cmd("ok")), "model": "m1"},
                "fails": {"command": cmd("fail")}, "prose": {"command": cmd("prose")}, "nan": {"command": cmd("nan")},
                "slow": {"command": cmd("slow"), "timeout": 0.5}, "bytes": {"command": cmd("bytes")},
                "unquoted": {"command": "stand 'open"},
                "chain": {"command": me + ["ask", "--request", "-", "--backend", "stand", "--caller", "inner"]},
                "bare": {"price_per_mtok": 1.0}}}, f)
        self.assertTrue(manifest_findings() == ["augur.json: backends.bare: names no `type`; give it one of command, laya, systemone"],
                        f"command backends: {manifest_findings()}")
        ok, reason = available("stand")
        self.assertTrue(ok and "answers" in reason, f"command check: {reason}")
        r = ask("banana", fruit, backend="stand", caller="cmd")
        sent = json.loads(seen.read_text(encoding="utf-8"))
        self.assertTrue(r["backend"] == "stand" and r["answers"]["fruit"]["noul"] == 0.97, f"command ask: {r}")
        self.assertTrue(set(sent) == {"questions", "state"} and sent["state"]["text"] == "banana" and "fruit" in sent["questions"],
                        f"command request: {sent}")
        with open(Path(home) / "usage.csv") as f:
            row = list(csv.reader(f))[-1]
        self.assertTrue(row[1:3] == ["stand", "cmd"] and row[5:] == ["10", "0.000020"], f"command ledger: {row}")
        r = ask("banana", fruit, backend="named")
        self.assertTrue(json.loads(seen.read_text(encoding="utf-8")).get("model") == "m1" and r["model"] == "m1",
                        "command: a string command or its model not honoured")
        r = ask("banana", fruit, backend="chain")
        self.assertTrue(r["answers"]["fruit"]["noul"] == 0.97, f"augur ask --request - as a command: {r}")
        self.assertTrue(_command_argv({"command": "~/x --flag"}) == [os.path.expanduser("~/x"), "--flag"], "command: ~ not expanded")
        for name, fragment in (("fails", "exited 3: boom"), ("prose", "not JSON"), ("nan", "not a probability"),
                               ("bare", "names no `type`"), ("slow", "no answer in 0.5s"),
                               ("bytes", "not JSON"), ("unquoted", "does not parse")):
            self.assertTrue(unavailable(lambda: ask("banana", fruit, backend=name), fragment), f"command backend {name}")
        with open(Path(home) / "augur.json", "w") as f:
            json.dump({"backends": {"loose": {"comand": ["x"]}}}, f)
        found = " | ".join(manifest_findings())
        self.assertTrue("unknown key 'comand', did you mean 'command'?" in found and "names no `type`" in found,
                        f"command manifest: {found}")

    def test_typed_backends(self):
        home, stub, fruit = self.home, self.stub, self.fruit
        cmd, _ = self.stand()
        # the shipped defaults are configuration of the user's shape: every entry typed, all clean
        self.assertTrue(manifest_findings(paths.DEFAULTS_FILE) == [], f"defaults.json: {manifest_findings(paths.DEFAULTS_FILE)}")
        self.assertTrue(all(e.get("type") in BACKEND_TYPES for e in _defaults()["backends"].values()),
                        "defaults.json: an entry names no known type")
        # a keyless systemone entry, as Ollama serves one; a type change replacing a default whole
        with open(Path(home) / "augur.json", "w") as f:
            json.dump({"backends": {
                "local": {"type": "systemone", "url": "http://localhost:1/v1/systemone", "model": "nimble", "timeout": 7},
                "jev": {"type": "command", "command": cmd("ok")},
                "nourl": {"type": "systemone", "model": "m"}, "odd": {"type": "grpc"}}}, f)
        found = manifest_findings()
        self.assertTrue(found == ["augur.json: backends.nourl: missing required key 'url'",
                         "augur.json: backends.odd: unknown type 'grpc'; known: command, laya, systemone"],
               f"typed manifest: {found}")
        stub.answer = lambda state, qid, q: 0.95 if "banana" in json.dumps(state) else 0.05
        n = len(stub.requests)
        ok, reason = available("local")
        self.assertTrue(ok and "nimble answers" in reason, f"systemone check asks a real question: {reason}")
        with open(Path(home) / "usage.csv") as f:
            row = list(csv.reader(f))[-1]
        self.assertTrue(row[1:4] == ["local", "check", "nimble"], f"a check's question is not in the ledger: {row}")
        r = ask("banana", fruit, backend="local", caller="keyless")
        url, headers, wait = stub.requests[-1]
        self.assertTrue(r["backend"] == "local" and r["model"] == "nimble" and stub.calls[-1]["model"] == "nimble",
                        f"keyless systemone ask: {r}")
        self.assertTrue(url.endswith(":1/v1/systemone") and "Authorization" not in headers and wait == 7,
                        f"keyless systemone: url {url}, headers {sorted(headers)}, timeout {wait}")
        self.assertTrue(all(u.startswith("http://localhost:1/") for u, _, _ in stub.requests[n:]), "keyless: a call left the entry's url")
        r = ask("banana", fruit, backend="jev")
        self.assertTrue(r["answers"]["fruit"]["noul"] == 0.97 and "url" not in _backend("jev")[1],
                        "a type change did not replace the default whole")
        self.assertTrue(unavailable(lambda: ask("x", fruit, backend="nourl"), "systemone backend with no `url`"), "systemone without url")
        self.assertTrue(unavailable(lambda: ask("x", fruit, backend="odd"), "unknown type 'grpc'"), "unknown type accepted")
        with open(Path(home) / "augur.json", "w") as f:
            json.dump({"backends": {"hosted": {"type": "systemone", "url": "http://h/v1/systemone", "model": "m",
                                               "env_key": "AUGUR_SELFTEST_ABSENT_KEY"}}}, f)
        self.assertTrue(unavailable(lambda: ask("x", fruit, backend="hosted"), "AUGUR_SELFTEST_ABSENT_KEY unset"),
                        "a named key that is missing did not refuse")
        with open(Path(home) / "augur.json", "w") as f:
            json.dump({"backends": {"jev": {"models_url": "https://api.typesafe.ai/v1/models"}}}, f)
        self.assertTrue([f.split(",")[0] for f in manifest_findings()] == ["augur.json: backends.jev: unknown key 'models_url'"],
                        f"a 0.4 models_url drew no finding: {manifest_findings()}")
        with open(Path(home) / "augur.json", "w") as f:
            json.dump({"backends": {"hosted": {"type": "systemone", "url": "https://h/v1/systemone", "model": "m",
                                               "env_key": "AUGUR_SELFTEST_ABSENT_KEY", "key_host": "h:443"}}}, f)
        os.environ["AUGUR_SELFTEST_ABSENT_KEY"] = "k2"
        ask("banana", fruit, backend="hosted")
        self.assertTrue(stub.requests[-1][1].get("Authorization") == "Bearer k2", "a second hosted key was not sent")
        n = len(stub.requests)
        for url, key_host, why in (("https://elsewhere/v1/systemone", "h:443", "confirmed for h:443"),
                                   ("https://h/v1/systemone", None, "confirmed for no host"),
                                   ("http://h:443/v1/systemone", "h:443", "not https")):
            entry = {"type": "systemone", "url": url, "model": "m", "env_key": "AUGUR_SELFTEST_ABSENT_KEY"}
            with open(Path(home) / "augur.json", "w") as f:
                json.dump({"backends": {"hosted": {**entry, **({"key_host": key_host} if key_host else {})}}}, f)
            self.assertTrue(unavailable(lambda: ask("x", fruit, backend="hosted"), why), f"key sent: {url} {key_host}")
        self.assertEqual(len(stub.requests), n, "a refused key still made a request")
        os.environ.pop("AUGUR_SELFTEST_ABSENT_KEY")

    def test_unavailable(self):
        home, fruit = self.home, self.fruit
        ok, reason = available("laya")
        self.assertTrue(not ok and "does not name an interpreter" in reason, f"laya without a venv: {reason}")
        self.assertTrue(unavailable(lambda: ask("x", fruit, backend="nope"), "unknown backend"), "unknown backend accepted")
        with open(Path(home) / "augur.json", "w") as f:
            f.write("{not json")
        self.assertTrue(unavailable(lambda: ask("x", fruit), "not JSON"), "broken manifest accepted")
        found = manifest_findings()
        self.assertTrue(len(found) == 1 and found[0].startswith("augur.json does not parse: "), f"parse finding: {found}")


if __name__ == "__main__":
    unittest.main()
