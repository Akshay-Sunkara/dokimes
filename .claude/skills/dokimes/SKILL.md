---
name: dokimes
description: Run an autonomous improvement loop on the browser agent — find a failure class, search the literature for how it has been solved, make one change, sweep the eval, keep it only if the pass rate improves significantly, repeat. Use when asked to "run dokimes", "improve the agent", or "run N cycles".
---

# Dokimes

You are the loop. Nothing calls you between cycles — you run the eval yourself,
read your own results, edit the agent, and re-run. Default 20 cycles unless the
user says otherwise.

**Metric**: pass rate on the train split, higher is better. A change survives only
when the paired bootstrap CI excludes zero.

## Setup (once, at the start)

```bash
git status --porcelain                     # must be clean; if not, stop and ask
git checkout -b dokimes/<short-tag>        # never work on main or dokimes
```

Create `ledger.tsv` if absent (it is gitignored — it is your memory across cycles):

```
commit	variant	pass_rate	delta	ci_low	ci_high	status	description
```

Establish the baseline before changing anything:

```bash
.venv/bin/python -m eval --variant base --tasks eval/train.jsonl --runs 3 > run.log 2>&1
tail -40 run.log
```

## Each cycle

**1. Pick ONE change.** Read the failures first — never guess:

```bash
.venv/bin/python -c "
import json,collections
rows=[json.loads(l) for l in open('eval/results.jsonl') if l.strip()]
bad=[r for r in rows if r.get('verdict') is False]
for r in bad[:20]: print(r['id'][:12], '|', (r.get('failure_reason') or '')[:150])
"
```

Group those reasons into causes yourself. Do NOT cluster embeddings — that was
tried and produced categories nobody could act on. Read them, name concrete
categories, drop the small ones, subdivide the big ones.

For a cause worth understanding in depth, open one trace:

```bash
.venv/bin/python -m eval.traces <run_id>     # step-by-step: code run, what it saw
```

**2. Search the literature for how this failure has been solved.** You are not the
first person to hit it — web agents are a heavily published area, and the good
ideas are mostly already written down. Do this *before* inventing a fix.

Name the failure in the vocabulary of the field, then search arXiv:

- WebSearch: `arxiv web agent <failure class> 2026` — e.g. "arxiv web agent
  recovery from failed action", "arxiv LLM agent long horizon memory",
  "arxiv web agent grounding element selection"
- WebFetch `https://arxiv.org/abs/<id>` for the abstract, or
  `https://arxiv.org/html/<id>` for full text when a paper looks directly on point

Read 2-4 abstracts, then the one paper closest to the failure. What you are after
is the *mechanism* — a planning structure, a memory format, a verification step, a
retry policy — not the benchmark numbers. Papers report gains under their own
setup; that number tells you nothing about yours. The eval decides, not the paper.

Cite what you drew on in the ledger description (`arxiv:2504.01382 self-verify
before done`) so a later cycle can tell which ideas came from where, and so a
repeated failure does not send you back to the same paper twice.

If nothing relevant turns up in a few minutes, stop searching and reason from the
traces. The literature is a source of hypotheses, not a prerequisite for a cycle.

**3. Make the change.** Only these files are in scope:

- `agent/agent.py` — the SYSTEM prompt, MODEL
- `agent/helpers.py` — browser helpers
- `agent/run.py` — the execution namespace

Everything under `eval/` is off limits and permission-denied. If a change seems to
require editing the judge, the task set, or the report, that is a signal you are
about to game the metric — pick a different change.

**4. Commit before running**, so a crash still leaves a record:

```bash
git add -A && git commit -q -m "cycle N: <description>"
```

**5. Sweep.** Always redirect — a sweep prints every step of every run and will
bury your context:

```bash
.venv/bin/python -m eval --variant <tag> --tasks eval/train.jsonl --runs 3 > run.log 2>&1
tail -40 run.log
```

**6. Read the verdict.** The scoreboard prints the comparison:

```
  <tag> vs base on 20 shared tasks
    +12.5%  95% CI [+2.5%, +22.5%]  P(better)=0.98  -> significant
```

**7. Guard.** The sweep must not have introduced harness errors — check the
`harness errors N` line in the scoreboard. If it rose above baseline, the change
broke the agent even if the pass rate looks fine. Rework it (max 2 attempts),
then discard.

**8. Keep or discard.**

- CI excludes zero, guard passes → keep the commit
- CI spans zero → `git reset --hard HEAD~1`, status `discard`
- Small gain bought with ugly complexity → discard anyway
- Same score but less code → keep

Append the row to `ledger.tsv` either way. Discards are data — they stop you
retrying the same idea in cycle 14.

## Rules

**One change per cycle.** Two changes and you cannot attribute the result.

**Make big bets.** Small tweaks get lost in run-to-run variance and will read as
"not significant" every time. Prompt rewrites, new helpers, changed control flow —
not word-level edits.

**Never touch `eval/test.jsonl`.** It exists to check that your improvements
generalize, and it is worthless the moment the loop has seen it. The user runs it,
not you.

**Generalize, don't special-case.** The natural failure mode of this loop is
overfitting to individual tasks. A change that names a specific site or task is
almost always wrong — fix the class of failure, not the instance.

**Never fabricate a result.** If a sweep crashes, record the crash and move on.

## Reporting

Every 5 cycles, print a short summary: cycles run, kept, discarded, pass rate
from baseline to now, and what the surviving changes were. At the end, print the
ledger and leave the branch at the best result.
