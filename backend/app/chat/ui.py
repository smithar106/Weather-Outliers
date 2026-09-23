"""A minimal, self-contained chat page served by the API.

No framework and no external assets: one HTML document that posts a question to
``POST /api/chat`` and renders the answer, the SQL that produced it, and the
returned rows. It is deliberately plain — the point is to expose the read-only
SQL gateway, not to style a product.
"""

from __future__ import annotations

CHAT_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Weather Outliers — ask</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 system-ui, sans-serif; max-width: 860px; margin: 2rem auto;
         padding: 0 1rem; }
  h1 { font-size: 1.2rem; }
  label { display: block; margin: 0.75rem 0 0.25rem; font-weight: 600; }
  textarea, input[type=text], input[type=password] { width: 100%; box-sizing: border-box;
         padding: 0.5rem; font: inherit; }
  textarea { min-height: 3rem; resize: vertical; }
  button { margin-top: 0.75rem; padding: 0.5rem 1.25rem; font: inherit; cursor: pointer; }
  .result { margin-top: 1.5rem; }
  .muted { opacity: 0.65; font-size: 0.85rem; }
  pre.sql { background: rgba(0,0,0,0.05); padding: 0.6rem; overflow-x: auto;
            font: 0.85rem/1.4 ui-monospace, monospace; border-radius: 4px; }
  table { border-collapse: collapse; width: 100%; margin-top: 0.5rem; font-size: 0.9rem; }
  th, td { border: 1px solid rgba(0,0,0,0.2); padding: 0.3rem 0.5rem; text-align: left; }
  .error { color: #b00020; }
</style>
</head>
<body>
<h1>Ask about the data</h1>
<p class="muted">Questions are answered by a read-only SQL query over the
application database. Only SELECT is permitted; the query that ran is shown with
the answer.</p>

<form id="ask">
  <label for="question">Question</label>
  <textarea id="question" name="question" required
    placeholder="e.g. Which city had the most unusual event on 2026-09-21?"></textarea>

  <label for="apikey">API key (leave blank if the server does not require one)</label>
  <input type="password" id="apikey" name="apikey" autocomplete="off">

  <button type="submit">Ask</button>
</form>

<div class="result" id="result" hidden></div>

<script>
const form = document.getElementById("ask");
const result = document.getElementById("result");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = document.getElementById("question").value.trim();
  const apikey = document.getElementById("apikey").value.trim();
  if (!question) return;

  const headers = { "Content-Type": "application/json" };
  if (apikey) headers["X-API-Key"] = apikey;

  result.hidden = false;
  result.textContent = "Asking…";

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers,
      body: JSON.stringify({ question }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      renderError(body.detail || "The request failed.");
      return;
    }
    render(body);
  } catch (err) {
    renderError(String(err));
  }
});

function renderError(message) {
  result.innerHTML = "";
  const p = document.createElement("p");
  p.className = "error";
  p.textContent = message;
  result.appendChild(p);
}

function render(body) {
  result.innerHTML = "";
  const add = (tag, text) => {
    const el = document.createElement(tag);
    el.textContent = text;
    result.appendChild(el);
    return el;
  };

  add("p", body.answer).style.fontWeight = "600";
  if (body.explanation) add("p", body.explanation).className = "muted";

  if (body.sql) {
    add("p", "SQL that produced this answer:").className = "muted";
    add("pre", body.sql).className = "sql";
  }

  if (body.columns && body.columns.length) {
    const note = body.truncated
      ? ` (showing first ${body.row_count} rows; result truncated)`
      : ` (${body.row_count} rows)`;
    add("p", "Rows" + note).className = "muted";

    const table = document.createElement("table");
    const thead = document.createElement("thead");
    const tr = document.createElement("tr");
    for (const col of body.columns) {
      const th = document.createElement("th");
      th.textContent = col;
      tr.appendChild(th);
    }
    thead.appendChild(tr);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    for (const row of body.rows) {
      const r = document.createElement("tr");
      for (const cell of row) {
        const td = document.createElement("td");
        td.textContent = cell === null || cell === undefined ? "" : String(cell);
        r.appendChild(td);
      }
      tbody.appendChild(r);
    }
    table.appendChild(tbody);
    result.appendChild(table);
  }
}
</script>
</body>
</html>
"""
