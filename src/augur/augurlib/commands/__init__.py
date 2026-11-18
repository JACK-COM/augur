"""Augur's verbs. Each is one `base.Verb` in a module here: its name, its help, the arguments
it takes and what it runs. `cli.main` builds the command line from `VERBS` in this order, so
a new verb is one declaration in one module plus its module in the line below.
"""
from . import ask, calibrate, check, configure, score, setup

VERBS = (*check.VERBS, *configure.VERBS, *ask.VERBS, *score.VERBS, *calibrate.VERBS, *setup.VERBS)
