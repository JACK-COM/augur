"""The live check: one real, ledgered question per labelled item the user copied with
`augur configure check`, graded against a threshold.
"""
import json
import sys
from pathlib import Path

import _guard as guard

from . import client
from . import errors
from . import fsio
from . import paths
from . import qtypes
from . import request




def _check_spec(spec, threshold=0.5):
    """A calibrate items file, checked for use as the live check -> the stored form.
    Raises ValueError naming the first fault."""
    if not isinstance(spec, dict):
        raise ValueError("the file is not a JSON object")
    qs, items = spec.get("questions"), spec.get("items")
    if not isinstance(qs, dict) or not qs or not all(isinstance(q, dict) for q in qs.values()):
        raise ValueError("`questions` must be a non-empty object of named questions (`augur ask -h` shows one)")
    if not isinstance(items, list) or not 1 <= len(items) <= paths.CHECK_MAX:
        raise ValueError(f"`items` must list 1 to {paths.CHECK_MAX} items; each is one billed call on every check")
    if not (isinstance(threshold, (int, float)) and not isinstance(threshold, bool) and 0 < threshold < 1):
        raise ValueError("the threshold must lie strictly between 0 and 1")
    seen, kept = set(), []
    for i, it in enumerate(items):
        where = f"items[{i}]"
        if not isinstance(it, dict) or not isinstance(it.get("id"), str) or not it["id"]:
            raise ValueError(f"{where} needs a string `id`")
        if it["id"] in seen:
            raise ValueError(f"{where}: id {it['id']!r} appears twice")
        seen.add(it["id"])
        if not isinstance(it.get("text"), str) or not it["text"]:
            raise ValueError(f"{where} needs a non-empty string `text`")
        labels = it.get("labels")
        if not isinstance(labels, dict) or not labels:
            raise ValueError(f"{where} needs `labels`, {{question: true or false}}")
        for q, v in labels.items():
            if q not in qs or not isinstance(v, bool):
                raise ValueError(f"{where}: label {q!r} must name a question and be true or false")
            if qs[q].get("type") != "noul":
                raise ValueError(f"{where}: label {q!r} names a {qs[q].get('type')!r} question; a live check "
                                 f"grades nouls only")
        if it.get("subject") is not None and not (isinstance(it["subject"], str) and it["subject"]):
            raise ValueError(f"{where}: `subject` must be a non-empty string")
        sib = it.get("siblings")
        if sib is not None and not (isinstance(sib, list) and all(isinstance(x, str) for x in sib)):
            raise ValueError(f"{where}: `siblings` must be a list of strings")
        kept.append({k: it[k] for k in ("id", "text", "labels", "subject", "siblings") if k in it})
    return {"threshold": threshold, "questions": qs, "items": kept}


def _load_check():
    """The configured live check, or None. Raises OSError or ValueError on a broken
    check.json, which a hand edit can make."""
    path = paths.check_file()
    if not path.exists():
        return None
    raw = request.load(str(path), str(path))
    return _check_spec(raw, raw.get("threshold", 0.5) if isinstance(raw, dict) else 0.5)


def configure_check(file, threshold, reset):
    path = paths.check_file()
    if reset and (file is not None or threshold is not None):
        print("--reset takes no file or threshold", file=sys.stderr)
        return 2
    if threshold is not None and file is None:
        print("--threshold applies to a file being configured: augur configure check FILE --threshold P",
              file=sys.stderr)
        return 2
    if reset:
        if path.exists():
            path.unlink()
            print(f"removed {path}; `augur check --live` asks its two built-in questions again")
        else:
            print("no live check configured; `augur check --live` asks its two built-in questions")
        return 0
    if file is None:
        try:
            spec = _load_check()
        except (OSError, ValueError) as e:
            print(f"{path}: {e}; `augur configure check FILE` replaces it, `--reset` removes it", file=sys.stderr)
            return 2
        print(f"live check: {len(spec['items'])} items from {path}, threshold {spec['threshold']}" if spec
              else "live check: the two built-in questions (fruit, fish)")
        return 0
    try:
        spec = _check_spec(request.load(file, file), 0.5 if threshold is None else threshold)
    except (OSError, ValueError) as e:
        print(f"{file}: {e}", file=sys.stderr)
        return 2
    fsio.write_json(path, spec, 1)
    print(f"live check: {len(spec['items'])} items, {len(spec['questions'])} question(s), threshold {spec['threshold']}, "
          f"copied to {path}; `augur check --live` asks them, {len(spec['items'])} billed call(s)")
    return 0


def live_check(backend):
    """Known questions with known answers, asked for real: the backend answers AND answers
    right. The questions are the user's own from `augur configure check`, else two built-in
    Nouls in one call."""
    path = paths.check_file()
    try:
        spec = _load_check()
    except (OSError, ValueError) as e:
        print(f"{path}: {e}; `augur configure check FILE` replaces it, `--reset` removes it", file=sys.stderr)
        return 2
    if spec:
        t, wrong, tokens, r = spec["threshold"], [], 0, None
        for it in spec["items"]:
            qs = {q: spec["questions"][q] for q in it["labels"]}
            try:
                r = client.ask(it["text"], qs, subject=it.get("subject"), siblings=it.get("siblings"),
                        backend=backend, caller="check")
            except errors.AugurUnavailable as e:
                print(f"unavailable: {e}", file=sys.stderr)
                return 3
            tokens += r["usage"]["input_tokens"]
            for q, want in it["labels"].items():
                a = r["answers"].get(q, {})
                got = a.get("noul") if a.get("type") == "noul" else None
                if got is None or (got >= t) != want:
                    wrong.append(f"  {guard.relay(it['id'], 60)}.{guard.relay(q, 60)}: expected {'at or above' if want else 'below'} {t}, got {got}")
        n = sum(len(it["labels"]) for it in spec["items"])
        assert r is not None                      # the loop above ran: a live check has items
        print(f"{'ok' if not wrong else 'wrong'}: {n - len(wrong)} of {n} answers right at {t}  backend {r['backend']}  "
              f"model {guard.relay(r['model'], 80)}  tokens {tokens}  ({path})")
        for w in wrong:
            print(w)
        return 0 if not wrong else 1
    q = {"fruit": qtypes.noul("Is the word in `text` the name of a fruit?", true="a fruit", false="not a fruit"),
         "fish": qtypes.noul("Is the word in `text` the name of a fish?", true="a fish", false="not a fish")}
    try:
        r = client.ask("banana", q, backend=backend, caller="check")
    except errors.AugurUnavailable as e:
        print(f"unavailable: {e}", file=sys.stderr)
        return 3
    a = r["answers"]
    fruit, fish = (a.get("fruit") or {}).get("noul"), (a.get("fish") or {}).get("noul")
    ok = isinstance(fruit, float) and isinstance(fish, float) and fruit > 0.8 and fish < 0.2
    print(f"{'ok' if ok else 'wrong'}: backend {r['backend']}  model {r['model']}  fruit {fruit}  "
          f"fish {fish}  tokens {r['usage']['input_tokens']}")
    return 0 if ok else 1
