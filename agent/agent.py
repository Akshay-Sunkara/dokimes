import asyncio
import base64
import mimetypes
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from agent.run import run
from eval.traces import observe

MODEL = "claude-sonnet-5"

SYSTEM = """
You control a real Chrome browser by writing Python.

The run tool executes Python in a namespace that persists across calls, so
variables and functions you define stay available.

A library of helpers is already imported, but you are not told what is in it.
Look before you write, and never guess a helper name:
  list_helpers()            every helper with its signature
  grep_helpers("upload")    search names, docstrings, and helpers.py
  helper_source("name")     the implementation, useful as a template
  helpers_file()            the full text of helpers.py

Start by finding out what exists.

cdp(method, **params) is the raw Chrome DevTools Protocol and always works,
whether or not a helper covers what you need:
  cdp("Page.navigate", url="https://example.com")
  cdp("Runtime.evaluate", expression="document.title", returnByValue=True)

If nothing in the library fits, write the function and save it:
  add_helper("def drag(x1, y1, x2, y2):\\n    ...")

add_helper appends to helpers.py and makes it callable immediately. 

Work the page as text, not as a picture. The loop that gets tasks finished is:

  page_map()               numbered list of everything clickable on screen
  click_index(3)           real mouse click on element 3
  fill_index(7, "text")    focus element 7, clear it, type
  select_index(9, "NC")    choose an option in a <select>
  page_text("Wilkes")      the visible text, filtered to lines mentioning Wilkes

Read, act by number, read again. Never invent a coordinate and never invent a
CSS selector — page_map() already tells you what is there and click_index takes
the number, so a click lands on the element you actually saw.

page_map() lists only what is inside the viewport, because that is what can be
clicked. If what you want is not there, scroll() and map again, or filter with
page_map("filter") to cut a long page down.

Text pulled from the DOM is exact; text read off an image is a guess, and
unusual characters (IPA, math, accents) come back wrong. Screenshot only to
understand a layout you cannot make sense of from the map, never to read what
the page says.

Clicks dispatched from JavaScript are untrusted events that many sites ignore,
which is why click_index dispatches real input events at the element instead.

Print what you want to see. The last expression is printed automatically.
"""

MAX_IMAGE_BYTES = 4_000_000


def _image_block(path):
    data = Path(path).read_bytes()
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    return {
        "type": "image",
        "data": base64.b64encode(data).decode("ascii"),
        "mimeType": mimetypes.guess_type(path)[0] or "image/png",
    }


@tool("run", "Execute Python against a live Chrome browser and return its output.", {"code": str})
async def run_tool(args):
    text, images = await run(args["code"])
    content = [{"type": "text", "text": text}]
    for path in images:
        try:
            block = _image_block(path)
        except OSError:
            block = None
        if block:
            content.append(block)
    return {"content": content}

SERVER = create_sdk_mcp_server(name="browser", version="1.0.0", tools=[run_tool])

OPTIONS = ClaudeAgentOptions(
    model=MODEL,
    system_prompt=SYSTEM,
    mcp_servers={"browser": SERVER},
    strict_mcp_config=True,
    allowed_tools=["mcp__browser__run"],
    tools=[],
    permission_mode="dontAsk",
    thinking={"type": "enabled", "budget_tokens": 2048, "display": "summarized"},
    # screenshots ride the stdio transport as base64 (~1.33x MAX_IMAGE_BYTES),
    # which overflows the SDK's 1MB default on any real page
    max_buffer_size=32 * 1024 * 1024,
)

async def main(task):
    result = None
    with observe(task) as run:
        print(f"trace: {run.path}\n", flush=True)
        async for message in query(prompt=task, options=OPTIONS):
            run.on_message(message)
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock) and block.input.get("code"):
                        print(f"\n--- run ---\n{block.input['code']}\n", flush=True)
            elif isinstance(message, ResultMessage):
                result = message.result
    return result


if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) or "Go to my gmail, find emails from identity logan park, go to the website and login to it. if its not cached, use my email - akshay.sunkara@gmail.com, password is SHIELd8712@!"
    print(asyncio.run(main(task)))
