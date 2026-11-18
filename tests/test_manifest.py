"""Manifest findings and the written schema."""
import json
import unittest
from pathlib import Path

from tests.harness import AugurCase

from augur import manifest_findings, write_schema  # noqa: E402


class Manifest(AugurCase):
    def test_manifest_findings_and_schema(self):
        home = self.home
        with open(Path(home) / "augur.json", "w") as f:
            json.dump({"backnd": "laya", "backends": {"laya": {"pyton": "/x", "device": None, "head_max_len": "512"}}}, f)
        found = " | ".join(manifest_findings())
        for w in ("unknown key 'backnd', did you mean 'backend'?", "unknown key 'pyton', did you mean 'python'?",
                  "head_max_len: expected integer"):
            self.assertTrue(w in found, f"manifest: not caught: {w}")
        self.assertTrue("device" not in found, "manifest: a null device was flagged")
        schema_path = write_schema()
        self.assertTrue(json.loads(schema_path.read_text(encoding="utf-8"))["$id"] == "augur.schema.json", "schema: not written")


if __name__ == "__main__":
    unittest.main()
