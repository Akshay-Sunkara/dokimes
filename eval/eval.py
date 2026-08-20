"""run the agent over the WebVoyager tasks, n times each!!"""

import asyncio
import collections
import hashlib
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from eval import traces

HERE = Path(__file__).resolve().parent
TASKS = HERE / "mind2web.jsonl"
RESULTS = HERE / "results.jsonl"
TRACES = traces.RUNS_DIR
RUNS = 3
TIMEOUT = 300
VARIANT = "base"  # label for the agent config under test; see --variant

import agent.run
from agent.agent import main


def load(site=None, path=None, level=None):
    path = Path(path) if path else TASKS
    tasks = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    if site:
        tasks = [t for t in tasks if t["web_name"].lower().startswith(site.lower())]
    if level:
        tasks = [t for t in tasks if t.get("level") == level]
    return tasks


def sample(tasks, n, seed=0):
    """Stratified random sample of n tasks, preserving the difficulty mix.

    --n takes the head of the file, and the file is grouped by site: the first 20
    train tasks are half easy and 14 of them come from four domains. A sweep on
    that measures those four sites. Keep the seed fixed across variants — the
    comparison only uses tasks both arms actually ran.
    """
    if n >= len(tasks):
        return tasks

    by_level = collections.defaultdict(list)
    for task in tasks:
        by_level[task.get("level") or "?"].append(task)

    # largest remainder, so the quotas sum to exactly n
    exact = {lvl: n * len(g) / len(tasks) for lvl, g in by_level.items()}
    quota = {lvl: int(v) for lvl, v in exact.items()}
    for lvl in sorted(by_level, key=lambda l: exact[l] - quota[l], reverse=True):
        if sum(quota.values()) >= n:
            break
        quota[lvl] += 1

    rng = random.Random(seed)
    picked = []
    for lvl in sorted(by_level):
        picked += rng.sample(by_level[lvl], min(quota[lvl], len(by_level[lvl])))
    rng.shuffle(picked)  # don't hand the sweep one difficulty at a time
    return picked


def task_file(name):
    """--tasks accepts a bare name (mind2web) or a path to a .jsonl."""
    candidate = Path(name)
    if candidate.exists():
        return candidate
    named = HERE / f"{name}.jsonl"
    if named.exists():
        return named
    raise SystemExit(f"no task file {name!r} (looked for {candidate} and {named})")


def done():
    if not RESULTS.exists():
        return set()
    seen = set()
    for line in RESULTS.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            seen.add((r["id"], r["run"], r.get("variant", VARIANT)))
    return seen


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def _fingerprint():
    """What the agent actually was, independent of the label given to it.

    A variant name is a claim; this is evidence. Without it a mislabelled sweep
    is indistinguishable from a real effect.
    """
    from agent import agent as agent_module

    return {
        "agent_model": getattr(agent_module, "MODEL", None),
        "system_sha": hashlib.sha256(
            (getattr(agent_module, "SYSTEM", "") or "").encode()
        ).hexdigest()[:12],
    }


def _trace_facts(run_id):
    """Pull what the trace knows about a finished run."""
    records = traces.read(run_id)
    entries = traces.steps(run_id)
    end = next((r for r in records if r["event"] == "run_end"), {})
    err = next((r for r in records if r["event"] == "run_error"), {})
    return {
        "subtype": end.get("subtype"),
        "is_error": end.get("is_error"),
        "turns": end.get("turns"),
        "cost_usd": end.get("cost_usd"),
        "duration_ms": end.get("duration_ms"),
        "steps": len(entries),
        "step_errors": sum(
            1
            for e in entries
            if e.get("is_error") or "Traceback (most recent call last)" in (e.get("output") or "")
        ),
        "unfinished": sum(1 for r in records if r["event"] == "unfinished"),
        "shots": [r["path"] for r in records if r["event"] == "shot"],
        "trace_error": err.get("error"),
    }


async def one(task, index, n, variant=VARIANT):
    # run-<variant>-<task>-<subrun>: the variant is in the id so two arms of the
    # same experiment write separate traces instead of overwriting each other.
    run_id = f"run-{_slug(variant)}-{index}-{n}"
    prompt = f"{task['ques']}\n\nStart at: {task['web']}"

    agent.run._ns.clear() 
    traces.new_run_id = lambda: run_id  

    record = {
        "id": task["id"],
        "web_name": task["web_name"],
        "ques": task["ques"],
        "web": task["web"],
        "run": n,
        "run_id": run_id,
        "variant": variant,
        **_fingerprint(),
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    clock = time.monotonic()
    try:
        record["answer"] = await asyncio.wait_for(main(prompt), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        record["error"] = f"timeout after {TIMEOUT}s"
    except Exception as e:
        record["error"] = f"{type(e).__name__}: {e}"

    record["wall_s"] = round(time.monotonic() - clock, 1)
    record["trace"] = str(TRACES / run_id / f"{run_id}.jsonl")
    record.update(_trace_facts(run_id))
    return record


async def sweep(tasks, runs=RUNS, variant=VARIANT):
    already = done()
    total = len(tasks) * runs
    i = 0
    for index, task in enumerate(tasks, 1):
        for n in range(1, runs + 1):
            i += 1
            head = f"[{i}/{total}] {task['id']} run {n}/{runs} [{variant}]"
            if (task["id"], n, variant) in already:
                print(f"{head}  skip")
                continue

            print(f"\n{'=' * 70}\n{head}\n{task['ques']}\n{'=' * 70}")
            record = await one(task, index, n, variant)
            with RESULTS.open("a") as f:
                f.write(json.dumps(record) + "\n")
            print(
                f"-> {record.get('answer') or record.get('error')}\n"
                f"   {record.get('steps')} steps  {record.get('turns')} turns  "
                f"{record['wall_s']}s  ${record.get('cost_usd') or 0:.4f}"
            )


if __name__ == "__main__":
    args = sys.argv[1:]
    flags = dict(a.lstrip("-").split("=", 1) for a in args if "=" in a)
    site = next((a for a in args if "=" not in a), None)

    runs = int(flags.get("runs", RUNS))
    tasks = load(site)
    if "n" in flags:
        tasks = tasks[: int(flags["n"])]

    print(f"{len(tasks)} tasks x {runs} runs = {len(tasks) * runs} -> {RESULTS}")
    for t in tasks:
        print(f"  {t['id']:<28} {t['ques'][:80]}")
    if flags.get("dry"):
        sys.exit(0)
    asyncio.run(sweep(tasks, runs))
