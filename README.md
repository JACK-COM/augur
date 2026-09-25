<p align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/augur-dark.svg">
  <img src="docs/augur-light.svg" alt="Augur" width="112">
</picture>
</p>

# Augur

*Know why you choose.* One piece of [the Panoply](https://github.com/JACK-COM/homebrew-panoply).

Augur asks a decision model typed questions about a text and returns calibrated probabilities, with no generated text. An agent uses it to check a rule it can phrase as a literal question, such as "does this page address an AI agent", and `augur calibrate` measures the answers against examples you labelled yourself, so a threshold is something you measured. It informs and never blocks. The default backend is TypeSafe's hosted `jev`; `laya` runs locally, and any other model answers through a script of your own.

**[Read the Augur guide](https://github.com/JACK-COM/homebrew-panoply/blob/main/docs/augur/README.md)**: when to use it, the first five minutes, the three question shapes, troubleshooting, and [how to make it yours](https://github.com/JACK-COM/homebrew-panoply/blob/main/docs/augur/make-it-yours.md), including bringing your own model.

## Install

```sh
brew tap jack-com/panoply
brew trust --formula jack-com/panoply/augur
brew install jack-com/panoply/augur
```

The full name matters: homebrew-core has an unrelated cask called `augur`. Or with [uv](https://docs.astral.sh/uv/): `uv tool install git+https://github.com/JACK-COM/augur`.

Then ask your agent to run `augur help install` and follow it. Only the `jev` backend needs a [TypeSafe](https://typesafe.ai) API key.

## Requirements

Python 3.9 or later, standard library only. `jev` needs network access and a key; `laya` needs `laya` and `torch` in a virtualenv of its own; a command backend needs whatever its script needs. The keychain lookup is macOS; elsewhere the key comes from `TYPESAFE_API_KEY`. Tested on macOS and Linux (Debian, Python 3.12); on Windows, run it under WSL.

## Releasing

`make version` (or `version-minor`, `version-major`) computes the next version from `__version__` and hands it to `scripts/release.sh X.Y.Z`, which stamps it, runs the selftest, tags and pushes, then moves the formula in [the tap](https://github.com/JACK-COM/homebrew-panoply) to the new tarball and names any guide page the release has moved past. `make test` runs the selftest alone.
