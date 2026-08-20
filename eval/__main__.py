"""
    python -m eval --n 2 --runs 1
"""

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def cli():
    p = argparse.ArgumentParser(prog="python -m eval", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n", type=int, help="only the first N tasks after filtering (head, not a sample)")
    p.add_argument("--sample", type=int,
                   help="random N tasks, stratified by level; use for experiments")
    p.add_argument("--seed", type=int, default=0,
                   help="sampling seed — keep fixed across variants (default 0)")
    p.add_argument("--runs", type=int, default=1, help="runs per task (default 1)")
    p.add_argument("--variant", default="base",
                   help="label this sweep's agent config, so arms can be compared")
    p.add_argument("--tasks", default="mind2web",
                   help="task set: mind2web, or a path to a .jsonl")
    p.add_argument("--site", help="filter tasks by web_name prefix, e.g. bestbuy")
    p.add_argument("--level", choices=("easy", "medium", "hard"),
                   help="filter by difficulty: easy/medium/hard")
    p.add_argument("--ids", help="comma-separated task ids")
    p.add_argument("--timeout", type=int, help="per-run timeout in seconds")
    p.add_argument("--judge-model", default=None, help="override the judge model")
    p.add_argument("--judge-concurrency", type=int, default=4)
    p.add_argument("--no-vision", action="store_true", help="judge without screenshots")
    p.add_argument("--skip-run", action="store_true", help="judge + report what is already recorded")
    p.add_argument("--skip-judge", action="store_true", help="run the agent only")
    p.add_argument("--report-only", action="store_true", help="just print the scoreboard")
    p.add_argument("--fresh", action="store_true", help="delete results.jsonl and every recorded trace first")
    p.add_argument("--dry", action="store_true", help="list the selected tasks and exit")
    return p


def main():
    args = cli().parse_args()

    from eval import eval as sweeper
    from eval import judge as judger
    from eval import report as reporter

    if args.fresh:
        sweeper.RESULTS.unlink(missing_ok=True)
        for path in sweeper.TRACES.glob("run-*"):
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        print("cleared results.jsonl and eval/traces/run-*")

    if args.timeout:
        sweeper.TIMEOUT = args.timeout

    tasks = sweeper.load(args.site, path=sweeper.task_file(args.tasks), level=args.level)
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",")}
        tasks = [t for t in tasks if t["id"] in wanted]
    if args.sample:
        tasks = sweeper.sample(tasks, args.sample, args.seed)
    if args.n:
        tasks = tasks[: args.n]

    if not tasks and not (args.report_only or args.skip_run):
        raise SystemExit("no tasks matched")

    if not (args.report_only or args.skip_run):
        print(f"{len(tasks)} tasks x {args.runs} runs = {len(tasks) * args.runs} "
              f"[variant={args.variant}] -> {sweeper.RESULTS}")
        for t in tasks:
            print(f"  {t['id']:<28} {t['ques'][:80]}")
        if args.dry:
            return
        asyncio.run(sweeper.sweep(tasks, args.runs, args.variant))

    if not (args.report_only or args.skip_judge):
        jargs = judger.parser().parse_args([])
        jargs.concurrency = args.judge_concurrency
        jargs.no_vision = args.no_vision
        if args.judge_model:
            jargs.model = args.judge_model
        print(f"\n{'=' * 70}\njudging\n{'=' * 70}")
        asyncio.run(judger.main(jargs))

    rows = reporter.load()
    if rows:
        print()
        print(reporter.summarize(rows))
    else:
        print("nothing recorded yet")


if __name__ == "__main__":
    main()
