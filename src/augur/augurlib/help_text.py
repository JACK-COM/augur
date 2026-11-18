"""Help text: the question-file format and each verb's examples.
"""

USAGE = """augur: typed questions to a decision model, calibrated probabilities back, no text.

A state (string, object or array) and a map of typed questions go to a backend that
answers every question against the same state and returns a probability (noul), a
choice with per-option probabilities (choice) or a position on ordered levels (score).
No generated text, no reasoning, no tools. Every backend reads the question literally,
so a rule it is asked to check has to be phrased as a literal yes-or-no with the
boundary cases written into the criteria; a rule that cannot be phrased that way is
one this instrument cannot see.

This file is a CLIENT and holds no questions. Each project keeps its question file
beside the rule it enforces, so the rule and its test move together. Three constraints
are built in and are not to be relaxed:

  * It informs, never blocks. A probability answers a proxy for the question a rule
    asks, and a proxy may nudge but must never refuse. Callers threshold and act; this
    file returns numbers.
  * Nothing that must succeed may depend on it. Network, key, rate limits and a local
    model's weights are all outside this file. `available()` is the positive check; on
    failure the CLI exits 3 with a one-line reason and the library raises `AugurUnavailable`.
  * A backend earns a threshold by a measured run on the project's own labelled items,
    never by a published number. `calibrate` is that run, and a threshold read from one
    backend does not transfer to another.

A backend is a named entry under `backends` with a `type`, the protocol it speaks. The
shipped `defaults.json` holds two entries in the shape a user writes, and the user's
`augur.json` in the Augur home (`~/.augur`, or `$AUGUR_HOME`) merges over it by the same
rule; the default backend comes from there, then `AUGUR_BACKEND`, then `backend=`. Types:

  systemone  TypeSafe's System One API, which Ollama also serves at /v1/systemone: a
             `url`, a `model`, and an API key only when the entry names where to find
             one (`keychain_service`, then `env_key`). The shipped `jev` entry is this type
             with the model pinned (an alias moves without notice and a tuned threshold
             moves with it) and its key in the macOS keychain service `TYPESAFE_API_KEY`
             (`augur configure backend jev --store-key`, where `security` prompts for it), the
             same variable in the environment as fallback. Never in a repo, never printed.
  laya       Convai's open-weight ModernBERT decision model, run locally through
             `augur_laya.py` inside a virtualenv holding `laya` and `torch`; the entry
             names that interpreter. Free and fast, and by its makers' own card a base to
             fine-tune rather than a zero-shot engine: calibrate before trusting a threshold.
  command    Any script: it gets one `ask --request`-shaped object on stdin ({questions,
             state, model?}) and prints {answers, model?, usage?}, so any model a script
             can wrap answers through Augur, and `augur ask --request -` is itself valid.
             An entry naming only a `command` is taken as this type.

An entry merges over the default of the same name unless it names a different type, which
replaces that default whole.

Three design facts measured with Jev on labelled product-copy sentences, which is why the
API has the shape it has:

  * The state must name the subject the question is about, or "this X" is undefined
    and a sentence pointing at a different X scores as one about the subject. `ask`
    takes `subject` and `siblings` and puts them in state as named fields.
  * Batching many items into one state moves individual scores by about 0.04 at five
    to twenty items and breaks at forty (a floor sentence scored 0.77). `batch` caps a
    call at MAX_BATCH items and re-asks anything within REASK_BAND of a threshold in
    isolation when the caller passes one.
  * Run-to-run standard deviation is 0.008 with a fresh `uid` in state, so a threshold
    read from one run is defensible; the uid is added by default.

Usage is appended per call to `usage.csv` in the Augur home (timestamp, backend, caller,
model, questions, input tokens, USD at the backend's price; a local backend logs zero).

    augur check [--backend B] [--live]           positive check; --live asks known questions
    augur configure backend [NAME] [--type T --url U --model M ...] [--list] [--remove NAME]
                                                 add or change a backend: guided in a terminal, by flags anywhere
    augur configure check [FILE] [--reset]       the questions --live asks: yours, or two built-in
    augur ask -q questions.json (-t "text" | -s state.json) [--subject S] [--siblings A,B]
    augur calibrate items.json [--backend B] [--runs N] [--limit N] [--out DIR]
                               [--compare means.json] [--questions a,b]
    augur selftest                               an offline smoke check of the install
    augur help install                           the install steps an agent follows
    augur --help

Library:

    from augur import ask, batch, noul, choice, score, available, AugurUnavailable
    r = ask("Buy the Bravo for the altitude.", {"cond": noul("Does `text` recommend
        the aircraft named in `subject` to the reader on a condition?", true="...",
        false="...")}, subject="Mooney M20M Bravo", caller="copy_lint")
    r["answers"]["cond"]["noul"]  -> 0.92          r["backend"] -> "jev"

`calibrate` items file: {"name", "description", "questions": {qid: question},
"items": [{"id", "class", "text", "subject", "siblings", "labels": {qid: true|false|null}}]}.
A null label is reported as a distribution only. Every item is asked in isolation with
its subject and siblings named. The report per question: AUROC of labelled positives
against labelled negatives, the lowest positive and highest negative, per-class means,
and with `--runs` above one the per-item standard deviation. `--compare` prints the
mean absolute difference against a prior run's per-item means, which is how a backend
change or a client change is shown not to have moved the instrument.
"""



from . import check


QUESTION_FILE = """A question file is a JSON object of named, typed questions. `noul` is a
yes-or-no with the boundary written into the criteria; `choice` names its
options; `score` lists ordered levels:

  {"buyer_named": {"type": "noul",
                   "instructions": "Does the sentence name who this aircraft is for?",
                   "criteria": {"true": "A buyer, mission or pilot profile is named.",
                                "false": "It describes the aircraft only, or says nothing about a buyer."}}}
"""


EXAMPLES = {
    None: """examples:
  augur check                                  the backend answers and the key is found
  augur check --live                           known questions, known answers, asked for real
  augur configure check items.json             make those questions your own
  augur check --backend laya                   the same for the local backend
  augur ask -q closers.json -t "A four-seat single for an owner who flies IFR."
  augur calibrate items.json --out run-a/
  augur selftest                               an offline smoke check of the install
  augur schema                                 write the manifest schema for an editor
  augur help install                           the install steps, written for an agent to follow

`augur <command> -h` has that command's flags and examples. Library use:
`from augur import ask, noul`; `ask(state, {"q": noul(...)}, subject=..., caller=...)`
returns the same dict the CLI prints, and `batch(items, make_questions, threshold=...)`
runs up to 20 items in one call. The manifest and the usage ledger live in ~/.augur
(or $AUGUR_HOME).
""",
    "check": """examples:
  augur check                                  the default backend (augur.json or AUGUR_BACKEND)
  augur check --backend laya                   the local backend; needs the venv INSTALL-augur.md names
  augur check --backend nimble                 any backend named in augur.json, e.g. a systemone entry for Ollama
  augur check --live                          ask known questions and check the answers: your own from
                                               `augur configure check`, else two built-in Nouls in one call

Exit 0 and `ok:` when the backend answers; exit 3 and `unavailable: <reason>` otherwise.
A misspelled or mistyped manifest key is printed first, prefixed `augur.json:`, and never changes the exit.
""",
    "configure": """examples:
  augur configure backend                      add a backend, guided: Ollama, a hosted API, Laya or a script
  augur configure backend --list               every backend, its type, and which is the default
  augur configure check my-items.json          the questions `augur check --live` asks

`augur configure backend -h` and `augur configure check -h` have the flags.
""",
    "configure backend": """examples:
  augur configure backend                      guided, in a terminal: where the model runs, which model,
                                               then one real question before anything is saved
  augur configure backend nimble --type systemone --url http://localhost:11434/v1/systemone \\
      --model nimble:9b-q8_0 --price 0         the same by flags, for a script or an agent
  augur configure backend acme --type systemone --url https://api.acme.example/v1/systemone \\
      --model acme-2.1 --key-service ACME_API_KEY --key-env ACME_API_KEY --store-key
                                               a hosted API; `security` prompts for the key itself
  augur configure backend jev --store-key      store or replace jev's key, then ask it one question
  augur configure backend nimble --default     make an existing backend the default
  augur configure backend nimble --timeout 120 change one setting; the rest of the entry stays
  augur configure backend --list               every backend: type, shipped or yours, the default
  augur configure backend --remove nimble      remove yours; on a shipped name, its shipped settings return

A backend is saved only after it answers one question, booked in the ledger as caller
`configure`; --force saves one that does not. No flag takes an API key: --store-key hands
the prompt to macOS `security`, and elsewhere prints the variable to export.
""",
    "configure check": """examples:
  augur configure check my-items.json          copy a calibrate items file of up to %d items as the live check
  augur configure check my-items.json --threshold 0.3
                                               a label is right at or above 0.3 for true, below it for false
  augur configure check                        what `check --live` asks now
  augur configure check --reset                back to the two built-in questions

The file is copied to ~/.augur/check.json, so editing or deleting the original changes nothing.
Every item is one billed call on each `augur check --live`.
""" % check.CHECK_MAX,
    "schema": """examples:
  augur schema                                 write ~/.augur/augur.schema.json and say how to use it
""",
    "ask": QUESTION_FILE + """
examples:
  augur ask -q closers.json -t "A four-seat single for an owner who flies IFR."
                                               one state as a string; prints {backend, model, answers, usage}
  augur ask -q closers.json -s state.json --subject "Cessna 182" --siblings "Cessna 172,Piper Archer"
                                               a JSON state; subject and siblings are named in the state
                                               so the model knows which aircraft the sentence is about
  augur ask -q closers.json -t "..." --caller copy_lint
                                               the caller is what the usage ledger records the cost under
  echo '{"questions": {...}, "text": "..."}' | augur ask --request -
                                               the whole call as one JSON object on stdin, for a program
                                               that runs augur as a command; prints the same response

A request object holds `questions` and exactly one of `text` (a string) or `state` (an object),
and may add `subject`, `siblings` (a list), `model` and `caller`. Any other key is refused, and a
flag given beside --request wins over the request's field. Exit 2 is bad input, exit 3 an
unavailable backend; the answers are at `answers.<name>`, a `noul` as a probability from 0 to 1.
""",
    "calibrate": """examples:
  augur calibrate items.json --out run-a/
                                               every item's mean probability per question, AUROC against
                                               the labels, and the floor sentences' scores; the threshold
                                               a rule uses comes from this run, never from a published figure
  augur calibrate items.json --backend laya --out run-b/ --compare run-a/means-jev.json
                                               the same items on another backend, with the per-item
                                               difference against the first run
  augur calibrate items.json --limit 40 --questions cond,strict --workers 8
                                               a quick pass on a subset

An items file is {"questions": {name: question}, "items": [{"id": ..., "text": ..., "labels": {name: bool}}]};
`augur ask -h` shows the question shape.
""",
    "selftest": """examples:
  augur selftest                               no network, no key, no model; prints `selftest ok`
""",
    "help": """examples:
  augur help install                           the install steps, written for an agent to follow
""",
    "install": """examples:
  augur install                                link `augur` into ~/.local/bin, or $AUGUR_BIN (source checkout only)
""",
    "uninstall": """examples:
  augur uninstall --dry-run                    what removal would take, without taking it
  augur uninstall --yes                        remove what augur made; print what is left
""",
}
