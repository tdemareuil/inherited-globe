#!/usr/bin/env python3
"""A small review page for the article matches the pipeline is not sure about.

One article reaches the globe per site, and this is where it is chosen. The conflict is
between what the API resolution found — the pipeline's own pick, marked "in use" — and
its challengers: the article Wikipedia's own index names, and, where the pick is not in
English, what a search of the preferred languages turned up. Where an index row links
several articles, the one whose text best matches the site's description is marked as
the suggestion.

Some sites cannot be matched by rule. Wikidata splits an inscription from its subject
and keeps the sitelinks on the half our P757 join never sees; a new article cites no
UNESCO identifier to confirm it against. Full-text search reaches those, but it
guesses, so the guesses are shown to a human instead of being adopted.

The page runs on the standard library alone — the kernel has no ipywidgets — as a
background thread, so the notebook stays usable while it is open. Each click saves
straight to disk, so closing the tab loses nothing.

    from scripts import article_review
    review = article_review.serve(queue, "data/cache/wikidata/decisions.json")
    print(review.url)      # open it, choose, then:
    review.stop()
"""

from __future__ import annotations

import html
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE_CSS = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       margin: 0; padding: 24px 28px 96px; max-width: 1000px; }
h1 { font-size: 19px; margin: 0 0 4px; }
.sub { opacity: .65; margin: 0 0 22px; }
.item { border: 1px solid rgba(128,128,128,.33); border-radius: 9px;
        padding: 14px 16px; margin-bottom: 14px; }
.item.done { border-color: rgba(60,160,90,.75); background: rgba(60,160,90,.07); }
.label { font-weight: 600; }
.meta { opacity: .65; font-size: 12.5px; margin: 2px 0 10px; }
table { border-collapse: collapse; width: 100%; }
td { padding: 4px 8px 4px 0; vertical-align: middle; }
td.pick { width: 54px; white-space: nowrap; }
.lang { display: inline-block; min-width: 26px; padding: 1px 5px; border-radius: 4px;
        background: rgba(128,128,128,.2); font-size: 11.5px; text-align: center; }
.score { opacity: .6; font-variant-numeric: tabular-nums; font-size: 12.5px; }
.current { font-size: 12.5px; opacity: .75; margin-bottom: 8px; }
.bar { position: fixed; left: 0; right: 0; bottom: 0; padding: 10px 28px;
       background: Canvas; border-top: 1px solid rgba(128,128,128,.33);
       display: flex; gap: 16px; align-items: center; }
button { font: inherit; padding: 6px 14px; border-radius: 7px;
         border: 1px solid rgba(128,128,128,.5); background: transparent; cursor: pointer; }
a { color: inherit; }
"""

PAGE_JS = """
async function save(siteId) {
  const box = document.getElementById('item-' + siteId);
  const el = box.querySelector(`input[name="pick-${siteId}"]:checked`);
  const chosen = el && el.value !== '' ? JSON.parse(el.dataset.entry || 'null') : null;
  await fetch('/save', { method: 'POST', body: JSON.stringify({ site_id: siteId, chosen }) });
  box.classList.toggle('done', !!chosen);
  const done = document.querySelectorAll('.item.done').length;
  document.getElementById('count').textContent = done;
}
async function finish() {
  await fetch('/done', { method: 'POST' });
  document.getElementById('status').textContent = 'Saved. You can close this tab.';
}
"""


def _entry_attr(entry):
    keep = ("wiki_title", "wiki_language", "wiki_project", "wiki_url",
            "wiki_rank", "wiki_lookup_source")
    return html.escape(json.dumps({k: entry.get(k) for k in keep}), quote=True)


def _render(queue, decisions):
    rows = []
    for item in queue:
        site_id = str(item["site_id"])
        chosen = decisions.get(site_id) or {}
        chosen_url = (chosen.get("chosen") or {}).get("wiki_url")
        current = item.get("current") or {}

        lines = [f'<div class="item{" done" if chosen_url else ""}" id="item-{html.escape(site_id)}">',
                 f'<div class="label">{html.escape(str(item.get("label") or ""))}</div>',
                 f'<div class="meta">{html.escape(site_id)}'
                 + (f' · {html.escape(str(item["states"]))}' if item.get("states") else "")
                 + '</div>']
        if current.get("wiki_url"):
            lines.append(
                f'<div class="current">currently: <span class="lang">'
                f'{html.escape(current.get("wiki_language") or "?")}</span> '
                f'<a href="{html.escape(current["wiki_url"])}" target="_blank">'
                f'{html.escape(current.get("wiki_title") or "")}</a></div>')
        else:
            lines.append('<div class="current">currently: no article</div>')

        lines.append("<table>")
        for index, candidate in enumerate(item["candidates"]):
            url = candidate.get("wiki_url") or ""
            origin = candidate.get("wiki_lookup_source") or ""
            if candidate.get("current"):
                origin += " &middot; in use"
            elif url and url == item.get("suggested"):
                origin += " &middot; suggested"
            lines.append(
                f'<tr><td class="pick">'
                f'<input type="radio" name="pick-{site_id}" value="{index}" '
                f'data-entry="{_entry_attr(candidate)}" onchange="save(\'{site_id}\')"'
                f'{" checked" if url and url == chosen_url else ""}></td>'
                f'<td><span class="lang">{html.escape(candidate.get("wiki_language") or "")}</span></td>'
                f'<td><a href="{html.escape(url)}" target="_blank">'
                f'{html.escape(candidate.get("wiki_title") or "")}</a></td>'
                f'<td class="score">{candidate.get("score", "")}</td>'
                f'<td class="score">{candidate.get("text_score", "")}</td>'
                f'<td class="score">{origin}</td></tr>')
        lines.append(
            f'<tr><td class="pick">'
            f'<input type="radio" name="pick-{site_id}" value="" onchange="save(\'{site_id}\')"'
            f'{"" if chosen_url else " checked"}></td>'
            f'<td colspan="5" class="score">leave it as it is</td></tr>')
        lines.append("</table></div>")
        rows.append("\n".join(lines))

    done = sum(1 for item in queue if (decisions.get(str(item["site_id"])) or {}).get("chosen"))
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Article review</title><style>{PAGE_CSS}</style></head><body>
<h1>Article review</h1>
<p class="sub">One article per site reaches the globe: pick it. The row marked
<em>in use</em> is what the API resolution found; the others are what Wikipedia's index
and the language search propose. The two scores are the title's likeness to the site
name, then how much of the article's opening matches the site's own description &mdash;
that second one is what marks the <em>suggested</em> challenger where an index row links
several articles. Choices save as you click.</p>
{''.join(rows)}
<div class="bar"><strong><span id="count">{done}</span> / {len(queue)}</strong> chosen
<button onclick="finish()">Done</button><span id="status"></span></div>
<script>{PAGE_JS}</script></body></html>"""


class _Review:
    def __init__(self, queue, decisions_path, port):
        self.queue = queue
        self.path = Path(decisions_path)
        self.decisions = {}
        if self.path.exists():
            try:
                self.decisions = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                self.decisions = {}
        self.port = port
        self.url = f"http://127.0.0.1:{port}/"
        self._server = None
        self._thread = None

    def save(self, payload):
        site_id = str(payload.get("site_id"))
        if payload.get("chosen"):
            self.decisions[site_id] = {"chosen": payload["chosen"]}
        else:
            self.decisions.pop(site_id, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.decisions, ensure_ascii=False, indent=1))
        os.replace(temporary, self.path)

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        return len(self.decisions)


def serve(queue, decisions_path, port=8765):
    """Start the review page in a background thread and return a handle."""
    review = _Review(queue, decisions_path, port)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):           # keep the notebook output clean
            pass

        def do_GET(self):
            body = _render(review.queue, review.decisions).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if self.path == "/save":
                review.save(json.loads(raw or b"{}"))
            self.send_response(204)
            self.end_headers()
            if self.path == "/done":
                threading.Thread(target=review.stop, daemon=True).start()

    review._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    review._thread = threading.Thread(target=review._server.serve_forever, daemon=True)
    review._thread.start()
    return review
