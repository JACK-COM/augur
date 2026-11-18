"""The command backend: any script, fed one `augur ask --request` object on stdin, printing
{answers, model?, usage?}.
"""
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, List, Optional, Tuple

import _guard as guard

from .. import errors
from . import common


def argv(cfg: dict) -> list:
    cmd = cfg["command"]
    try:
        out = shlex.split(cmd) if isinstance(cmd, str) else [str(a) for a in cmd]
    except ValueError as e:                 # an unbalanced quote in a hand-written manifest
        raise errors.AugurUnavailable(f"command backend: `command` does not parse ({e})") from e
    if not out:
        raise errors.AugurUnavailable("command backend: `command` is empty")
    out[0] = os.path.expanduser(out[0])     # a manifest is not a shell: `~` would otherwise reach exec verbatim
    return out


def ask(cfg: dict, state: Any, questions: dict, model: Optional[str], timeout: float, retries: int) -> dict:
    """`retries` does not apply, as for laya: a command that fails once fails the same way
    again. The request is a valid `augur ask --request` object, so a wrapper can be tested
    by piping the same JSON to it by hand."""
    run = argv(cfg)
    model = model or cfg.get("model")
    wait = cfg.get("timeout") or timeout
    req = {"questions": questions, "state": state, **({"model": model} if model else {})}
    try:
        # errors="replace": a stray non-UTF-8 byte would otherwise raise past every except here
        out = subprocess.run(run, input=json.dumps(req), capture_output=True, text=True,
                             errors="replace", timeout=wait)
    except subprocess.TimeoutExpired:
        raise errors.AugurUnavailable(f"command {run[0]} gave no answer in {wait}s")
    except OSError as e:
        raise errors.AugurUnavailable(f"command {run[0]}: {e}") from e
    if out.returncode != 0:
        why = guard.relay((out.stderr.strip().splitlines() or ["no message"])[-1], 200)
        raise errors.AugurUnavailable(f"command {run[0]} exited {out.returncode}: {why}")
    try:
        resp = json.loads(out.stdout)
    except ValueError as e:
        raise errors.AugurUnavailable(f"command {run[0]} printed something that is not JSON: {guard.relay(repr(out.stdout[:200]), 220)}") from e
    return common.reply(resp, model or Path(run[0]).name)


def gates(new: dict, old: dict) -> List[str]:
    if new.get("command") == old.get("command"):
        return []
    return [f"run  {describe(new)}  on every call, each text Augur asks on its stdin"]


def describe(cfg: dict) -> str:
    c = cfg.get("command", "")
    return c if isinstance(c, str) else shlex.join(str(a) for a in c)


def guided(ui: Any, shipped: Optional[dict]) -> Optional[Tuple[dict, str]]:
    c = ui.prompt("The command. It reads one `augur ask --request` object on stdin and prints "
                  "{answers}; `augur ask -h` shows the shape")
    if not c:
        return None
    s = {"type": "command", "command": c}
    model = ui.prompt("A model name to send with every request (blank: none)")
    if model:
        s["model"] = model
    try:
        return s, Path(shlex.split(c)[0]).stem
    except (ValueError, IndexError):        # an unbalanced quote: the check below names the fault
        return s, "script"


TYPE = common.BackendType(
    name="command",
    schema={"type": "object", "additionalProperties": False, "required": ["command"], "properties": {
        "type": common.TYPE,
        "command": {"type": ["array", "string"], "items": {"type": "string"},
                    "description": "The command: an argument list, or one string split as a shell would. "
                                   "It reads {questions, state, model?} on stdin and prints {answers, model?, usage?}."},
        "model": {"type": "string", "description": "Sent as `model` in every request; --model overrides it."},
        "models": common.MODELS,
        "workers": common.WORKERS,
        "timeout": common.TIMEOUT,
        "price_per_mtok": common.PRICE}},
    ask=ask, describe=describe, flags=("command", "model"),
    kinds=(common.Kind("a script of your own", guided),), gates=gates,
)
