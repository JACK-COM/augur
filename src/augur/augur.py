#!/usr/bin/env python3
"""augur: typed questions to a decision model, calibrated probabilities back, no text.

The entry point and the library facade (`from augur import ask, noul`). What Augur is and
how it is used is `augurlib/help_text.py`'s USAGE; each job has its own module in augurlib/.
"""
import sys
from pathlib import Path

__version__ = "0.8.0"                          # the one home: pyproject.toml and the formula's test read it

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:                  # augurlib and the panoply-lib copies, installed flat or not;
    sys.path.append(str(HERE))                 # appended, so a caller's own modules keep their names

from augurlib import paths  # noqa: E402
from augurlib.cli import main as cli_main  # noqa: E402
# the library API, as `from augur import ask, noul`
from augurlib.calibrate import calibrate  # noqa: E402,F401
from augurlib.client import ask, available, batch  # noqa: E402,F401
from augurlib.errors import AugurUnavailable, BatchTooLarge, NoKey  # noqa: E402,F401
from augurlib.manifest import manifest_findings, write_schema  # noqa: E402,F401
from augurlib.qtypes import choice, noul, score  # noqa: E402,F401


def main(argv, version=__version__):
    return cli_main(argv, version)


def cli():
    """The entry point a `uv tool` or pip install puts on PATH."""
    paths.PACKAGED = True
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        sys.exit(0)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        sys.exit(0)
