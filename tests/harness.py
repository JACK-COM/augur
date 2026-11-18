"""What every suite shares: the source under test, the stand-in backend, and the scratch
Augur home each case runs in.

AUGUR_SRC names the source the suites import (default: this checkout's src/augur). Point
it at an older tree to see a case fail there:
    AUGUR_SRC=<old src/augur> python3 -m unittest discover -s tests -t .
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SRC = Path(os.environ.get("AUGUR_SRC", REPO / "src/augur"))

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from augur import AugurUnavailable, noul  # noqa: E402


class _Stub:
    """A stand-in for the jev endpoint, installed over urllib.request.urlopen. `answer`
    maps (state, qid, question) to a noul; `script` is a queue of HTTP errors to raise
    before answering."""

    def __init__(self):
        self.io = io
        self.calls = []
        self.requests = []                  # (url, headers, timeout) per call, keys included
        self.script = []
        self.routes = {}                    # URL suffix -> body, for Ollama's own API
        self.answer = lambda state, qid, q: 0.95 if "banana" in json.dumps(state) and "fruit" in q["instructions"] else 0.05

    def __call__(self, req, timeout=None):
        self.requests.append((req.full_url, dict(req.header_items()), timeout))
        for suffix, reply in self.routes.items():
            if req.full_url.endswith(suffix):
                return self.io.BytesIO(reply if isinstance(reply, bytes) else json.dumps(reply).encode())
        body = json.loads(req.data)
        self.calls.append(body)
        if self.script:
            code = self.script.pop(0)
            from email.message import Message
            h = Message()
            h["retry-after"] = "0"
            raise urllib.error.HTTPError(req.full_url, code, "stub", h, self.io.BytesIO(b"stub error"))
        answers = {qid: {"type": "noul", "noul": self.answer(body["state"], qid, q)} for qid, q in body["questions"].items()}
        return self.io.BytesIO(json.dumps({"model": body["model"], "answers": answers,
                                           "usage": {"input_tokens": 1000}}).encode())


def unavailable(fn, fragment):
    """True when `fn` refuses with an AugurUnavailable naming `fragment`."""
    try:
        fn()
    except AugurUnavailable as e:
        return fragment in str(e)
    return False


class AugurCase(unittest.TestCase):
    """A case under a scratch AUGUR_HOME (`self.home`) holding the one jev entry the
    selftest always wrote, with the key in the environment and `self.stub` answering in
    place of the network. The environment and urlopen are restored on the way out, failed
    or not. time.sleep is left alone: a case exercising retry patches it itself."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="augur-test-")
        self.addCleanup(shutil.rmtree, self.home, True)
        env = mock.patch.dict(os.environ, {"AUGUR_HOME": self.home, "TYPESAFE_API_KEY": "selftest"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AUGUR_BACKEND", None)       # restored by the patch on the way out
        # a keychain service nobody has, so the key comes from the environment
        with open(Path(self.home) / "augur.json", "w") as f:
            json.dump({"backends": {"jev": {"keychain_service": "augur-selftest-absent", "price_per_mtok": 1.0}}}, f)
        self.stub = _Stub()
        opener = mock.patch("urllib.request.urlopen", self.stub)
        opener.start()
        self.addCleanup(opener.stop)
        # A person at a terminal says yes, so a case can save a backend that runs a command or
        # sends a key; the gate itself is test_gates.py's, against the real check.
        try:
            import _guard
        except ImportError:                       # a release before the gate
            self.person = None
        else:
            self.person = mock.patch.object(_guard, "person_confirms", lambda *a, **k: True)
            self.person.start()
            self.addCleanup(self.person.stop)
        self.fruit = {"fruit": noul("Is the word in `text` the name of a fruit?")}

    def stand(self):
        """A stand-in command backend under the home -> (cmd, seen): `cmd(mode)` is its
        argv, and `seen` the file it writes each request it reads to."""
        home = self.home
        stand = Path(home) / "stand.py"
        stand.write_text(
            "import json, sys\n"
            "mode = sys.argv[1]\n"
            "req = json.load(sys.stdin)\n"
            "open(sys.argv[2], 'w').write(json.dumps(req))\n"
            "if mode == 'fail':\n    sys.stderr.write('boom\\n'); sys.exit(3)\n"
            "if mode == 'prose':\n    print('I think it is a fruit'); sys.exit(0)\n"
            "if mode == 'slow':\n    import time; time.sleep(5)\n"
            "if mode == 'bytes':\n    sys.stdout.buffer.write(b'\\xff\\xfe'); sys.exit(0)\n"
            "p = float('nan') if mode == 'nan' else 0.97 if 'banana' in json.dumps(req['state']) else 0.03\n"
            "print(json.dumps({'answers': {q: {'type': 'noul', 'noul': p} for q in req['questions']},"
            " 'usage': {'input_tokens': 10}}))\n")
        seen = Path(home) / "seen.json"

        def cmd(mode):
            return [sys.executable, str(stand), mode, str(seen)]
        return cmd, seen
