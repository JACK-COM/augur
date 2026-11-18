"""Installing Augur by hand (a link on PATH) and removing it, a packaged install handing
both to its package manager.
"""
import os
import shlex
import shutil
import sys
from pathlib import Path

from . import errors
from . import guided
from . import manifest
from . import paths


def _install():
    """Symlink `augur` into paths.BIN for a source checkout. A link works from any shell, a
    subprocess and cron, where an alias reaches an interactive shell only."""
    if paths.PACKAGED:
        print("augur is on PATH through its package manager; nothing to link")
        return 0
    me = paths.HERE / "augur.py"
    me.chmod(me.stat().st_mode | 0o111)
    paths.BIN.mkdir(parents=True, exist_ok=True)
    link = paths.BIN / "augur"
    if link.is_symlink():
        link.unlink()
    elif link.exists():
        print(f"link: {link} exists and is not a symlink; leaving it. Remove it and rerun.")
        return 1
    link.symlink_to(me)
    print(f"link: {link} -> {me}")
    on_path = str(paths.BIN) in os.environ.get("PATH", "").split(os.pathsep)
    if not on_path:
        print(f"PATH: {paths.BIN} is not on it; add  export PATH=\"{paths.BIN}:$PATH\"  to the shell profile")
    print("verify from another directory:  augur check")
    return 0 if on_path else 2


def _uninstall(yes, dry):
    link = paths.BIN / "augur"
    links = [link] if not paths.PACKAGED and link.is_symlink() else []
    venv = paths.LAYA_VENV if paths.LAYA_VENV.is_dir() else None
    print("uninstall will remove:")
    for ln in links:
        print(f"  link      {ln}")
    if venv:
        print(f"  directory {venv}  (the laya virtualenv)")
    if not links and not venv:
        print("  nothing it made")
    if dry:
        return 0
    if (links or venv) and not yes:
        if sys.stdin.isatty():
            if not guided._yes("proceed?"):
                print("nothing removed")
                return 1
        else:
            print("nothing removed; rerun with --yes")
            return 1
    for ln in links:
        ln.unlink()
        print(f"removed {ln}")
    if venv:
        shutil.rmtree(venv)
        print(f"removed {venv}")
    print("\nleft for you, if you want it gone completely:")
    try:
        backends = manifest._manifest().get("backends") or {}
    except errors.AugurUnavailable:
        backends = {}
    for service in sorted({c["keychain_service"] for c in backends.values()
                           if manifest._type(c) == "systemone" and c.get("keychain_service")}):
        print(f"  a key:        security delete-generic-password -a \"$USER\" -s {service}")
    if paths.home().is_dir():
        print(f"  the home:     rm -r {paths.home()}   (augur.json, its schema and the usage ledger)")
    for py in sorted({c["python"] for c in backends.values() if manifest._type(c) == "laya" and c.get("python")}):
        if not str(Path(py).expanduser()).startswith(str(paths.LAYA_VENV)):
            print(f"  a laya interpreter the manifest names elsewhere: {py}")
    if paths.PACKAGED:
        print("  the command:  brew uninstall jack-com/panoply/augur   (or `uv tool uninstall augur`)")
    else:
        print(f"  the files:    rm -r {shlex.quote(str(paths.HERE))}   (augur.py and everything beside it)")
    return 0
