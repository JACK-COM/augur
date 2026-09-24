"""Augur as a library under a pip or uv install: `from augur import ask, noul`."""
from .augur import (  # noqa: F401
    AugurUnavailable, BatchTooLarge, __version__, ask, available, batch, calibrate, choice, cli, main, noul, score,
)
