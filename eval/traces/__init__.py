"""
Simple JSON traces for browser-agents!!
"""

import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

RUNS_DIR = Path(__file__).resolve().parent  # traces are written into this package
SHOT_EVERY = 2  # passive screenshot cadence, in steps
RESET_BROWSER = True  # start every run on one fresh tab (set False to keep open tabs)
NEW_TAB_URL = "chrome://new-tab-page"


def new_run_id():
    return f"run_{uuid.uuid4().hex[:8]}"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _scrub(value):
    if isinstance(value, dict):
        if "data" in value and value.get("type") in ("image", "audio"):
            size = len(value["data"]) if isinstance(value["data"], str) else 0
            return {**value, "data": f"<{value.get('type')} {size} b64 chars>"}
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _text(value, limit=8000):
    if not isinstance(value, str):
        value = json.dumps(_scrub(value), default=str)
    return value if len(value) <= limit else value[:limit] + f"… ({len(value)} chars)"


def _reset_browser():
    """Put Chrome back to a single fresh tab, keeping the profile.

    Cookies, logins and history all survive — this only clears leftover tabs, so
    a run never inherits whatever the previous task left on screen. The fresh tab
    is opened and attached *before* the old ones close: closing the last tab
    first would take Chrome down with it.
    """
    from browser_harness.admin import ensure_daemon
    from browser_harness.helpers import cdp, list_tabs, switch_tab, wait_for_load

    ensure_daemon()
    stale = [t["targetId"] for t in list_tabs()]

    try:
        fresh = cdp("Target.createTarget", url=NEW_TAB_URL)["targetId"]
    except Exception:
        fresh = cdp("Target.createTarget", url="about:blank")["targetId"]
    switch_tab(fresh)

    for target_id in stale:
        if target_id == fresh:
            continue
        try:
            cdp("Target.closeTarget", targetId=target_id)
        except Exception:
            pass

    try:
        wait_for_load(timeout=5.0)
    except Exception:
        pass
    return fresh


class Run:
    def __init__(self, task, run_id=None, runs_dir=None, reset=None):
        self.run_id = run_id or new_run_id()
        self.task = task
        self.step = 0
        self.open_steps = {}
        self.pending_text = []
        self.pending_thinking = []
        self.started = time.monotonic()

        directory = Path(runs_dir) if runs_dir else RUNS_DIR
        # everything a run produces — its trace and its screenshots — lives in one folder
        self.dir = directory / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{self.run_id}.jsonl"
        self.shots_dir = self.dir
        self.shots = 0

        self.event("run_start", task=task)
        if RESET_BROWSER if reset is None else reset:
            self.reset()
        self.shot("start")

    def reset(self):
        """Fresh Chrome before the first screenshot. Never fatal to the run."""
        try:
            target = _reset_browser()
        except Exception as e:
            self.event("reset_error", error=str(e))
            return None
        self.event("reset", target_id=target, url=NEW_TAB_URL)
        return target

    def shot(self, at):
        """Screenshot the page ourselves. The agent never asks for this."""
        try:
            from browser_harness.helpers import capture_screenshot

            n = self.shots + 1
            self.shots_dir.mkdir(parents=True, exist_ok=True)
            path = self.shots_dir / f"{n:02d}.png"
            capture_screenshot(path=str(path), max_dim=1600)
        except Exception:
            return None
        self.shots = n
        self.event("shot", n=n, at=at, path=str(path))
        return path

    def event(self, event, **fields):
        record = {"event": event, "run_id": self.run_id, "t": _now(), **fields}
        with self.path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
        return record

    def on_message(self, message):
        blocks = getattr(message, "content", None)

        if isinstance(blocks, list):
            for block in blocks:
                if hasattr(block, "text") and block.text.strip():
                    self.pending_text.append(block.text.strip())
                elif hasattr(block, "thinking") and block.thinking.strip():
                    self.pending_thinking.append(block.thinking.strip())
                elif hasattr(block, "name") and hasattr(block, "input"):
                    self._action(block)
                elif hasattr(block, "tool_use_id"):
                    self._observation(block)

        if hasattr(message, "subtype") and hasattr(message, "duration_ms"):
            self._finish(message)

    def _drain(self):
        reasoning = " ".join(self.pending_text) or None
        thinking = " ".join(self.pending_thinking) or None
        self.pending_text.clear()
        self.pending_thinking.clear()
        return reasoning, thinking

    def _action(self, block):
        reasoning, thinking = self._drain()
        self.step += 1
        self.open_steps[block.id] = (self.step, time.monotonic())
        self.event(
            "action",
            step=self.step,
            tool_use_id=block.id,
            tool=block.name,
            reasoning=reasoning,
            thinking=thinking,
            input={k: _text(v) for k, v in (block.input or {}).items()},
        )

    def _observation(self, block):
        step, started = self.open_steps.pop(block.tool_use_id, (None, None))
        self.event(
            "observation",
            step=step,
            tool_use_id=block.tool_use_id,
            is_error=bool(getattr(block, "is_error", False)),
            duration_ms=round((time.monotonic() - started) * 1000) if started else None,
            output=_text(block.content),
        )
        if step and step % SHOT_EVERY == 0:
            self.shot(f"step {step}")

    def _finish(self, message):
        reasoning, thinking = self._drain()
        if reasoning or thinking:
            self.event("message", reasoning=reasoning, thinking=thinking)
        self.event(
            "run_end",
            subtype=message.subtype,
            result=_text(getattr(message, "result", None)),
            is_error=bool(getattr(message, "is_error", False)),
            turns=getattr(message, "num_turns", None),
            cost_usd=getattr(message, "total_cost_usd", None),
            duration_ms=getattr(message, "duration_ms", None),
        )

    def close(self, error=None):
        self.shot("end")
        if self.open_steps:
            for tool_use_id, (step, _) in self.open_steps.items():
                self.event("unfinished", step=step, tool_use_id=tool_use_id)
            self.open_steps.clear()
        if error is not None:
            self.event("run_error", error=str(error))


@contextmanager
def observe(task, run_id=None, runs_dir=None, reset=None):
    run = Run(task, run_id=run_id, runs_dir=runs_dir, reset=reset)
    try:
        yield run
    except Exception as e:
        run.close(error=e)
        raise
    else:
        run.close()


def runs(runs_dir=None):
    directory = Path(runs_dir) if runs_dir else RUNS_DIR
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*/*.jsonl"))


def read(run_id, runs_dir=None):
    directory = Path(runs_dir) if runs_dir else RUNS_DIR
    path = directory / run_id / f"{run_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def steps(run_id, runs_dir=None):
    joined = {}
    order = []
    for record in read(run_id, runs_dir):
        step = record.get("step")
        if step is None:
            continue
        if step not in joined:
            joined[step] = {"step": step}
            order.append(step)
        joined[step].update(
            {k: v for k, v in record.items() if k not in ("event", "run_id", "step")}
        )
        if record["event"] == "action":
            joined[step]["t"] = record["t"]
    return [joined[s] for s in order]


def summary(run_id, runs_dir=None):
    records = read(run_id, runs_dir)
    if not records:
        return f"no trace for {run_id}"

    head = records[0]
    lines = [f"{run_id}  {head.get('t', '')}", f"task: {head.get('task', '')}", ""]

    for entry in steps(run_id, runs_dir):
        mark = "!" if entry.get("is_error") else " "
        took = f"{entry['duration_ms']}ms" if entry.get("duration_ms") else "unfinished"
        lines.append(f"{mark} step {entry['step']}  {entry.get('tool', '?')}  {took}")
        why = entry.get("reasoning") or entry.get("thinking")
        if why:
            lines.append(f"    why: {' '.join(why.split())[:160]}")
        code = (entry.get("input") or {}).get("code")
        if code:
            first = [ln for ln in code.splitlines() if ln.strip()][:3]
            for ln in first:
                lines.append(f"    | {ln[:100]}")
        if entry.get("output"):
            lines.append(f"    saw: {entry['output'].strip().splitlines()[0][:120]}")
        lines.append("")

    end = [r for r in records if r["event"] == "run_end"]
    if end:
        e = end[0]
        lines.append(
            f"end: {e.get('subtype')}  {e.get('turns')} turns  "
            f"{e.get('duration_ms')}ms  ${e.get('cost_usd') or 0:.4f}"
        )
    return "\n".join(lines)
