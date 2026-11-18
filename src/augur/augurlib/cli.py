"""The command line, built from the verbs in commands/: each verb's parser, then its run."""
import argparse
from typing import List

from . import help_text
from .commands import VERBS


def main(argv: List[str], version: str = "") -> int:
    fmt = dict(formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--backend", metavar="NAME", help="default from augur.json or AUGUR_BACKEND")
    p = argparse.ArgumentParser(prog="augur", description=help_text.USAGE.split("\n\n")[0],
                                epilog=help_text.EXAMPLES[None], **fmt)  # type: ignore[arg-type]
    p.add_argument("-v", "-V", "--version", action="version", version=f"augur {version}")
    sub = p.add_subparsers(dest="cmd", title="commands", metavar="<command>")
    for v in VERBS:
        sp = sub.add_parser(v.name, help=v.help, description=v.help, epilog=help_text.EXAMPLES[v.name],
                            parents=[common] if v.backend else [], **fmt)  # type: ignore[arg-type]
        if v.args:
            v.args(sp)
        sp.set_defaults(run=v.run)
    args = p.parse_args(argv)
    if not getattr(args, "run", None):
        p.print_help()
        return 2
    return args.run(args)
