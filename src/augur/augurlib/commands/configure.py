"""`augur configure backend|check`: add, change, list or remove a backend, guided in a
terminal or by flags; or set what `check --live` asks."""
import argparse
import sys

from .. import backends, check, client, entries, errors, fsio, guided, help_text, keys, manifest, paths
from ..backends import common as btype
from .base import Verb, unavailable


def args(f: argparse.ArgumentParser) -> None:
    fs = f.add_subparsers(dest="what", title="what", metavar="<what>", required=True)

    def part(name: str, help: str) -> argparse.ArgumentParser:
        return fs.add_parser(name, help=help, description=help, epilog=help_text.EXAMPLES[f"configure {name}"],
                             allow_abbrev=False, formatter_class=argparse.RawDescriptionHelpFormatter)
    fb = part("backend", "add, change, list or remove a backend")
    fb.add_argument("name", nargs="?", help="the backend's name; guided in a terminal when no flag follows")
    fb.add_argument("--type", choices=sorted(backends.TYPES), help="the protocol it speaks")
    for key in backends.entry_flags()[1:]:
        spec = btype.FLAGS[key]
        fb.add_argument(spec.get("flag", "--" + key.replace("_", "-")), dest=key, type=spec.get("type"),
                        metavar=spec.get("metavar"), help=backends.flag_help(key))
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


def run(a: argparse.Namespace) -> int:
    if a.what == "backend":
        return backend(a)
    return check._configure_check(a.file, a.threshold, a.reset)


def backend(args: argparse.Namespace) -> int:
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
        return unavailable(e)
    except OSError as e:
        print(f"{e.filename or 'augur.json'}: {e.strerror or e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted; augur.json unchanged", file=sys.stderr)
        return 130


VERBS = (Verb("configure", "set up a backend, or what `check --live` asks", run, args),)
