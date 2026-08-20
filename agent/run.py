import ast
import asyncio
import io
import traceback
from contextlib import redirect_stdout

MAX_OUTPUT = 4000

_ns = {}

def _setup():
    from agent import helpers

    if not _ns:
        from browser_harness.admin import ensure_daemon

        ensure_daemon()

    for name, value in helpers._all().items():
        _ns.setdefault(name, value)


def _exec(code):
    _setup()
    buf = io.StringIO()
    try:
        tree = ast.parse(code)
        last = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = ast.Expression(tree.body.pop().value)

        with redirect_stdout(buf):
            exec(compile(tree, "<agent>", "exec"), _ns)
            if last is not None:
                value = eval(compile(last, "<agent>", "eval"), _ns)
                if value is not None:
                    print(repr(value))
    except Exception:
        buf.write(traceback.format_exc())

    from agent import helpers

    images = helpers._take_pending_images()

    out = buf.getvalue().strip()
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + f"\n... truncated, {len(out)} chars total"
    return out or "(no output)", images

async def run(code):
    return await asyncio.to_thread(_exec, code)
