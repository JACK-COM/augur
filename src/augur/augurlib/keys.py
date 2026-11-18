"""A backend's key in the macOS keychain: storing it through `security`, and reading back
which account holds it.
"""
import getpass
import re
import shutil
import subprocess
import sys


def _keychain_account(service):
    """The account an existing item for SERVICE is stored under, else the login name. `-U` updates
    only an item matching account and service, and a second item under another account is one
    `find-generic-password -s` may find first. Without -w or -g, `security` prints attributes only."""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", service], capture_output=True, text=True,
                           timeout=10)
        return _acct(r.stdout) if r.returncode == 0 else getpass.getuser()
    except (OSError, subprocess.SubprocessError):
        return getpass.getuser()


def _acct(attributes):
    """The account in `security`'s attribute listing: `"acct"<blob>="name"`, or for a non-ASCII
    name `"acct"<blob>=0x<hex>  "<escaped>"`, read from the hex."""
    m = re.search(r'"acct"<blob>=(?:0x([0-9A-Fa-f]+)\s+)?"((?:[^"\\]|\\.)*)"', attributes)
    if not m:
        return getpass.getuser()
    return bytes.fromhex(m.group(1)).decode("utf-8", "replace") if m.group(1) else m.group(2)


def _store_key(name, cfg):
    """Put the key where the entry looks for it. On macOS `security` prompts for it itself, so
    the key is never an argument, never in shell history and never in Augur's output; Augur
    takes no key by flag at all. Elsewhere, say which variable to export."""
    service, env = cfg.get("keychain_service"), cfg.get("env_key")
    if not service and not env:
        print(f"{name} names no `keychain_service` or `env_key`, so it sends no key; give it --key-service "
              f"or --key-env first", file=sys.stderr)
        return 2
    if service and sys.platform == "darwin" and shutil.which("security"):
        if not sys.stdin.isatty():
            print(f"storing a key needs a terminal, where `security` prompts for it. Or run:\n"
                  f"  security add-generic-password -a \"$USER\" -s {service} -U -w", file=sys.stderr)
            return 2
        print(f"Paste the key at the prompt, then again to confirm; nothing echoes. "
              f"It goes in the keychain as {service}.")
        r = subprocess.run(["security", "add-generic-password", "-a", _keychain_account(service), "-s", service,
                            "-U", "-w"])
        if r.returncode:
            print(f"security exited {r.returncode}; no key stored", file=sys.stderr)
            return 3
        return 0
    if not env:
        print(f"{name} keeps its key in the macOS keychain only; give it --key-env to read one from the "
              f"environment here", file=sys.stderr)
        return 2
    print(f"Add this to your shell profile, with your key, and open a new shell:\n  export {env}=<your key>")
    return 0
