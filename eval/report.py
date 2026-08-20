import argparse
import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results.jsonl"


def load():
    """Every run row. The judge writes its verdict onto these same rows."""
    if not RESULTS.exists():
        return []
    return [json.loads(l) for l in RESULTS.read_text().splitlines() if l.strip()]


def bootstrap(per_task, iters=10000, seed=0):
    """95% CI on the mean pass rate, resampling tasks (the unit of independence)."""
    if not per_task:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(per_task)
    means = []
    for _ in range(iters):
        sample = [per_task[rng.randrange(n)] for _ in range(n)]
        means.append(statistics.fmean(sample))
    means.sort()
    return (means[int(0.025 * iters)], means[int(0.975 * iters)])


def rates_by_task(rows, variant=None):
    """Per-task pass rate, the unit everything downstream resamples."""
    per_task = defaultdict(list)
    for row in rows:
        if "verdict" not in row:
            continue
        if variant is not None and row.get("variant", "base") != variant:
            continue
        per_task[row["id"]].append(bool(row["verdict"]))
    return {tid: statistics.fmean(v) for tid, v in per_task.items()}


def compare(rows, a, b, iters=10000, seed=0):
    """Paired bootstrap on the per-task pass-rate difference, b minus a.

    Both arms run the same tasks, so pairing cancels task difficulty out of the
    variance — the same reason a before/after measurement is paired rather than
    treated as two independent samples. Unpaired here would widen the interval
    enough to hide any effect a small sweep could produce.

    Returns (diff, lo, hi, prob_better, n_tasks) or None when the arms share no
    tasks. `prob_better` is the share of resamples where b beat a; it is a
    bootstrap proportion, not a frequentist p-value.
    """
    ra, rb = rates_by_task(rows, a), rates_by_task(rows, b)
    shared = sorted(set(ra) & set(rb))
    if not shared:
        return None

    deltas = [rb[t] - ra[t] for t in shared]
    observed = statistics.fmean(deltas)

    rng = random.Random(seed)
    n = len(deltas)
    means = []
    wins = 0
    for _ in range(iters):
        sample = [deltas[rng.randrange(n)] for _ in range(n)]
        m = statistics.fmean(sample)
        means.append(m)
        wins += m > 0
    means.sort()
    return observed, means[int(0.025 * iters)], means[int(0.975 * iters)], wins / iters, n


def _pct(x):
    return f"{100 * x:5.1f}%"


def summarize(rows):
    by_task = defaultdict(list)
    for r in rows:
        by_task[r["id"]].append(r)

    judged = [r for r in rows if "verdict" in r]
    unjudged = [r for r in rows if "verdict" not in r]

    task_rates = []
    pass_k, pass_at_k = 0, 0
    for tid, runs in sorted(by_task.items()):
        v = [bool(r.get("verdict")) for r in runs if "verdict" in r]
        if not v:
            continue
        task_rates.append(statistics.fmean(v))
        pass_k += all(v)
        pass_at_k += any(v)

    lo, hi = bootstrap(task_rates)
    n_tasks = len(task_rates)

    def s(key):
        vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        return vals

    out = []
    out.append("=" * 74)
    out.append("EVAL SCOREBOARD")
    out.append("=" * 74)
    out.append(f"tasks {n_tasks}   runs {len(rows)}   judged {len(judged)}   unjudged {len(unjudged)}")
    if n_tasks:
        out.append("")
        out.append(f"  pass rate (all runs)   {_pct(statistics.fmean(task_rates))}   95% CI [{_pct(lo).strip()}, {_pct(hi).strip()}]")
        out.append(f"  pass^k (every run)     {_pct(pass_k / n_tasks)}   {pass_k}/{n_tasks} tasks")
        out.append(f"  pass@k (any run)       {_pct(pass_at_k / n_tasks)}   {pass_at_k}/{n_tasks} tasks")

    errors = [r for r in rows if r.get("error")]
    captcha = [r for r in judged if r.get("reached_captcha")]
    impossible = [r for r in judged if r.get("impossible_task")]
    judge_errs = [r for r in rows if r.get("judge_error")]
    out.append("")
    out.append(f"  harness errors {len(errors)}   captcha {len(captcha)}   impossible {len(impossible)}   judge errors {len(judge_errs)}")

    cost, wall, steps, turns = s("cost_usd"), s("wall_s"), s("steps"), s("turns")
    if cost:
        out.append(f"  cost  ${sum(cost):.4f} total   ${statistics.fmean(cost):.4f}/run")
    if wall:
        out.append(f"  wall  {sum(wall) / 60:.1f} min total   {statistics.fmean(wall):.0f}s/run   p50 {statistics.median(wall):.0f}s")
    if steps:
        out.append(f"  steps {statistics.fmean(steps):.1f}/run   turns {statistics.fmean(turns) if turns else 0:.1f}/run")

    by_site = defaultdict(list)
    for r in judged:
        by_site[r["web_name"]].append(bool(r.get("verdict")))
    if len(by_site) > 1:
        out.append("")
        out.append("BY SITE")
        for site, v in sorted(by_site.items(), key=lambda kv: -statistics.fmean(kv[1])):
            out.append(f"  {site:<22} {_pct(statistics.fmean(v))}  ({sum(v)}/{len(v)})")

    variants = sorted({r.get("variant", "base") for r in judged})
    if len(variants) > 1:
        out.append("")
        out.append("BY VARIANT")
        for name in variants:
            per_task = rates_by_task(rows, name)
            runs_n = sum(1 for r in judged if r.get("variant", "base") == name)
            if per_task:
                lo_v, hi_v = bootstrap(list(per_task.values()))
                out.append(
                    f"  {name:<22} {_pct(statistics.fmean(per_task.values()))}  "
                    f"95% CI [{_pct(lo_v).strip()}, {_pct(hi_v).strip()}]  "
                    f"{len(per_task)} tasks, {runs_n} runs"
                )

        base = "base" if "base" in variants else variants[0]
        for name in variants:
            if name == base:
                continue
            result = compare(rows, base, name)
            if result is None:
                out.append(f"\n  {name} vs {base}: no shared tasks, not comparable")
                continue
            diff, lo_d, hi_d, prob, n_shared = result
            verdict = "significant" if lo_d > 0 or hi_d < 0 else "NOT significant (CI spans 0)"
            out.append("")
            out.append(f"  {name} vs {base} on {n_shared} shared tasks")
            out.append(
                f"    {diff:+.1%}  95% CI [{lo_d:+.1%}, {hi_d:+.1%}]  "
                f"P(better)={prob:.2f}  -> {verdict}"
            )

    out.append("")
    out.append("BY TASK")
    for tid, runs in sorted(by_task.items()):
        v = [bool(r.get("verdict")) for r in runs if "verdict" in r]
        marks = "".join("." if "verdict" not in r else ("P" if r.get("verdict") else "F") for r in runs)
        rate = _pct(statistics.fmean(v)) if v else "    -"
        out.append(f"  {tid:<26} {marks:<6} {rate}")
        for r in runs:
            why = r.get("failure_reason") or r.get("error") or r.get("judge_error")
            if why and not r.get("verdict"):
                flags = "".join(
                    f" [{f}]" for f in ("impossible_task", "reached_captcha") if r.get(f)
                )
                out.append(f"      r{r['run']}:{flags} {' '.join(str(why).split())[:170]}")
    return "\n".join(out)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="aggregate judged runs")
    p.add_argument("--json", action="store_true", help="dump the merged rows as JSON instead")
    args = p.parse_args()
    rows = load()
    if not rows:
        raise SystemExit(f"nothing in {RESULTS}")
    print(json.dumps(rows, indent=2) if args.json else summarize(rows))
