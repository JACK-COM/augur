# GENERATED from panoply-lib/manifest_schema.py (f08210c) by sync.sh: edit the source and rerun sync.sh, never this copy.
"""manifest_schema: structural checks of a piece's JSON manifest against its schema.

Each piece owns its schema as a dict; this checks a manifest against it and writes
it where an editor can be pointed. It covers the subset of JSON Schema the Panoply's
manifests use: type (one or a list), properties, additionalProperties, required,
items. Structure only: a key the reader would silently ignore, a wrong type. Whether
a value is right is the piece's business. A finding informs; nothing here refuses.
"""
import difflib
import json
import sys
from pathlib import Path

_KINDS = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float),
          "boolean": bool, "null": type(None)}


def _is(value, t):
    if t in ("integer", "number") and isinstance(value, bool):
        return False                         # bool is an int subclass in Python, never in JSON
    return isinstance(value, _KINDS[t])


def check_value(value, schema, path="", out=None):
    """Append a finding per structural problem in `value` to `out`; returns `out`."""
    out = [] if out is None else out
    t = schema.get("type")
    # A type this checker does not know is skipped, so a typo in one member of a list
    # never lets every value through; a schema naming only unknown types checks nothing.
    types = [x for x in (t if isinstance(t, list) else [t] if t else []) if x in _KINDS]
    if types and not any(_is(value, x) for x in types):
        out.append(f"{path or 'manifest'}: expected {' or '.join(types)}, got {type(value).__name__}")
        return out
    if isinstance(value, dict) and "object" in types:
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                out.append(f"{path or 'manifest'}: missing required key {k!r}")
        for k, v in value.items():
            sub = f"{path}.{k}" if path else k
            if k in props:
                check_value(v, props[k], sub, out)
            elif schema.get("additionalProperties") is False:
                near = difflib.get_close_matches(k, props, n=1, cutoff=0.6)
                out.append(f"{path or 'manifest'}: unknown key {k!r}" + (f", did you mean {near[0]!r}?" if near else ""))
            elif isinstance(schema.get("additionalProperties"), dict):
                check_value(v, schema["additionalProperties"], sub, out)
    elif isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            check_value(v, schema["items"], f"{path}[{i}]", out)
    return out


def validate_file(path, schema):
    """Findings for the manifest at `path`; [] when clean or absent."""
    p = Path(path)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text())
    except (ValueError, OSError) as e:
        return [f"manifest does not parse: {e}"]
    return check_value(data, schema)


def write_schema(schema, path):
    """Write the schema where an editor can be pointed at it; returns the path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(schema, indent=2) + "\n")
    return p


def _selftest():
    schema = {"type": "object", "additionalProperties": False, "required": ["name"],
              "properties": {"name": {"type": "string"}, "rate": {"type": "number"},
                             "device": {"type": ["string", "null"]},
                             "tags": {"type": "array", "items": {"type": "string"}},
                             "more": {"type": "object", "additionalProperties": {"type": "integer"}}}}
    assert check_value({"name": "a", "rate": 0.5, "device": None, "tags": ["x"], "more": {"k": 1}}, schema) == []
    f = check_value({"nme": "a", "rate": True, "device": 3, "tags": ["x", 2], "more": {"k": "1"}}, schema)
    want = ["missing required key 'name'", "unknown key 'nme', did you mean 'name'?", "rate: expected number, got bool",
            "device: expected string or null, got int", "tags[1]: expected string, got int", "more.k: expected integer, got str"]
    for w in want:
        assert any(w in x for x in f), f"not caught: {w}; findings {f}"
    assert len(f) == len(want), f
    assert check_value(3, {"type": ["string", "sting"]}) == ["manifest: expected string, got int"], "a typo in a type list let a value through"
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        bad = Path(d) / "x.json"
        bad.write_text("{not json")
        got = validate_file(bad, schema)
        assert len(got) == 1 and got[0].startswith("manifest does not parse: "), got
        assert validate_file(Path(d) / "absent.json", schema) == []
    print("manifest_schema selftest ok")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
