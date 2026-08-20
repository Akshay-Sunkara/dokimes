"""
judge browser-agent execution traces with an llm!!
inspired by browser-use.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESULTS = HERE / "results.jsonl"

JUDGE_FIELDS = (
    "verdict",
    "reasoning",
    "failure_reason",
    "impossible_task",
    "reached_captcha",
    "judge_model",
    "prompt_version",
    "judge_tokens",
    "judge_error",
)

load_dotenv(ROOT / ".env")

MODEL = os.environ.get("JUDGE_MODEL", "gpt-5.4-mini")
PROMPT_VERSION = "bu-v1"
MAX_IMAGES = 10
CONCURRENCY = 2
RETRIES = 6       
RETRY_CODES = (429, 403, 500, 502, 503, 504)


class JudgementResult(BaseModel):
    """LLM judgement of agent trace"""

    reasoning: str | None = Field(default=None, description="Explanation of the judgement")
    verdict: bool = Field(description="Whether the trace was successful or not")
    failure_reason: str | None = Field(
        default=None,
        description=(
            "Max 5 sentences explanation of why the task was not completed successfully "
            "in case of failure. If verdict is true, use an empty string."
        ),
    )
    impossible_task: bool = Field(
        default=False,
        description=(
            "True if the task was impossible to complete due to vague instructions, broken "
            "website, inaccessible links, missing login credentials, or other insurmountable "
            "obstacles"
        ),
    )
    reached_captcha: bool = Field(
        default=False,
        description="True if the agent encountered captcha challenges during task execution",
    )


def _retry_after(error):
    """Honour a Retry-After header when the API sends one."""
    headers = getattr(getattr(error, "response", None), "headers", None) or {}
    try:
        return int(float(headers.get("retry-after")))
    except (TypeError, ValueError):
        return None


def _out_of_credit(error):
    """Insufficient quota means the account is out of credit, not rate limited.

    OpenAI returns 429 for both. Retrying a spent balance just burns attempts
    against a wall that will not move until someone tops the account up.
    """
    return "insufficient_quota" in str(error)


async def _generate(client, model, system, contents, retries=RETRIES):
    for attempt in range(retries):
        try:
            return await client.responses.parse(
                model=model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": contents},
                ],
                text_format=JudgementResult,
            )
        except Exception as e:
            code = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
            if code not in RETRY_CODES or attempt == retries - 1:
                raise
            if code == 429 and _out_of_credit(e):
                raise
            await asyncio.sleep((_retry_after(e) or min(2**attempt, 30)) + random.random())


def _image_part(image_path):
    try:
        path = Path(image_path)
        if not path.exists():
            return None
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return {"type": "input_image", "image_url": f"data:image/png;base64,{data}", "detail": "high"}
    except Exception:
        return None


def _select_shots(paths, max_images):
    """Keep the last `max_images` *distinct* frames.

    Passive capture fires every SHOT_EVERY steps whether or not the page changed,
    so a trajectory that sits on one screen yields byte-identical frames. Taking
    the last N blindly spends the image budget re-showing the judge the same page.
    """
    seen, unique = set(), []
    for path in reversed(paths):
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            continue
        if digest in seen:
            continue
        seen.add(digest)
        unique.append(path)
        if len(unique) == max_images:
            break
    return list(reversed(unique))


def _truncate_text(text, max_length):
    if len(text) <= max_length:
        return text
    return text[: max_length - 23] + "...[text truncated]..."


def agent_steps(trace_path, keep_narration=True):
    path = Path(trace_path)
    if not path.exists():
        return []

    steps = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        step = record.get("step")
        if step is None or record["event"] not in ("action", "observation"):
            continue
        entry = steps.setdefault(step, {"step": step})
        if record["event"] == "action":
            entry["code"] = (record.get("input") or {}).get("code", "")
            if keep_narration:
                entry["reasoning"] = record.get("reasoning")
        else:
            entry["output"] = record.get("output", "")
            entry["is_error"] = record.get("is_error")

    out = []
    for step in sorted(steps):
        entry = steps[step]
        mark = " [error]" if entry.get("is_error") else ""
        lines = [f"Step {entry['step']}{mark}"]
        if entry.get("reasoning"):
            lines.append(f"Reasoning: {entry['reasoning']}")
        lines.append(f"Ran:\n{entry.get('code', '')}")
        lines.append(f"Output:\n{entry.get('output', '')}")
        out.append("\n".join(lines))
    return out


def construct_judge_messages(
    task,
    final_result,
    steps,
    screenshot_paths,
    max_images=MAX_IMAGES,
    use_vision=True,
):
    """Construct (system_prompt, contents) for judge evaluation of an agent trace."""
    task_truncated = _truncate_text(task, 40000)
    final_result_truncated = _truncate_text(final_result, 40000)
    steps_text_truncated = _truncate_text("\n".join(steps), 40000)

    image_parts = []
    if use_vision is not False:
        for img_path in _select_shots(screenshot_paths, max_images):
            part = _image_part(img_path)
            if part:
                image_parts.append(part)

    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    system_prompt = f"""You are an expert judge evaluating browser automation agent performance.

<evaluation_framework>

**PRIMARY EVALUATION CRITERIA (in order of importance):**
1. **Task Satisfaction (Most Important)**: Did the agent accomplish what the user asked for? Break down the task into the key criteria and evaluate if the agent met all of them. Focus on user intent and final outcome.
2. **Output Quality**: Is the final result in the correct format and complete? Does it match exactly what was requested?
3. **Tool Effectiveness**: Did the browser interactions work as expected? Were tools used appropriately? How many % of the tools failed?
4. **Agent Reasoning**: Quality of decision-making, planning, and problem-solving throughout the trajectory.
5. **Browser Handling**: Navigation stability, error recovery, and technical execution. If the browser crashes, does not load or a captcha blocks the task, the score must be very low.

**VERDICT GUIDELINES:**
- true: Task completed as requested, human-like execution, all of the users criteria were met and the agent did not make up any information.
- false: Task not completed, or only partially completed.

**Examples of task completion verdict:**
- If task asks for 10 items and agent finds 4 items correctly: false
- If task completed to full user requirements but with some errors to improve in the trajectory: true
- If task impossible due to captcha/login requirements: false
- If the trajectory is ideal and the output is perfect: true
- If the task asks to search all headphones in amazon under $100 but the agent searches all headphones and the lowest price is $150: false
- If the task asks to research a property and create a google doc with the result but the agents only returns the results in text: false
- If the task asks to complete an action on the page, and the agent reports that the action is completed but the screenshot or page shows the action is not actually complete: false
- If the task asks to use a certain tool or site to complete the task but the agent completes the task without using it: false
- If the task asks to look for a section of a page that does not exist: false
- If the agent concludes the task is impossible but it is not: false
- If the agent concludes the task is impossible and it truly is impossible: false
- If the agent is unable to complete the task because no login information was provided and it is truly needed to complete the task: false

**FAILURE CONDITIONS (automatically set verdict to false):**
- Blocked by captcha or missing authentication
- Output format completely wrong or missing
- Infinite loops or severe technical failures
- Critical user requirements ignored
- Page not loaded
- Browser crashed
- Agent could not interact with required UI elements
- The agent moved on from a important step in the task without completing it
- The agent made up content that is not in the screenshot or the page state
- The agent calls done action before completing all key points of the task

**ACTION-TASK CRITERIA (apply whenever the task asks the agent to filter, sort, or act on a page):**
1. Filtered results must actually be displayed. If a filter was never selected, never confirmed, or has no visible effect on the results, the task failed.
2. Superlatives — "best", "highest", "cheapest", "latest", "most recent", "lowest", "closest", "highest-rated", "largest", "newest" — must be achieved with the site's own sort or filter controls, not by eyeballing a list.
3. Typing the requirements into a search box is NOT filtering. A search whose query merely mentions the constraints cannot guarantee the results satisfy them, and counts as a failure.
4. Numeric ranges (price, year, beds, baths) must match the request exactly — neither broader nor narrower. Examples of failure:
   - required under $50, filter set to under $25
   - required $1500-$2500, filter set to $2000-$2500
   - required $25-$200, filter set to $0-$200
   - required years 2004-2012, filter set to 2001-2012
   - required before 2015, filter set to 2000-2014
   - required exactly 2 beds, filter set to 2+ beds
5. Some tasks are only complete once a submission is made or the result is actually displayed.
6. An empty or "no match found" result is still a SUCCESS when the agent performed the required actions correctly — the site simply had nothing to show.
7. If the page already lists every available item, applying a filter is unnecessary; selecting the item that meets the requirement is enough.

**IMPOSSIBLE TASK DETECTION:**
Set `impossible_task` to true when the task fundamentally could not be completed due to:
- Vague or ambiguous task instructions that cannot be reasonably interpreted
- Website genuinely broken or non-functional (be conservative - temporary issues don't count)
- Required links/pages truly inaccessible (404, 403, etc.)
- Task requires authentication/login but no credentials were provided
- Task asks for functionality that doesn't exist on the target site
- Other insurmountable external obstacles beyond the agent's control

Do NOT mark as impossible if:
- Agent made poor decisions but task was achievable
- Temporary page loading issues that could be retried
- Agent didn't try the right approach
- Website works but agent struggled with it

**CAPTCHA DETECTION:**
Set `reached_captcha` to true if:
- Screenshots show captcha challenges (reCAPTCHA, hCaptcha, etc.)
- Agent reports being blocked by bot detection
- Error messages indicate captcha/verification requirements
- Any evidence the agent encountered anti-bot measures during execution

**IMPORTANT EVALUATION NOTES:**
- **evaluate for action** - For each key step of the trace, double check whether the action that the agent tried to performed actually happened. If the required action did not actually occur, the verdict should be false.
- **screenshot is not entire content** - The agent has the entire DOM content, but the screenshot is only part of the content. If the agent extracts information from the page, but you do not see it in the screenshot, you can assume this information is there.
- **Penalize poor tool usage** - Wrong tools, inefficient approaches, ignoring available information.
- **current date/time is {current_date}** - content with recent dates is real, not fabricated.
- **ignore unexpected dates and times** - traces are recorded at varying times, so assume the dates the agent uses for search or filtering are correct.
- **IMPORTANT**: be very picky about the user's request - Have very high standard for the agent completing the task exactly to the user's request.
- **IMPORTANT**: be initially doubtful of the agent's self reported success, be sure to verify that its methods are valid and fulfill the user's desires to a tee.

</evaluation_framework>
"""

    user_prompt = f"""
<task>
{task_truncated or 'No task provided'}
</task>

<agent_trajectory>
{steps_text_truncated or 'No agent trajectory provided'}
</agent_trajectory>

<final_result>
{final_result_truncated or 'No final result provided'}
</final_result>

{len(image_parts)} screenshots from execution are attached.

Evaluate this agent execution given the criteria and respond with the requested structure."""

    contents = [{"type": "input_text", "text": user_prompt}, *image_parts]
    return system_prompt, contents


async def judge_one(client, record, args, gate):
    async with gate:
        out = {
            "id": record["id"],
            "run": record["run"],
            "web_name": record["web_name"],
            "judge_model": args.model,
            "prompt_version": PROMPT_VERSION,
        }
        task = record["ques"]
        if record.get("error"):
            task += f"\n\n[the run errored: {record['error']}]"

        system, contents = construct_judge_messages(
            task=task,
            final_result=record.get("answer") or "",
            steps=agent_steps(record["trace"], not args.no_narration),
            screenshot_paths=record.get("shots") or [],
            use_vision=not args.no_vision,
        )
        try:
            response = await _generate(client, args.model, system, contents)
            parsed = response.output_parsed
            if parsed is None:
                out["judge_error"] = f"no structured output ({response.status})"
            else:
                out.update(parsed.model_dump())
                usage = response.usage
                out["judge_tokens"] = {
                    "in": getattr(usage, "input_tokens", None),
                    "out": getattr(usage, "output_tokens", None),
                }
        except Exception as e:
            out["judge_error"] = f"{type(e).__name__}: {e}"
        return out


def load_rows(path=None):
    path = path or RESULTS
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def save_rows(rows, path=None):
    """Rewrite results.jsonl in place — judgements land on the run rows themselves."""
    path = path or RESULTS
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    tmp.replace(path)


async def main(args):
    rows = load_rows()
    if not rows:
        print(f"no runs in {RESULTS} — run the agent first")
        return

    todo = [r for r in rows if args.redo or "verdict" not in r]
    if args.n:
        todo = todo[: args.n]
    for row in todo:
        for field in JUDGE_FIELDS:
            row.pop(field, None)

    print(f"{len(todo)} to judge -> {RESULTS}  (model={args.model}, max_images={MAX_IMAGES})")
    if not todo:
        return

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise SystemExit("set OPENAI_API_KEY (in .env or the environment)")

    client = AsyncOpenAI(api_key=key)
    gate = asyncio.Semaphore(args.concurrency)
    pending = {(r["id"], r["run"]): r for r in todo}
    tasks = [asyncio.create_task(judge_one(client, r, args, gate)) for r in todo]
    for task in asyncio.as_completed(tasks):
        out = await task
        pending[(out["id"], out["run"])].update(out)
        save_rows(rows)
        mark = out.get("judge_error") or ("PASS" if out.get("verdict") else "FAIL")
        print(f"  {out['id']} r{out['run']}  {mark}")


def parser():
    p = argparse.ArgumentParser(description="judge recorded runs")
    p.add_argument("--model", default=MODEL, help=f"judge model (default {MODEL})")
    p.add_argument("--concurrency", type=int, default=CONCURRENCY)
    p.add_argument("--no-narration", action="store_true", help="drop agent reasoning from the trajectory")
    p.add_argument("--no-vision", action="store_true", help="skip screenshots entirely")
    p.add_argument("--redo", action="store_true", help="re-judge every run, discarding the verdicts already recorded")
    p.add_argument("--n", type=int, help="only judge the first N")
    return p


if __name__ == "__main__":
    asyncio.run(main(parser().parse_args()))
