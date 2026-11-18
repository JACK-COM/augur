"""Measuring a backend on a labelled item set: per-question AUROC and per-item means, the
run that earns a threshold.
"""
import json
import os
import time
from pathlib import Path

from . import client
from . import errors
from . import manifest


def _auroc(pos, neg):
    """Probability a random positive outranks a random negative, ties half."""
    if not pos or not neg:
        return float("nan")
    n = 0.0
    for p in pos:
        for q in neg:
            n += 1 if p > q else 0.5 if p == q else 0
    return n / (len(pos) * len(neg))


def calibrate(spec, *, backend=None, runs=1, limit=None, questions=None, workers=6, out_dir=None, compare=None,
              caller="calibrate", log=print):
    """Ask every item in isolation, `runs` times, and report per question. Returns the per-item means."""
    import statistics as st
    from concurrent.futures import ThreadPoolExecutor
    name, cfg, t = manifest._backend(backend)
    qs = {k: v for k, v in spec["questions"].items() if not questions or k in questions}
    items = spec["items"][:limit] if limit else spec["items"]
    if t == "laya":
        workers = 1  # one worker process, one device
    t0 = time.time()
    scores = {it["id"]: {q: [] for q in qs} for it in items}
    tokens = []
    failed = []

    def one(it):
        try:
            r = client.ask(it["text"], qs, subject=it.get("subject"), siblings=it.get("siblings"), backend=name, caller=caller)
        except errors.AugurUnavailable as e:
            failed.append(f"{it['id']}: {e}")
            return
        tokens.append(r["usage"]["input_tokens"])  # append is atomic under threads, += is not
        for q in qs:
            a = r["answers"].get(q, {})
            v = a.get("noul") if a.get("type") == "noul" else a.get("score", a.get("confidence"))
            if v is not None:
                scores[it["id"]][q].append(float(v))

    for _ in range(runs):
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(one, items))
    elapsed = time.time() - t0
    means = {id_: {q: (st.mean(v) if v else None) for q, v in d.items()} for id_, d in scores.items()}
    log(f"{spec.get('name', 'calibration')} on backend {name}: {len(items)} items x {runs} run(s), "
        f"{len(qs)} question(s), {sum(tokens):,} input tokens, ${sum(tokens) * float(cfg.get('price_per_mtok') or 0) / 1e6:.4f}, "
        f"{elapsed:.0f}s{', ' + str(len(failed)) + ' errors' if failed else ''}")
    for e in failed[:5]:
        log("  error " + e)
    classes = sorted({it.get("class", "?") for it in items})
    for q in qs:
        log(f"\n[{q}]")
        pos = [means[it["id"]][q] for it in items if it["labels"].get(q) is True and means[it["id"]][q] is not None]
        neg = [means[it["id"]][q] for it in items if it["labels"].get(q) is False and means[it["id"]][q] is not None]
        if pos and neg:
            log(f"  AUROC {_auroc(pos, neg):.3f}   positives n={len(pos)} min {min(pos):.2f} mean {st.mean(pos):.2f}"
                f"   negatives n={len(neg)} max {max(neg):.2f} mean {st.mean(neg):.2f}")
        for c in classes:
            v = [means[it["id"]][q] for it in items if it.get("class") == c and means[it["id"]][q] is not None]
            if v:
                v.sort()
                log(f"  {c:10s} n={len(v):3d}  mean {st.mean(v):.2f}  min {v[0]:.2f}  med {v[len(v) // 2]:.2f}  max {v[-1]:.2f}")
        if runs > 1:
            sds = [st.pstdev(scores[it["id"]][q]) for it in items if len(scores[it["id"]][q]) > 1]
            if sds:
                log(f"  run-to-run sd: mean {st.mean(sds):.3f}  max {max(sds):.3f}  n>0.05 {sum(1 for s in sds if s > 0.05)}")
        if compare:
            prev = json.loads(Path(compare).read_text(encoding="utf-8"))
            d = [abs(means[i][q] - prev[i][q]) for i in means if i in prev and q in prev[i] and means[i][q] is not None and prev[i][q] is not None]
            if d:
                log(f"  vs {Path(compare).name}: mean |diff| {st.mean(d):.3f}  max {max(d):.2f}  n={len(d)}")
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(Path(out_dir) / f"means-{name}.json", "w", encoding="utf-8") as f:
            json.dump(means, f, indent=1)
        log(f"\nper-item means: {Path(out_dir) / f'means-{name}.json'}")
    return means
