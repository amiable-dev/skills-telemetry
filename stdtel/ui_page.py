"""The one page `stdtel-ui` serves (ADR-017), inlined so nothing is read from disk.

Design: berth's handoff pack (`berth ui`) — JetBrains Mono, hairlines, no radii,
dark by default with a light theme, state colours by outcome. Every value from the
server is inserted with textContent, never as HTML. `__NONCE__` and `__PORT__` are
filled per response.
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>stdtel health</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&display=swap">
<style nonce="__NONCE__">
:root, body {
  --bg:#0b0d10; --panel:#10131a; --panel2:#161a22; --line:#232932; --fg:#d7dce3; --dim:#7f8994;
  --faint:#3b434e; --sel:#1a2130;
  --pass:#3fd68b; --fail:#ff6b5d; --pending:#f2b135; --unknown:#9aa5b1; --link:#4ea6ff;
}
body[data-theme="light"] {
  --bg:#f2f2ee; --panel:#fafaf7; --panel2:#ebebe6; --line:#d5d5cf; --fg:#1b1e22; --dim:#646c75;
  --faint:#b9bdc2; --sel:#e3e6ea;
  --pass:#137a42; --fail:#c9352a; --pending:#a56d00; --unknown:#5d6772; --link:#175fb8;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:12px/1.45 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace; min-height:100vh; }
header { display:flex; flex-wrap:wrap; align-items:center; gap:0 12px; padding:0 12px; min-height:40px;
  background:var(--panel); border-bottom:1px solid var(--line); }
.mark { font-size:13px; font-weight:600; letter-spacing:.04em; }
.mark small { font-size:11px; font-weight:400; color:var(--dim); letter-spacing:0; margin-left:6px; }
.spacer { flex:1; }
.ctl { display:flex; align-items:center; gap:8px; height:40px; color:var(--dim); font-size:11px; }
.dot { width:6px; height:6px; border-radius:50%; background:var(--pass); }
.dot.live { animation:pulse 2s ease-in-out infinite; }
@keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:.35 } }
@media (prefers-reduced-motion: reduce) { .dot.live { animation:none; } }
button { height:22px; padding:0 8px; background:none; color:var(--fg); border:1px solid var(--line);
  font:500 10px 'JetBrains Mono', ui-monospace, monospace; letter-spacing:.06em; text-transform:uppercase;
  cursor:pointer; }
button:hover, button:focus-visible { border-color:var(--dim); outline:none; }
.bar { display:flex; flex-wrap:wrap; align-items:center; gap:4px 6px; padding:5px 12px; min-height:32px;
  border-bottom:1px solid var(--line); }
.label { font-size:10px; font-weight:500; letter-spacing:.08em; text-transform:uppercase; color:var(--dim); }
.chip { display:inline-flex; align-items:center; gap:5px; height:20px; padding:0 7px; font-size:10px;
  letter-spacing:.04em; border:1px solid var(--line); background:none; text-transform:none; }
.chip i { width:7px; height:7px; display:inline-block; background:currentColor; }
.chip[aria-pressed="true"] { background:var(--sel); }
.bar.filtering .chip[aria-pressed="false"] { opacity:.5; }
.summary { margin-left:auto; font-size:10px; color:var(--dim); }
main { overflow-x:auto; }
table { width:100%; min-width:760px; border-collapse:collapse; }
th { position:sticky; top:0; background:var(--bg); text-align:left; white-space:nowrap; padding:6px 5px; border-bottom:1px solid var(--line);
  font-size:10px; font-weight:500; letter-spacing:.08em; text-transform:uppercase; color:var(--dim); }
td { padding:6px 5px; border-bottom:1px solid var(--line); vertical-align:top; }
th:first-child, td:first-child { padding-left:16px; }
th:last-child, td:last-child { padding-right:16px; }
tr:hover td { background:var(--sel); }
td.name { font-weight:600; white-space:nowrap; }
td.observed { color:var(--fg); }
td.remedy { color:var(--dim); font-size:11px; }
td.age { color:var(--dim); font-size:11px; white-space:nowrap; text-align:right; }
.state { font-size:10px; font-weight:500; letter-spacing:.06em; text-transform:uppercase; border:1px solid currentColor;
  padding:1px 6px; white-space:nowrap; }
.s-pass { color:var(--pass); } .s-fail { color:var(--fail); } .s-pending { color:var(--pending); } .s-unknown { color:var(--unknown); }
tr.r-pass td { opacity:.7; }
.empty, .error { padding:24px 16px; color:var(--dim); }
.error { color:var(--fail); }
a.link { color:var(--link); font-size:11px; }
footer { padding:12px 16px; color:var(--dim); font-size:10px; border-top:1px solid var(--line); }
footer a { color:var(--link); }
</style>
</head>
<body>
<header>
  <span class="mark">stdtel<small>health :__PORT__</small></span>
  <span class="spacer"></span>
  <span class="ctl"><span id="dot" class="dot live"></span><span id="ago">loading</span>
    <button id="poll" type="button" aria-pressed="true">poll on</button>
    <button id="refresh" type="button">refresh</button>
    <button id="theme" type="button" aria-label="switch theme">☾</button></span>
</header>
<div class="bar" id="bar">
  <span class="label">outcome</span>
  <span id="chips"></span>
  <span class="summary" id="summary"></span>
</div>
<main>
  <table>
    <thead><tr><th>outcome</th><th>check</th><th>observed</th><th>what to do</th><th>observed at</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div id="note" class="empty" hidden></div>
</main>
<footer>The same checks as <code>stdtel-doctor --json</code>, run on this machine. Aggregates live in Grafana:
  <a href="http://localhost:3000/d/stdtel-operational" rel="noreferrer">operational dashboard</a>.
  This page holds no state and changes nothing.</footer>
<script type="module" nonce="__NONCE__">
const OUTCOMES = ["fail", "unknown", "pending", "pass"];
const POLL_MS = 30000;
const $ = (id) => document.getElementById(id);
const active = new Set();
let checks = [], fetchedAt = 0, polling = true, timer = null;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = String(text);
  return e;
}

function age(iso) {
  const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  if (!Number.isFinite(s)) return iso;
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`;
}

function drawChips() {
  const box = $("chips");
  box.replaceChildren();
  for (const o of OUTCOMES) {
    const n = checks.filter((c) => c.outcome === o).length;
    const b = el("button", `chip s-${o}`);
    b.type = "button";
    b.setAttribute("aria-pressed", String(active.has(o)));
    const sw = el("i");
    b.append(sw, el("span", "", `${o} ${n}`));
    b.addEventListener("click", () => { active.has(o) ? active.delete(o) : active.add(o); draw(); });
    box.append(b);
  }
  $("bar").classList.toggle("filtering", active.size > 0);
}

function draw() {
  drawChips();
  const shown = checks.filter((c) => active.size === 0 || active.has(c.outcome))
    .sort((a, b) => OUTCOMES.indexOf(a.outcome) - OUTCOMES.indexOf(b.outcome));
  const rows = $("rows");
  rows.replaceChildren();
  for (const c of shown) {
    const tr = el("tr", `r-${c.outcome}`);
    const st = el("td"); st.append(el("span", `state s-${c.outcome}`, c.outcome));
    const at = el("td", "age", age(c.observed_at)); at.title = c.observed_at;
    const todo = el("td", "remedy", c.outcome === "pass" ? "" : c.remedy);
    for (const l of c.links || []) {
      if (!/^https?:\/\//.test(l.url)) continue;          // only http(s): never javascript: or data:
      const a = el("a", "link", l.label);
      a.href = l.url; a.target = "_blank"; a.rel = "noreferrer";
      todo.append(document.createElement("br"), a);
    }
    tr.append(st, el("td", "name", c.name), el("td", "observed", c.observed), todo, at);
    rows.append(tr);
  }
  const attention = checks.filter((c) => c.outcome !== "pass").length;
  $("summary").textContent = `${shown.length} of ${checks.length} checks · ${attention} need attention`;
  const note = $("note");
  note.hidden = shown.length > 0;
  note.className = "empty";
  note.textContent = checks.length ? "No check matches the current filter." : "No checks returned.";
}

async function load() {
  try {
    const r = await fetch("/api/checks", { cache: "no-store" });
    if (!r.ok) throw new Error(`the server answered ${r.status}`);
    checks = (await r.json()).checks;
    fetchedAt = Date.now();
    draw();
  } catch (e) {
    const note = $("note");
    note.hidden = false; note.className = "error";
    note.textContent = `Could not read the checks: ${e.message}. Is stdtel-ui still running?`;
  }
}

function tick() {
  $("ago").textContent = polling ? (fetchedAt ? `${Math.round((Date.now() - fetchedAt) / 1000)}s ago` : "loading") : "manual";
}

function schedule() {
  clearInterval(timer);
  if (polling) timer = setInterval(load, POLL_MS);
}

$("poll").addEventListener("click", () => {
  polling = !polling;
  $("poll").textContent = polling ? "poll on" : "poll off";
  $("poll").setAttribute("aria-pressed", String(polling));
  $("dot").classList.toggle("live", polling);
  schedule(); tick();
});
$("refresh").addEventListener("click", load);
$("theme").addEventListener("click", () => {
  const light = document.body.dataset.theme !== "light";
  document.body.dataset.theme = light ? "light" : "dark";
  $("theme").textContent = light ? "☼" : "☾";
  try { localStorage.setItem("stdtel-ui-theme", document.body.dataset.theme); } catch (_) {}
});
try {
  if (localStorage.getItem("stdtel-ui-theme") === "light") { document.body.dataset.theme = "light"; $("theme").textContent = "☼"; }
} catch (_) {}

setInterval(tick, 1000);
load(); schedule();
</script>
</body>
</html>
"""
