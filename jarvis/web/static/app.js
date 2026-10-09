"use strict";

// ---------- helpers ----------
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const money = (n) => (n == null ? "—" : (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const signed = (n, suffix = "") => (n == null ? "—" : (n > 0 ? "+" : "") + n.toFixed(2) + suffix);
const cls = (n) => (n > 0 ? "good" : n < 0 ? "bad" : "");
const when = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
};
const price = (p) => (p == null ? "—" : p >= 1 ? p.toLocaleString(undefined, { maximumFractionDigits: 2 }) : Number(p).toPrecision(4));
const plat = (name) => `<span class="plat ${esc(name)}">${esc(name)}</span>`;

let busy = 0;
function setBusy(delta) {
  busy += delta;
  $("#reactor").classList.toggle("busy", busy > 0);
}

async function api(path, opts = {}) {
  setBusy(1);
  try {
    const init = { method: opts.method || "GET", headers: {} };
    if (opts.body instanceof FormData) init.body = opts.body;
    else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
    const r = await fetch(path, init);
    if (r.status === 401) { location.href = "/login"; throw new Error("login required"); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
    return data;
  } finally { setBusy(-1); }
}

let toastTimer;
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 4500);
}

// Tiny, safe markdown: escape first, then format.
function md(text) {
  const lines = esc(text).split("\n");
  let html = "", inList = false, para = [];
  const inline = (s) => s.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|\W)\*([^*\n]+)\*(?=\W|$)/g, "$1<em>$2</em>");
  const flush = () => { if (para.length) { html += `<p>${inline(para.join("<br>"))}</p>`; para = []; } };
  for (const line of lines) {
    const li = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)/);
    if (li) { flush(); if (!inList) { html += "<ul>"; inList = true; } html += `<li>${inline(li[1])}</li>`; continue; }
    if (inList) { html += "</ul>"; inList = false; }
    const h = line.match(/^#{1,4}\s+(.*)/);
    if (h) { flush(); html += `<p><strong>${inline(h[1])}</strong></p>`; continue; }
    if (!line.trim()) flush(); else para.push(line);
  }
  flush();
  if (inList) html += "</ul>";
  return html;
}

// ---------- routing ----------
const loaders = {};
function show(page) {
  if (!$(`#page-${page}`)) page = "overview";
  $$(".page").forEach((p) => p.classList.toggle("hidden", p.id !== `page-${page}`));
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  $("#nav a.active")?.scrollIntoView({ inline: "center", block: "nearest" });
  (loaders[page] || (() => {}))();
}
window.addEventListener("hashchange", () => show(location.hash.slice(1)));

// ---------- overview ----------
let status = null;
async function loadStatus() {
  status = await api("/api/status");
  const c = status.counts;
  const hour = new Date().getHours();
  $("#greeting").textContent = `${hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening"}, ${status.owner}.`;
  $("#status-line").textContent = `Managing ${status.brand} · ${status.platforms.length} platform(s) · ${status.timezone}`;
  $("#status-pills").innerHTML = [
    `<span class="pill ${status.mode === "autopilot" ? "on" : "off"}"><span class="dot"></span>${status.mode === "autopilot" ? "Autopilot" : "Review mode"}</span>`,
    status.dry_run ? `<span class="pill off"><span class="dot"></span>Dry run</span>` : `<span class="pill on"><span class="dot"></span>Live posting</span>`,
    `<span class="pill ${status.autopilot_running ? "on" : "off"}"><span class="dot"></span>${status.autopilot_running ? "Scheduler running" : "Scheduler off"}</span>`,
  ].join("");
  const stat = (v, label, hot, href) => `<a class="card stat ${hot ? "hot" : ""}" href="${href}" style="text-decoration:none;color:inherit"><div class="value">${v}</div><div class="label">${label}</div></a>`;
  $("#stats").innerHTML = [
    stat(c.pending, "Awaiting approval", c.pending > 0, "#approvals"),
    stat(c.scheduled, "Scheduled", false, "#content"),
    stat(c.published_today, "Published today", false, "#content"),
    stat(c.new_mentions, "New mentions", c.new_mentions > 0, "#inbox"),
    stat(c.flagged, "Flagged for you", c.flagged > 0, "#inbox"),
    status.trading ? stat(money(status.trading.cash_usd), "Paper cash", false, "#markets") : "",
  ].join("");
  $("#b-pending").textContent = c.pending || ""; $("#b-pending").dataset.n = c.pending;
  $("#b-inbox").textContent = c.new_mentions + c.flagged || ""; $("#b-inbox").dataset.n = c.new_mentions + c.flagged;
  $("#platforms").innerHTML = status.platforms.length ? status.platforms.map((p) =>
    `<div class="row" style="justify-content:space-between">${plat(p.name)}<span class="muted">${p.posts_per_day}/day · ${p.dry_run ? "dry run" : "connected"}</span></div>`).join("")
    : `<div class="muted">No platforms enabled yet. Turn them on under <code>platforms:</code> in config.yaml.</div>`;
  const names = { plan: "Plan content", publish: "Publish due posts", engage: "Check mentions", metrics: "Refresh stats",
    briefing: "Daily briefing", videos: "Check video inbox", watch: "Watchlist alerts", exits: "Stop-loss check", desk: "Market desk" };
  $("#jobs").innerHTML = status.jobs.length ? status.jobs.map((j) =>
    `<div class="row" style="justify-content:space-between"><span>${esc(names[j.id] || j.id)}</span><span class="muted mono">${when(j.next_run)}</span></div>`).join("")
    : `<div class="muted">Scheduler is off.</div>`;
  $("#foot-status").textContent = `${status.model} · ${status.mode}`;
}
async function loadFeed() {
  const items = await api("/api/activity");
  $("#feed").innerHTML = items.length ? items.map((i) => `<div><time>${when(i.time)}</time>${esc(i.message)}</div>`).join("")
    : `<div class="muted" style="border:0;background:none">Jarvis will report here as he works.</div>`;
}
loaders.overview = () => Promise.all([loadStatus(), loadFeed()]).catch((e) => toast(e.message, true));

// Quick-action buttons anywhere on the page
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-job]");
  if (!btn) return;
  e.preventDefault();
  const out = btn.closest(".card")?.querySelector(".result") || $("#job-result");
  btn.disabled = true;
  out.innerHTML = `<span class="typing"><span></span><span></span><span></span></span> Jarvis is on it…`;
  try {
    const r = await api(`/api/run/${btn.dataset.job}`, { method: "POST" });
    out.innerHTML = md(r.result);
    refreshCurrent();
  } catch (err) { out.innerHTML = `<span class="bad">${esc(err.message)}</span>`; }
  finally { btn.disabled = false; }
});

// ---------- chat ----------
const messages = $("#messages");
function addMsg(who, text, extra = "") {
  const div = document.createElement("div");
  div.className = `msg ${who} ${extra}`;
  div.innerHTML = who === "jarvis" ? `<div class="who">JARVIS</div>${md(text)}` : esc(text);
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
  return div;
}
async function send(text) {
  text = text.trim();
  if (!text) return;
  $("#suggestions").classList.add("hidden");
  addMsg("me", text);
  $("#input").value = ""; autosize();
  const typing = addMsg("jarvis", "");
  typing.innerHTML = `<div class="who">JARVIS</div><span class="typing"><span></span><span></span><span></span></span>`;
  $("#send").disabled = true;
  try {
    const r = await api("/api/chat", { method: "POST", body: { message: text } });
    typing.innerHTML = `<div class="who">JARVIS</div>${md(r.reply)}`;
    loadStatus().catch(() => {});
  } catch (err) {
    typing.classList.add("error");
    typing.innerHTML = `<div class="who">JARVIS</div>Something went wrong: ${esc(err.message)}`;
  } finally { $("#send").disabled = false; messages.scrollTop = messages.scrollHeight; }
}
function autosize() { const t = $("#input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 200) + "px"; }
$("#composer").addEventListener("submit", (e) => { e.preventDefault(); send($("#input").value); });
$("#input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send($("#input").value); } });
$("#input").addEventListener("input", autosize);
$$("#suggestions button").forEach((b) => b.addEventListener("click", () => send(b.textContent)));
$("#chat-reset").addEventListener("click", async () => {
  await api("/api/chat/reset", { method: "POST" });
  messages.innerHTML = ""; $("#suggestions").classList.remove("hidden"); greet();
});
function greet() {
  if (!messages.children.length) addMsg("jarvis", `At your service${status ? ", " + status.owner : ""}. What shall we work on?`);
}
loaders.chat = () => { greet(); $("#input").focus(); };

// ---------- approvals ----------
function postCard(p, actions) {
  const title = p.meta?.title ? `<div style="font-weight:600;margin-bottom:6px">🎬 ${esc(p.meta.title)}</div>` : "";
  const err = p.error ? `<div class="bad" style="margin-top:8px;font-size:13px">${esc(p.error)}</div>` : "";
  const link = p.url ? ` · <a href="${esc(p.url)}" target="_blank" rel="noopener">view</a>` : "";
  return `<div class="item" data-id="${p.id}">
    <div class="meta">${plat(p.platform)}<span>#${p.id} · ${esc(p.kind)}</span><span>· ${p.status.replace("_", " ")}</span>
      ${p.scheduled_at ? `<span>· ${p.status === "published" ? "out" : "for"} ${when(p.published_at || p.scheduled_at)}</span>` : ""}${link}</div>
    ${title}<div class="body">${esc(p.text)}</div>
    ${p.rationale ? `<div class="why">Why: ${esc(p.rationale)}</div>` : ""}${err}
    ${actions ? `<div class="actions row">${actions}</div>` : ""}</div>`;
}
async function loadApprovals() {
  const posts = await api("/api/posts?status=pending_approval");
  $("#approvals").innerHTML = posts.length ? posts.map((p) => postCard(p,
    `<button class="btn good small" data-act="approve">Approve</button><button class="btn small" data-act="edit">Edit</button><button class="btn bad small" data-act="reject">Reject</button>`)).join("")
    : `<div class="empty">All clear. Nothing waiting for approval.</div>`;
}
$("#approvals").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const item = btn.closest(".item"), id = item.dataset.id;
  try {
    if (btn.dataset.act === "approve") { const r = await api(`/api/posts/${id}/approve`, { method: "POST" }); toast(r.status === "published" ? "Published." : "Approved and scheduled."); }
    if (btn.dataset.act === "reject") { await api(`/api/posts/${id}/reject`, { method: "POST" }); toast("Rejected."); }
    if (btn.dataset.act === "edit") {
      const body = $(".body", item);
      body.outerHTML = `<textarea rows="5">${esc(body.textContent)}</textarea>`;
      btn.dataset.act = "save"; btn.textContent = "Save";
      return;
    }
    if (btn.dataset.act === "save") { await api(`/api/posts/${id}`, { method: "PATCH", body: { text: $("textarea", item).value } }); toast("Saved."); }
    loadApprovals(); loadStatus();
  } catch (err) { toast(err.message, true); }
});
$("#approve-all").addEventListener("click", async () => {
  try { const r = await api("/api/posts/approve-all", { method: "POST" }); toast(`Approved ${r.filter((x) => !x.error).length} item(s).`); loadApprovals(); loadStatus(); }
  catch (err) { toast(err.message, true); }
});
loaders.approvals = () => loadApprovals().catch((e) => toast(e.message, true));

// ---------- content ----------
let contentStatus = "";
async function loadContent() {
  const posts = await api(`/api/posts?status=${contentStatus}&limit=200`);
  $("#content").innerHTML = posts.length ? posts.map((p) => postCard(p, p.status === "scheduled" ? `<button class="btn bad small" data-cancel="${p.id}">Cancel</button>` : "")).join("")
    : `<div class="empty">Nothing here yet. Ask Jarvis to plan some content.</div>`;
}
$("#content-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  $$("#content-tabs button").forEach((x) => x.classList.toggle("active", x === b));
  contentStatus = b.dataset.status; loadContent();
});
$("#content").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-cancel]"); if (!b) return;
  try { await api(`/api/posts/${b.dataset.cancel}/reject`, { method: "POST" }); toast("Cancelled."); loadContent(); } catch (err) { toast(err.message, true); }
});
loaders.content = () => loadContent().catch((e) => toast(e.message, true));

// ---------- inbox ----------
let inboxStatus = "new";
async function loadInbox() {
  const items = await api(`/api/interactions?status=${inboxStatus}`);
  $("#inbox").innerHTML = items.length ? items.map((i) => `<div class="item" data-id="${i.id}">
      <div class="meta">${plat(i.platform)}<strong style="color:var(--text)">${esc(i.author)}</strong><span>· ${esc(i.kind)}</span><span>· ${esc(i.status)}</span></div>
      <div class="body">${esc(i.text)}</div>
      ${i.note ? `<div class="why">Jarvis: ${esc(i.note)}</div>` : ""}
      ${i.status === "new" || i.status === "flagged" ? `<div class="actions"><textarea rows="2" placeholder="Write a reply…"></textarea>
        <div class="row" style="margin-top:8px"><button class="btn primary small" data-act="reply">Send reply</button>
        <button class="btn small" data-act="draft">Ask Jarvis to draft</button><button class="btn small" data-act="ignore">Ignore</button></div></div>` : ""}
    </div>`).join("") : `<div class="empty">No ${inboxStatus === "all" ? "" : inboxStatus} messages.</div>`;
}
$("#inbox-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  $$("#inbox-tabs button").forEach((x) => x.classList.toggle("active", x === b));
  inboxStatus = b.dataset.status; loadInbox();
});
$("#inbox").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-act]"); if (!btn) return;
  const item = btn.closest(".item"), id = item.dataset.id, box = $("textarea", item);
  try {
    if (btn.dataset.act === "reply") {
      if (!box.value.trim()) return toast("Write a reply first.", true);
      const r = await api(`/api/interactions/${id}/reply`, { method: "POST", body: { text: box.value } });
      toast(r.status === "published" ? "Reply sent." : `Reply ${r.status.replace("_", " ")}.`);
    } else if (btn.dataset.act === "ignore") {
      await api(`/api/interactions/${id}/ignore`, { method: "POST" }); toast("Ignored.");
    } else if (btn.dataset.act === "draft") {
      btn.disabled = true; box.value = "Jarvis is drafting…";
      const r = await api("/api/chat", { method: "POST", body: { message:
        `Draft a reply to interaction #${id} (${$(".body", item).textContent.slice(0, 300)}). Reply with ONLY the reply text, nothing else. Do not send it.` } });
      box.value = r.reply.trim(); btn.disabled = false; return;
    }
    loadInbox(); loadStatus();
  } catch (err) { toast(err.message, true); btn.disabled = false; }
});
loaders.inbox = () => loadInbox().catch((e) => toast(e.message, true));

// ---------- youtube ----------
async function loadYouTube() {
  $("#yt-off").classList.toggle("hidden", !status || status.youtube);
  const v = await api("/api/videos");
  $("#yt-inbox").innerHTML = v.inbox.length ? v.inbox.map((f) => `<div class="item"><div class="meta"><span class="plat youtube">${f.is_short ? "short" : "long"}</span><span>${f.size_mb} MB</span></div>
      <div class="body">${esc(f.file.split(/[\\/]/).pop())}</div>${f.owner_notes ? `<div class="why">${esc(f.owner_notes)}</div>` : ""}</div>`).join("")
    : `<div class="empty">No new videos. Upload one, or drop files in <code>videos/long</code> and <code>videos/shorts</code>.</div>`;
  $("#yt-queued").innerHTML = v.queued.length ? v.queued.map((p) => postCard(p, "")).join("") : `<div class="empty">No uploads yet.</div>`;
}
$("#yt-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const file = $("#yt-file").files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file); fd.append("kind", $("#yt-kind").value); fd.append("notes", $("#yt-notes").value);
  const bar = $("#yt-progress"), btn = $("#yt-upload");
  bar.classList.remove("hidden"); btn.disabled = true;
  const xhr = new XMLHttpRequest();          // XHR gives upload progress for big video files
  xhr.open("POST", "/api/videos");
  xhr.upload.onprogress = (ev) => { if (ev.lengthComputable) $("div", bar).style.width = (ev.loaded / ev.total * 100) + "%"; };
  xhr.onload = () => {
    btn.disabled = false; bar.classList.add("hidden"); $("div", bar).style.width = "0";
    if (xhr.status === 401) { location.href = "/login"; return; }
    let data = {}; try { data = JSON.parse(xhr.responseText); } catch (_) {}
    if (xhr.status >= 400) return toast(data.detail || "Upload failed", true);
    toast(`Uploaded (${data.size_mb} MB). Jarvis will pick it up shortly.`);
    $("#yt-form").reset(); loadYouTube();
  };
  xhr.onerror = () => { btn.disabled = false; toast("Upload failed - check your connection.", true); };
  xhr.send(fd);
});
loaders.youtube = () => loadYouTube().catch((e) => toast(e.message, true));

// ---------- markets ----------
async function loadMarkets() {
  let pf;
  try { pf = await api("/api/portfolio"); }
  catch (err) { $("#mk-off").classList.remove("hidden"); $("#mk-off").textContent = err.message; return; }
  $("#mk-off").classList.add("hidden");
  const ret = pf.return_pct ?? 0;
  const stat = (v, label, c = "") => `<div class="card stat"><div class="value ${c}">${v}</div><div class="label">${label}</div></div>`;
  $("#mk-stats").innerHTML = [
    stat(money(pf.total_equity_usd), `Equity (started ${money(pf.starting_cash)})`),
    stat(signed(ret, "%"), "Total return", cls(ret)),
    stat(money(pf.cash_usd), "Cash"),
    stat(money(pf.realized_pnl_today_usd), "Realized P&L today", cls(pf.realized_pnl_today_usd)),
    stat(money(pf.unrealized_pnl_usd), "Open P&L", cls(pf.unrealized_pnl_usd)),
  ].join("");
  const halt = $("#halt");
  halt.textContent = pf.halted ? "Resume trading" : "Pause trading";
  halt.className = pf.halted ? "btn good" : "btn bad";
  halt.dataset.halted = pf.halted ? "1" : "";
  $("#mk-positions").innerHTML = pf.positions.length ? `<table><thead><tr><th>Asset</th><th>Market</th><th class="num">Value</th><th class="num">Avg</th><th class="num">Price</th><th class="num">P&amp;L</th><th class="num">Stop / Target</th></tr></thead><tbody>${
    pf.positions.map((p) => `<tr><td><strong>${esc(p.symbol)}</strong>${p.chain ? `<div class="muted mono" style="font-size:11px">${esc(p.chain)}</div>` : ""}</td><td>${esc(p.market)}</td>
      <td class="num">${money(p.value_usd)}</td><td class="num">${price(p.avg_price)}</td><td class="num">${price(p.price)}</td>
      <td class="num ${cls(p.pnl_usd)}">${money(p.pnl_usd)}<br><small>${signed(p.pnl_pct, "%")}</small></td>
      <td class="num">-${p.stop_loss_pct}% / +${p.take_profit_pct}%</td></tr>`).join("")}</tbody></table>`
    : `<div class="empty">No open positions.</div>`;
  const trades = await api("/api/trades?limit=40");
  $("#mk-trades").innerHTML = trades.length ? `<table><thead><tr><th>When</th><th>Trade</th><th class="num">USD</th><th>Why</th></tr></thead><tbody>${
    trades.map((t) => `<tr><td class="muted mono" style="font-size:12px">${when(t.filled_at || t.created_at)}</td>
      <td><span class="${t.side === "buy" ? "good" : "bad"}">${t.side.toUpperCase()}</span> ${esc(t.symbol)}${t.status !== "filled" ? ` <span class="warn">(${esc(t.status)})</span>` : ""}</td>
      <td class="num">${money(t.usd)}</td><td class="muted" style="font-size:13px">${esc(t.reason || "")}</td></tr>`).join("")}</tbody></table>`
    : `<div class="empty">No trades yet. Run a desk session or ask Jarvis in chat.</div>`;
  $("#mk-watch").innerHTML = `<div class="muted">Loading prices…</div>`;
  api("/api/watchlist").then((w) => {
    $("#mk-watch").innerHTML = (w.quotes.length ? `<table><thead><tr><th>Asset</th><th class="num">Price</th><th class="num">24h</th></tr></thead><tbody>${
      w.quotes.map((q) => `<tr><td>${esc(q.asset)}</td><td class="num">${price(q.price)}</td><td class="num ${cls(q.change_24h_pct)}">${q.change_24h_pct == null ? "—" : signed(q.change_24h_pct, "%")}</td></tr>`).join("")}</tbody></table>` : "")
      + (w.errors.length ? `<div class="muted" style="font-size:13px;margin-top:8px">${w.errors.map(esc).join("<br>")}</div>` : "");
  }).catch((err) => { $("#mk-watch").innerHTML = `<div class="muted">${esc(err.message)}</div>`; });
}
$("#halt").addEventListener("click", async (e) => {
  try { await api("/api/trading/halt", { method: "POST", body: { halted: !e.target.dataset.halted } }); loadMarkets(); }
  catch (err) { toast(err.message, true); }
});
loaders.markets = () => loadMarkets().catch((e) => toast(e.message, true));

// ---------- boot ----------
function refreshCurrent() { show(location.hash.slice(1) || "overview"); }
$("#logout").addEventListener("click", async () => { await api("/api/logout", { method: "POST" }); location.href = "/login"; });
loadStatus().catch(() => {}).finally(refreshCurrent);
setInterval(() => { if (!document.hidden) { loadStatus().catch(() => {}); if ((location.hash || "#overview") === "#overview") loadFeed().catch(() => {}); } }, 30000);
