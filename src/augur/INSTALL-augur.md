# Install Augur

Augur sends a text and a map of typed questions to a decision model and returns calibrated probabilities with no generated text. It is one piece of the Panoply, and it is built on one rule: it informs, never blocks, and nothing that must succeed may depend on it. These steps are written for the agent doing the install; the user answers only the questions marked as theirs.

## What it does

A question is one of three shapes. A **noul** asks a literal yes-or-no and returns a probability. A **choice** names options and returns one with a probability per option. A **score** names ordered levels and returns a position on them. Every backend reads the question literally, so a rule it checks has to be written as a literal yes-or-no with the boundary cases in the criteria. A rule that cannot be written that way is one this instrument cannot see, and that is a fact about the rule.

Augur holds no questions. A project keeps each question file beside the rule it enforces, so the rule and its test move together.

## Backends

A backend is a named entry under `backends` in the manifest, and its `type` says how Augur talks to it. Augur ships `jev` and `laya` as entries of the same shape the user writes, and the user's manifest merges over them.

- **jev** (default, type `systemone`): TypeSafe's hosted System One model, priced per input token. The model is pinned, because an alias moves without notice and a tuned threshold moves with it.
- **a model Ollama serves** (type `systemone`): Ollama 0.35 and later serves decision models such as `nimble` on the same System One API, locally, with no key and no bill. Use it when the user wants a local model that answers well without training.
- **laya** (type `laya`): Convai's open-weight ModernBERT model, local and free. By its makers' card it is a base to fine-tune rather than a zero-shot engine; on labelled product copy the base checkpoint measured near chance where Jev measured 0.99 AUROC. Use it when the user has fine-tuned it on their own items.
- **any command** (type `command`): a script of the user's own reads one `augur ask --request`-shaped object on stdin (`questions`, `state`, and `model` when one is set) and prints `{"answers": ...}` in Augur's reply shape, with `model` and `usage.input_tokens` optional. Use it for a decision model that does not speak System One.

Ask the user which backend they want before Step 2. Only a hosted backend needs a key.

## Step 1. Install the command

```
brew tap jack-com/panoply
brew trust --formula jack-com/panoply/augur
brew install jack-com/panoply/augur
```

Spell the formula in full: homebrew-core has an unrelated cask named `augur`. Without Homebrew, `uv tool install git+https://github.com/JACK-COM/augur` does the same. Run `augur selftest`, which needs no network or key, and expect `selftest ok`.

## Step 2. Add the backend

`augur configure backend` asks a backend one real question and writes its entry to `~/.augur/augur.json` (or `augur.json` in `$AUGUR_HOME`) only if it answers. A user at a terminal can run it with no arguments and answer its prompts. An agent uses the flags below, one command per backend. Only what differs from the shipped `jev` and `laya` entries is written.

For `jev`, nothing needs adding but the key. Ask the user to run this in their own terminal; in Claude Code they can type it after `!`:

```
augur configure backend jev --store-key
```

On macOS, `security` prompts for the key, so it never appears as an argument, in shell history or in Augur's output. Elsewhere, the command prints the `export TYPESAFE_API_KEY=` line for the shell profile. Never ask for the key in conversation, and never write it into a repository, a manifest or a log.

For a model Ollama serves, check `ollama --version` is 0.35 or later, then pull a sized tag and add it:

```
ollama pull nimble:9b-q8_0
augur configure backend nimble --type systemone --url http://localhost:11434/v1/systemone --model nimble:9b-q8_0 --price 0 --default
```

Name the sized tag over `latest`, which moves when the library updates and takes a calibrated threshold with it. Ollama refuses a model that is not a decision model ("not supported by System One") and one never pulled ("not found"), and either refusal leaves nothing saved. The first call loads the model; if later calls time out, rerun with `--timeout 120`.

For another hosted System One provider, give the command to the user to run, because `--store-key` prompts for their key before the entry is checked:

```
augur configure backend acme --type systemone --url https://api.acme.example/v1/systemone --model acme-2.1 --key-service ACME_API_KEY --key-env ACME_API_KEY --price 0.05 --store-key
```

For `laya`:

```
uv venv --python 3.12 ~/.augur-laya
uv pip install --python ~/.augur-laya/bin/python laya torch
augur configure backend laya --python ~/.augur-laya/bin/python
```

For a command backend:

```
augur configure backend mine --type command --command /abs/path/to/wrapper --price 0 --default
```

Test a wrapper by hand first by piping it a request: `echo '{"questions": {...}, "state": {"text": "banana"}}' | /abs/path/to/wrapper`.

`--default` makes the backend the default, and `AUGUR_BACKEND` in the environment overrides that for one shell. `--force` saves an entry that did not answer. `augur configure backend --list` shows every backend, and `--remove NAME` removes one; on `jev` or `laya` that restores the shipped settings. The manifest stays plain JSON a user may edit by hand: `augur schema` writes `~/.augur/augur.schema.json` for an editor, and `augur check` names any misspelled or mistyped key.

## Step 3. Prove it answers

```
augur check
augur check --live
```

`check` says whether the backend can answer at all. `--live` asks questions with known answers: two built-in ones in one billed call, or the user's own if `augur configure check` has set them, one call per item. Exit 3 means unavailable, with one line saying why. Nothing else the user runs should depend on this passing.

## Step 4. Calibrate before trusting a threshold

A backend earns a threshold by a measured run on the user's own labelled items, never by a published number. Write an items file (the format is in `augur calibrate -h`), then:

```
augur calibrate items.json --out runs/
```

Read the AUROC, the lowest positive and the highest negative per question. A threshold read from one backend does not transfer to another. After a backend or client change, `--compare` against the prior run's per-item means shows whether the instrument moved.

## Step 5. Report

Tell the user, in five lines or fewer: the version (`augur --version`), the backend, whether `check --live` passed, where the manifest and the usage ledger live (`~/.augur/usage.csv` records every call's tokens and cost), and anything left for them to do.

## Removal

`augur uninstall` removes the laya virtualenv if one exists, asking once (`--yes` skips the question, `--dry-run` only prints), and then lists what it leaves: each keychain entry a backend names, `~/.augur`, and the command itself (`brew uninstall jack-com/panoply/augur`; the short name reaches homebrew-core's unrelated `augur` cask).

## What is not covered

Augur checks whether a text satisfies a literal condition. It cannot fetch, read across documents, decide what to look at next, or write. A probability is a proxy for the question the rule asks, so the caller thresholds and acts, and the number never refuses anything on its own.
