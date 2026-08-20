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


_MAP_JS = r"""
(() => {
  const ATTR = 'data-dk';
  const TAGS = new Set(['A','BUTTON','INPUT','SELECT','TEXTAREA','SUMMARY']);
  const ROLES = new Set(['button','link','checkbox','radio','textbox','combobox','listbox',
                         'menuitem','menuitemcheckbox','menuitemradio','tab','switch',
                         'searchbox','slider','spinbutton','treeitem','option']);
  const CONTAINERS = new Set(['A','BUTTON','SUMMARY','LABEL']);

  const out = [];
  let n = 0, offscreen = 0;
  const emitted = new WeakSet();
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const cut = (s, k) => s.length > k ? s.slice(0, k) + '…' : s;

  const nameOf = el => {
    let t = el.getAttribute('aria-label') || '';
    if (!t) {
      const id = el.getAttribute('aria-labelledby');
      if (id) { const r = document.getElementById(id); if (r) t = r.innerText || r.textContent || ''; }
    }
    if (!t) t = el.innerText || '';
    if (!t) t = el.getAttribute('placeholder') || el.getAttribute('title') ||
                el.getAttribute('alt') || el.getAttribute('name') || '';
    return cut(clean(t), 80);
  };

  const interactive = (el, st) => {
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
    const tag = el.tagName;
    if (TAGS.has(tag)) return !(tag === 'INPUT' && (el.type === 'hidden'));
    if (tag === 'LABEL') return !el.querySelector('input,select,textarea');
    if (ROLES.has((el.getAttribute('role') || '').toLowerCase())) return true;
    if (el.hasAttribute('onclick') || el.isContentEditable) return true;
    const ti = el.getAttribute('tabindex');
    if (ti !== null && ti !== '-1') return true;
    // a leaf-most element the page styles as clickable — div/span buttons
    if (st.cursor === 'pointer' && el.children.length < 3 &&
        !Array.from(el.children).some(c => getComputedStyle(c).cursor === 'pointer')) return true;
    return false;
  };

  const describe = el => {
    const tag = el.tagName.toLowerCase();
    const bits = [];
    if (el.type && tag === 'input') bits.push(el.type);
    const role = el.getAttribute('role');
    if (role && !TAGS.has(el.tagName)) bits.push('role=' + role);
    const name = nameOf(el);
    if (name) bits.push('"' + name + '"');
    if (tag === 'input' || tag === 'textarea') {
      if (el.type === 'checkbox' || el.type === 'radio') bits.push(el.checked ? 'CHECKED' : 'unchecked');
      else if (el.value) bits.push('value="' + cut(clean(el.value), 40) + '"');
    }
    if (tag === 'select') {
      const opts = Array.from(el.options).slice(0, 8).map(o => clean(o.text)).filter(Boolean);
      bits.push('selected="' + clean(el.value) + '"');
      bits.push('options=[' + cut(opts.join('|'), 120) + (el.options.length > 8 ? '|…' : '') + ']');
    }
    if (el.getAttribute('aria-expanded')) bits.push('expanded=' + el.getAttribute('aria-expanded'));
    return '<' + tag + ' ' + bits.join(' ') + '>';
  };

  const walk = (doc, ox, oy, depth) => {
    let els;
    try { els = doc.querySelectorAll('*'); } catch (e) { return; }
    const vw = window.innerWidth, vh = window.innerHeight;
    for (const el of els) {
      if (el.tagName === 'IFRAME') {
        if (depth > 2) continue;
        let inner = null;
        try { inner = el.contentDocument; } catch (e) { inner = null; }
        if (inner) {
          const r = el.getBoundingClientRect();
          walk(inner, ox + r.left, oy + r.top, depth + 1);
        }
        continue;
      }
      const r = el.getBoundingClientRect();
      if (r.width < 3 || r.height < 3) continue;
      const x = r.left + ox + r.width / 2, y = r.top + oy + r.height / 2;
      if (r.bottom + oy < 0 || r.top + oy > vh || r.right + ox < 0 || r.left + ox > vw) {
        if (TAGS.has(el.tagName)) offscreen++;
        continue;
      }
      const st = getComputedStyle(el);
      if (st.visibility === 'hidden' || parseFloat(st.opacity) === 0) continue;
      if (!interactive(el, st)) continue;

      // a span inside a button is the same click target as the button
      let skip = false;
      for (let p = el.parentElement; p; p = p.parentElement) {
        if (emitted.has(p) && CONTAINERS.has(p.tagName)) { skip = true; break; }
      }
      if (skip) continue;

      // covered by a modal or overlay: clicking here would hit something else
      try {
        const top = doc.elementFromPoint(x - ox, y - oy);
        if (top && top !== el && !el.contains(top) && !top.contains(el)) continue;
      } catch (e) {}

      el.setAttribute(ATTR, String(n));
      emitted.add(el);
      out.push({i: n, d: describe(el), x: Math.round(x), y: Math.round(y)});
      n++;
      if (n >= 250) return;
    }
  };

  document.querySelectorAll('[' + ATTR + ']').forEach(e => e.removeAttribute(ATTR));
  walk(document, 0, 0, 0);
  return {items: out, offscreen: offscreen, url: location.href,
          scroll: Math.round(window.scrollY), height: Math.round(document.body.scrollHeight)};
})()
"""


def page_map(match=None):
    """Every interactive element you can click right now, numbered, as text.

    This is the cheap way to see a page: one call, no image, exact strings. Act on
    what it returns with click_index / fill_index / select_index, which use the
    number, not a guessed coordinate.

    Only elements inside the viewport are listed, because only those are clickable —
    scroll and call it again to reach the rest. `match` filters to lines containing
    that substring (case-insensitive), which keeps a long page readable.
    """
    data = js(_MAP_JS)
    if not isinstance(data, dict):
        return f"page_map failed: {data!r}"

    lines = [f"[{it['i']}] {it['d']} @({it['x']},{it['y']})" for it in data.get("items", [])]
    if match:
        needle = match.lower()
        lines = [l for l in lines if needle in l.lower()]
    head = (
        f"{data.get('url')}\n"
        f"{len(lines)} elements, scrolled {data.get('scroll')} of {data.get('height')}px"
        + (f", {data['offscreen']} more outside the viewport" if data.get("offscreen") else "")
    )
    return head + "\n" + ("\n".join(lines) if lines else "(nothing matched)")


def _mapped(index):
    selector = f'[data-dk="{int(index)}"]'
    rect = js(
        "(() => { const e = document.querySelector('%s'); if (!e) return null;"
        " e.scrollIntoView({block:'center', inline:'center'});"
        " const r = e.getBoundingClientRect();"
        " return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),"
        "         tag: e.tagName.toLowerCase()}; })()" % selector
    )
    if not isinstance(rect, dict):
        raise RuntimeError(f"no element [{index}] on this page — run page_map() again, the page has changed")
    return selector, rect


def click_index(index):
    """Click the element page_map() numbered `index`, with a real mouse event."""
    selector, rect = _mapped(index)
    click_at_xy(rect["x"], rect["y"])
    return f"clicked [{index}] {rect['tag']} at ({rect['x']},{rect['y']})"


def fill_index(index, text):
    """Focus the element page_map() numbered `index`, clear it, and type `text`."""
    selector, _ = _mapped(index)
    fill_input(selector, text)
    return f"filled [{index}] with {text!r}"


def select_index(index, option):
    """Choose `option` in the <select> page_map() numbered `index`.

    Matches on the option's visible text or its value, so you can pass either.
    """
    selector, _ = _mapped(index)
    result = js(
        "(() => { const s = document.querySelector('%s'); if (!s) return 'gone';"
        " const want = %r.trim().toLowerCase();"
        " const o = Array.from(s.options).find(o => o.text.trim().toLowerCase() === want"
        "   || o.value.trim().toLowerCase() === want)"
        "   || Array.from(s.options).find(o => o.text.trim().toLowerCase().includes(want));"
        " if (!o) return 'no option like that: ' + Array.from(s.options).map(o=>o.text).join('|');"
        " s.value = o.value;"
        " s.dispatchEvent(new Event('input', {bubbles: true}));"
        " s.dispatchEvent(new Event('change', {bubbles: true}));"
        " return 'selected ' + o.text; })()" % (selector, option)
    )
    return result


def page_text(match=None, limit=6000):
    """The visible text of the page, iframes included — exact, unlike a screenshot.

    Pass `match` to get only the lines containing that substring, which is how you
    check a filter took effect or find a price without reading the whole page.
    """
    text = js(
        "(() => { let t = document.body ? document.body.innerText : '';"
        " for (const f of document.querySelectorAll('iframe')) {"
        "   try { if (f.contentDocument && f.contentDocument.body)"
        "     t += '\\n' + f.contentDocument.body.innerText; } catch (e) {} }"
        " return t; })()"
    )
    if not isinstance(text, str):
        return f"page_text failed: {text!r}"

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if match:
        needle = match.lower()
        lines = [l for l in lines if needle in l.lower()]
    out = "\n".join(lines)
    return out[:limit] + (f"\n... truncated, {len(out)} chars total" if len(out) > limit else "")
