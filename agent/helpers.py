import ast
import inspect
import re
import textwrap
from pathlib import Path

from browser_harness.helpers import capture_screenshot as _capture_screenshot
from browser_harness.helpers import (
    cdp,
    click_at_xy,
    close_tab,
    current_tab,
    dispatch_key,
    drain_events,
    ensure_real_tab,
    fill_input,
    goto_url,
    http_get,
    iframe_target,
    js,
    list_tabs,
    new_tab,
    page_info,
    press_key,
    scroll,
    switch_tab,
    type_text,
    upload_file,
    wait,
    wait_for_element,
    wait_for_load,
    wait_for_network_idle,
)

HELPERS_FILE = Path(__file__)

_PENDING_IMAGES = []


def capture_screenshot(path=None, full=False, max_dim=None):
    """Screenshot the page and attach it to your result so you can look at it.

    Captured at CSS-pixel size, so a coordinate you read off the image is the
    same coordinate you pass to click_at_xy.
    """
    if max_dim is None:
        info = page_info()
        max_dim = max(info.get("w") or 0, info.get("h") or 0) or 1200
    saved = _capture_screenshot(path=path, full=full, max_dim=max_dim)
    _PENDING_IMAGES.append(str(saved))
    return f"screenshot attached ({saved})"


def show(path):
    """Attach any image file on disk to your result so you can look at it."""
    _PENDING_IMAGES.append(str(path))
    return f"image attached ({path})"


def _take_pending_images():
    images = list(_PENDING_IMAGES)
    _PENDING_IMAGES.clear()
    return images


_TOOLING = {"helpers_file", "list_helpers", "grep_helpers", "helper_source", "add_helper"}
_MODULES = {"browser_harness.helpers", "agent.helpers"}


def _all():
    return {
        name: value
        for name, value in globals().items()
        if not name.startswith("_")
        and callable(value)
        and getattr(value, "__module__", "") in _MODULES
    }


def _public():
    return {n: v for n, v in _all().items() if n not in _TOOLING}


def _describe(name, fn):
    try:
        sig = str(inspect.signature(fn))
    except (TypeError, ValueError):
        sig = "(...)"
    doc = (inspect.getdoc(fn) or "").strip().splitlines()
    return f"{name}{sig}" + (f"\n    {doc[0]}" if doc else "")


def helpers_file():
    text = HELPERS_FILE.read_text()
    return f"{HELPERS_FILE}  ({len(text.splitlines())} lines)\n\n{text}"


def list_helpers():
    return "\n".join(_describe(n, f) for n, f in sorted(_public().items()))


def grep_helpers(pattern):
    rx = re.compile(pattern, re.IGNORECASE)
    hits = []
    for name, fn in sorted(_public().items()):
        doc = inspect.getdoc(fn) or ""
        if rx.search(name) or rx.search(doc):
            hits.append(_describe(name, fn))

    for i, line in enumerate(HELPERS_FILE.read_text().splitlines(), 1):
        if rx.search(line):
            hits.append(f"helpers.py:{i}: {line.strip()}")

    return "\n".join(hits) if hits else f"no helper matches {pattern!r}"


def helper_source(name):
    fn = _public().get(name)
    if fn is None:
        return f"no helper named {name!r}"
    try:
        return inspect.getsource(fn)
    except (OSError, TypeError):
        return f"source unavailable for {name}"


def add_helper(source):
    source = textwrap.dedent(source).strip()

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return f"syntax error, nothing written: {e}"

    defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not defs:
        return "pass a complete function definition, nothing written"

    existing = _all()
    clash = [d.name for d in defs if d.name in existing]
    if clash:
        return f"already defined: {', '.join(clash)} — grep_helpers first, or pick another name"

    current = HELPERS_FILE.read_text()
    updated = current.rstrip() + "\n\n\n" + source + "\n"
    try:
        compile(updated, str(HELPERS_FILE), "exec")
    except SyntaxError as e:
        return f"would break helpers.py, nothing written: {e}"

    HELPERS_FILE.write_text(updated)
    exec(compile(tree, str(HELPERS_FILE), "exec"), globals())

    added = ", ".join(d.name for d in defs)
    return (
        f"added {added} to helpers.py "
        f"({len(current.splitlines())} -> {len(updated.splitlines())} lines), "
        "available now"
    )


def count_matching(selector):
    return js(f"document.querySelectorAll({selector!r}).length")
