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

All work happens on `experiments`. Never commit to `main`.

```bash
git status --porcelain          # must be clean; if not, stop and ask

# resume the branch if it already exists, otherwise start it from main
git rev-parse --verify experiments >/dev/null 2>&1 \
  && git checkout experiments \
  || git checkout -b experiments
```

If `experiments` already has cycles on it, read `ledger.tsv` and `git log --oneline
main..experiments` before planning anything — you are continuing a run, not
starting one, and repeating an idea already recorded there wastes a cycle.

Create `ledger.tsv` if absent (it is gitignored — it is your memory across cycles):

```
commit	variant	pass_rate	delta	ci_low	ci_high	status	description
```

Establish the baseline before changing anything. `--sample` draws a random,
difficulty-stratified set; `--n` would take the head of the file, which is grouped
by site and skews easy. Use whatever sample size the user asked for, and keep it
and the seed identical for every cycle that follows:

```bash
.venv/bin/python -m eval --variant base --tasks eval/train.jsonl \
  --sample 20 --seed 0 --runs 3 > run.log 2>&1
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

**2. Find out how this failure has already been solved.** You are not the first
person to hit it. Do this *before* inventing a fix.

Search two sources, and prefer the second when they disagree.

**Papers — for the mechanism.** Name the failure in the field's vocabulary, then:

- WebSearch: `arxiv web agent <failure class> 2026` — e.g. "arxiv web agent
  recovery from failed action", "arxiv LLM agent long horizon memory",
  "arxiv web agent grounding element selection"
- WebFetch `https://arxiv.org/abs/<id>` for the abstract, or
  `https://arxiv.org/html/<id>` for full text when a paper is directly on point

Read 2-4 abstracts, then the closest paper. Take the *mechanism* — a planning
structure, a memory format, a verification step, a retry policy.

**Production code — for what survives contact with the real web.** Papers optimize
for a benchmark; shipped agents handle CAPTCHAs, shadow DOM, iframes, lazy loading
and anti-bot measures, and their repos record which ideas actually held up. Read
how serious teams solved it:

- `browser-use/browser-use` — the closest system to this one: a Python browser
  agent with a code-execution tool. Its `browser_use/` source, `CLAUDE.md`, and
  `examples/` are the highest-value reading for almost any failure here.
- `browser-use/benchmark`, `OSU-NLP-Group/Online-Mind2Web` — the eval and judge
  designs this harness already borrows from
- `microsoft/playwright`, `Skyvern-AI/skyvern`, `web-arena-x/webarena`,
  `openai/openai-agents-python`, `anthropics/anthropic-quickstarts`
- WebSearch: `github <failure class> browser agent` to find others

Read source and issues, not just READMEs — a closed issue describing the same
symptom is often worth more than a paper. WebFetch works on
`https://raw.githubusercontent.com/<owner>/<repo>/main/<path>` for a specific file.

**Neither source decides anything.** A paper's reported gain and a company's design
choice are both hypotheses about *their* setup, not evidence about yours. The eval
decides. Never keep a change because a respected source endorsed it.

Cite what you drew on in the ledger description — `arxiv:2504.01382 self-verify
before done`, or `browser-use/browser-use dom serialization` — so a later cycle can
see which ideas came from where, and a repeated failure does not send you back to
the same source twice.

If nothing relevant turns up in a few minutes, stop searching and reason from the
traces. This is a source of hypotheses, not a prerequisite for a cycle.

**3. Make the change.** Only these files are in scope:

- `agent/agent.py` — the SYSTEM prompt, MODEL
- `agent/helpers.py` — browser helpers
- `agent/run.py` — the execution namespace

Everything under `eval/` is off limits and permission-denied. If a change seems to
require editing the judge, the task set, or the report, that is a signal you are
about to game the metric — pick a different change.

**4. Commit before running**, so a crash still leaves a record. Write the message
as a sentence about what you just tried, in the voice described under *Writing*
below:

```bash
git add -A && git commit -q -m "i tried making the agent verify the page before it finishes"
```

The result is not known yet, so the message says only what you attempted. You will
amend it in step 8 once you know whether it worked.

**5. Sweep.** Use the SAME `--sample` and `--seed` as the baseline in every cycle —
the comparison only counts tasks both arms actually ran, so changing either one
silently shrinks it. Always redirect; a sweep prints every step of every run and
will bury your context:

```bash
.venv/bin/python -m eval --variant <tag> --tasks eval/train.jsonl \
  --sample 20 --seed 0 --runs 3 > run.log 2>&1
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

**Write every cycle up in `experiments.md`** — keeps and discards both. This file
is committed, so it is the record anyone reads on GitHub, and a failed experiment
is worth as much there as a successful one. Append one entry, newest last:

```markdown
## cycle 3 — i taught the agent to check its work before finishing

i kept seeing runs where the agent said it was done while the page still showed
the unfiltered results, so i made it re-read the page and confirm every part of
the task before it can finish.

source: arxiv:2504.01382 — webjudge identifies the key points of a task and
checks each one against the page, so i had the agent do the same to itself.
result: +12.5% [+2.5%, +22.5%] — kept
```

Every entry needs a **source** line naming where the idea came from — an arXiv id
or a repo and what you took from it — and one sentence on why it applied here. If
an idea genuinely came from reading traces rather than a source, say that:
`source: none — i got this from reading the traces on cycle 3`.

**On a keep**, fold the write-up into the same commit and amend the message with
the outcome:

```bash
git add -A
git commit -q --amend -m "i tried making the agent verify the page before it finishes, and it worked"
```

**On a discard**, reset first so the code change is gone, *then* write the entry and
commit it on its own. Order matters — `reset --hard` would throw away the write-up
along with the change:

```bash
git reset --hard HEAD~1          # drops the change and its commit
# now append the discard entry to experiments.md
git add -A
git commit -q -m "i tried giving the agent a scratchpad for long tasks, but it made no difference"
```

Either way the branch gains exactly one commit per cycle, and `experiments.md`
records what happened. Then push:

```bash
git push -u origin experiments
```

Never push before the decision. A discarded cycle is erased with `git reset --hard
HEAD~1`, and if that commit is already on the remote the branch can only be
repaired with a force push. Pushing after the decision means `origin/experiments`
contains kept cycles only, and always fast-forwards.

## Writing

Everything you write as prose — commit messages, `experiments.md`, `ledger.tsv`
descriptions, the summaries you print — follows the same voice:

- **all lowercase.** never capitalise a sentence. code, identifiers, file paths and
  arxiv ids keep whatever case they actually have — you are not lowercasing code.
- **first person.** you ran the experiment, so say so: "i tried", "i noticed",
  "i was wrong about". not "the agent was modified" and not "we".
- **full sentences, but short.** a commit message is one sentence. an
  `experiments.md` entry is two or three. say what you tried and why, then stop.
- **plain and conversational.** "i kept seeing runs where the agent said it was
  done while the page still showed old results" beats "addressed premature
  termination in the completion path".
- **honest about failures.** a discarded cycle is written up the same way: "i
  thought giving it a scratchpad would help it keep track of long tasks, but it
  made no difference."

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
