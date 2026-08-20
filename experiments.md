# experiments

i am running a loop on this browser agent: find a failure class, look up how
other people solved it, make one change, sweep the eval, keep it only if the
paired bootstrap ci excludes zero. every sweep below is 30 train tasks,
`--seed 0 --runs 1`, three chrome shards.

baseline: 6.7% (2/30), with 15 of the 30 runs hitting the 300s timeout.

## cycle 1 — i gave the agent a numbered map of the page's clickable elements

reading the traces, almost every run was the same loop: guess a coordinate,
`click_at_xy`, screenshot, look at the picture, guess again. half the steps in a
run were screenshots, and half the runs ran out of time before finishing. so i
wrote `page_map()`, which returns every clickable element in the viewport as a
numbered line with its role, its text and its state, plus `click_index`,
`fill_index` and `select_index` that act on the number, and `page_text()` for
reading the page as exact text. then i rewrote the system prompt so read → act
by number → read is the loop it reaches for first.

source: browser-use/browser-use's dom serializer, which numbers interactive
elements and has the model act by index rather than by coordinate, and
arxiv:2508.04412 (d2snap), which measures a text dom snapshot at 73% against 65%
for a grounded screenshot and finds images add almost nothing over text — which
matched what the traces looked like.
result: +20.0% [+3.3%, +36.7%], 6.7% -> 26.7% — kept. timeouts fell from 15 runs
to 7 and the median run went from 296s to 176s.

## cycle 2 — i made the agent write down what the page must show, and check it

most of what was left after cycle 1 was the judge saying the final answer was
fabricated or never verified — filters that silently reset, a search that kept
its default city, a list of apartments nobody had read off the page. so i added
`plan()`, where the agent commits up front to the literal strings the finished
page will contain, and `check()`, which greps the live page for each one and
prints MISSING when it isn't there.

source: arxiv:2607.24167 (falsifiable commitment planning), which has the agent
commit to confirming and falsifying evidence per step, and arxiv:2504.01382,
whose judge scores a run against key points extracted from the task — i pointed
the same idea at the agent instead of at the judge.
result: -3.3% [-10.0%, +0.0%] against cycle 1 — discarded. it used plan() and
check() in 19 of the 30 runs, so it wasn't ignored; it just didn't convert.
check() told the agent a filter was missing and the agent still couldn't apply
it, which says the bottleneck is applying the filter, not noticing it failed.

## cycle 3 — i made actions report what changed, and found typing was broken

reading a thumbtack trace, the agent clicked the zip box, typed 10001, got no
feedback, and every search it ran afterwards still used the default zip 76248.
so i made click_index/fill_index/select_index wait for the page to settle and
return the diff — new lines, new url, and for a fill, the value the field
actually holds now.

the read-back immediately caught a real bug. typing into a field that already
had something in it did nothing at all: the clear step never landed, and the zip
box has maxlength=5, so with "76248" already there every keystroke was silently
dropped. focus + setSelectionRange + insert replaces the contents and works.

source: browser-use/browser-use's tools/service.py, which reads the field back
after typing and warns when the actual value differs from what was typed — that
one line of theirs is what made the bug visible here.
result: +6.7% [-10.0%, +23.3%] against cycle 1, 26.7% -> 33.3% — discarded, the
ci spans zero. i think the typing fix in here is real and the change-reporting is
what cost the steps, so i am going to try them separately rather than keep a
bundle the eval cannot resolve.

## cycle 4 — i shipped the typing fix on its own

cycle 3 bundled two things, so i split it and re-ran just the part i had watched
fail by hand: fill_index now selects whatever the field holds before typing, so
the insert replaces it instead of being appended to a field that is already at
its maxlength, and it reads the field back and says what it actually holds.

source: none for the fix itself — i got it from stepping through the thumbtack
zip box by hand in cycle 3. the read-back that exposed it came from
browser-use/browser-use's tools/service.py.
result: +10.0% [-3.3%, +23.3%] against cycle 1, 26.7% -> 36.7% — discarded, the
ci spans zero again. steps per run dropped from 45 to 38 and timeouts from 7 to
4, which is what i would expect if the fix is real, but at 30 tasks and one run
each the interval is about ±13% wide, so nothing smaller than that can be seen
from here. i am recording it as a discard and taking bigger swings.

## cycle 5 — i swapped the model from haiku to sonnet

three cycles in a row i had changes with good point estimates that the interval
could not separate from zero, so i went for the biggest lever the rules leave me:
MODEL, which sits right at the top of agent.py. everything else stayed exactly as
cycle 1 left it.

source: none — this is not an idea from anywhere, it is the one knob i had not
turned, and i wanted to know how much of what was left is the model rather than
the scaffolding.
result: +26.7% [+10.0%, +46.7%] against cycle 1, 26.7% -> 53.3% — kept. it is
also cheaper per task finished than it looks: 32 steps a run instead of 45, a
median run of 101s instead of 176s, and 5 timeouts instead of 7. the sweep costs
about twice as much, $7.56 against $3.94.

it is worth being blunt about what this says. the scaffolding work in cycles 1-4
moved the agent from 6.7% to somewhere around 27-37%, and one line moved it
another 27 points. the two are not in competition — sonnet is running on the
element map from cycle 1 — but a better model was the largest single thing
available, and i had been spending cycles on smaller ones.

## cycle 6 — i tried the action-reporting change again, on sonnet

cycle 3 bundled action-reporting with the typing fix and the interval could not
separate it from zero on haiku, so i brought the whole thing back on top of
sonnet to see whether a model that actually reads its tool output would get more
out of it. i picked the compass run to justify it: the agent clicked "Price",
wrote "with ascending sort already applied", and stopped — 21 steps and 66
seconds into a 300 second budget — without ever confirming the sort took.

source: browser-use/browser-use tools/service.py again, same read-back idea as
cycle 3.
result: +0.0% [-16.7%, +16.7%] against cycle 5 — discarded, and this one is a
clean no rather than an underpowered maybe. sonnet was already ending almost
every action with its own page_map() call, so folding the observation into the
action gave it something it was already going and getting. the thing i actually
wanted to fix — it stops before confirming the effect — is not an observation
problem, it is a stopping problem.

## cycle 7 — i gave the agent a clock and told it not to stop early

cycle 6 said the problem was not that the agent could not see what changed, so i
went at the stopping itself: a time_left() helper, and a section of the prompt
that says a sort control clicked is not a list sorted, that it should go back and
read each requirement off the page before calling the task done, and that a short
honest "i could not do this part" beats a confident summary of something that did
not happen.

source: none — this came from the sonnet traces, where run after run stopped at
around 30 steps and 100 seconds of a 300 second budget.
result: +3.3% [-10.0%, +16.7%] against cycle 5 — discarded. it did move the run
longer, 101s to 122s median, and timeouts stayed flat at 3, so the agent did
spend more of its budget; it just did not convert that into more tasks passed.
telling it to check more is apparently not the same as it checking the right
thing.
