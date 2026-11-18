"""The backends Augur asks, by protocol. Each type is one module here declaring a
`common.BackendType`; everything else (the manifest schema, the configure flags, the guided
menu, --list, calibrate's concurrency, uninstall's leftovers) reads `TYPES`. A new protocol
is one module and one name in the tuple below. The name a backend has is the user's, never
dispatched on.
"""
from typing import Dict, List, Tuple

from . import command, common, laya, systemone
from .common import BackendType, Kind  # noqa: F401  (re-exported)

TYPES: Dict[str, BackendType] = {t.name: t for t in (systemone.TYPE, laya.TYPE, command.TYPE)}


def schemas() -> Dict[str, dict]:
    return {n: t.schema for n, t in TYPES.items()}


def entry_flags() -> Tuple[str, ...]:
    """Every entry key a flag sets: `type`, each type's own, then the ones every type takes."""
    own = [k for t in TYPES.values() for k in t.flags]
    shared = [k for k, spec in common.FLAGS.items() if spec.get("common")]
    return ("type", *dict.fromkeys(own + shared))


def flag_help(key: str) -> str:
    """A flag's help, prefixed with the types that take it unless every type does."""
    spec = common.FLAGS[key]
    if spec.get("common"):
        return spec["help"]
    takers = [n for n, t in TYPES.items() if key in t.flags]
    return f"{' or '.join(takers)}: {spec['help']}"


def kinds() -> List[Tuple[str, Kind]]:
    """The guided menu: (type name, kind), in the order TYPES lists them."""
    return [(n, k) for n, t in TYPES.items() for k in t.kinds]
