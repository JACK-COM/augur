"""augur.json writes: whole, mode and link kept, locked, the keychain account parse."""
import getpass
import json
import threading
import unittest
from pathlib import Path

from tests.harness import AugurCase

from augur import _acct, _manifest_lock, _user_manifest, _write_json  # noqa: E402


class Files(AugurCase):
    def saved(self):
        return json.loads((Path(self.home) / "augur.json").read_text(encoding="utf-8"))

    def test_write_lock_and_keychain_account(self):
        home = self.home
        saved = self.saved
        # the write: whole, its mode and its link kept, no temporary left behind
        real = Path(home) / "real.json"
        real.write_text("{}")
        real.chmod(0o640)
        link = Path(home) / "linked.json"
        link.symlink_to(real)
        _write_json(link, {"name": "café"})
        self.assertTrue(link.is_symlink() and json.loads(real.read_text(encoding="utf-8")) == {"name": "café"}
                        and (real.stat().st_mode & 0o777) == 0o640 and "café" in real.read_text(encoding="utf-8")
                        and not list(Path(home).glob("*.tmp")), "_write_json: link, mode, text or a leftover temporary")
        with _manifest_lock():
            self.assertTrue((Path(home) / "augur.json.lock").exists(), "the manifest lock")
        self.assertTrue(_acct('    "acct"<blob>="okure"\n    "svce"<blob>="X"\n') == "okure"
                        and _acct("") == getpass.getuser()
                        and _acct('    "acct"<blob>=0x636166C3A9  "caf\\303\\251"\n') == "café", "keychain account parse")
        (Path(home) / "augur.json").write_text("{}")

        def writer(i):
            with _manifest_lock():
                u = _user_manifest()
                u["backends"][f"w{i}"] = {"type": "command", "command": "x"}
                _write_json(Path(home) / "augur.json", u)
        ts = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for t_ in ts:
            t_.start()
        for t_ in ts:
            t_.join()
        self.assertTrue(sorted(saved()["backends"]) == [f"w{i}" for i in range(8)], f"concurrent writers: {saved()}")
        (Path(home) / "augur.json").write_text("{}")


if __name__ == "__main__":
    unittest.main()
