<p align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/augur-dark.svg">
  <img src="docs/augur-light.svg" alt="Augur" width="112">
</picture>
</p>

# Augur

*Know why you choose.* One piece of [the Panoply](https://github.com/JACK-COM/homebrew-panoply).

Augur asks a decision model typed questions about a text and returns calibrated probabilities, with no generated text. A question is a literal yes-or-no, a choice between named options, or a position on ordered levels. An agent uses it to check a rule it can phrase that way: does this sentence recommend the product on a condition, does this page address an AI agent. It keeps the rule's threshold honest by measuring it on your own labelled examples.

It informs and never blocks. The caller decides what a probability means, and nothing that must succeed should depend on the model answering.

## Install

```
brew tap jack-com/panoply
brew trust --formula jack-com/panoply/augur
brew install jack-com/panoply/augur
```

The full name matters: homebrew-core has an unrelated cask called `augur`. Or with [uv](https://docs.astral.sh/uv/): `uv tool install git+https://github.com/JACK-COM/augur`.

Then ask your agent to run `augur help install` and follow it. The default backend needs a [TypeSafe](https://typesafe.ai) API key.

## Use

```
augur check --live                            the backend answers, and answers right
augur ask -q questions.json -t "some text"    one call; probabilities per question
augur ask --request - < call.json             the same call as one JSON object on stdin
augur calibrate items.json --out run-a/       AUROC and floor scores on your labelled items
augur configure check items.json              make check --live ask your own questions
augur selftest                                the client, offline
```

From Python:

```python
from augur import ask, noul
r = ask("Buy the Bravo for the altitude.",
        {"cond": noul("Does `text` recommend the aircraft in `subject` on a condition?")},
        subject="Mooney M20M Bravo")
r["answers"]["cond"]["noul"]   # 0.92
```

Every call's tokens and cost are logged to `~/.augur/usage.csv`.

## Backends

`jev`, TypeSafe's hosted System One model, is the default and is pinned to one version, because a threshold tuned on one model does not transfer to the next. `laya`, Convai's open-weight model, runs locally in a virtualenv you provide; it is a base to fine-tune, and near chance on our items before that. A backend earns a threshold only through `augur calibrate` on your items.

### Choosing a backend

Each of these overrides the one before it:

1. **The manifest** at `~/.augur/augur.json` (or `$AUGUR_HOME/augur.json`) sets the default for every call.
2. **`AUGUR_BACKEND`** in the environment overrides it for one shell or one process.
3. **`--backend`** on the command line, or `backend=` in Python, overrides both for one call.

```
AUGUR_BACKEND=laya augur check
augur ask --backend laya -q questions.json -t "some text"
```

```python
ask("some text", questions, backend="laya")
```

### Configuring a backend

The manifest holds only what differs from the defaults, and settings merge per backend. To make `laya` the default and point it at its virtualenv:

```
uv venv --python 3.12 ~/.augur-laya
uv pip install --python ~/.augur-laya/bin/python laya torch
```

```json
{
  "backend": "laya",
  "backends": {
    "laya": {"python": "~/.augur-laya/bin/python", "checkpoint": "you/your-fine-tune", "device": "mps"}
  }
}
```

`augur check` flags a misspelled or mistyped key, and `augur schema` writes the schema to `~/.augur/augur.schema.json` for your editor.

`jev` takes `model` (the pinned version), `price_per_mtok` (for the usage ledger) and `keychain_service` or `env_key` (where to find the key). `laya` takes `python`, `checkpoint`, `device` (`cuda`, `mps` or `cpu`; unset lets Laya pick) and `head_max_len`. `--model` overrides the model or checkpoint for one call.

Run `augur check --live` after switching, and recalibrate: `augur calibrate items.json --backend laya --compare run-a/means-jev.json` shows how far each item moved from the previous backend.

`check --live` asks two built-in questions (is "banana" a fruit, is it a fish). To make it ask the questions you actually rely on, point it at a few items from your own calibration file: `augur configure check my-items.json` copies up to ten items to `~/.augur/check.json`, and `--threshold` sets where a true label must land (0.5 by default). Each item is one billed call per check; `augur configure check --reset` brings the built-in pair back.

## Requirements

Python 3.9 or later, standard library only. The `jev` backend needs network access and a key; `laya` needs `laya` and `torch` in a separate virtualenv. The keychain lookup is macOS; elsewhere the key comes from `TYPESAFE_API_KEY`. Tested on macOS and Linux (Debian, Python 3.12); on Windows, run it under WSL.

## Releasing

`make version` (or `version-minor`, `version-major`) computes the next version from `__version__` and hands it to `scripts/release.sh X.Y.Z`, which stamps it, runs the selftest, tags and pushes, then moves the formula in [the tap](https://github.com/JACK-COM/homebrew-panoply) to the new tarball. `make test` runs the selftest alone.
