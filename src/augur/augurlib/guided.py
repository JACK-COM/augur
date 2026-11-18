"""Configuring a backend in a terminal, by questions: the menu is every guided kind the
backend types declare (backends.kinds()), and each kind asks its own.
"""
from types import SimpleNamespace

from . import backends
from . import entries
from . import manifest


def _prompt(text, default=None):
    a = input(f"{text}{f' [{default}]' if default else ''}: ").strip()
    return a or default


def _yes(text, default=False):
    a = input(f"{text} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return a in ("y", "yes") if a else default


def _choose(text, options):
    print(text)
    for i, o in enumerate(options, 1):
        print(f"  {i}) {o}")
    while True:
        a = input("> ").strip()
        if a.isdigit() and 1 <= int(a) <= len(options):
            return int(a) - 1
        print(f"enter a number from 1 to {len(options)}")


# What a backend type's guided questions are asked through.
UI = SimpleNamespace(prompt=_prompt, yes=_yes, choose=_choose)


def _guided_backend(name=None):
    """Add or replace a backend in a terminal, by the same verify-then-write as the flags."""
    try:
        menu = backends.kinds()
        pick = _choose("Where does the model run?", [k.label for _, k in menu])
        tname, kind = menu[pick]
        got = kind.run(UI, manifest._defaults()["backends"].get(tname))
        if got is None:
            return kind.declined
        settings, suggested = got
        while True:
            name = name or _prompt("A name for this backend", suggested)
            if name and entries.NAME_CHARS.fullmatch(name):
                break
            print(entries.NAME_RULE)
            name = None
        known = manifest._manifest()["backends"]
        shipped = manifest._defaults()["backends"].get(name)
        if shipped and shipped.get("type") == settings["type"]:
            left = sorted(set(shipped) - set(settings) - {"type"})
            print(f"{name} is shipped; what you leave out keeps its shipped value ({', '.join(left)})")
        if name in known and not _yes(f"{name} exists ({manifest._type(known[name])}, {backends.TYPES[manifest._type(known[name])].describe(known[name])}). "
                                      f"Replace it?"):
            print("augur.json unchanged")
            return 1
        store = kind.named(UI, name, settings) if kind.named else False
        return entries._save_backend(name, settings, replace=True, store_key=store, confirm=_yes)
    except (EOFError, KeyboardInterrupt):
        print("\naugur.json unchanged")
        return 1
