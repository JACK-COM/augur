"""The laya backend: Convai's open-weight model, answered by a worker process running in a
virtualenv of its own (`augur_laya.py`), one request at a time.
"""
import json
import os
import select
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

import _guard as guard

from .. import errors, paths, qtypes
from . import common

_proc: Optional[subprocess.Popen] = None
_lock = threading.Lock()


def _worker(cfg: dict) -> subprocess.Popen:
    global _proc
    if _proc is not None and _proc.poll() is None:
        return _proc
    py = os.path.expanduser(cfg.get("python") or "")
    if not py or not os.path.exists(py):
        raise errors.AugurUnavailable("laya: the backend's `python` does not name an interpreter with laya installed")
    _proc = subprocess.Popen([py, str(paths.HERE / "augur_laya.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, bufsize=1)
    # the answer is read from the raw descriptor (`_line`), so nothing may sit in a buffer above it
    return _proc


LINE_CAP = 1 << 20           # one answer line; a longer one is not an answer


def _line(p: subprocess.Popen, timeout: float) -> Optional[str]:
    """The worker's next line, read under one deadline and a byte cap: select then readline
    would block on a line the worker starts and never ends. None past either; "" at EOF."""
    assert p.stdout is not None
    fd, buf, end = p.stdout.fileno(), bytearray(), time.monotonic() + timeout
    while b"\n" not in buf:
        left = end - time.monotonic()
        if left <= 0 or len(buf) > LINE_CAP:
            return None
        ready, _, _ = select.select([fd], [], [], left)
        if not ready:
            return None
        chunk = os.read(fd, 65536)
        if not chunk:
            return buf.decode("utf-8", "replace")
        buf += chunk
    return buf[:buf.index(b"\n") + 1].decode("utf-8", "replace")


def ask(cfg: dict, state: Any, questions: dict, model: Optional[str], timeout: float, retries: int) -> dict:
    """`retries` does not apply: a local worker that fails once fails the same way again.
    `timeout` bounds the wait for the answer line; past it the worker is killed, since a
    hung model load would otherwise hold the lock and every later caller behind it."""
    global _proc
    req = {"state": state, "questions": questions, "checkpoint": model or cfg["checkpoint"],
           "device": cfg.get("device"), "head_max_len": cfg.get("head_max_len")}
    with _lock:  # one model on one device answers one request at a time
        try:
            p = _worker(cfg)
            assert p.stdin is not None and p.stdout is not None
            p.stdin.write(json.dumps(req) + "\n")
            p.stdin.flush()
            line = _line(p, timeout)
            if line is None:
                p.kill()
                p.wait()
                for f in (p.stdin, p.stdout):
                    if f:
                        f.close()
                _proc = None
                raise errors.AugurUnavailable(f"laya worker gave no whole answer in {timeout}s; killed")
        except (OSError, ValueError) as e:  # OSError covers an interpreter Popen cannot start
            raise errors.AugurUnavailable(f"laya worker: {e}") from e
    if not line:
        raise errors.AugurUnavailable("laya worker exited without answering (missing package, bad checkpoint, or out of memory)")
    try:
        resp = common.parse(line)
    except errors.AugurUnavailable as e:
        raise errors.AugurUnavailable(f"laya worker sent a line that is not JSON: {guard.relay(repr(line[:200]), 220)}") from e
    if isinstance(resp, dict) and "error" in resp:
        raise errors.AugurUnavailable(f"laya: {guard.relay(resp['error'], 300)}")
    return common.reply(resp, model or cfg["checkpoint"], keep=True)


def available(cfg: dict) -> Tuple[bool, str]:
    try:
        r = ask(cfg, {"text": "banana"}, {"fruit": qtypes.noul("Is the word in `text` the name of a fruit?")}, None, 60, 0)
        return True, f"{r['model']} on {guard.relay(r.get('device', '?'), 20)}, context {guard.relay(r.get('max_len', '?'), 20)}"
    except (errors.AugurUnavailable, ValueError, OSError) as e:
        return False, str(e)


def gates(new: dict, old: dict) -> List[str]:
    out = []
    if new.get("python") and new.get("python") != old.get("python"):
        out.append(f"run  {new['python']}  as laya's interpreter, each text Augur asks on its stdin")
    if new.get("checkpoint") != old.get("checkpoint") or (new.get("python") != old.get("python") and new.get("python")):
        # a checkpoint is weights the worker loads, which can be code, from a path or a download
        out.append(f"load  {new.get('checkpoint')}  as laya's checkpoint")
    return out


def describe(cfg: dict) -> str:
    return f"{cfg.get('checkpoint')} via {cfg.get('python') or '(no interpreter named)'}"


def guided(ui: Any, shipped: Optional[dict]) -> Optional[Tuple[dict, str]]:
    venv_py = paths.LAYA_VENV / "bin" / "python"
    py = ui.prompt("The python of a virtualenv holding laya and torch (`augur help install` makes one)",
                   str(venv_py) if venv_py.exists() else None)
    if not py:
        return None
    s = {"type": "laya", "python": py}
    default = (shipped or {}).get("checkpoint")
    ck = ui.prompt("Checkpoint; a fine-tune goes here", default)
    if ck != default:
        s["checkpoint"] = ck
    return s, "laya"


def leftovers(cfg: dict) -> List[str]:
    py = cfg.get("python")
    if py and not str(Path(py).expanduser()).startswith(str(paths.LAYA_VENV)):
        return [f"  a laya interpreter the manifest names elsewhere: {py}"]
    return []


TYPE = common.BackendType(
    name="laya",
    schema={"type": "object", "additionalProperties": False, "required": ["checkpoint"], "properties": {
        "type": common.TYPE,
        "python": {"type": "string", "description": "Interpreter of a venv holding laya and torch."},
        "checkpoint": {"type": "string", "description": "Checkpoint to load; a fine-tune goes here."},
        "device": {"type": ["string", "null"], "description": "cuda, mps or cpu; null lets laya pick."},
        "head_max_len": {"type": "integer"},
        "models": common.MODELS,
        "price_per_mtok": common.PRICE}},
    ask=ask, describe=describe, flags=("python", "checkpoint", "device"),
    kinds=(common.Kind("Laya, in a Python virtualenv", guided),),
    available=available, workers=1, leftovers=leftovers, gates=gates, pool=lambda cfg: 1,   # one worker, one device
    own_model=lambda cfg: cfg.get("checkpoint"),
)
