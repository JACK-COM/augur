"""Measuring a backend on a labelled item set: per-question AUROC and per-item means, the
run that earns a threshold.

`measure` asks and `report` prints. With an output directory, each answer is appended to
`results-<backend>.jsonl` as it lands, so an interrupted run keeps what it paid for and
`resume` asks only what is missing; the means are derived from that journal. An item that
fails is asked once more at the end, and the failures that remain are named, since an AUROC
over the survivors is biased when failures follow length or difficulty.
"""
import json
import os
import statistics as st
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import _guard as guard

from . import backends
from . import client
from . import errors
from . import manifest


def _auroc(pos: List[float], neg: List[float]) -> float:
    """Probability a random positive outranks a random negative, ties half, by rank sum."""
    if not pos or not neg:
        return float("nan")
    ranked = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    rank_sum, i = 0.0, 0
    while i < len(ranked):
        j = i
        while j < len(ranked) and ranked[j][0] == ranked[i][0]:
            j += 1
        mid = (i + j + 1) / 2                 # the average rank of a tied run, 1-based
        rank_sum += mid * sum(1 for k in range(i, j) if ranked[k][1])
        i = j
    n1, n0 = len(pos), len(neg)
    return (rank_sum - n1 * (n1 + 1) / 2) / (n1 * n0)


def check_spec(spec: Any) -> dict:
    """An items file Augur can run, or ValueError naming the first fault, before a call is paid for."""
    if not isinstance(spec, dict):
        raise ValueError("an items file is a JSON object with `questions` and `items`")
    from . import request
    request.questions_ok(spec.get("questions"))
    items = spec.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("`items` must be a non-empty list")
    for i, it in enumerate(items):
        if not (isinstance(it, dict) and isinstance(it.get("id"), (str, int)) and "text" in it
                and isinstance(it.get("labels", {}), dict)):
            raise ValueError(f"item {i}: each item needs an `id`, a `text` and an object of `labels`")
        sib = it.get("siblings")
        if it.get("subject") is not None and not isinstance(it["subject"], str):
            raise ValueError(f"item {i}: `subject` must be a string")
        if sib is not None and not (isinstance(sib, list) and all(isinstance(x, str) for x in sib)):
            raise ValueError(f"item {i}: `siblings` must be a list of strings")
    return spec


def fingerprint(it: dict, qs: dict) -> str:
    """What a journal row answered: the item's text and the questions asked, so a resume over
    an edited file or another --questions asks again instead of reusing a stale answer."""
    import hashlib
    return hashlib.sha256(json.dumps([it.get("text"), it.get("subject"), it.get("siblings"), qs],
                                     sort_keys=True, default=str).encode()).hexdigest()[:16]


def headline(answer: dict) -> Optional[float]:
    """The one number an answer gives a threshold: a noul's probability, else a score's
    position, else a choice's confidence."""
    v = answer.get("noul") if answer.get("type") == "noul" else answer.get("score", answer.get("confidence"))
    return float(v) if isinstance(v, (int, float)) else None


def lengths(items: List[dict]) -> Dict[str, int]:
    """The shape of the texts calibrated on, so a later caller asking much longer or shorter
    texts is told the threshold may not transfer."""
    n = sorted(len(it["text"] if isinstance(it["text"], str) else json.dumps(it["text"])) for it in items)
    return {"p10": n[len(n) // 10], "median": n[len(n) // 2], "p90": n[min(len(n) - 1, len(n) * 9 // 10)]} if n else {}


def workers_for(cfg: dict, t: str, asked: Optional[int]) -> int:
    """Parallel calls: the type's ceiling first (one laya worker, one device), then what the
    caller asked, then the entry's `workers`, then the type's own pool (one for a server on
    this machine, which queues, so more only add wait inside each call's timeout)."""
    ceiling = backends.TYPES[t].workers
    n = asked or cfg.get("workers") or backends.TYPES[t].pool(cfg)
    return min(n, ceiling) if ceiling else n


def measure(spec: dict, name: str, qs: dict, items: List[dict], runs: int, workers: int, caller: str,
            journal: Optional[Path] = None, resume: bool = False) -> Tuple[dict, List[int], List[str]]:
    """Ask each item `runs` times. -> (scores {id: {q: [values]}}, tokens per call, failures)."""
    scores: dict = {it["id"]: {q: [] for q in qs} for it in items}
    tokens: List[int] = []
    done: Dict[str, int] = {}
    lock = threading.Lock()
    prints = {it["id"]: fingerprint(it, qs) for it in items}
    if journal and resume and journal.exists():
        text = journal.read_text(encoding="utf-8")
        if text and not text.endswith("\n"):
            with open(journal, "a", encoding="utf-8") as f:
                f.write("\n")                # the interrupted row's tail, so the next row starts clean
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue                      # a line cut short by the interruption
            if not (isinstance(row, dict) and isinstance(row.get("id"), (str, int)) and isinstance(row.get("values"), dict)):
                continue                      # not a row this journal writes
            if row["id"] in scores and row.get("fp") == prints[row["id"]]:
                done[row["id"]] = done.get(row["id"], 0) + 1
                tokens.append(row["tokens"] if isinstance(row.get("tokens"), int) and row["tokens"] >= 0 else 0)
                for q, v in row["values"].items():
                    if q in scores[row["id"]] and isinstance(v, (int, float)) and not isinstance(v, bool):
                        scores[row["id"]][q].append(float(v))
    todo = [it for it in items for _ in range(max(0, runs - done.get(it["id"], 0)))]
    out = open(journal, "a", encoding="utf-8") if journal else None

    def one(it: dict) -> Optional[str]:
        try:
            r = client.ask(it["text"], qs, subject=it.get("subject"), siblings=it.get("siblings"), backend=name,
                           caller=caller)
        except (errors.AugurUnavailable, ValueError, OSError) as e:
            return f"{it['id']}: {e}"
        values = {q: headline(r["answers"].get(q) or {}) for q in qs}
        with lock:
            tokens.append(r["usage"]["input_tokens"])
            for q, v in values.items():
                if v is not None:
                    scores[it["id"]][q].append(v)
            if out:
                out.write(json.dumps({"id": it["id"], "fp": prints[it["id"]], "values": values,
                                      "tokens": r["usage"]["input_tokens"]}) + "\n")
                out.flush()
        return None

    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        failed_items = [it for it, e in zip(todo, ex.map(one, todo)) if e]
        failures = [e for e in ex.map(one, failed_items) if e]           # each failure asked once more
    except KeyboardInterrupt:
        ex.shutdown(wait=True, cancel_futures=True)  # stop paying: the queued calls never start
        raise
    finally:
        ex.shutdown(wait=True)
        if out:
            out.close()
    return scores, tokens, failures


def report(spec: dict, name: str, cfg: dict, qs: dict, items: List[dict], runs: int, scores: dict,
           tokens: List[int], failures: List[str], elapsed: float, compare: Optional[str],
           log: Callable[[str], Any], previous: Optional[dict] = None) -> dict:
    means = {id_: {q: (st.mean(v) if v else None) for q, v in d.items()} for id_, d in scores.items()}
    log(f"{guard.relay(spec.get('name', 'calibration'), 80)} on backend {name}: {len(items)} items x {runs} run(s), "
        f"{len(qs)} question(s), {sum(tokens):,} input tokens, ${sum(tokens) * float(cfg.get('price_per_mtok') or 0) / 1e6:.4f}, "
        f"{elapsed:.0f}s{', ' + str(len(failures)) + ' errors' if failures else ''}")
    for e in failures[:5]:
        log("  error " + guard.relay(e, 300))
    if failures:
        log(f"  {len(failures)} item(s) unanswered after a second try; the figures below leave them out")
    shape = lengths(items)
    if shape:
        log(f"  texts: median {shape['median']} chars, p10 {shape['p10']}, p90 {shape['p90']}; "
            f"a threshold measured here may not hold for texts far outside that range")
    classes = sorted({str(it.get("class", "?")) for it in items})
    prev = previous if isinstance(previous, dict) else None
    for q in qs:
        log(f"\n[{guard.relay(q, 60)}]")
        pos: list = [means[it["id"]][q] for it in items if it["labels"].get(q) is True and means[it["id"]][q] is not None]
        neg: list = [means[it["id"]][q] for it in items if it["labels"].get(q) is False and means[it["id"]][q] is not None]
        if pos and neg:
            log(f"  AUROC {_auroc(pos, neg):.3f}   positives n={len(pos)} min {min(pos):.2f} mean {st.mean(pos):.2f}"
                f"   negatives n={len(neg)} max {max(neg):.2f} mean {st.mean(neg):.2f}")
        for c in classes:
            v: list = [means[it["id"]][q] for it in items if str(it.get("class", "?")) == c and means[it["id"]][q] is not None]
            if v:
                v.sort()
                log(f"  {guard.relay(c, 10):10s} n={len(v):3d}  mean {st.mean(v):.2f}  min {v[0]:.2f}  med {v[len(v) // 2]:.2f}  max {v[-1]:.2f}")
        if runs > 1:
            sds = [st.pstdev(scores[it["id"]][q]) for it in items if len(scores[it["id"]][q]) > 1]
            if sds:
                log(f"  run-to-run sd: mean {st.mean(sds):.3f}  max {max(sds):.3f}  n>0.05 {sum(1 for s in sds if s > 0.05)}")
        if prev is not None and compare:
            d = [abs(means[i][q] - prev[i][q]) for i in means if isinstance(prev.get(i), dict) and q in prev[i]
                 and means[i][q] is not None and isinstance(prev[i][q], (int, float)) and not isinstance(prev[i][q], bool)]
            if d:
                log(f"  vs {guard.relay(Path(compare).name, 80)}: mean |diff| {st.mean(d):.3f}  max {max(d):.2f}  n={len(d)}")
    return means


def calibrate(spec: dict, *, backend: Optional[str] = None, runs: int = 1, limit: Optional[int] = None,
              questions: Optional[List[str]] = None, workers: Optional[int] = None, out_dir: Optional[str] = None,
              compare: Optional[str] = None, resume: bool = False, caller: str = "calibrate",
              log: Callable[[str], Any] = print, previous: Optional[dict] = None) -> dict:
    """Ask every item in isolation, `runs` times, and report per question. Returns the per-item means."""
    check_spec(spec)
    if compare and previous is None:
        from . import request
        previous = request.load(compare, "--compare")
    name, cfg, t = manifest.backend_entry(backend)
    qs = {k: v for k, v in spec["questions"].items() if not questions or k in questions}
    unknown = sorted(set(questions or []) - set(spec["questions"]))
    if unknown:
        raise ValueError(f"no question named {', '.join(map(repr, unknown))}; the file has {guard.relay(', '.join(spec['questions']), 300)}")
    items = spec["items"][:limit] if limit else spec["items"]
    journal = None
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        journal = Path(out_dir) / f"results-{name}.jsonl"
        if journal.exists() and not resume:
            journal.unlink()                  # a fresh run starts a fresh journal; --resume keeps it
    t0 = time.time()
    scores, tokens, failures = measure(spec, name, qs, items, runs, workers_for(cfg, t, workers), caller,
                                       journal, resume)
    means = report(spec, name, cfg, qs, items, runs, scores, tokens, failures, time.time() - t0, compare, log,
                   previous)
    if out_dir:
        with open(Path(out_dir) / f"means-{name}.json", "w", encoding="utf-8") as f:
            json.dump(means, f, indent=1)
        with open(Path(out_dir) / f"shape-{name}.json", "w", encoding="utf-8") as f:
            json.dump(lengths(items), f)
        log(f"\nper-item means: {Path(out_dir) / f'means-{name}.json'}")
    return means
