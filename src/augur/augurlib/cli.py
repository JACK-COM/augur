"""The command line: the parser, each verb's dispatch, and the install smoke check.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

from . import calibrate
from . import check
from . import client
from . import entries
from . import errors
from . import fsio
from . import guided
from . import help_text
from . import install
from . import keys
from . import manifest
from . import paths
from . import request


def _configure_backend(args):
    given = {k: getattr(args, k) for k in entries.ENTRY_FLAGS if getattr(args, k) is not None}
    acted = bool(given or args.default or args.force or args.store_key)
    try:
        if args.list or args.remove is not None:
            if args.name or acted or (args.list and args.remove is not None):
                print("--list and --remove NAME stand alone", file=sys.stderr)
                return 2
            return entries._list_backends() if args.list else entries._remove_backend(args.remove)
        terminal = sys.stdin.isatty() and sys.stdout.isatty()
        if not acted:
            if terminal:
                return guided._guided_backend(args.name)
            return entries._show_backend(args.name) if args.name else entries._list_backends()
        if not args.name:
            print("name the backend: augur configure backend NAME --type ...", file=sys.stderr)
            return 2
        if given:
            return entries._save_backend(args.name, given, make_default=args.default, force=args.force,
                                 store_key=args.store_key)
        if args.name not in manifest._manifest()["backends"]:
            print(f"no backend named {args.name!r}; `augur configure backend --list` shows them", file=sys.stderr)
            return 2
        name, cfg, t = manifest._backend(args.name)          # an existing backend: a key, the default, or both
        if args.force:
            print("--force saves settings; give some, or drop it", file=sys.stderr)
            return 2
        if args.store_key:
            code = keys._store_key(name, cfg)
            if code:
                return code
            ok, reason = client._probe(name, cfg, t, caller="configure", timeout=entries.PROBE_TIMEOUT)
            print(("ok: " if ok else "unavailable: ") + reason)
            if not ok:
                return 3
        if args.default:
            with fsio._manifest_lock():
                user = manifest._user_manifest()
                entries._set_default(user, name)
                path = fsio._write_json(paths.manifest(), user)
            print(f"{name} is the default ({path}); "
                  f"`augur check` asks it a question")
        return 0
    except ValueError as e:
        print(f"augur.json: {e}", file=sys.stderr)
        return 2
    except errors.AugurUnavailable as e:
        print(f"unavailable: {e}", file=sys.stderr)
        return 3
    except OSError as e:
        print(f"{e.filename or 'augur.json'}: {e.strerror or e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted; augur.json unchanged", file=sys.stderr)
        return 130


def _smoke():
    """An offline smoke check of an install: no network, no key, no model. The shipped
    defaults are clean, a scorer-contract request parses, and one ask runs end to end
    through a stand-in command backend under a scratch Augur home. The behaviour suites
    are tests/, run by `make test`."""
    try:
        from . import _scorer_contract as contract
    except ImportError:
        import _scorer_contract as contract
    failures = [f"defaults.json: {f}" for f in manifest.manifest_findings(paths.DEFAULTS_FILE)]
    saved = os.environ.get("AUGUR_HOME")
    with tempfile.TemporaryDirectory() as home:
        os.environ["AUGUR_HOME"] = home
        try:
            text, questions, kw = request._request(contract.REQUEST)
            reply = f"import sys; sys.stdin.read(); print({json.dumps(json.dumps(contract.REPLY))})"
            fsio._write_json(Path(home) / "augur.json", {"backends": {"smoke": {
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


def main(argv, version=""):
    import argparse
    fmt = dict(formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--backend", metavar="NAME", help="default from augur.json or AUGUR_BACKEND")
    p = argparse.ArgumentParser(prog="augur", description=help_text.USAGE.split("\n\n")[0], epilog=help_text.EXAMPLES[None], **fmt)
    p.add_argument("-v", "-V", "--version", action="version", version=f"augur {version}")
    sub = p.add_subparsers(dest="cmd", title="commands", metavar="<command>")

    def cmd(name, help, **kw):
        return sub.add_parser(name, help=help, description=help, epilog=help_text.EXAMPLES[name], **fmt, **kw)
    k = cmd("check", "positive check for the backend", parents=[common])
    k.add_argument("--live", action="store_true",
                   help="ask known questions and check the answers: yours from `augur configure check`, else two built-in")
    f = cmd("configure", "set up a backend, or what `check --live` asks")
    fs = f.add_subparsers(dest="what", title="what", metavar="<what>", required=True)

    def part(name, help):
        return fs.add_parser(name, help=help, description=help, epilog=help_text.EXAMPLES[f"configure {name}"],
                             allow_abbrev=False, **fmt)
    fb = part("backend", "add, change, list or remove a backend")
    fb.add_argument("name", nargs="?", help="the backend's name; guided in a terminal when no flag follows")
    fb.add_argument("--type", choices=sorted(manifest.BACKEND_TYPES), help="the protocol it speaks")
    fb.add_argument("--url", help="systemone: the endpoint")
    fb.add_argument("--model", help="systemone or command: the model to ask; pin a version")
    fb.add_argument("--key-service", dest="keychain_service", metavar="NAME",
                    help="systemone: the macOS keychain service holding the key")
    fb.add_argument("--key-env", dest="env_key", metavar="VAR", help="systemone: the environment variable holding the key")
    fb.add_argument("--command", help="command: the script, as one string split as a shell would")
    fb.add_argument("--python", help="laya: the interpreter of a venv holding laya and torch")
    fb.add_argument("--checkpoint", help="laya: the checkpoint to load")
    fb.add_argument("--device", help="laya: cuda, mps or cpu")
    fb.add_argument("--timeout", type=entries._positive, metavar="S", help="seconds to wait for an answer")
    fb.add_argument("--price", type=entries._price, dest="price_per_mtok", metavar="USD",
                    help="USD per million input tokens, for the usage ledger")
    fb.add_argument("--default", action="store_true", help="make it the default backend")
    fb.add_argument("--store-key", action="store_true", help="prompt for its key through macOS `security`")
    fb.add_argument("--force", action="store_true", help="save it even when it does not answer")
    fb.add_argument("--list", action="store_true", help="every backend, its type and origin, and the default")
    fb.add_argument("--remove", metavar="NAME", help="remove your entry; a shipped name gets its shipped settings back")
    fc = part("check", "set what `check --live` asks")
    fc.add_argument("file", nargs="?", help="a calibrate items file (at most %d items) to copy as the live check" % check.CHECK_MAX)
    fc.add_argument("--threshold", type=float, metavar="P", help="a label is right when its answer is at or above P for "
                    "true and below it for false (default 0.5)")
    fc.add_argument("--reset", action="store_true", help="remove the live check; the built-in questions return")
    a = cmd("ask", "one call; prints the response JSON", parents=[common])
    a.add_argument("-q", "--questions", metavar="FILE", help="JSON file: {name: question}")
    g = a.add_mutually_exclusive_group()
    g.add_argument("-t", "--text", help="state as a string (stored under `text`)")
    g.add_argument("-s", "--state", metavar="FILE", help="JSON file with the state object")
    g.add_argument("-r", "--request", metavar="FILE",
                   help="the whole call as one JSON object; `-` reads it from stdin")
    a.add_argument("--subject", help="what the state is about, named to the model")
    a.add_argument("--siblings", metavar="A,B", help="comma-separated neighbours of the subject")
    a.add_argument("--model", help="override the backend's pinned model or checkpoint")
    a.add_argument("--caller", help="the name the usage ledger books the cost under (default cli)")
    c = cmd("calibrate", "measure a backend on a labelled items file", parents=[common])
    c.add_argument("items", help="JSON file of questions and labelled items")
    c.add_argument("--runs", type=int, default=1, metavar="N", help="repeats per item (default 1)")
    c.add_argument("--limit", type=int, metavar="N", help="first N items only")
    c.add_argument("--questions", metavar="a,b", help="comma-separated subset of the file's questions")
    c.add_argument("--workers", type=int, default=6, metavar="N", help="parallel calls (default 6)")
    c.add_argument("--out", metavar="DIR", help="directory for per-item means")
    c.add_argument("--compare", metavar="FILE", help="a prior run's per-item means JSON")
    cmd("selftest", "an offline smoke check of the install: no network, no key, no model")
    cmd("schema", "write the manifest schema where an editor can be pointed at it")
    h = cmd("help", "print a guide")
    h.add_argument("topic", choices=["install"])
    cmd("install", "put `augur` on PATH from a source checkout (a symlink in ~/.local/bin)")
    u = cmd("uninstall", "remove what augur made; print what is left")
    u.add_argument("--yes", action="store_true", help="remove without asking")
    u.add_argument("--dry-run", action="store_true", help="print what would be removed and stop")
    args = p.parse_args(argv)
    if args.cmd == "selftest":
        return _smoke()
    if args.cmd == "schema":
        path = manifest.write_schema()
        print(f"schema: {path}")
        print(f'editor: add "$schema": "{path}" to augur.json, or map augur.json to it in the editor\'s JSON schema settings')
        return 0
    if args.cmd == "help":
        print((paths.HERE / "INSTALL-augur.md").read_text(encoding="utf-8"), end="")
        return 0
    if args.cmd == "install":
        return install._install()
    if args.cmd == "uninstall":
        return install._uninstall(args.yes, args.dry_run)
    if args.cmd == "configure":
        if args.what == "backend":
            return _configure_backend(args)
        return check._configure_check(args.file, args.threshold, args.reset)
    if args.cmd == "check":
        for finding in manifest.manifest_findings():
            print(finding)
        if args.live:
            return check._live_check(args.backend)
        ok, reason = client.available(args.backend)
        print(("ok: " if ok else "unavailable: ") + reason)
        return 0 if ok else 3
    if args.cmd == "ask":
        # a flag given beside --request wins over the request's own field
        flags = {"subject": args.subject, "model": args.model, "caller": args.caller,
                 "siblings": [x.strip() for x in args.siblings.split(",") if x.strip()] if args.siblings else None}
        try:
            if args.request is not None:
                if args.questions is not None:
                    raise ValueError("--request carries the questions; drop -q")
                if args.request == "-":
                    req = json.load(sys.stdin)
                else:
                    with open(args.request) as f:
                        req = json.load(f)
                state, questions, kw = request._request(req)
                kw.update({k: v for k, v in flags.items() if v is not None})
            else:
                if args.questions is None or (args.text is None and args.state is None):
                    raise ValueError("give -q with -t or -s, or the whole call with --request")
                with open(args.questions) as f:
                    questions = json.load(f)
                if args.text is not None:
                    state = args.text
                else:
                    with open(args.state) as f:
                        state = json.load(f)
                kw = flags
        except (OSError, ValueError) as e:
            print(f"bad input: {e}", file=sys.stderr)
            return 2
        try:
            r = client.ask(state, questions, subject=kw["subject"], siblings=kw["siblings"],
                    backend=args.backend, model=kw["model"], caller=kw["caller"] or "cli")
        except errors.AugurUnavailable as e:
            print(f"unavailable: {e}", file=sys.stderr)
            return 3
        print(json.dumps(r, indent=1))
        return 0
    if args.cmd == "calibrate":
        try:
            with open(args.items) as f:
                spec = json.load(f)
        except (OSError, ValueError) as e:
            print(f"bad input: {e}", file=sys.stderr)
            return 2
        ok, reason = client.available(args.backend)
        if not ok:
            print(f"unavailable: {reason}", file=sys.stderr)
            return 3
        try:
            calibrate.calibrate(spec, backend=args.backend, runs=args.runs, limit=args.limit,
                      questions=[x.strip() for x in args.questions.split(",")] if args.questions else None,
                      workers=args.workers, out_dir=args.out, compare=args.compare)
        except errors.AugurUnavailable as e:
            print(f"unavailable: {e}", file=sys.stderr)
            return 3
        return 0
    p.print_help()
    return 2
