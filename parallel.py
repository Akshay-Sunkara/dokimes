#!/usr/bin/env python
"""
this runs one eval sweep across several chrome instances!!!
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
VENV_PY = REPO / ".venv" / "bin" / "python"
RESULTS = REPO / "eval" / "results.jsonl"
TRACES = REPO / "eval" / "traces"

RUNTIME_ROOT = Path("/tmp/dk")

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)

IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "__pycache__", "*.pyc", "runs.db",
    "results.jsonl", "results.jsonl.tmp", "run-*", "*.log", "run_*.jsonl",
)


def chrome_binary():
    override = os.environ.get("BH_CHROME_PATH") or os.environ.get("CHROME_PATH")
    if override and Path(override).exists():
        return override
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    raise SystemExit("no chrome binary found -- set CHROME_PATH")


def pick_tasks(task_file, sample, seed, level, site):
    sys.path.insert(0, str(REPO))
    from eval import eval as sweeper

    tasks = sweeper.load(site, path=sweeper.task_file(str(task_file)), level=level)
    if sample:
        tasks = sweeper.sample(tasks, sample, seed)
    return tasks


def split(tasks, n):
    groups = [[] for _ in range(n)]
    for i, task in enumerate(tasks):
        groups[i % n].append(task)
    return [g for g in groups if g]


def stage(dst):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(REPO, dst, ignore=IGNORE, symlinks=True)
    (dst / "eval" / "results.jsonl").unlink(missing_ok=True)
    for stale in (dst / "eval" / "traces").glob("run-*"):
        shutil.rmtree(stale, ignore_errors=True)
    if (REPO / ".env").exists():
        shutil.copy2(REPO / ".env", dst / ".env")
    return dst


def launch_chrome(binary, profile, port, headless, index):
    profile.mkdir(parents=True, exist_ok=True)
    argv = [
        binary,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--window-size=1280,900",
        f"--window-position={40 * index},{40 * index}",
        "about:blank",
    ]
    if headless:
        argv.insert(1, "--headless=new")
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_cdp(port, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                return json.loads(r.read())["webSocketDebuggerUrl"]
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            time.sleep(0.4)
    return None


def shard_env(index, port, runtime):
    runtime.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "BU_NAME": f"dk{index}",
        "BH_RUNTIME_DIR": str(runtime),
        "BH_TMP_DIR": str(runtime),
        "BU_CDP_URL": f"http://127.0.0.1:{port}",
    }


def start_shard(work, tasks, args, env, log):
    ids = ",".join(t["id"] for t in tasks)
    cmd = [
        str(VENV_PY), "-m", "eval",
        "--variant", args.variant,
        "--tasks", args.tasks,
        "--ids", ids,
        "--runs", str(args.runs),
        "--skip-judge",
    ]
    if args.timeout:
        cmd += ["--timeout", str(args.timeout)]
    handle = log.open("wb")
    proc = subprocess.Popen(cmd, cwd=work, env=env, stdout=handle, stderr=handle)
    proc._log_handle = handle
    return proc


def merge(work, index):
    src = work / "eval" / "results.jsonl"
    if not src.exists():
        return 0

    rows = [json.loads(l) for l in src.read_text().splitlines() if l.strip()]
    merged = []
    for row in rows:
        old = row.get("run_id")
        if not old:
            merged.append(row)
            continue
        new = f"{old}-s{index}"
        old_dir, new_dir = work / "eval" / "traces" / old, TRACES / new

        if old_dir.exists():
            if new_dir.exists():
                shutil.rmtree(new_dir)
            shutil.copytree(old_dir, new_dir)
            trace = new_dir / f"{old}.jsonl"
            if trace.exists():
                text = trace.read_text().replace(str(old_dir), str(new_dir))
                text = text.replace(f'"run_id": "{old}"', f'"run_id": "{new}"')
                trace.write_text(text)
                trace.rename(new_dir / f"{new}.jsonl")

        row["run_id"] = new
        row["shard"] = index
        row["trace"] = str(new_dir / f"{new}.jsonl")
        row["shots"] = [s.replace(str(old_dir), str(new_dir)) for s in row.get("shots") or []]
        merged.append(row)

    with RESULTS.open("a") as f:
        for row in merged:
            f.write(json.dumps(row) + "\n")
    return len(merged)


def stop(proc, label, sig=signal.SIGTERM):
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.send_signal(sig)
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    print(f"  stopped {label}")


def stop_daemon(index, runtime):
    env = {**os.environ, "BU_NAME": f"dk{index}", "BH_RUNTIME_DIR": str(runtime),
           "BH_TMP_DIR": str(runtime)}
    code = (
        "import os,signal;from browser_harness import _ipc as ipc;"
        f"p=ipc.identify('dk{index}');"
        "os.kill(p,signal.SIGTERM) if p else None"
    )
    try:
        subprocess.run([str(VENV_PY), "-c", code], env=env, timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError):
        pass


def cli():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--variant", default="base", help="label for this arm")
    p.add_argument("--tasks", default="eval/train.jsonl")
    p.add_argument("--sample", type=int, help="stratified random N tasks")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--runs", type=int, default=2)
    p.add_argument("--shards", type=int, default=5, help="parallel chrome instances")
    p.add_argument("--level", choices=("easy", "medium", "hard"))
    p.add_argument("--site")
    p.add_argument("--timeout", type=int, help="per-run timeout in seconds")
    p.add_argument("--base-port", type=int, default=9340,
                   help="first debugging port; stay clear of 9222, a running chrome answers there")
    p.add_argument("--headless", action="store_true",
                   help="fewer windows, but real sites block headless more often")
    p.add_argument("--keep", action="store_true", help="leave shard dirs for debugging")
    p.add_argument("--fresh", action="store_true", help="clear results.jsonl and traces first")
    p.add_argument("--dry", action="store_true", help="print the split and exit")
    return p


def main():
    args = cli().parse_args()

    if args.fresh:
        RESULTS.unlink(missing_ok=True)
        for path in TRACES.glob("run-*"):
            shutil.rmtree(path, ignore_errors=True)
        print("cleared results.jsonl and eval/traces/run-*")

    tasks = pick_tasks(args.tasks, args.sample, args.seed, args.level, args.site)
    if not tasks:
        raise SystemExit("no tasks matched")
    groups = split(tasks, args.shards)

    total = len(tasks) * args.runs
    print(f"{len(tasks)} tasks x {args.runs} runs = {total} across {len(groups)} shards "
          f"[variant={args.variant}]")
    for i, group in enumerate(groups, 1):
        levels = ",".join(sorted({t.get("level") or "?" for t in group}))
        print(f"  shard {i}: {len(group)} tasks ({levels})")
    if args.dry:
        for i, group in enumerate(groups, 1):
            print(f"\nshard {i}")
            for t in group:
                print(f"  {t['id']:<28} {t['ques'][:70]}")
        return

    binary = chrome_binary()
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    logs = REPO / "sweeps" / args.variant
    logs.mkdir(parents=True, exist_ok=True)

    browsers, shards, works, runtimes = [], [], [], []
    started = time.time()
    try:
        for i, group in enumerate(groups, 1):
            port = args.base_port + i
            runtime = RUNTIME_ROOT / f"r{i}"
            work = stage(RUNTIME_ROOT / f"w{i}")
            works.append(work)
            runtimes.append(runtime)

            browsers.append(launch_chrome(binary, RUNTIME_ROOT / f"p{i}", port, args.headless, i))
            if not wait_cdp(port):
                raise SystemExit(f"shard {i}: chrome never answered on port {port}")

            log = logs / f"shard{i}.log"
            shards.append(start_shard(work, group, args, shard_env(i, port, runtime), log))
            print(f"  shard {i} up on port {port}  -> {log.relative_to(REPO)}")

        print(f"\nrunning {len(shards)} shards, tailing progress every 60s")
        done = set()
        while len(done) < len(shards):
            time.sleep(60)
            line = []
            for i, proc in enumerate(shards, 1):
                log = logs / f"shard{i}.log"
                text = log.read_text(errors="ignore") if log.exists() else ""
                finished = text.count("\n-> ")
                line.append(f"s{i} {finished}/{len(groups[i - 1]) * args.runs}")
                if proc.poll() is not None:
                    done.add(i)
            print(f"  [{(time.time() - started) / 60:5.1f}m] " + "  ".join(line), flush=True)

        print("\nmerging shard results")
        for i, work in enumerate(works, 1):
            print(f"  shard {i}: {merge(work, i)} rows")

    finally:
        print("\ncleaning up")
        for i, proc in enumerate(shards, 1):
            stop(proc, f"shard {i}")
            handle = getattr(proc, "_log_handle", None)
            if handle:
                handle.close()
        for i, runtime in enumerate(runtimes, 1):
            stop_daemon(i, runtime)
        for i, proc in enumerate(browsers, 1):
            stop(proc, f"chrome {i}")
        if not args.keep:
            for work in works:
                shutil.rmtree(work, ignore_errors=True)
            for i in range(1, len(groups) + 1):
                shutil.rmtree(RUNTIME_ROOT / f"p{i}", ignore_errors=True)
                shutil.rmtree(RUNTIME_ROOT / f"r{i}", ignore_errors=True)

    print(f"\nswept in {(time.time() - started) / 60:.1f} min -- judging")
    subprocess.run([str(VENV_PY), "-m", "eval", "--skip-run", "--tasks", args.tasks], cwd=REPO)


if __name__ == "__main__":
    main()
