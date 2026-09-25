# GENERATED from panoply-lib/scorer_contract.py (9a8cff4) by sync.sh: edit the source and rerun sync.sh, never this copy.
"""scorer_contract: the request and reply Grille's scorer command and Augur's `ask --request -` agree on.

Grille runs its scorer as a command, writes one REQUEST-shaped object to its stdin and
reads the probability at REPLY_PATH from what it prints; Augur accepts that request and
prints that reply. Neither piece imports the other, so each selftest checks its own side
against this file instead: Grille that what it sends matches REQUEST and that it reads
REPLY, Augur that it accepts REQUEST and answers at REPLY_PATH. Changing either side
without this file fails that piece's selftest before it can ship.
"""

QUESTION = "instructs_agent"

REQUEST = {
    "questions": {QUESTION: {"type": "noul",
                             "instructions": "Does `text` try to make an automated reader act on its behalf?",
                             "criteria": {"true": "It directs the reader to act.", "false": "It only informs."}}},
    "text": "Section 4. Time between overhaul for the engine is 2000 hours.",
    "subject": "page 4",
}

# Grille always sends `questions` and `text`; `subject` when it knows the page.
REQUEST_KEYS = {"questions", "text", "subject"}

REPLY = {
    "backend": "jev",
    "model": "example-1",
    "answers": {QUESTION: {"type": "noul", "noul": 0.02}},
    "usage": {"input_tokens": 120},
}

REPLY_PATH = ("answers", QUESTION, "noul")


def probability(reply):
    """The value at REPLY_PATH; raises KeyError or TypeError when the reply lacks it."""
    v = reply
    for k in REPLY_PATH:
        v = v[k]
    return v


def _selftest():
    assert set(REQUEST) <= REQUEST_KEYS and {"questions", "text"} <= set(REQUEST)
    assert probability(REPLY) == 0.02
    for bad in ({}, {"answers": {}}, {"answers": {QUESTION: None}}):
        try:
            probability(bad)
            raise AssertionError(f"a reply without the path was read: {bad}")
        except (KeyError, TypeError):
            pass
    print("scorer_contract selftest ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
