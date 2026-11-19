"""The install's own verbs: the offline smoke check, the manifest schema, the install
guide, and putting Augur on PATH or taking it off."""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .. import client, fsio, install, manifest, paths, request
from .base import Verb


def smoke(a: argparse.Namespace) -> int:
    """An offline smoke check of an install: no network, no key, no model. The shipped
    defaults are clean, a scorer-contract request parses, and one ask runs end to end
    through a stand-in command backend under a scratch Augur home. The behaviour suites
    are tests/, run by `make test`."""
    import _scorer_contract as contract
    failures = [f"defaults.json: {f}" for f in manifest.manifest_findings(paths.DEFAULTS_FILE)]
    saved = os.environ.get("AUGUR_HOME")
    with tempfile.TemporaryDirectory() as home:
        os.environ["AUGUR_HOME"] = home
        try:
            text, questions, kw = request.parse_request(contract.REQUEST)
            reply = f"import sys; sys.stdin.read(); print({json.dumps(json.dumps(contract.REPLY))})"
            fsio.write_json(Path(home) / "augur.json", {"backends": {"smoke": {
                "type": "command", "command": [sys.executable, "-c", reply]}}})
            r = client.ask(text, questions, subject=kw["subject"], backend="smoke", caller="selftest")
            if contract.probability(r) != contract.probability(contract.REPLY):
                failures.append(f"ask: {r}")
        except Exception as e:  # noqa: a smoke check reports a failure as a line, never a traceback
            failures.append(f"{type(e).__name__}: {e}")
        finally:
            if saved is None:
                os.environ.pop("AUGUR_HOME", None)
            else:
                os.environ["AUGUR_HOME"] = saved
    for f in failures:
        print(f"FAIL {f}")
    print("selftest FAILED" if failures else "selftest ok")
    return 1 if failures else 0


def schema(a: argparse.Namespace) -> int:
    path = manifest.write_schema()
    print(f"schema: {path}")
    print(f'editor: add "$schema": "{path}" to augur.json, or map augur.json to it in the editor\'s JSON schema settings')
    return 0


def guide(a: argparse.Namespace) -> int:
    print((paths.HERE / "INSTALL-augur.md").read_text(encoding="utf-8"), end="")
    return 0


def guide_args(h: argparse.ArgumentParser) -> None:
    h.add_argument("topic", choices=["install"])


def uninstall_args(u: argparse.ArgumentParser) -> None:
    u.add_argument("--yes", action="store_true", help="remove without asking")
    u.add_argument("--dry-run", action="store_true", help="print what would be removed and stop")


VERBS = (
    Verb("selftest", "an offline smoke check of the install: no network, no key, no model", smoke),
    Verb("schema", "write the manifest schema where an editor can be pointed at it", schema),
    Verb("help", "print a guide", guide, guide_args),
    Verb("install", "put `augur` on PATH from a source checkout (a symlink in ~/.local/bin)",
         lambda a: install.install()),
    Verb("uninstall", "remove what augur made; print what is left",
         lambda a: install.uninstall(a.yes, a.dry_run), uninstall_args),
)
