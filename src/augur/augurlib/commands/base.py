"""What a verb declares, and how every verb reports the two failures they share."""
import argparse
import sys
from typing import Callable, NamedTuple, Optional


class Verb(NamedTuple):
    name: str
    help: str
    run: Callable[[argparse.Namespace], int]
    args: Optional[Callable[[argparse.ArgumentParser], None]] = None
    backend: bool = False                 # takes --backend
    # A verb with a second level (configure backend, configure check) builds its own
    # subparsers in `args` and dispatches on them in `run`.


def unavailable(e: object) -> int:
    """No answer from the backend: one line, exit 3, the caller's cue to fall back."""
    print(f"unavailable: {e}", file=sys.stderr)
    return 3


def bad_input(e: object) -> int:
    """A file or request the verb cannot use: one line, exit 2."""
    print(f"bad input: {e}", file=sys.stderr)
    return 2
