"""Augur's manifest: the shipped defaults merged with the user's augur.json, each backend
entry resolved, and the schema both are checked against.
"""
import json
import os
from pathlib import Path

from . import backends
from . import errors
from . import paths


# One schema per backend type, each declared by its type's module. An entry is checked
# against the schema its `type` names; the shared checker has no oneOf, so the dispatch is
# Augur's (`_entry_findings`).
BACKEND_TYPES = backends.schemas()


# Every key any type takes: an entry whose type cannot be resolved is checked against this,
# so a misspelled key still draws a did-you-mean.
_ANY_TYPE = {"type": "object", "additionalProperties": False,
             "properties": {k: v for s in BACKEND_TYPES.values() for k, v in s["properties"].items()}}


MANIFEST_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "augur.schema.json",
    "title": "Augur manifest (augur.json)",
    "description": "Augur's settings, merged over the defaults Augur ships in the same shape. "
                   "Every key is optional; an entry merges over the default of the same name.",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "$schema": {"type": "string", "description": "Editor hint only; ignored by Augur."},
        "backend": {"type": "string", "description": "The default backend, by its name under `backends`. "
                    "AUGUR_BACKEND and --backend override it."},
        "backends": {
            "type": "object",
            "description": "Backends by name. Each has a `type`; jev and laya are shipped entries of the same shape.",
            # for editors only: Augur itself checks each entry against BACKEND_TYPES by its type
            "additionalProperties": {
                "type": "object",
                "properties": {"type": {"enum": sorted(BACKEND_TYPES)}},
                "allOf": [{"if": {"properties": {"type": {"const": t}}, "required": ["type"]}, "then": s}
                          for t, s in BACKEND_TYPES.items()]},
        },
    },
}


def _schema_lib():
    """The shared checker, panoply-lib's copy beside augur.py, which puts its directory on
    the path however Augur is installed."""
    import _manifest_schema
    return _manifest_schema


def _entry_findings(data, defaults):
    """Findings for each entry under `backends`, checked against the schema its type names."""
    out = []
    backends = data.get("backends") if isinstance(data, dict) else None
    for name, entry in (backends or {}).items() if isinstance(backends, dict) else ():
        if not isinstance(entry, dict):
            continue                         # the top-level check already named it
        where = f"backends.{name}"
        t = _type(entry, defaults.get(name))
        if t is None:
            out.append(f"{where}: names no `type`; give it one of {', '.join(sorted(BACKEND_TYPES))}")
            _schema_lib().check_value(entry, _ANY_TYPE, where, out)
        elif t not in BACKEND_TYPES:
            out.append(f"{where}: unknown type {t!r}; known: {', '.join(sorted(BACKEND_TYPES))}")
        else:
            schema = BACKEND_TYPES[t]
            if entry.get("type") in (None, (defaults.get(name) or {}).get("type")):
                schema = {**schema, "required": []}      # it merges over a default that supplies them
            _schema_lib().check_value(entry, schema, where, out)
    return out


def manifest_findings(path=None):
    """Structural problems in the manifest, as lines; [] when clean or absent. `path` checks
    another file of the same shape, which is how the selftest holds the shipped defaults to it."""
    p = Path(path) if path else paths.manifest()
    found = _schema_lib().validate_file(p, MANIFEST_SCHEMA)
    if p.is_file() and not any(f.startswith("manifest does not parse") for f in found):
        defaults = {} if path else _defaults().get("backends", {})
        found += _entry_findings(json.loads(p.read_text(encoding="utf-8")), defaults)
    # the shared checker anchors every finding on the word "manifest"; name the file instead
    return [p.name + (f[len("manifest"):] if f.startswith("manifest") else ": " + f) for f in found]


def _defaults():
    try:
        return json.loads(paths.DEFAULTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise errors.AugurUnavailable(f"{paths.DEFAULTS_FILE}: the shipped defaults do not load ({e}); reinstall augur")


def _type(entry, default=None):
    """An entry's type: its own, else the default's of the same name, else `command` for a
    0.4 entry that names only a command. None when none of those resolves, or when it is not
    a string (a hand edit), which no lookup may take as a key."""
    t = entry.get("type") or (default or {}).get("type") or ("command" if entry.get("command") else None)
    return t if isinstance(t, str) else None


def _merged(base, cfg, default=None):
    """The manifest's one rule: `cfg` merges over `base`, unless it names a type other than
    base's (resolved through `default`), which replaces base whole so no key of the old type
    lingers."""
    if cfg.get("type") and cfg["type"] != _type(base, default):
        base = {}
    return {**base, **cfg}


def _manifest():
    """The shipped defaults, then augur.json over them by `_merged`'s rule. Then AUGUR_BACKEND."""
    m = _defaults()
    p = paths.manifest()
    if p.exists():
        try:
            with open(p, encoding="utf-8") as f:
                user = json.load(f)
        except (ValueError, RecursionError) as e:
            raise errors.AugurUnavailable(f"{p}: not JSON ({str(e)[:120]})")
        if not isinstance(user, dict) or not isinstance(user.get("backends") or {}, dict) \
                or not isinstance(user.get("backend", ""), str):
            raise errors.AugurUnavailable(f"{p}: not an object of settings; `augur check` names the fault")
        if "backend" in user:
            m["backend"] = user["backend"]
        for name, cfg in (user.get("backends") or {}).items():
            if not isinstance(cfg or {}, dict):
                raise errors.AugurUnavailable(f"{p}: backends.{name} is not an object")
            m["backends"][name] = _merged(m["backends"].get(name) or {}, cfg or {})
    if os.environ.get("AUGUR_BACKEND"):
        m["backend"] = os.environ["AUGUR_BACKEND"]
    return m


def _backend(name=None):
    """(name, merged entry, type) for the named or default backend, or AugurUnavailable."""
    m = _manifest()
    name = name or m["backend"]
    if name not in m["backends"]:
        raise errors.AugurUnavailable(f"unknown backend {name!r}; manifest knows {sorted(m['backends'])}")
    cfg = m["backends"][name]
    return name, cfg, _resolve(name, cfg)


def _resolve(name, cfg):
    """A merged entry's type, once it names one Augur speaks and every key that type requires."""
    t = _type(cfg)
    if t is None:
        raise errors.AugurUnavailable(f"backend {name!r} names no `type`; give it one of {', '.join(sorted(BACKEND_TYPES))}")
    if t not in BACKEND_TYPES:
        raise errors.AugurUnavailable(f"backend {name!r} has unknown type {t!r}; known: {', '.join(sorted(BACKEND_TYPES))}")
    for k in BACKEND_TYPES[t].get("required", []):
        if not cfg.get(k):
            raise errors.AugurUnavailable(f"backend {name!r} is a {t} backend with no `{k}`")
    found: list = []
    _schema_lib().check_value(cfg, BACKEND_TYPES[t], f"backends.{name}", found)
    if found:
        raise errors.AugurUnavailable(f"augur.json: {found[0]}; `augur check` lists every fault")
    return t


def _user_manifest():
    """augur.json as the user wrote it, {} when absent. Raises ValueError when it does not parse."""
    p = paths.manifest()
    if not p.exists():
        return {"backends": {}}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except RecursionError:
        raise ValueError(f"{p}: nests too deep to read") from None
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: not a JSON object")
    b = raw.setdefault("backends", {})
    if b is None:
        raw["backends"] = b = {}
    if not isinstance(b, dict) or not all(isinstance(e, dict) for e in b.values()):
        raise ValueError(f"{p}: `backends` must be an object of entries; `augur check` names the fault")
    return raw


def _origin(name, user, shipped):
    if name not in shipped:
        return "yours"
    if name not in user:
        return "shipped"
    same = _type(user[name], shipped[name]) == shipped[name].get("type")
    return "shipped, changed" if same else "yours, replaces shipped"


def write_schema():
    return _schema_lib().write_schema(MANIFEST_SCHEMA, paths.home() / "augur.schema.json")
