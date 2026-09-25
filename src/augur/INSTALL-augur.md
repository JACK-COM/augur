# Install Augur

Augur sends a text and a map of typed questions to a decision model and returns calibrated probabilities with no generated text. It is one piece of the Panoply, and it is built on one rule: it informs, never blocks, and nothing that must succeed may depend on it. These steps are written for the agent doing the install; the user answers only the questions marked as theirs.

## What it does

A question is one of three shapes. A **noul** asks a literal yes-or-no and returns a probability. A **choice** names options and returns one with a probability per option. A **score** names ordered levels and returns a position on them. Every backend reads the question literally, so a rule it checks has to be written as a literal yes-or-no with the boundary cases in the criteria. A rule that cannot be written that way is one this instrument cannot see, and that is a fact about the rule.

Augur holds no questions. A project keeps each question file beside the rule it enforces, so the rule and its test move together.

## Backends

- **jev** (default): TypeSafe's hosted System One model, priced per input token. The model is pinned in the manifest, because an alias moves without notice and a tuned threshold moves with it.
- **laya**: Convai's open-weight ModernBERT model, local and free. By its makers' card it is a base to fine-tune rather than a zero-shot engine; on labelled product copy the base checkpoint measured near chance where Jev measured 0.99 AUROC. Use it when the user has fine-tuned it on their own items.

## Step 1. Install the command

```
brew tap jack-com/panoply
brew trust --formula jack-com/panoply/augur
brew install jack-com/panoply/augur
```

Spell the formula in full: homebrew-core has an unrelated cask named `augur`. Without Homebrew, `uv tool install git+https://github.com/JACK-COM/augur` does the same. Run `augur selftest`, which needs no network or key, and expect `selftest ok`.

## Step 2. Store the key (the user's step)

Ask the user for their TypeSafe API key, or to store it themselves. On macOS it goes in the keychain:

```
security add-generic-password -a "$USER" -s TYPESAFE_API_KEY -w <key> -U
```

Elsewhere, export `TYPESAFE_API_KEY` from the shell profile. Never write the key into a repository, a manifest or a log, and never print it.

## Step 3. Write the manifest, if anything differs

The manifest is `~/.augur/augur.json`, or `augur.json` in `$AUGUR_HOME`. Only what differs from the defaults needs to be there. For the local backend:

```
uv venv --python 3.12 ~/.augur-laya
uv pip install --python ~/.augur-laya/bin/python laya torch
```

```json
{"backend": "jev", "backends": {"laya": {"python": "~/.augur-laya/bin/python"}}}
```

`AUGUR_BACKEND` in the environment overrides the manifest's default for one shell.

`augur schema` writes `~/.augur/augur.schema.json`; point the editor at it for completion, and `augur check` names any misspelled or mistyped key.

## Step 4. Prove it answers

```
augur check
augur check --live
```

`check` says whether the backend can answer at all. `--live` asks questions with known answers: two built-in ones in one billed call, or the user's own if `augur configure check` has set them, one call per item. Exit 3 means unavailable, with one line saying why. Nothing else the user runs should depend on this passing.

## Step 5. Calibrate before trusting a threshold

A backend earns a threshold by a measured run on the user's own labelled items, never by a published number. Write an items file (the format is in `augur calibrate -h`), then:

```
augur calibrate items.json --out runs/
```

Read the AUROC, the lowest positive and the highest negative per question. A threshold read from one backend does not transfer to another. After a backend or client change, `--compare` against the prior run's per-item means shows whether the instrument moved.

## Step 6. Report

Tell the user, in five lines or fewer: the version (`augur --version`), the backend, whether `check --live` passed, where the manifest and the usage ledger live (`~/.augur/usage.csv` records every call's tokens and cost), and anything left for them to do.

## Removal

`augur uninstall` removes the laya virtualenv if one exists, asking once (`--yes` skips the question, `--dry-run` only prints), and then lists what it leaves: the keychain entry, `~/.augur`, and the command itself (`brew uninstall jack-com/panoply/augur`; the short name reaches homebrew-core's unrelated `augur` cask).

## What is not covered

Augur checks whether a text satisfies a literal condition. It cannot fetch, read across documents, decide what to look at next, or write. A probability is a proxy for the question the rule asks, so the caller thresholds and acts, and the number never refuses anything on its own.
