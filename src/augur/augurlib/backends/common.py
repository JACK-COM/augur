"""What every backend type declares, and the pieces they share: the schema fragments, the
checks an entry's values pass, and the one reading of a backend's reply.
"""
import argparse
import math
import re
import urllib.parse
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from .. import errors

VAR_CHARS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PRICE = {"type": "number", "description": "USD per million input tokens, for the usage ledger."}
TIMEOUT = {"type": "number", "description": "Seconds to wait for an answer (default 60)."}
TYPE = {"type": "string", "description": "The protocol this backend speaks: systemone, laya or command."}


class Kind(NamedTuple):
    """One way a guided `configure backend` sets up an entry of this type. `run(ui, shipped)`
    asks its questions through `ui` (prompt, yes, choose) and returns (settings, suggested
    name), or None after saying why not; `shipped` is the shipped entry of this type's
    default name, if any. `named(ui, name, settings)`, when given, runs once the name is
    known and returns whether to store a key through macOS `security`."""
    label: str
    run: Callable[[Any, Optional[dict]], Optional[Tuple[dict, str]]]
    named: Optional[Callable[[Any, str, dict], bool]] = None


class BackendType(NamedTuple):
    """A backend protocol, declared once: everything Augur does with an entry of this type
    reads it from here, so a new type is one module under backends/."""
    name: str
    schema: dict                                    # the entry's JSON schema, checked by its `type`
    ask: Callable[..., dict]                        # (cfg, state, questions, model, timeout, retries) -> reply
    describe: Callable[[dict], str]                 # one line for --list and the replace prompt
    flags: Tuple[str, ...]                          # entry keys `configure backend` sets by flag
    kinds: Tuple[Kind, ...] = ()                    # its entries in the guided menu
    available: Optional[Callable[[dict], Tuple[bool, str]]] = None   # None: ask one real question
    workers: Optional[int] = None                   # calibrate's ceiling on parallel calls; None: the caller's
    cold_start: Callable[[dict], bool] = lambda cfg: False   # whether a first call may load a model for minutes
    leftovers: Callable[[dict], List[str]] = lambda cfg: []  # what `uninstall` leaves for the user to remove


# How each settable key reads on the command line: (flag, metavar, help), where the flag
# differs from `--<key>` or the key needs a converter. The help names the types that take it.
def positive(v: str) -> float:
    """argparse type: a finite number above zero, so `nan` never reaches the JSON and `0` never
    reads as unset."""
    try:
        f = float(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{v!r} is not a number")
    if not (math.isfinite(f) and f > 0):
        raise argparse.ArgumentTypeError(f"{v!r} must be a finite number above zero")
    return f


def price(v: str) -> float:
    """argparse type: a finite price, zero for a backend with no bill."""
    return 0.0 if v.strip() in ("0", "0.0") else positive(v)


FLAGS: Dict[str, Dict[str, Any]] = {
    "url": {"help": "the endpoint"},
    "model": {"help": "the model to ask; pin a version"},
    "keychain_service": {"flag": "--key-service", "metavar": "NAME", "help": "the macOS keychain service holding the key"},
    "env_key": {"flag": "--key-env", "metavar": "VAR", "help": "the environment variable holding the key"},
    "command": {"help": "the script, as one string split as a shell would"},
    "python": {"help": "the interpreter of a venv holding laya and torch"},
    "checkpoint": {"help": "the checkpoint to load"},
    "device": {"help": "cuda, mps or cpu"},
    "timeout": {"type": positive, "metavar": "S", "help": "seconds to wait for an answer", "common": True},
    "price_per_mtok": {"flag": "--price", "type": price, "metavar": "USD",
                       "help": "USD per million input tokens, for the usage ledger", "common": True},
}


def check_url(url: str) -> None:
    """A key belongs in the keychain or the environment, where Augur never prints it; a URL is
    printed by --list, so one carrying a password or a query string is refused."""
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError(f"--url {url!r} is not an http(s) address")
    if u.username or u.password or u.query:
        raise ValueError("--url may not carry a username, password or query string; give the key with "
                         "--key-service or --key-env, and Augur sends it as a header")


def reply(resp: Any, model: str, keep: bool = False) -> dict:
    """A backend's parsed reply as Augur returns it: `answers` an object, `usage` a count of
    input tokens, `model` what answered (else `model`); `keep` passes the reply's other keys
    through (laya's device and context). Any other shape is unavailable."""
    if not isinstance(resp, dict) or not isinstance(resp.get("answers"), dict):
        raise errors.AugurUnavailable(f"unexpected response shape: {str(resp)[:200]}")
    usage: dict = resp["usage"] if isinstance(resp.get("usage"), dict) else {}
    out = dict(resp) if keep else {"answers": resp["answers"]}
    out["model"] = str(resp.get("model") or model)
    try:
        out["usage"] = {"input_tokens": int(usage.get("input_tokens") or 0)}
    except (TypeError, ValueError):
        out["usage"] = {"input_tokens": 0}
    return out
