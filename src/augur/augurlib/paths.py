"""Where Augur's files are: this install, the shipped defaults, and the Augur home that
holds the manifest, the usage ledger and the live-check items.
"""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent    # the install: augur.py, defaults.json, augur_laya.py
# A package manager owns the command and moves this file on every upgrade: Homebrew's
# wrapper sets AUGUR_PACKAGED, a `uv tool` or pip install enters through `cli()`, which sets
# this. So the manifest and the usage ledger live in the Augur home, never beside this file.
PACKAGED = os.environ.get("AUGUR_PACKAGED") == "1"

# The shipped defaults are `defaults.json` beside this file, in the same shape as a user's
# augur.json and merged by the same rule, so a default backend is configuration, never code.
# There, jev's model is pinned (see the docstring) and its price is USD per million input
# tokens from docs.typesafe.ai/models on 2026-09-17; laya's empty `python` means unavailable
# until a user names a venv, and a null `device` lets laya pick cuda, mps, cpu in that order.
DEFAULTS_FILE = HERE / "defaults.json"

BIN = Path(os.environ.get("AUGUR_BIN", str(Path.home() / ".local" / "bin")))
LAYA_VENV = Path.home() / ".augur-laya"       # the venv INSTALL-augur.md tells a user to make


def home() -> Path:
    """Read per call, so a caller (or a test) can point AUGUR_HOME elsewhere."""
    return Path(os.environ.get("AUGUR_HOME") or Path.home() / ".augur").expanduser()


def manifest() -> Path:
    return home() / "augur.json"


def check_file() -> Path:
    return home() / "check.json"
