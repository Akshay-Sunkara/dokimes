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
