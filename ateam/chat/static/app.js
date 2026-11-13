/* a-team front end. No framework, no build: state in one object, DOM by hand. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const QUIET = new Set(["ack", "fyi", "status"]);
const LOUD = new Set(["deliverable", "question", "blocker", "review", "reminder"]);
const S = {
  teams: [], team: null, state: null, view: null, messages: [], replyCounts: {},
  thread: null, events: null, seen: load("agychat.seen", {}), latest: {}, atMe: {},
  // When each thread was last opened, by root id; and, for the view on
  // screen, what had been seen before it was opened (`base`).
  tseen: load("agychat.tseen", {}), base: "", fresh: {}, threadLatest: {},
  // Unsent text, per view: what you type in one channel stays there.
  drafts: load("agychat.drafts", {}),
  // Views you marked unread, by team|view, until you next open them.
  marked: load("agychat.marked", {}),
  // Every message this page has loaded, by id: for the menu, links, tasks.
  msgIndex: new Map(), fromMsg: {},
};
const TERMINAL = new Set(["done", "failed", "cancelled"]);

function load(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } }
function save(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} }

async function api(path, body) {
  const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" },
                       body: JSON.stringify({ team: S.team, ...body }) } : {};
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  // This page is read from disk on each load; the server runs the code it
  // started with. After an upgrade under a running server, the page calls
  // what only the new code answers.
  if (r.status === 404 && j.error === "unknown endpoint") {
    staleServer();
    throw new Error("a-team was upgraded but this server still runs the old code: restart it to finish the update");
  }
  if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

// The installed a-team is newer than the running server. Restart re-execs
// it on the installed code, same port and token; the page reloads when it
// is back.
function staleServer(installed) {
  if (S.toldStale) return;
  S.toldStale = true;
  const b = document.createElement("div");
  b.className = "update-banner";
  const running = S.state?.app_version;
  b.innerHTML = `a-team ${installed ? esc(installed) + " is installed" : "was upgraded"}, but this server is still running `
    + `${running ? esc(running) : "the old code"}. ${S.state?.member ? "Ask the operator to restart it." : '<button class="btn small">Restart a-team</button>'}`;
  b.querySelector("button")?.addEventListener("click", async (ev) => {
    ev.target.disabled = true; ev.target.textContent = "restarting…";
    try { await api("/api/update", { action: "restart" }); }
    catch { /* an old server has no restart: Admin's Update & restart, or restart it by hand */ }
    for (let i = 0; i < 40; i++) {
      await new Promise((r) => setTimeout(r, 750));
      try { const st = await (await fetch(`/api/state?${q({})}`)).json(); if (st.app_version && st.app_version !== running) return location.reload(); }
      catch { /* down while it restarts */ }
    }
    ev.target.textContent = "still the old code: restart a-team by hand";
  });
  document.body.appendChild(b);
}
const q = (o) => new URLSearchParams({ team: S.team, ...o }).toString();

function toast(text, err) {
  const t = $("#toast"); t.textContent = text; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.add("hidden"), err ? 6000 : 2500);
}

// --- rendering helpers ----------------------------------------------------------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const COLORS = ["#3a6ff7", "#19a463", "#d4891a", "#8a5cf6", "#d64545", "#2b8fd6", "#c2418f", "#5b8a72"];
function color(name) { let h = 0; for (const c of String(name)) h = (h * 31 + c.charCodeAt(0)) >>> 0; return COLORS[h % COLORS.length]; }
function hhmm(ts) { return ts ? fmtTime(null, ts, "time") : ""; }

// What people are called: an agent's alias, else its name; the operator's
// roster name for "user". Every name shown goes through here.
function agentOf(n) { return (S.state?.agents || []).find((a) => a.name === n); }
function nameOf(n) {
  if (n === "user") return S.state?.operator?.name || S.state?.author || "you";
  return agentOf(n)?.alias || n;
}
// What an @ that means you can say: @user, the roster operator's handles,
// and the name you post as (the server's list; the roster's if it has none).
function myHandles() {
  return new Set((S.state?.my_handles || ["user", ...(S.state?.operator?.handles || [])]).map((h) => h.toLowerCase()));
}
const HANDLE_RE = /(?<![\w@])@([A-Za-z0-9](?:[\w-]|\.(?=\w))*)/g;
function boardURL(taskId) {
  return `/board?team=${encodeURIComponent(S.team)}${taskId ? "#task=" + encodeURIComponent(taskId) : ""}`;
}

// The handle a mention names: an exact agent name, an alias, the name
// ignoring case, or a unique name past a team prefix -- as a-team resolves
// it -- shown by the agent's alias. Operator handles are you.
function resolveHandle(h) {
  const low = h.toLowerCase(); const agents = S.state?.agents || [];
  if (low === "here" || low === "all" || low === "everyone" || low === "channel") return { label: low, cls: "group" };
  if (myHandles().has(low)) return { label: nameOf("user"), cls: "me", title: "you" };
  const pick = (xs) => (xs.length === 1 ? xs[0] : null);
  const a = agents.find((x) => x.name === h)
    || pick(agents.filter((x) => [x.alias, ...(x.aliases || [])].filter(Boolean).some((y) => y.toLowerCase() === low)))
    || pick(agents.filter((x) => x.name.toLowerCase() === low))
    || pick(agents.filter((x) => { const n = x.name.toLowerCase(); return n.length > low.length && n.endsWith(low) && "-_.".includes(n[n.length - low.length - 1]); }));
  return a ? { label: nameOf(a.name), title: a.name } : null;
}
Object.assign(RenderCtx, {
  mention: resolveHandle,
  channel: (c) => !!(S.state?.channels || {})[c],
  task: (id) => (S.state?.tasks || []).find((t) => t.id === id) || null,
  taskURL: (id) => boardURL(id),
  channelURL: (c) => `/?team=${encodeURIComponent(S.team)}#channel=${encodeURIComponent(c)}`,
  fileURL: (p) => `/api/file?${q({ path: p })}`,
  appLink: (u) => appLink(u),
});
const markdown = (src) => markdownHTML(src, RenderCtx);

// --- links to messages, threads and views ------------------------------------------
// /?team=T#channel=billing&thread=<root>&msg=<id>: where it is, for a person
// reading the link, and the message itself, which is what the page opens.

const enc = encodeURIComponent;
function viewOfMsg(m) {
  if (m.channel) return { type: "channel", id: m.channel };
  if (m.from === "supervisor" || m.to === "supervisor") return { type: "dm", pair: ["supervisor", "user"] };
  return { type: "dm", pair: [m.from, m.to] };
}
function hashFor(v) {
  if (v.type === "channel") return `channel=${enc(v.id)}`;
  const mine = v.pair.includes("user") && !v.pair.includes("supervisor");
  return `dm=${enc(mine ? v.pair.find((p) => p !== "user") || "user" : [...v.pair].sort().join(","))}`;
}
const appURL = (hash) => `${location.origin}/?team=${enc(S.team)}#${hash}`;
const msgHash = (m) => `${hashFor(viewOfMsg(m))}${m.thread ? "&thread=" + enc(m.thread) : ""}&msg=${enc(m.id)}`;
const msgURL = (m) => appURL(msgHash(m));
const threadURL = (m) => appURL(`${hashFor(viewOfMsg(m))}&thread=${enc(m.thread || m.id)}`);
function viewLabel(v) {
  if (v.type === "channel") return "#" + v.id;
  const other = v.pair.find((p) => p !== "user");
  return v.pair.includes("user") ? (other === "supervisor" ? "notices" : nameOf(other)) : v.pair.map(nameOf).join(" ↔ ");
}
// A link into this app, for the markdown renderer: named for where it goes.
function appLink(u) {
  let x; try { x = new URL(u); } catch { return null; }
  if (x.origin !== location.origin || !["/", "/index.html"].includes(x.pathname)) return null;
  const h = new URLSearchParams(x.hash.slice(1));
  const v = viewFromHash(h); const msg = h.get("msg"); const th = h.get("thread");
  if (!v && !msg) return null;
  const team = x.searchParams.get("team");
  const elsewhere = team && team !== S.team ? ` (${team})` : "";
  const where = v ? viewLabel(v) : "";
  const m = msg && S.msgIndex.get(msg);
  if (m && !elsewhere) return { label: `↪ ${sender(m)}: “${snippet(m.content, 60)}”`, title: `${where} · ${m.ts}` };
  const label = msg ? `↪ a message${where ? " in " + where : ""}` : th ? `↪ a thread in ${where}` : `↪ ${where}`;
  return { label: label + elsewhere, title: u };
}
const snippet = (t, n) => { const s = String(t || "").replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
// Message links whose message was not loaded when they were drawn: ask for
// it once, then name the link by who said what.
const linkLookups = new Map();
function hydrateLinks(box) {
  for (const a of box.querySelectorAll("a.app-link:not([data-named])")) {
    a.dataset.named = "1";
    let x; try { x = new URL(a.href); } catch { continue; }
    const id = new URLSearchParams(x.hash.slice(1)).get("msg");
    if (!id || !a.textContent.startsWith("↪ a message") || (x.searchParams.get("team") || S.team) !== S.team) continue;
    if (!linkLookups.has(id)) linkLookups.set(id, api(`/api/messages?${q({ id })}`).then((r) => r.message).catch(() => null));
    linkLookups.get(id).then((m) => {
      if (!m) { a.title = "no such message here"; return; }
      a.textContent = `↪ ${sender(m)}: “${snippet(m.content, 60)}”`; a.title = `${viewLabel(viewOfMsg(m))} · ${m.ts}`;
    });
  }
}

// Open a message wherever it is: its view, its thread if it is a reply,
// and mark it for a moment.
function flash(el) {
  if (!el) return;
  el.scrollIntoView({ block: "center" });
  el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 2600);
}
async function goToMessage(id) {
  S.goingTo = true;                 // one history entry for the message, not one per step
  try { return await showMessage(id); }
  finally { S.goingTo = false; const m = S.msgIndex.get(id); if (m) remember(msgHash(m)); }
}
async function showMessage(id) {
  let m = S.msgIndex.get(id);
  if (!m) { try { m = (await api(`/api/messages?${q({ id })}`)).message; } catch (err) { return toast(err.message, true); } }
  const v = viewOfMsg(m);
  if (!S.view || viewKey(S.view) !== viewKey(v)) await openView(v, { jump: false });
  if (m.thread) {
    await openThread(m.thread).catch(() => {});
    return flash($(`#drawer-thread .msg[data-id="${CSS.escape(id)}"]`));
  }
  const find = () => $(`#messages .msg[data-id="${CSS.escape(id)}"]`);
  if (!find() && QUIET.has(m.kind) && !$("#show-quiet").checked) { $("#show-quiet").checked = true; renderMessages(); }
  if (find()) return flash(find());
  toast("That message is further back than this view shows; it is open in the side panel.");
  openThread(id).catch(() => {});
}

function avatarHTML(who, key) {
  const f = (S.state?.avatars || {})[key];
  return f ? `<div class="avatar img"><img src="/api/avatar?${q({ who: key })}" alt=""></div>`
           : `<div class="avatar" style="background:${color(who)}">${esc(who[0] || "?").toUpperCase()}</div>`;
}

function sender(m) {
  if (m.from !== "user") return nameOf(m.from);
  const op = S.state?.operator?.name;
  return op && (!m.author || m.author.toLowerCase() === op.toLowerCase()) ? op : (m.author || nameOf("user"));
}
// Recorded as a mention of you when it was posted, or an @ of one of your
// handles in it: what was recorded depends on the code the agent posted with.
// A handle in backticks is written about, not called: `@roelle` in a note on
// how mentions work is no mention, including one recorded as such before
// code was left out.
const CODE_RE = /```[\s\S]*?(?:```|$)|`[^`\n]*`/g;
const prose = (s) => (s || "").replace(CODE_RE, " ");
const namesMe = (text) => [...text.matchAll(HANDLE_RE)].some((x) => myHandles().has(x[1].toLowerCase()));
function mentionsMe(m) {
  if (m.from === "user") return false;
  if (namesMe(prose(m.content))) return true;
  return (m.mentions || []).includes("user") && !namesMe(m.content || "");
}
// Addressed to you: an @mention, or an agent's direct message.
const forMe = (m) => m.from !== "user" && m.from !== "supervisor" && (mentionsMe(m) || (!m.channel && m.to === "user"));

function msgHTML(m, opts = {}) {
  const kind = m.kind || "work";
  const quiet = QUIET.has(kind);
  const who = sender(m);
  const badge = kind !== "work" ? `<span class="badge ${quiet ? "quiet" : kind}">${esc(kind)}</span>` : "";
  const tk = m.task_id ? RenderCtx.task(m.task_id) : null;
  const task = m.task_id ? `<a class="badge quiet" href="${boardURL(m.task_id)}" title="${esc(m.task_id)} — open on the board">${esc(tk ? tk.title : m.task_id)}</a>` : "";
  const to = opts.showTo && m.to && !String(m.to).startsWith("#") ? ` <span class="when">→ ${esc(nameOf(m.to))}</span>` : "";
  const n = S.replyCounts[m.id] || 0;
  const fr = !opts.inThread && S.fresh[m.id];
  const freshText = fr ? ` <span class="fresh">· ${fr.n} new</span>${fr.at ? ` <span class="at-you" title="a reply mentions you">@ you</span>` : ""}` : "";
  const mine = m.from === "user" && m.id && !m.deleted && (!S.state?.member || m.author === S.state.author);
  const own = mine ? `<span class="mk-task" data-edit="${esc(m.id)}">edit</span><span class="mk-task" data-del="${esc(m.id)}">delete</span>` : "";
  // Any message, a reply in a thread too, can be made into a task, and more
  // than one; the ⋯ (or a right-click) has the rest.
  const acts = m.id && !m.deleted
    ? `${!opts.inThread && !n && m.from !== "supervisor" ? `<span data-thread="${esc(m.id)}">reply in thread</span>` : ""}`
      + `<span class="mk-task" data-mktask="${esc(m.id)}">make a task</span>${own}`
      + `<span class="mk-task more" data-menu="${esc(m.id)}" title="Link, mark unread, follow up… (or right-click the message)">⋯</span>`
    : own;
  const replies = (!opts.inThread && n ? `<div class="replies ${fr ? "new" : ""}" data-thread="${esc(m.id)}">${n} repl${n === 1 ? "y" : "ies"}${freshText}</div>` : "")
    + (acts ? `<div class="replies faint">${acts}</div>` : "");
  // The tasks made from this message, each a link to it on the board.
  const made = (m.id && S.fromMsg[m.id]) || [];
  const madeHTML = made.length ? `<div class="msg-tasks">${made.map((t) => `<a class="task-ref" href="${boardURL(t.id)}" title="${esc(t.id)} · ${esc(t.status)} · ${esc(t.owner === "user" ? "yours" : taskOwner(t))}"><span class="st ${esc(t.status)}"></span>${esc(t.owner === "user" && !TERMINAL.has(t.status) ? "🔖 " : "")}${esc(t.title)}</a>`).join("")}</div>` : "";
  const edited = m.edited && !m.deleted ? `<span class="when edited" title="edited ${esc(m.edited)}">(edited)</span>` : "";
  return `<div class="msg ${quiet ? "quiet" : ""} ${LOUD.has(kind) ? kind : ""} ${mentionsMe(m) ? "mentions-me" : ""}" data-id="${esc(m.id || "")}">
    ${avatarHTML(who, m.from)}
    <div><div><span class="who">${esc(who)}</span>${to}<span class="when" title="${esc(m.ts)} (the team's clock)">${esc(fmtTime(m.t, m.ts))}</span>${edited}${badge}${task}</div>
    <div class="body ${m.deleted ? "deleted" : ""}">${m.deleted ? "<em>message deleted</em>" : markdown(m.content || "")}</div>${madeHTML}${replies}</div></div>`;
}

// --- editing what you sent ------------------------------------------------------------
// An agent may already have acted on a message, so the server says what an
// edit did: changed where it was still unread, a note where it was read.
function editSummary(r) {
  const who = (xs) => xs.map(nameOf).join(", ");
  const parts = [];
  if (r.changed_unread.length) parts.push(`${r.deleted ? "taken out before" : "changed before"} ${who(r.changed_unread)} read it`);
  if (r.told.length) parts.push(`${who(r.told)} had read it and ${r.told.length === 1 ? "was" : "were"} sent ${r.deleted ? "a note to disregard it" : "the new text"}`);
  return (r.deleted ? "Deleted" : "Edited") + (parts.length ? ": " + parts.join("; ") : "");
}
function applyEdit(e) {
  const fix = (m) => {
    if (m.id !== e.id) return m;
    return e.deleted ? { ...m, content: "", deleted: e.ts, edited: e.ts } : { ...m, content: e.content, edited: e.ts };
  };
  if (S.messages.some((m) => m.id === e.id)) { S.messages = S.messages.map(fix); indexMsgs(S.messages); renderMessages(); }
  if (S.thread) openThread(S.thread);
}
function startEdit(btn) {
  const row = btn.closest(".msg"); const id = btn.dataset.edit;
  const m = S.messages.find((x) => x.id === id);
  const body = row?.querySelector(".body");
  if (!body || row.querySelector(".edit-box")) return;
  const text = m ? m.content : body.textContent;
  body.insertAdjacentHTML("afterend", `<form class="edit-box"><textarea rows="3"></textarea>
    <div class="composer-row"><span class="hint">an agent that already read it gets the new text</span>
    <button type="button" class="btn small ghost" data-cancel>Cancel</button><button class="btn small">Save</button></div></form>`);
  const form = row.querySelector(".edit-box"); const ta = form.querySelector("textarea");
  ta.value = text; body.classList.add("hidden"); ta.focus();
  const close = () => { form.remove(); body.classList.remove("hidden"); };
  form.querySelector("[data-cancel]").onclick = close;
  ta.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") close();
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); form.requestSubmit(); }
  });
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      const r = await api("/api/message", { action: "edit", id, content: ta.value });
      close(); applyEdit({ id, content: ta.value.trim(), ts: new Date().toISOString() }); toast(editSummary(r));
    } catch (err) { toast(err.message, true); }
  };
}
async function deleteMessage(id) {
  if (!confirm("Delete this message? Agents that have not read it will never see it; any that have are told to disregard it.")) return;
  try {
    const r = await api("/api/message", { action: "delete", id });
    applyEdit({ id, deleted: true, ts: new Date().toISOString() }); toast(editSummary(r));
  } catch (err) { toast(err.message, true); }
}

// --- views ------------------------------------------------------------------------

function viewKey(v) { return v.type === "channel" ? `c:${v.id}` : `d:${[...v.pair].sort().join(",")}`; }
function keyOf(m) {
  if (m.channel) return `c:${m.channel}`;
  if (m.from && m.to) return `d:${[m.from, m.to].sort().join(",")}`;
  return null;
}

// The first run: no teams yet, so say what this machine can run and make one.
async function renderSetup() {
  const { runtimes, teams_root } = await api("/api/setup");
  $("#composer").classList.add("hidden");
  const ready = runtimes.filter((r) => r.ready);
  $("#messages").innerHTML = `<form class="setup" id="setup">
    <h2>Start your first team</h2>
    <p>A team is a manager, a project lead, a coder, a systems engineer and a reviewer. You can change them later. It will live in ${esc(teams_root)}.</p>
    <label>Team name</label><input type="text" name="name" value="default" required>
    <label>What the agents run on</label>
    ${runtimes.map((r) => `<label class="runtime ${r.ready ? "" : "off"}"><input type="radio" name="runtime" value="${esc(r.id)}" ${r.ready ? "" : "disabled"} ${r === ready[0] ? "checked" : ""}>
      <span><b>${esc(r.label)}</b>${r.needs ? `<span class="hint">needs ${esc(r.needs)}</span>` : ""}${r.note ? `<span class="hint">${esc(r.note)}</span>` : ""}</span></label>`).join("")}
    ${ready.length ? `<button class="btn">Create and start</button>` : `<p class="hint">Nothing here can run agents yet. Add one of the above, restart a-team, and come back.</p>`}
  </form>`;
  $("#setup").onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      const made = await api("/api/new_team", { name: fd.get("name"), runtime: fd.get("runtime") });
      if (made.note) toast(made.note);
      location.search = `?team=${encodeURIComponent(made.team)}`;
    } catch (err) { toast(err.message, true); }
  };
}

async function loadTeams() {
  const { teams } = await api("/api/teams");
  S.teams = teams;
  const sel = $("#team");
  sel.innerHTML = teams.map((t) => `<option value="${esc(t.name)}">${esc(t.name)}${t.supervisor ? " ●" : ""}</option>`).join("");
  if (!teams.length) return renderSetup();
  const asked = new URLSearchParams(location.search).get("team");
  const want = asked || load("agychat.team", null);
  S.team = teams.some((t) => t.name === want) ? want : teams[0].name;
  sel.value = S.team;
  await openTeam();
}

async function openTeam() {
  save("agychat.team", S.team);
  renderNav("chat", S.team, () => { renderMessages(); renderAgents(); renderViewTasks(); if (S.thread) openThread(S.thread); });
  await refreshState();
  const v = linkedView() || load(`agychat.view.${S.team}`, null);
  const chans = Object.keys(S.state.channels || {});
  const open = chans.filter((c) => !S.state.channels[c].archived);
  // Opening a team is where its history starts, in this entry: what Back
  // walks is this team's views, and a link to another team's reloads.
  S.view = null; S.thread = null; restoring++;
  try {
    await openView(v && (v.type !== "channel" || chans.includes(v.id)) ? v : { type: "channel", id: open[0] || chans[0] || "general" },
                   { jump: !linkedMsg() });
  } finally { restoring--; }
  history.replaceState(null, "", `${location.pathname}?team=${enc(S.team)}#${linkedMsg() || linkedView() ? location.hash.slice(1) : hashFor(S.view)}`);
  lastHash = location.hash;
  if (linkedMsg()) goToMessage(linkedMsg());
  else if (linkedView() && linkedThread()) openThread(linkedThread()).catch(() => {});
  listen();
}

// The board links here as /?team=T#channel=billing, #channel=billing&thread=<id>
// (a task's discussion) or #dm=coder; a link to one message adds &msg=<id>.
// #dm=a,b is two agents' conversation.
function linkedThread() { return new URLSearchParams(location.hash.slice(1)).get("thread"); }
function linkedMsg() { return new URLSearchParams(location.hash.slice(1)).get("msg"); }
function viewFromHash(h) {
  if (h.get("channel")) return { type: "channel", id: h.get("channel") };
  if (h.get("dm")) { const p = h.get("dm").split(","); return { type: "dm", pair: p.length > 1 ? p : ["user", p[0]] }; }
  return null;
}
function linkedView() { return viewFromHash(new URLSearchParams(location.hash.slice(1))); }
async function followHash() {
  if (!S.state) return;
  if (linkedMsg()) return goToMessage(linkedMsg());
  const v = linkedView(); if (!v) return;
  await openView(v);
  if (linkedThread()) openThread(linkedThread());
  else if (S.thread && !$("#drawer-thread").classList.contains("hidden")) closeDrawer();
}
// Back and forward walk what you opened: each view, thread and message is a
// history entry (remember), and going back or forward opens it again
// without adding one (restore).
let restoring = 0, lastHash = location.hash;
async function restore() {
  const team = new URLSearchParams(location.search).get("team");
  if (team && S.team && team !== S.team) return location.reload();
  if (location.hash === lastHash) return;
  lastHash = location.hash; restoring++;
  try { await followHash(); } finally { restoring--; }
}
function remember(h) {
  if (restoring || S.goingTo || !S.state || location.hash.slice(1) === h) return;
  history.pushState(null, "", `${location.pathname}${location.search}#${h}`);
  lastHash = location.hash;
}
window.addEventListener("hashchange", restore);
window.addEventListener("popstate", restore);

async function refreshState() {
  S.state = await api(`/api/state?${q({})}`);
  // An open tab keeps the code it loaded. When the server has been updated
  // since, say so: otherwise a fix that has shipped looks like it has not.
  S.loadedVersion ??= S.state.app_version;
  if (S.state.app_version && S.state.app_version !== S.loadedVersion && !S.toldUpdate) {
    S.toldUpdate = true;
    const b = document.createElement("div");
    b.className = "update-banner";
    b.innerHTML = `a-team was updated to ${esc(S.state.app_version)}; this page is still ${esc(S.loadedVersion)}. <button class="btn small">Reload</button>`;
    b.querySelector("button").onclick = () => location.reload();
    document.body.appendChild(b);
  }
  if (S.state.installed_version && S.state.app_version && S.state.installed_version !== S.state.app_version)
    staleServer(S.state.installed_version);
  S.replyCounts = S.state.reply_counts || {};
  S.threadLatest = S.state.thread_latest || {};
  const before = JSON.stringify(S.fromMsg);
  S.fromMsg = {};
  for (const t of S.state.tasks || []) if (t.origin?.id) (S.fromMsg[t.origin.id] ||= []).push({ id: t.id, title: t.title, status: t.status, owner: t.owner });
  const tasksMoved = JSON.stringify(S.fromMsg) !== before;
  // What each view last had, so unread survives a reload. On a team never
  // opened in this browser, everything so far counts as read.
  const latest = S.state.latest || {};
  if (!Object.keys(S.seen).some((k) => k.startsWith(`${S.team}|`))) {
    for (const [k, ts] of Object.entries(latest)) S.seen[`${S.team}|${k}`] = ts;
    save("agychat.seen", S.seen);
  }
  for (const [k, ts] of Object.entries(latest)) if (ts > (S.latest[k] || "")) S.latest[k] = ts;
  // Mentions of you since you last looked, not only the ones that arrive live.
  for (const [k, ts] of Object.entries(S.state.mentions_of_user || {}))
    if (ts > (S.seen[`${S.team}|${k}`] || "") && !(S.view && viewKey(S.view) === k)) S.atMe[k] = true;
  $("#me").textContent = `posting as ${S.state.author}`;
  document.querySelector('.nav-link[href^="/admin"]')?.classList.toggle("hidden", !!S.state.member);
  $("#new-channel")?.classList.toggle("hidden", !!S.state.member);
  renderSidebar(); renderAgents(); renderTasks(); renderSupervisor();
  if (S.view) { renderViewTasks(); renderBookmarks(); }
  if (tasksMoved && S.view && S.messagesFor === viewKey(S.view)) { renderMessages(); if (S.thread) openThread(S.thread).catch(() => {}); }
}

function renderSupervisor() {
  const st = S.state; const pill = $("#sup-pill");
  if (st.supervisor && st.supervisor.draining) {
    const b = st.supervisor;
    pill.textContent = `restarting: waiting for ${(b.inflight || []).join(", ") || "the restart"}`;
    pill.title = "No new turns start; mail keeps queuing. The supervisor restarts on the installed code when these turns finish.";
    pill.className = "pill";
  }
  else if (st.supervisor) {
    const b = st.supervisor, src = b.source || {};
    pill.textContent = "running" + (b.runner ? ` · ${b.runner}` : "") + (b.busy ? ` · ${b.busy}` : "");
    // Where the runner came from: "default" on a team that needs another
    // runner is the misconfiguration the roster's supervisor block fixes.
    pill.title = b.runner ? `runner ${b.runner_class || b.runner} (from ${src.runner || "?"}); manager ${b.manager || "none"}`
      + ` (${src.manager === "roster" ? "from the roster" : "detected from roles"}); pid ${b.pid}` : `pid ${b.pid}`;
    pill.className = "pill ok";
  }
  else if (st.stopped) { pill.textContent = `stopped: ${st.stopped}`.slice(0, 60); pill.className = "pill bad"; }
  else { pill.textContent = "no supervisor: nothing will wake"; pill.className = "pill bad"; }
  $("#sup-start").classList.toggle("hidden", !!st.supervisor || !!S.state.member);
  $("#sup-stop").classList.toggle("hidden", !st.supervisor || !!S.state.member);
  $("#sup-restart").classList.toggle("hidden", !st.supervisor || !!st.supervisor.draining || !!S.state.member);
}

// A view is unread while anything in it is: a message since you last
// looked, or a thread there with a reply since you last opened it (a thread
// never opened counts from when you last looked at the view). Opening the
// channel alone no longer clears a thread you have not read.
function threadUnread(k) {
  const base = S.seen[`${S.team}|${k}`] || "";
  return Object.entries(S.threadLatest?.[k] || {}).some(([root, ts]) => ts > (S.tseen[tKey(root)] ?? base));
}
function navItem(v, label, extra = "", opts) {
  const k = viewKey(v); const active = S.view && viewKey(S.view) === k;
  const unread = ((S.latest[k] || "") > (S.seen[`${S.team}|${k}`] || "") || threadUnread(k) || S.marked[`${S.team}|${k}`]) && !active;
  const mentioned = S.atMe[k] && !active;
  const at = mentioned ? `<span class="at" title="${v.type === "dm" ? "a message to you" : "mentions you"}">@</span>` : "";
  return `<li class="${active ? "active" : ""} ${unread ? "unread" : ""} ${mentioned ? "mentioned" : ""} ${opts?.cls || ""}" data-view='${esc(JSON.stringify(v))}'>${label}${at}${extra}</li>`;
}

// The thread's reply box, addressed to whoever asked, unless something is
// already being written there.
function answerTo(agent) {
  const ta = $("#drawer-thread .thread-compose textarea");
  if (!ta) return;
  if (!ta.value.trim()) ta.value = `@${agent} `;
  ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
}

// What needs a person, first and only when there is something: a-team decides
// what counts, and a reply, a closed task or the × clears it. A notice has no
// one to reply to, so the × and "Clear all" are the way to clear one.
function needsItem(n) {
  const text = (s) => esc(String(s || "").replace(/\s+/g, " ").slice(0, 90));
  const row = (cls, label, title, attrs) => `<li class="need ${cls}" title="${esc(title || "")}" ${attrs}>${label}</li>`;
  if (n.kind === "stopped") return row("bad", `<span class="what">Team stopped</span> ${text(n.reason)}`, n.reason, "");
  if (n.kind === "question" || n.kind === "blocker") {
    const v = n.channel ? { type: "channel", id: n.channel } : { type: "dm", pair: ["user", n.dm] };
    const key = n.key || n.id;   // key: also for a message from before ids
    const clear = key && !S.state.member
      ? `<button class="need-clear" data-clear="${esc(key)}" title="Clear without replying. It stays in the conversation, and ${n.from === "supervisor" ? "nothing is sent" : esc(nameOf(n.from)) + " is not told"}." aria-label="Clear">×</button>` : "";
    // In a channel a reply answers it in its own thread, or by naming who
    // asked (which is also what wakes them): so the row opens that thread
    // with the @ already written.
    const th = n.thread || (n.channel && n.id) || "";
    const answer = n.channel && n.from !== "supervisor" ? ` data-answer="${esc(n.from)}"` : "";
    return row(n.kind === "blocker" ? "bad" : "", `${clear}<span class="what">${esc(nameOf(n.from))}</span> ${text(n.content)}`,
      `${n.kind} from ${n.from}: ${n.content}`, `data-view='${esc(JSON.stringify(v))}'${th ? ` data-then-thread="${esc(th)}"` : ""}${answer}`);
  }
  if (n.kind === "duty")
    return row("bad", `<a href="${esc(boardURL(n.id))}"><span class="what">${esc(nameOf(n.owner))}'s duty is behind</span> ${text(n.title)}</a>${talkLink(n)}`,
      `${n.title} (every ${n.every}): ${n.reason}${n.last_done ? "; last done " + n.last_done : "; never done"}`, "");
  const what = n.kind === "stuck" ? `${esc(nameOf(n.owner))} is blocked`
    : S.state.member ? "The operator's task" : "Your task";
  const done = n.kind === "task" && !S.state.member
    ? `<button class="need-clear" data-done-task="${esc(n.id)}" title="Done: close this task" aria-label="Done">✓</button>` : "";
  return row(n.kind === "stuck" ? "bad" : "", `${done}<a href="${esc(boardURL(n.id))}"><span class="what">${what}</span> ${text(n.title)}</a>${talkLink(n)}`, n.title, "");
}
// Where a task in Needs you is talked about: its thread, its channel, or the
// message it came from.
function talkLink(n) {
  const o = n.origin || {};
  if (o.id) return ` <a href="#" class="need-talk" title="Open the message it came from" data-goto="${esc(o.id)}">💬</a>`;
  const v = n.channel ? { type: "channel", id: n.channel } : o.channel ? { type: "channel", id: o.channel }
    : o.id ? { type: "dm", pair: [o.from, o.to] } : null;
  if (!v) return "";
  const th = n.channel ? n.thread : (o.thread || o.id);
  return ` <a href="#" class="need-talk" title="Open where it is discussed" data-view='${esc(JSON.stringify(v))}'${th ? ` data-then-thread="${esc(th)}"` : ""}>💬</a>`;
}

function renderNeeds() {
  const rows = S.state.needs_you || [];
  $("#needs-head").classList.toggle("hidden", !rows.length);
  $("#needs").classList.toggle("hidden", !rows.length);
  $("#needs-count").textContent = rows.length;
  const notices = rows.filter((n) => n.from === "supervisor" && (n.key || n.id)).length;
  $("#needs").innerHTML = (notices > 1 && !S.state.member
    ? `<li class="need-all"><button data-clear-notices>Clear all ${notices} notices</button></li>` : "")
    + rows.map(needsItem).join("");
}

async function clearWaiting(id) {
  try {
    await api("/api/clear_waiting", id ? { team: S.team, ids: [id] } : { team: S.team, from: "supervisor" });
    await refreshState();
  } catch (err) { toast(err.message, true); }
}

function renderSidebar() {
  const st = S.state;
  renderNeeds();
  const chans = Object.entries(st.channels || {}).sort();
  const open = chans.filter(([, c]) => !c.archived);
  const shut = chans.filter(([, c]) => c.archived);
  $("#channels").innerHTML = open.map(([n, c]) =>
    navItem({ type: "channel", id: n }, `# ${esc(n)}`, `<span class="count">${(c.members || []).length}</span>`)).join("");
  $("#archived-head").classList.toggle("hidden", !shut.length);
  $("#archived-count").textContent = shut.length ? `(${shut.length})` : "";
  const showShut = load("agychat.showArchived", false);
  $("#archived-toggle").firstChild.textContent = (showShut ? "▾" : "▸") + " Archived ";
  $("#archived").classList.toggle("hidden", !showShut || !shut.length);
  $("#archived").innerHTML = shut.map(([n, c]) =>
    navItem({ type: "channel", id: n }, `# ${esc(n)}`, c.deleted ? `<span class="count">deleted</span>` : "",
            { cls: c.deleted ? "deleted" : "" })).join("");
  const pres = Object.fromEntries(st.agents.map((a) => [a.name, a.presence.state || "offline"]));
  $("#dms").innerHTML = st.agents.map((a) =>
    navItem({ type: "dm", pair: ["user", a.name] }, `<span class="dot ${pres[a.name]}"></span>${esc(nameOf(a.name))}`)).join("")
    + navItem({ type: "dm", pair: ["supervisor", "user"] }, `<span class="dot offline"></span>notices`);
  const pairs = (st.conversations || []).filter((p) => !p.includes("user") && !p.includes("supervisor"));
  $("#pairs-head").classList.toggle("hidden", !pairs.length);
  $("#pairs").innerHTML = pairs.map((p) => navItem({ type: "dm", pair: p }, `${esc(nameOf(p[0]))} ↔ ${esc(nameOf(p[1]))}`)).join("");
}

function renderAgents() {
  $("#agents").innerHTML = S.state.agents.map((a) => {
    const p = a.presence || {}; const m = a.model || {};
    const state = { running: `working${p.detail ? " — for " + p.detail : ""} since ${hhmm(p.since)}`,
                    distilling: `saving what it learned to memory${p.detail ? " " + p.detail : ""}${p.since ? ", since " + hhmm(p.since) : ""}`,
                    held: `waiting: ${p.detail || "hop budget spent"}; your messages still wake it`,
                    failing: `last turn failed: ${p.detail || ""}`, idle: `idle${p.since ? ", last turn " + hhmm(p.since) : ""}`,
                    offline: "offline (no supervisor)" }[p.state] || "";
    const over = (m.source || "").startsWith("override");
    const bad = m.available === false;
    const fallsBack = over && m.roster_available === false;
    const tip = bad ? `${m.model} is not offered by this team's runner: every turn will fail. Click to choose another.`
      : fallsBack ? `${m.source}. When it expires, ${a.name} goes back to the roster's ${m.roster_model}, which this runner does not offer.`
      : (m.source || "");
    const qt = quotaText(m.quota);
    return `<li data-agent="${esc(a.name)}"><span class="dot ${esc(p.state || "offline")}"></span><span class="name" title="${esc(a.name)}">${esc(nameOf(a.name))}${a.alias ? ` <span class="alias-of">${esc(a.name)}</span>` : ""}</span>
      <button class="model ${over ? "override" : ""} ${bad ? "unavailable" : ""}" data-agent="${esc(a.name)}" title="${esc([tip, qt].filter(Boolean).join(" — "))}">${bad || fallsBack ? "⚠ " : ""}${esc(m.model || "default")}${m.effort ? "·" + esc(m.effort) : ""}${m.quota?.low ? `<span class="quota-low">${m.quota.cooling_until ? "cooling" : m.quota.remaining != null ? Math.round(m.quota.remaining * 100) + "%" : "limit"}</span>` : ""}</button>
      <span class="state" title="${esc(state)}">${esc(state)}</span>${loadHTML(a.load)}</li>`;
  }).join("");
}

// What is known of a model's quota (ateam.quota): how much is left of the
// tightest limit, when it resets, a credit balance. "" when nothing is.
function quotaText(qu) {
  if (!qu) return "";
  const parts = [];
  if (qu.remaining != null) parts.push(`${Math.round(qu.remaining * 100)}% left${qu.window ? ` of the ${qu.window.replace(/_/g, " ")} limit` : ""}`);
  else if (qu.status === "rejected") parts.push("at its limit");
  else if (qu.status === "allowed_warning") parts.push("near its limit");
  if (qu.resets_at) parts.push(`resets ${fmtTime(Date.parse(qu.resets_at) / 1000, qu.resets_at)}`);
  if (qu.credits != null) parts.push(`credits ${qu.credits}`);
  if (qu.cooling_until) parts.unshift(`rate limited: passed over for its fallback until ${fmtTime(Date.parse(qu.cooling_until) / 1000, qu.cooling_until)}`);
  return parts.join("; ");
}

// The same figures agents get from list_teammates, to see who has room.
function loadHTML(l) {
  if (!l) return "";
  const pct = (x) => `${Math.round(x * 100)}%`;
  const hours = l.busy_s >= 3600 ? `${(l.busy_s / 3600).toFixed(1)} h` : `${Math.round(l.busy_s / 60)} min`;
  const tip = `In turns: ${pct(l.busy.hour)} of the last hour, ${pct(l.busy.day)} of the last day; `
    + `${l.turns} turn(s), ${hours} in all.${l.waiting ? ` ${l.waiting} waking message(s) not yet picked up.` : ""}`
    + " Agents see the same in list_teammates.";
  return `<span class="load" title="${esc(tip)}"><span class="bar"><i style="width:${pct(l.busy.hour)}"></i></span>`
    + `${pct(l.busy.hour)} hr · ${pct(l.busy.day)} day${l.waiting ? ` · ${l.waiting} waiting` : ""}</span>`;
}

function renderTasks() {
  const done = $("#show-done").checked;
  const groups = {};
  for (const t of S.state.tasks || []) {
    const finished = ["done", "cancelled", "abandoned"].includes(t.status);
    if (finished && !done) continue;
    (groups[t.status || "open"] ||= []).push(t);
  }
  const order = ["running", "claimed", "blocked", "review", "queued", "icebox", "done", "failed", "cancelled"];
  const keys = Object.keys(groups).sort((a, b) => (order.indexOf(a) + 99) % 99 - (order.indexOf(b) + 99) % 99);
  $("#tasks").innerHTML = keys.length ? keys.map((k) => `<div class="task-group">${esc(k)} · ${groups[k].length}</div>` +
    groups[k].map((t) => `<div class="task"><div class="t"><a href="${boardURL(t.id)}" title="Open on the board">${esc(t.title)}</a></div><div class="meta">
      <span>${esc(t.owner ? nameOf(t.owner) : "unclaimed")}</span><span class="tag">${esc(t.project || "")}</span>
      ${t.channel ? `<span class="chan" data-view='${esc(JSON.stringify({ type: "channel", id: t.channel }))}'>#${esc(t.channel)}</span>` : ""}
      ${t.requires_review ? `<span class="tag ${t.reviewed ? "ok" : "rev"}">${t.reviewed ? "reviewed" : "needs review"}</span>` : ""}
      ${t.check_after ? `<span class="tag" title="reminder">⏰ ${esc(String(t.check_after).slice(5, 16))}</span>` : ""}
      ${t.until ? `<span class="tag">waiting on condition</span>` : ""}</div></div>`).join("")).join("")
    : `<div class="task-group">no tasks</div>`;
}

async function openView(v, { jump = true } = {}) {
  keepDraft();
  if (!S.view || viewKey(S.view) !== viewKey(v)) remember(hashFor(v));
  S.view = v; save(`agychat.view.${S.team}`, v);
  S.keepUnread = null; S.messagesFor = null;
  if (S.marked[`${S.team}|${viewKey(v)}`]) { delete S.marked[`${S.team}|${viewKey(v)}`]; save("agychat.marked", S.marked); }
  const st = S.state;
  const c = v.type === "channel" ? (st.channels || {})[v.id] || {} : {};
  delete S.atMe[viewKey(v)];
  const banner = $("#view-banner");
  banner.classList.toggle("hidden", !c.archived);
  if (c.archived) banner.innerHTML = c.deleted
    ? `Deleted ${esc(c.deleted)}. Its history is hidden here and kept in the team's record. <button class="btn small" data-chan-act="restore">Restore</button>`
    : `Archived ${esc(c.archived)}: read-only, and out of every agent's channel list. <button class="btn small" data-chan-act="restore">Restore</button>`;
  if (v.type === "channel") {
    $("#view-title").textContent = `# ${v.id}`;
    const wake = (c.wake || []).map(nameOf).join(", ");
    $("#view-sub").textContent = `${(c.members || []).map(nameOf).join(", ") || "no agents"}${c.topic ? " — " + c.topic : ""}`
      + (wake ? ` · every post wakes ${wake}` : "");
    $("#input").placeholder = wake ? `Message #${v.id} — wakes ${wake}; anyone else needs an @mention`
      : `Message #${v.id} — only @mentions wake agents (@name, @here, @all)`;
    $("#edit-members").classList.remove("hidden");
    $("#new-task").classList.toggle("hidden", !!c.archived);
  } else {
    const other = v.pair.find((p) => p !== "user") || v.pair[0];
    const mine = v.pair.includes("user") && other !== "supervisor";
    $("#view-title").textContent = mine ? nameOf(other) : v.pair.map(nameOf).join(" ↔ ");
    $("#view-sub").textContent = mine ? "direct messages — every message wakes them" :
      other === "supervisor" ? "notices from the supervisor — nothing reads a reply; clear them under Waiting on you" : "agent-to-agent conversation (read-only)";
    $("#input").placeholder = mine ? `Message ${nameOf(other)}` : "Read-only";
    $("#edit-members").classList.add("hidden");
    $("#new-task").classList.add("hidden");
  }
  renderViewTasks(); renderBookmarks();
  S.draftFor = `${S.team}|${viewKey(v)}`;
  $("#input").value = S.drafts[S.draftFor] || "";
  $("#composer").classList.toggle("hidden", !!c.archived ||
    (v.type === "dm" && (!v.pair.includes("user") || v.pair.includes("supervisor"))));
  const params = v.type === "channel" ? { channel: v.id } : { dm: v.pair.join(",") };
  const res = await api(`/api/messages?${q(params)}`);
  if (S.view !== v) return;                    // another view was opened meanwhile
  S.messages = res.messages; S.replyCounts = res.reply_counts || S.replyCounts;
  S.messagesFor = viewKey(v); indexMsgs(S.messages);
  S.base = S.seen[`${S.team}|${viewKey(v)}`] || "";
  noteThreads();
  markSeen(); renderMessages(); renderSidebar();
  if (jump) jumpToNew();
}
function indexMsgs(list) { for (const m of list) if (m.id) S.msgIndex.set(m.id, m); }

// Threads: a reply is new if it came after you last opened its thread, or,
// for a thread you never opened, after you last looked at the view. That
// second date is written down when the view opens, so an unopened thread
// stays marked on later visits until you open it.
const tKey = (id) => `${S.team}|${id}`;
function lastReplies() {
  const out = {};
  for (const m of S.messages) if (m.thread) (out[m.thread] ||= []).push(m);
  return out;
}
function noteThreads() {
  let changed = false;
  for (const [root, reps] of Object.entries(lastReplies())) {
    if (S.tseen[tKey(root)] !== undefined) continue;
    // A view never looked at before: its threads count as read.
    S.tseen[tKey(root)] = S.base || reps[reps.length - 1].ts; changed = true;
  }
  if (changed) saveThreadsSeen();
}
function saveThreadsSeen() {
  const keys = Object.keys(S.tseen);
  if (keys.length > 4000)                // keep the most recent
    S.tseen = Object.fromEntries(Object.entries(S.tseen).sort((a, b) => String(b[1]).localeCompare(String(a[1]))).slice(0, 3000));
  save("agychat.tseen", S.tseen);
}
function computeFresh() {
  S.fresh = {};
  for (const [root, reps] of Object.entries(lastReplies())) {
    const since = S.tseen[tKey(root)] ?? S.base;
    const fresh = reps.filter((m) => m.from !== "user" && m.ts > since);
    if (fresh.length) S.fresh[root] = { n: fresh.length, at: fresh.some(forMe) };
  }
}
// Open on what changed: the first thing addressed to you, else the first
// new message or thread with new replies; marked for a moment. A mention in
// a thread reply opens that thread too (first, since the panel narrows the
// view and moves things). Nothing new: the end, as before.
async function jumpToNew() {
  const box = $("#messages");
  const showQuiet = $("#show-quiet").checked;
  const roots = S.messages.filter((m) => !m.thread || !S.messages.some((r) => r.id === m.thread))
    .filter((m) => showQuiet || !QUIET.has(m.kind));
  const isNew = (m) => S.base && m.from !== "user" && m.ts > S.base;
  const target = roots.find((m) => (isNew(m) && forMe(m)) || S.fresh[m.id]?.at)
    || roots.find((m) => isNew(m) || S.fresh[m.id]);
  if (!target) { box.scrollTop = box.scrollHeight; return; }
  if (S.fresh[target.id]?.at && !(isNew(target) && forMe(target))) await openThread(target.id).catch(() => {});
  const el = box.querySelector(`.msg[data-id="${CSS.escape(target.id || "")}"]`);
  if (!el) { box.scrollTop = box.scrollHeight; return; }
  el.scrollIntoView({ block: "center" });
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 2600);
}

// The tasks this channel is about: the ones that name it. They live in the
// panel's Tasks tab; the header has only a count that opens it. Chips in the
// header grew it by a line or more per few tasks, and squeezed the tools.
const taskOwner = (t) => (t.owner ? nameOf(t.owner) : "unclaimed");
function tokensText(u) {
  if (!u || !u.tokens) return "";
  const n = u.tokens >= 1e6 ? (u.tokens / 1e6).toFixed(1) + "M" : u.tokens >= 1e3 ? Math.round(u.tokens / 1e3) + "k" : String(u.tokens);
  return `${n} tokens`;
}
function channelTasks() {
  const v = S.view;
  return v && v.type === "channel" ? (S.state.tasks || []).filter((t) => t.channel === v.id) : [];
}
function renderViewTasks() {
  const btn = $("#view-tasks"); const mine = channelTasks();
  const open = mine.filter((t) => !TERMINAL.has(t.status));
  btn.classList.toggle("hidden", !mine.length);
  btn.textContent = open.length ? `Tasks · ${open.length} open ▸` : "Tasks ▸";
  btn.title = open.length
    ? open.slice(0, 8).map((t) => `${t.status} · ${t.title} · ${taskOwner(t)}`).join("\n") + (open.length > 8 ? `\n+${open.length - 8} more` : "")
    : `${mine.length} closed task${mine.length === 1 ? "" : "s"}`;
  if (!$("#drawer-tasks").classList.contains("hidden")) renderPanelTasks();
}
function renderPanelTasks() {
  const box = $("#drawer-tasks"); const v = S.view;
  if (!v || v.type !== "channel") { box.innerHTML = `<div class="empty">Tasks are shown for a channel.</div>`; return; }
  const all = load("agychat.panelTasksAll", false);
  const mine = channelTasks(); const rows = all ? mine : mine.filter((t) => !TERMINAL.has(t.status));
  const order = ["running", "claimed", "review", "blocked", "queued", "icebox", "done", "failed", "cancelled"];
  rows.sort((a, b) => order.indexOf(a.status) - order.indexOf(b.status) || (b.t_updated || 0) - (a.t_updated || 0));
  box.innerHTML = `<div class="artifact-head">#${esc(v.id)} — tasks
      <label class="toggle"><input type="checkbox" id="panel-all" ${all ? "checked" : ""}> closed too</label></div>
    ${rows.map((t) => `<div class="panel-task"><div class="t"><span class="st ${esc(t.status)}"></span><a href="${boardURL(t.id)}">${esc(t.title)}</a></div>
      <div class="meta"><span>${esc(taskOwner(t))}</span><span class="tag">${esc(t.status)}</span>${t.usage ? `<span class="tag" title="${esc(t.usage.direct)} turn(s) named it, ${esc(t.usage.inferred)} inferred from who was working on it">${esc(tokensText(t.usage))}</span>` : ""}
      <span class="when">${esc(fmtTime(t.t_updated, t.updated_at, "short"))}</span></div></div>`).join("")
      || `<div class="empty">${mine.length ? "No open tasks." : "No tasks in this channel. + Task in the header makes one."}</div>`}`;
  $("#panel-all").onchange = (e) => { save("agychat.panelTasksAll", e.target.checked); renderPanelTasks(); };
}

// Bookmarks on a channel: links, go links, paths, kept with the channel.
function bookmarkHTML(b) {
  const h = href(escHTML(b.url));
  return h ? `<a class="bookmark" href="${h}" target="_blank" rel="noopener" title="${esc(b.url)}">🔖 ${esc(b.title)}</a>`
           : `<span class="bookmark pill-file" data-path="${esc(b.url)}" title="${esc(b.url)}">🔖 ${esc(b.title)}</span>`;
}
function renderBookmarks() {
  const box = $("#view-bookmarks"); const v = S.view;
  const c = v && v.type === "channel" ? (S.state.channels || {})[v.id] || {} : null;
  box.classList.toggle("hidden", !c);
  if (!c) return;
  box.innerHTML = (c.bookmarks || []).map(bookmarkHTML).join("")
    + (c.archived ? "" : `<button class="bookmark add" data-bookmarks="1" title="Add or edit this channel's bookmarks">${(c.bookmarks || []).length ? "edit" : "+ bookmark"}</button>`);
}
function bookmarksModal() {
  const name = S.view.id; const c = S.state.channels[name] || {};
  const text = (c.bookmarks || []).map((b) => b.title && b.title !== b.url ? `${b.title} | ${b.url}` : b.url).join("\n");
  modal(`<h3>#${esc(name)} bookmarks</h3>
    <label>One per line: <code>title | link</code>, or just the link</label>
    <textarea name="b" rows="7" placeholder="Runbook | go/runbook&#10;https://example.com/dashboard&#10;Design | ~/docs/design.md">${esc(text)}</textarea>
    <div class="hint">A link can be a web address, a go link or a file path.</div>`,
    async (fd) => {
      const marks = String(fd.get("b") || "").split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
        const [a, ...rest] = l.split("|"); return rest.length ? { title: a.trim(), url: rest.join("|").trim() } : { url: a.trim() };
      });
      await api("/api/bookmarks", { channel: name, bookmarks: marks });
      await refreshState(); renderBookmarks();
    });
}

function markSeen() {
  const last = S.messages[S.messages.length - 1];
  const k = viewKey(S.view);
  if (S.keepUnread === k) return;              // marked unread while open
  if (last) { S.seen[`${S.team}|${k}`] = last.ts; S.latest[k] = S.latest[k] > last.ts ? S.latest[k] : last.ts; save("agychat.seen", S.seen); }
}

function renderMessages() {
  const box = $("#messages");
  const showQuiet = $("#show-quiet").checked;
  const roots = S.messages.filter((m) => !m.thread || !S.messages.some((r) => r.id === m.thread));
  if (!roots.length) { box.innerHTML = `<div class="empty">Nothing here yet.</div>`; return; }
  computeFresh();
  const html = []; let hidden = 0; let divided = false;
  const flush = () => { if (hidden) { html.push(`<div class="collapsed-quiet" data-unhide="1">${hidden} quiet update${hidden > 1 ? "s" : ""} (ack, fyi, status) hidden</div>`); hidden = 0; } };
  for (const m of roots) {
    if (!showQuiet && QUIET.has(m.kind)) { hidden++; continue; }
    flush();
    if (!divided && S.base && m.from !== "user" && m.ts > S.base) { html.push(`<div class="new-divider"><span>new</span></div>`); divided = true; }
    html.push(msgHTML(m, { showTo: S.view.type === "dm" && !S.view.pair.includes("user") }));
  }
  flush();
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.innerHTML = html.join("");
  hydrateLinks(box);
  if (atBottom || !renderMessages.done) box.scrollTop = box.scrollHeight;
  renderMessages.done = true;
}

// --- drawer -----------------------------------------------------------------------

function openDrawer(tab) {
  $("#drawer").classList.remove("hidden"); $("#drawer-split").classList.remove("hidden");
  for (const b of document.querySelectorAll(".tab")) b.classList.toggle("active", b.dataset.tab === tab);
  for (const t of ["tasks", "thread", "artifact", "activity"]) $(`#drawer-${t}`).classList.toggle("hidden", tab !== t);
  if (tab === "tasks") renderPanelTasks();
}

// A reply arriving re-renders the thread's messages only: the reply box, and
// what is being typed in it, stays as it is. What is typed is also kept under
// the thread (S.drafts), so another thread or a reload does not lose it.
// One at a time for a thread: two things can open the same one at once (a
// Waiting on you row, and the view it opens jumping to what is new), and the
// second used to draw over the first, losing where the unread began and
// whatever was in the reply box. A request that arrives meanwhile has the
// thread drawn once more after, so a reply that landed in between shows.
let threadOpening = null;
async function openThread(id) {
  if (threadOpening?.id === id) { threadOpening.again = true; return threadOpening.promise; }
  const mine = threadOpening = { id, again: false };
  mine.promise = (async () => {
    await drawThread(id);
    while (mine.again && S.thread === id) { mine.again = false; await drawThread(id); }
  })();
  try { await mine.promise; } finally { if (threadOpening === mine) threadOpening = null; }
}
async function drawThread(id) {
  // Opened, as against drawn again because a reply arrived while it was open.
  const opening = S.thread !== id || $("#drawer").classList.contains("hidden") || $("#drawer-thread").classList.contains("hidden");
  if (S.thread !== id && S.view) remember(`${hashFor(S.view)}&thread=${enc(id)}`);
  S.thread = id;
  const box = $("#drawer-thread");
  const kept = box.querySelector(`.thread-compose[data-thread="${CSS.escape(id)}"]`);
  const pop = $("#mention-box");               // keep the popover out of what is re-rendered
  if (!kept && pop && pop.closest("#drawer")) { pop.classList.add("hidden"); $("#composer").insertBefore(pop, $("#composer").firstChild); S.ta = null; }
  const { messages } = await api(`/api/messages?${q({ thread: id })}`);
  if (S.thread !== id) return;                 // another thread was opened meanwhile
  indexMsgs(messages); S.threadMsgs = messages;
  const root = messages.find((m) => m.id === id) || messages[0];
  const last = messages.reduce((a, m) => (m.ts > a ? m.ts : a), "");
  // Where what you have not read begins, as it was when you opened the
  // thread: marked "new", and where the pane starts. A long thread opened
  // at its first message had you scroll past all you had read to find it.
  if (opening) {
    const since = S.tseen[tKey(id)] ?? (S.base || undefined);
    const first = since === undefined ? null : messages.find((m) => m.id !== id && m.from !== "user" && m.ts > since);
    S.threadNew = first ? { thread: id, id: first.id } : null;
  }
  const newAt = S.threadNew?.thread === id ? S.threadNew.id : null;
  if (last && last !== S.tseen[tKey(id)]) {
    S.tseen[tKey(id)] = last; saveThreadsSeen();
    if (S.fresh[id]) renderMessages();
    renderSidebar();
  }
  const where = root?.channel ? { channel: root.channel } : { to: (root?.from === "user" ? root?.to : root?.from) };
  const canReply = root && root.from !== "supervisor" && (root.channel || root.from === "user" || root.to === "user");
  const html = messages.map((m) => (newAt && m.id === newAt ? `<div class="new-divider"><span>new</span></div>` : "") + msgHTML(m, { inThread: true })).join("");
  // Looked for again now: the thread may have been drawn while this was
  // fetching (opened twice at once), and its reply box may have text in it.
  const here = kept || box.querySelector(`.thread-compose[data-thread="${CSS.escape(id)}"]`);
  if (here && box.querySelector(".thread-msgs")) {
    box.querySelector(".thread-msgs").innerHTML = html;
    hydrateLinks(box);
  } else {
    box.innerHTML = `<div class="thread-msgs">${html}</div>` +
      (canReply ? `<form class="thread-compose" data-thread="${esc(id)}" data-where='${esc(JSON.stringify(where))}'><textarea rows="2" placeholder="Reply in thread — Enter sends, Shift+Enter for a new line"></textarea>
        <div class="composer-row"><span class="hint">${root.channel ? "mention to wake" : "wakes " + esc(where.to)}</span><button class="btn small">Reply</button></div></form>` : "");
    const ta = box.querySelector(".thread-compose textarea");
    if (ta) ta.value = S.drafts[threadDraftKey(id)] || "";
    hydrateLinks(box);
  }
  openDrawer("thread");
  if (opening) {
    const mark = box.querySelector(".new-divider");
    box.scrollTop = mark ? box.scrollTop + mark.getBoundingClientRect().top - box.getBoundingClientRect().top - 4 : 0;
  }
}

function threadDraftKey(id) { return `${S.team}|thread:${id}`; }
function keepThreadDraft(ta) {
  const id = ta.closest(".thread-compose")?.dataset.thread; if (!id) return;
  if (ta.value.trim()) S.drafts[threadDraftKey(id)] = ta.value; else delete S.drafts[threadDraftKey(id)];
  save("agychat.drafts", S.drafts);
}

async function openArtifact(path) {
  openDrawer("artifact");
  const box = $("#drawer-artifact");
  box.innerHTML = `<div class="artifact-head">${esc(path)}</div><div class="empty">loading…</div>`;
  try {
    const a = await api(`/api/artifact?${q({ path })}`);
    if (a.error) throw new Error(a.error);
    let body;
    if (a.kind === "dir") body = `<ul class="dir-list">${a.entries.map((e) => `<li data-path="${esc(e.path)}">${esc(e.name)}</li>`).join("")}</ul>`;
    else if (a.kind === "binary") body = `<div class="empty">binary file, ${a.size} bytes</div>`;
    else if (a.kind === "image") body = `<div class="artifact-img"><img src="/api/file?${q({ path: a.path })}" alt="${esc(a.path)}"></div>`;
    else if (a.kind === "markdown") {
      const raw = load("agychat.mdRaw", false);
      body = `<div class="md-toggle"><button class="btn small ghost" data-mdraw="${raw ? 0 : 1}" data-p="${esc(a.path)}">${raw ? "Rendered" : "Raw"}</button></div>`
        + (raw ? `<div class="artifact">${esc(a.content)}</div>` : `<div class="artifact-md body">${markdown(a.content)}</div>`);
    }
    else if (a.kind === "diff") body = `<div class="artifact">${esc(a.content).split("\n").map((l) =>
      `<span class="${l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : ""}">${l}</span>`).join("\n")}</div>`;
    else body = `<div class="artifact">${esc(a.content)}</div>`;
    box.innerHTML = `<div class="artifact-head">${esc(a.path)}${a.truncated ? " (truncated)" : ""}</div>${body}`;
  } catch (e) {
    box.innerHTML = `<div class="artifact-head">${esc(path)}</div><div class="empty">${esc(e.message)}</div>`;
  }
}

async function openActivity(agent) {
  openDrawer("activity");
  const box = $("#drawer-activity");
  const { events } = await api(`/api/activity?${q({ agent })}`);
  const rows = events.slice().reverse().map((e) => {
    const when = `<span class="when">${esc(fmtTime(e.t, e.ts, "short"))}</span>`;
    if (e.event === "tool_call") {
      const args = e.args ? esc(JSON.stringify(e.args)).slice(0, 400) : "";
      return `<details class="act"><summary>${when} <code>${esc(e.tool || "tool")}</code>${e.error ? ' <span class="badge blocker">error</span>' : ""}</summary><pre>${args}${e.result ? "\n→ " + esc(String(e.result)).slice(0, 600) : ""}</pre></details>`;
    }
    if (e.event === "turn") return `<div class="act">${when} <strong>turn</strong> ${e.duration_s ? Math.round(e.duration_s) + "s" : ""} · ${(e.input_tokens || 0).toLocaleString()} in · ${esc(e.model || "")}</div>`;
    if (e.event === "failure") return `<div class="act">${when} <span class="badge blocker">failed</span> ${esc((e.error || "").slice(0, 300))}</div>`;
    return `<div class="act">${when} ${esc(e.event)}</div>`;
  }).join("");
  box.innerHTML = `<div class="artifact-head">${esc(nameOf(agent))} — recent turns, tool calls and failures (newest first)</div>${rows || '<div class="empty">no recorded activity</div>'}`;
}

// --- posting ------------------------------------------------------------------------

async function send(content, where, thread) {
  const kind = thread ? "work" : $("#kind").value;
  const r = await api("/api/post", { ...where, content, thread, kind });
  const idle = S.state && !S.state.supervisor ? " — but no supervisor is running, so nothing wakes until you Start team" : "";
  if (r.woke) toast((r.woke.length ? `woke ${r.woke.join(", ")}` : "posted — nobody mentioned, so nobody was woken") + (r.woke.length ? idle : ""));
  else toast("sent" + idle);
}

$("#composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#input").value.trim(); if (!text) return;
  const v = S.view;
  const where = v.type === "channel" ? { channel: v.id } : { to: v.pair.find((p) => p !== "user") };
  try { await send(text, where); $("#input").value = ""; $("#kind").value = "work"; keepDraft(); } catch (err) { toast(err.message, true); }
});
$("#input").addEventListener("keydown", (e) => {
  if (!$("#mention-box").classList.contains("hidden") && ["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].includes(e.key)) return mentionKey(e);
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); }
});
$("#input").addEventListener("input", () => { useBox($("#input")); mentionSuggest(); keepDraft(); });
// Kept under the view the text was typed in (S.draftFor), not the one now on
// screen: openView calls this after the team may already have changed.
function keepDraft() {
  const k = S.draftFor; if (!k) return;
  const text = $("#input").value;
  if (text.trim()) S.drafts[k] = text; else delete S.drafts[k];
  save("agychat.drafts", S.drafts);
}

// --- emoji ---------------------------------------------------------------------------

const EMOJI = Object.entries({
  smile: "😄", grin: "😁", joy: "😂", wink: "😉", blush: "😊", thinking: "🤔", neutral: "😐", sweat: "😅",
  cry: "😢", sob: "😭", angry: "😠", scream: "😱", shrug: "🤷", facepalm: "🤦", eyes: "👀", wave: "👋",
  thumbsup: "👍", thumbsdown: "👎", ok: "👌", clap: "👏", pray: "🙏", muscle: "💪", raised_hands: "🙌", point_up: "☝️",
  heart: "❤️", fire: "🔥", sparkles: "✨", star: "⭐", tada: "🎉", rocket: "🚀", trophy: "🏆", hundred: "💯",
  check: "✅", x: "❌", warning: "⚠️", stop: "🛑", question: "❓", exclamation: "❗", bulb: "💡", memo: "📝",
  bug: "🐛", wrench: "🔧", hammer: "🔨", gear: "⚙️", lock: "🔒", key: "🔑", link: "🔗", pin: "📌",
  calendar: "📅", clock: "🕒", hourglass: "⏳", chart: "📈", package: "📦", books: "📚", mag: "🔍", bell: "🔔",
  coffee: "☕", pizza: "🍕", beer: "🍺", cake: "🎂", sun: "☀️", moon: "🌙", cloud: "☁️", zap: "⚡",
  robot: "🤖", brain: "🧠", ghost: "👻", skull: "💀", poop: "💩", unicorn: "🦄", turtle: "🐢", snail: "🐌",
  plus: "➕", minus: "➖", arrow_right: "➡️", arrow_up: "⬆️", repeat: "🔁", recycle: "♻️", boom: "💥", ship: "🚢",
});
function insertAtCursor(ta, text) {
  const pos = ta.selectionStart ?? ta.value.length;
  ta.value = ta.value.slice(0, pos) + text + ta.value.slice(pos);
  ta.selectionStart = ta.selectionEnd = pos + text.length; ta.focus();
}
$("#emoji").onclick = () => {
  const box = $("#emoji-box");
  if (!box.classList.contains("hidden")) return box.classList.add("hidden");
  box.innerHTML = EMOJI.map(([n, e]) => `<button type="button" class="emo" title=":${n}:" data-emo="${e}">${e}</button>`).join("");
  box.classList.remove("hidden");
};
$("#emoji-box").addEventListener("click", (e) => {
  const b = e.target.closest("[data-emo]"); if (!b) return;
  insertAtCursor($("#input"), b.dataset.emo); $("#emoji-box").classList.add("hidden");
});
document.addEventListener("mousedown", (e) => { if (!e.target.closest("#emoji-box, #emoji")) $("#emoji-box").classList.add("hidden"); });

// The box being typed in -- the message box or a thread's reply box. The
// suggestion popover moves to whichever it is.
const TA = () => (S.ta && document.body.contains(S.ta) ? S.ta : $("#input"));
function useBox(ta) {
  S.ta = ta;
  const form = ta.closest("form"); const box = $("#mention-box");
  if (form && box.parentElement !== form) form.insertBefore(box, form.firstChild);
}
document.addEventListener("input", (e) => {
  if (e.target.matches?.(".thread-compose textarea")) { useBox(e.target); mentionSuggest(); keepThreadDraft(e.target); }
});
document.addEventListener("keydown", (e) => {
  if (!e.target.matches?.(".thread-compose textarea")) return;
  if (!$("#mention-box").classList.contains("hidden") && ["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].includes(e.key)) return mentionKey(e);
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.target.closest("form").requestSubmit(); }
});

// --- slash commands -----------------------------------------------------------------
//
// "/" at the start of a word lists commands. /task, /project and one command
// per level name (from the team's task_levels) insert a task, picked by its
// title, as its id -- shown as the title in the message. The others act.

const LEVEL_DEFAULTS = ["task", "project", "program", "portfolio"];
function slashCommands() {
  const names = S.state?.task_levels?.length ? S.state.task_levels : LEVEL_DEFAULTS;
  const refs = names.map((n, i) => ({ name: n, level: i, hint: `refer to a ${n}` }));
  if (!refs.some((r) => r.name === "task")) refs.unshift({ name: "task", level: null, hint: "refer to a task" });
  refs.find((r) => r.name === "task").level = null;          // /task finds any level
  return [...refs,
    { name: "new-task", act: () => taskModal(), hint: "make a task in this channel" },
    { name: "bookmark", act: () => S.view?.type === "channel" ? bookmarksModal() : toast("bookmarks belong to a channel", true), hint: "this channel's bookmarks" },
    { name: "help", act: () => toast("/task, /project … insert a reference; @name wakes; :name for emoji; paste a picture to attach"), hint: "what you can type" }];
}
function slashSuggest(before) {
  const m = before.match(/(?:^|\s)\/([a-z-]*)(?: ([^\n]*))?$/);
  if (!m) return false;
  const box = $("#mention-box"); const cmds = slashCommands();
  if (m[2] === undefined) {
    const hits = cmds.filter((c) => c.name.startsWith(m[1]));
    if (!hits.length) { box.classList.add("hidden"); return true; }
    box.innerHTML = hits.map((c, i) => `<div class="item ${i ? "" : "sel"}" data-cmd="${esc(c.name)}">/${esc(c.name)} <span class="k">${esc(c.hint)}</span></div>`).join("");
    box.classList.remove("hidden"); return true;
  }
  const cmd = cmds.find((c) => c.name === m[1]);
  if (!cmd || cmd.act) { box.classList.add("hidden"); return !!cmd; }
  const qy = m[2].toLowerCase();
  const tasks = (S.state.tasks || []).filter((t) => (cmd.level == null || (t.node?.level ?? t.level ?? 0) === cmd.level)
    && !t.node?.archived_by && (!qy || (t.title || "").toLowerCase().includes(qy) || t.id.includes(qy)))
    .sort((a, b) => (TERMINAL.has(a.status) - TERMINAL.has(b.status)) || (b.t_updated || 0) - (a.t_updated || 0)).slice(0, 8);
  box.innerHTML = tasks.map((t, i) => `<div class="item ${i ? "" : "sel"}" data-ref="${esc(t.id)}"><span class="st ${esc(t.status)}"></span> ${esc(t.title)}
      <span class="k">${esc((t.node?.path || []).join(" › ") || t.status)}</span></div>`).join("") || `<div class="item">no ${esc(cmd.name)} matches “${esc(m[2])}”</div>`;
  box.classList.remove("hidden"); return true;
}
function slashPick(item) {
  const ta = TA(); const pos = ta.selectionStart;
  const before = ta.value.slice(0, pos);
  if (item.dataset.cmd) {
    const cmd = slashCommands().find((c) => c.name === item.dataset.cmd);
    if (cmd.act) {
      ta.value = before.replace(/\/[a-z-]*$/, "") + ta.value.slice(pos);
      $("#mention-box").classList.add("hidden"); return cmd.act();
    }
    const nb = before.replace(/\/[a-z-]*$/, `/${cmd.name} `);
    ta.value = nb + ta.value.slice(pos); ta.selectionStart = ta.selectionEnd = nb.length; ta.focus();
    return slashSuggest(nb);
  }
  if (item.dataset.ref) {
    const nb = before.replace(/\/[a-z-]+ [^\n]*$/, item.dataset.ref + " ");
    ta.value = nb + ta.value.slice(pos); ta.selectionStart = ta.selectionEnd = nb.length; ta.focus();
    $("#mention-box").classList.add("hidden");
  }
}

// @mention and :emoji: autocomplete
function mentionSuggest() {
  const ta = TA(); const before = ta.value.slice(0, ta.selectionStart);
  const box = $("#mention-box");
  if (slashSuggest(before)) return;
  const em = before.match(/(?:^|\s):([a-z_]{2,})$/);
  if (em) {
    const hits = EMOJI.filter(([n]) => n.startsWith(em[1])).slice(0, 8);
    if (!hits.length) return box.classList.add("hidden");
    box.innerHTML = hits.map(([n, e], i) => `<div class="item ${i ? "" : "sel"}" data-emoji="${e}" data-name=":${n}">${e} <span class="k">:${n}:</span></div>`).join("");
    return box.classList.remove("hidden");
  }
  const m = before.match(/(?:^|\s)@([\w-]*)$/);
  if (!m) return box.classList.add("hidden");
  // "@Ada" offers at-Ada: a name matches on its start or on any part after a
  // team prefix, ignoring case, as a-team resolves it.
  // An agent is offered by its alias (inserted as @alias) and found by
  // either; a name also matches past a team prefix.
  const q = m[1].toLowerCase();
  const parts = (n) => n.toLowerCase().split(/(?=[-_.])/).map((_, i, ps) => ps.slice(i).join("").replace(/^[-_.]/, ""));
  const opts = [...S.state.agents.map((a) => ({ handle: a.alias || a.name, hint: a.alias ? a.name : "", keys: [...parts(a.name), ...(a.alias ? [a.alias.toLowerCase()] : [])] })),
                { handle: "here", hint: "everyone in this channel", keys: ["here"] },
                { handle: "all", hint: "the whole team", keys: ["all"] }]
    .filter((o) => o.keys.some((k) => k.startsWith(q)));
  if (!opts.length) return box.classList.add("hidden");
  box.innerHTML = opts.map((o, i) => `<div class="item ${i ? "" : "sel"}" data-name="${esc(o.handle)}">@${esc(o.handle)}
    <span class="k">${esc(o.hint)}</span></div>`).join("");
  box.classList.remove("hidden");
}
function mentionPick(name, emoji) {
  const ta = TA(); const pos = ta.selectionStart;
  if (emoji) {
    const before = ta.value.slice(0, pos).replace(/:([a-z_]*)$/, emoji + " ");
    ta.value = before + ta.value.slice(pos); ta.selectionStart = ta.selectionEnd = before.length;
    $("#mention-box").classList.add("hidden"); return ta.focus();
  }
  const before = ta.value.slice(0, pos).replace(/@([\w-]*)$/, `@${name} `);
  ta.value = before + ta.value.slice(pos); ta.selectionStart = ta.selectionEnd = before.length;
  $("#mention-box").classList.add("hidden"); ta.focus();
}
function mentionKey(e) {
  const items = [...document.querySelectorAll("#mention-box .item")]; let i = items.findIndex((x) => x.classList.contains("sel"));
  if (e.key === "Escape") { $("#mention-box").classList.add("hidden"); return e.preventDefault(); }
  if (e.key === "Enter" || e.key === "Tab") {
    e.preventDefault(); const it = items[i]; if (!it) return;
    if (it.dataset.cmd || it.dataset.ref) return slashPick(it);
    if (it.dataset.name) return mentionPick(it.dataset.name, it.dataset.emoji);
    return;
  }
  e.preventDefault(); items[i]?.classList.remove("sel");
  i = (i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length; items[i].classList.add("sel");
}
$("#mention-box").addEventListener("mousedown", (e) => {
  const it = e.target.closest(".item"); if (!it) return; e.preventDefault();
  if (it.dataset.cmd || it.dataset.ref) return slashPick(it);
  if (it.dataset.name) mentionPick(it.dataset.name, it.dataset.emoji);
});

// --- live -----------------------------------------------------------------------------

function listen() {
  if (S.events) S.events.close();
  S.events = new EventSource(`/api/events?${q({})}`);
  // One look at a time: while one is on its way, what arrives asks for one
  // more after it, not one each. Looks that overlap each slow the others.
  let refetch = null, looking = false, again = false;
  const look = async () => {
    if (looking) { again = true; return; }
    looking = true;
    try { await refreshState(); } catch {} finally { looking = false; if (again) { again = false; later(); } }
  };
  const later = () => { clearTimeout(refetch); refetch = setTimeout(look, 400); };
  S.events.onmessage = (ev) => {
    const { stream, item } = JSON.parse(ev.data);
    if (stream === "bus") {
      // A reply to a reply belongs to the first message's thread, as on a
      // reload (a server from before 4.15 sends it as written).
      const up = item.thread && S.msgIndex.get(item.thread);
      if (up?.thread) item.thread = up.thread;
      if (item.id) S.msgIndex.set(item.id, item);
      if (item.thread) S.replyCounts[item.thread] = (S.replyCounts[item.thread] || 0) + 1;
      if (item.thread && item.from !== "user" && item.from !== "supervisor" && !QUIET.has(item.kind) && keyOf(item))
        (S.threadLatest[keyOf(item)] ||= {})[item.thread] = item.ts;
      const k = item.from === "supervisor" ? `d:supervisor,user` : keyOf(item);
      if (k && !(item.from === "supervisor" && item.to !== "user")) S.latest[k] = item.ts;
      if (k && forMe(item) && !(S.view && k === viewKey(S.view))) S.atMe[k] = true;
      // Only into the list loaded for this view: while another view is
      // loading, S.messages is still the last one's, where a reply would
      // have no thread to sit under and showed as a post of its own.
      if (S.view && k === viewKey(S.view) && S.messagesFor === k) { S.messages.push(item); markSeen(); renderMessages(); }
      if (S.thread && (item.thread === S.thread || item.id === S.thread)) openThread(S.thread);
      renderSidebar();
      if (item.from !== "user" && item.from !== "supervisor" && (item.to === "user" || mentionsMe(item))) document.title = `• a-team`;
      // What is waiting on you changes with a question asked and with your
      // reply to one, whether or not anything else on the team moves.
      if (item.from === "user" || item.kind === "question" || item.kind === "blocker") later();
    } else if (stream === "edits") {
      applyEdit(item);
    } else later();
  };
  S.events.onerror = () => { setTimeout(() => { if (S.events.readyState === 2) listen(); }, 3000); };
  // Reconnected after the server restarted (an update, say): look again.
  S.events.onopen = () => { if (S.eventsOpened) refreshState().catch(() => {}); S.eventsOpened = true; };
}
window.addEventListener("focus", () => { document.title = "a-team"; });

// --- clicks ---------------------------------------------------------------------------

document.addEventListener("click", async (e) => {
  const menuItem = e.target.closest("#ctx-menu .item");
  if (menuItem) { const act = S.menuActs?.[menuItem.dataset.act]; hideMenu(); return act?.(); }
  hideMenu();
  const al = e.target.closest("a.app-link");
  if (al && !e.metaKey && !e.ctrlKey && !e.shiftKey && e.button === 0) {
    const x = new URL(al.href);
    if ((x.searchParams.get("team") || S.team) !== S.team) return;      // another team: load it
    e.preventDefault(); history.pushState(null, "", x.pathname + x.search + x.hash); return restore();
  }
  const go = e.target.closest("[data-goto]");
  if (go) { e.preventDefault(); return goToMessage(go.dataset.goto); }
  const dt = e.target.closest("[data-done-task]");
  if (dt) return doneTask(dt.dataset.doneTask);
  const mn = e.target.closest("[data-menu]");
  if (mn) { const m = S.msgIndex.get(mn.dataset.menu); const r = mn.getBoundingClientRect(); return m && msgMenu(m, r.left, r.bottom, !!mn.closest("#drawer")); }
  const clear = e.target.closest("[data-clear], [data-clear-notices]");
  if (clear) return clearWaiting(clear.dataset.clear);
  const nav = e.target.closest("[data-view]");
  if (nav) {
    e.preventDefault(); await openView(JSON.parse(nav.dataset.view));
    if (!nav.dataset.thenThread) return;
    await openThread(nav.dataset.thenThread);
    return nav.dataset.answer ? answerTo(nav.dataset.answer) : undefined;
  }
  const pnl = e.target.closest("[data-panel]");
  if (pnl) return openDrawer(pnl.dataset.panel);
  if (e.target.closest("[data-bookmarks]")) return bookmarksModal();
  const md = e.target.closest("[data-mdraw]");
  if (md) { save("agychat.mdRaw", md.dataset.mdraw === "1"); return openArtifact(md.dataset.p); }
  const th = e.target.closest("[data-thread]");
  if (th) return openThread(th.dataset.thread);
  const ed = e.target.closest("[data-edit]");
  if (ed) return startEdit(ed);
  const del = e.target.closest("[data-del]");
  if (del) return deleteMessage(del.dataset.del);
  const mk = e.target.closest("[data-mktask]");
  if (mk) return taskModal({ fromMessage: S.msgIndex.get(mk.dataset.mktask) });
  const ca = e.target.closest("[data-chan-act]");
  if (ca) return channelAction(ca.dataset.chanAct, S.view.id);
  const f = e.target.closest("[data-path]");
  if (f) return openArtifact(f.dataset.path);
  if (e.target.closest("[data-unhide]")) { $("#show-quiet").checked = true; return renderMessages(); }
  const model = e.target.closest(".model[data-agent]");
  if (model) return modelModal(model.dataset.agent);
  const ag = e.target.closest("#agents li[data-agent]");
  if (ag) return openActivity(ag.dataset.agent);
  const tab = e.target.closest(".tab");
  if (tab) return openDrawer(tab.dataset.tab);
});
document.addEventListener("submit", async (e) => {
  const form = e.target.closest(".thread-compose"); if (!form) return;
  e.preventDefault(); const ta = form.querySelector("textarea"); const text = ta.value.trim(); if (!text) return;
  try { await send(text, JSON.parse(form.dataset.where), form.dataset.thread); ta.value = ""; keepThreadDraft(ta); }
  catch (err) { toast(err.message, true); }
});
$("#open-panel").onclick = () => {
  const v = S.view;
  if (v?.type === "dm") { const other = v.pair.find((x) => x !== "user"); if (other && other !== "supervisor") return openActivity(other); }
  openDrawer("tasks");
};

// --- attachments: paste, drop or 📎 -------------------------------------------------

function fileToBase64(f) {
  return new Promise((ok, bad) => { const r = new FileReader(); r.onload = () => ok(String(r.result).split(",")[1] || ""); r.onerror = bad; r.readAsDataURL(f); });
}
async function uploadFiles(files, ta = $("#input")) {
  for (const f of files) {
    const d = new Date(); const pad = (n) => String(n).padStart(2, "0");
    const stamp = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
    const name = f.name && f.name !== "image.png" ? f.name : `pasted-${stamp}.${(f.type.split("/")[1] || "png").replace("jpeg", "jpg")}`;
    try {
      toast(`attaching ${name}…`);
      const r = await api("/api/attach", { name, data: await fileToBase64(f) });
      const text = (f.type || "").startsWith("image/") ? `![${name}](${r.path})` : `[${name}](${r.path})`;
      const pos = ta.selectionStart ?? ta.value.length; const pre = ta.value.slice(0, pos);
      const ins = (pre && !pre.endsWith("\n") && !pre.endsWith(" ") ? " " : "") + text + " ";
      ta.value = pre + ins + ta.value.slice(pos); ta.selectionStart = ta.selectionEnd = pos + ins.length; ta.focus();
      toast(`attached ${name}`);
    } catch (err) { toast(err.message, true); }
  }
}
$("#attach").onclick = () => $("#attach-file").click();
$("#attach-file").onchange = (e) => { uploadFiles([...e.target.files]); e.target.value = ""; };
document.addEventListener("paste", (e) => {
  const ta = e.target.closest?.("#input, .thread-compose textarea"); if (!ta) return;
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); uploadFiles(files, ta); }
});
document.addEventListener("dragover", (e) => { if (e.target.closest?.("#composer, .thread-compose")) e.preventDefault(); });
document.addEventListener("drop", (e) => {
  const ta = e.target.closest?.("#composer, .thread-compose")?.querySelector("textarea"); if (!ta) return;
  const files = [...(e.dataTransfer?.files || [])];
  if (files.length) { e.preventDefault(); uploadFiles(files, ta); }
});

function closeDrawer() {
  $("#drawer").classList.add("hidden"); $("#drawer-split").classList.add("hidden");
  if (S.thread && S.view) remember(hashFor(S.view));
  S.thread = null;
}
$("#drawer-close").onclick = closeDrawer;
$("#team").onchange = (e) => { S.team = e.target.value; openTeam(); };
$("#show-quiet").onchange = renderMessages;
$("#show-done").onchange = renderTasks;
$("#archived-toggle").onclick = () => { save("agychat.showArchived", !load("agychat.showArchived", false)); renderSidebar(); };
$("#sup-start").onclick = async () => { try { const r = await api("/api/supervisor", { action: "start" }); toast(r.started ? `supervisor started (pid ${r.pid})` : r.reason); setTimeout(refreshState, 1500); } catch (e) { toast(e.message, true); } };
$("#sup-restart").onclick = async () => {
  const b = S.state.supervisor || {};
  if (!b.restartable) { toast("This supervisor predates graceful restart: Stop and Start it once, or use Admin → Update.", true); return; }
  if (!confirm(`Restart ${S.team}'s supervisor on the installed code? No new turns start; ${(b.inflight || []).length ? (b.inflight.join(", ") + " finish first") : "nothing is running"}. Mail keeps queuing.`)) return;
  try { await api("/api/supervisor", { action: "restart" }); toast("restart requested"); setTimeout(refreshState, 1000); } catch (e) { toast(e.message, true); }
};
$("#sup-stop").onclick = async () => { if (!confirm("Stop the team? Turns in flight are cancelled; their mail stays queued and is delivered again on Start. To keep them, use Restart.")) return; try { await api("/api/supervisor", { action: "stop" }); toast("stopping"); setTimeout(refreshState, 1500); } catch (e) { toast(e.message, true); } };

// --- modals ---------------------------------------------------------------------------

function modal(html, onSubmit, extraActions = "") {
  $("#modal-card").innerHTML = `<form>${html}<div class="modal-actions"><span class="left-actions">${extraActions}</span><button type="button" class="btn ghost" data-cancel>Cancel</button><button class="btn">Save</button></div></form>`;
  $("#modal").classList.remove("hidden");
  const form = $("#modal-card form");
  form.querySelector("[data-cancel]").onclick = () => $("#modal").classList.add("hidden");
  form.onsubmit = async (e) => { e.preventDefault(); try { await onSubmit(new FormData(form), form); $("#modal").classList.add("hidden"); } catch (err) { toast(err.message, true); } };
  wireChecks(form);
  form.querySelector("input,select")?.focus();
}
const agentChecks = (checked = [], everyone = false) => `
  <label class="everyone"><input type="checkbox" name="everyone" ${everyone ? "checked" : ""}> Every agent, including ones added later</label>
  <div class="checks-wrap ${everyone ? "hidden" : ""}"><div class="checks-tools"><button type="button" class="link" data-all>all</button> · <button type="button" class="link" data-none>none</button></div>
  <div class="checks">${S.state.agents.map((a) =>
  `<label title="${esc(a.name)}"><input type="checkbox" name="m" value="${esc(a.name)}" ${everyone || checked.includes(a.name) ? "checked" : ""}> ${esc(nameOf(a.name))}</label>`).join("")}</div></div>`;
// The "every agent" switch and the all/none shortcuts in a members picker.
function wireChecks(form) {
  const all = form.querySelector("[name=everyone]"); const wrap = form.querySelector(".checks-wrap");
  if (!all) return;
  all.onchange = () => wrap.classList.toggle("hidden", all.checked);
  form.querySelector("[data-all]").onclick = () => form.querySelectorAll("[name=m]").forEach((c) => (c.checked = true));
  form.querySelector("[data-none]").onclick = () => form.querySelectorAll("[name=m]").forEach((c) => (c.checked = false));
}

$("#new-channel").onclick = () => modal(`<h3>New channel</h3><label>Name</label><input type="text" name="name" placeholder="billing" required>
  <label>Topic</label><input type="text" name="topic" placeholder="optional"><label>Agents in it</label>${agentChecks([], true)}`,
  async (fd) => { await api("/api/channel", { action: "create", name: fd.get("name"), topic: fd.get("topic"),
                                              everyone: !!fd.get("everyone"), members: fd.getAll("m") });
    await refreshState(); openView({ type: "channel", id: String(fd.get("name")).toLowerCase() }); });

$("#edit-members").onclick = () => {
  const name = S.view.id; const c = S.state.channels[name] || {}; const before = c.members || [];
  const acts = c.archived
    ? `<button type="button" class="btn ghost" data-chan-act="restore">Restore</button>`
    : `<button type="button" class="btn ghost" data-chan-act="archive">Archive</button>
       <button type="button" class="btn danger" data-chan-act="delete">Delete…</button>`;
  modal(`<h3>#${esc(name)}</h3><label>Topic</label><input type="text" name="topic" value="${esc(c.topic || "")}">
    <label>Agents in it</label>${agentChecks(before, !!c.everyone)}
    <label>Woken by every post, no @mention needed</label>
    <div class="checks">${S.state.agents.map((a) =>
      `<label title="${esc(a.name)}"><input type="checkbox" name="w" value="${esc(a.name)}" ${(c.wake || []).includes(a.name) ? "checked" : ""}> ${esc(nameOf(a.name))}</label>`).join("")}</div>
    <div class="hint">For an agent that owns this channel's work. Its own posts never wake it. Two such agents can wake each other back and forth, which the hop budget bounds.</div>
    <div class="hint" style="margin-top:10px">Archive: read-only, out of every agent's list, reversible.
    Delete: archive and hide its history here; the messages stay in the team's record, and Restore brings it back.</div>`,
    async (fd) => {
      const after = fd.getAll("m");
      if (fd.get("topic") !== (c.topic || "")) await api("/api/channel", { action: "topic", name, topic: fd.get("topic") });
      if (fd.get("everyone")) { if (!c.everyone) await api("/api/channel", { action: "members", name, everyone: true }); }
      else await api("/api/channel", { action: "members", name, everyone: false,
                                       add: after.filter((x) => !before.includes(x)), remove: before.filter((x) => !after.includes(x)) });
      // Only members can be woken by every post: ticking a non-member adds them.
      const wake = fd.getAll("w"); const members = fd.get("everyone") ? S.state.agents.map((a) => a.name) : after;
      const join = wake.filter((x) => !members.includes(x));
      if (join.length) await api("/api/channel", { action: "members", name, add: join });
      if ([...wake].sort().join() !== [...(c.wake || [])].sort().join() || join.length)
        await api("/api/channel", { action: "wake", name, agents: wake });
      await refreshState(); openView(S.view);
    }, acts);
};

async function channelAction(act, name) {
  if (act === "delete" && !confirm(`Delete #${name}? It is archived and its history hidden here; the messages stay in the team's record, and Restore brings it all back.`)) return;
  try {
    await api("/api/channel", { action: act, name });
    $("#modal").classList.add("hidden");
    toast({ archive: `#${name} archived`, delete: `#${name} deleted`, restore: `#${name} restored` }[act]);
    await refreshState(); openView({ type: "channel", id: name });
  } catch (err) { toast(err.message, true); }
}

// A task, from the channel header (linked to it) or from a message.
function taskModal({ fromMessage } = {}) {
  const v = S.view; const m = fromMessage;
  const channel = m?.channel || (v.type === "channel" ? v.id : "");
  const parents = (S.state.tasks || []).filter((t) => !TERMINAL.has(t.status) && !t.node?.archived_by && (t.node?.level ?? 0) >= 1);
  const open = Object.entries(S.state.channels || {}).filter(([, c]) => !c.archived).map(([n]) => n).sort();
  const title = m ? (m.content || "").split("\n")[0].slice(0, 120) : "";
  const note = m ? `From ${sender(m)}${m.channel ? " in #" + m.channel : ""} at ${m.ts}${m.id ? " (message " + m.id + ")" : ""}:\n${m.content}` : "";
  modal(`<h3>New task</h3><label>Title</label><input type="text" name="title" value="${esc(title)}" required>
    <label>Part of <span class="hint">(optional: the larger piece of work it serves)</span></label>
    <input type="text" name="parent" list="parent-tasks" placeholder="top level">
    <datalist id="parent-tasks">${parents.map((t) => `<option value="${esc(t.title)}">${esc((t.node?.path || []).concat(t.title).join(" › "))}</option>`).join("")}</datalist>
    <label>Owner</label><select name="owner"><option value="">nobody yet</option>${S.state.member ? "" : `<option value="user">${esc(nameOf("user"))} (you: it waits on you)</option>`}${S.state.agents.map((a) => `<option value="${esc(a.name)}">${esc(nameOf(a.name))}</option>`).join("")}</select>
    <label>Channel</label><select name="channel"><option value="">none</option>${open.map((n) => `<option ${n === channel ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>
    <label>Column</label><select name="status"><option value="queued">To do</option><option value="icebox">Icebox</option></select>
    <label>Note</label><textarea name="note" rows="4" style="width:100%">${esc(note)}</textarea>
    <label style="display:flex;gap:6px;align-items:center"><input type="checkbox" name="rr"> requires an approved review to close</label>`,
    async (fd) => {
      const pick = String(fd.get("parent") || "").trim();
      const parent = pick ? (S.state.tasks || []).find((t) => t.id === pick || t.title === pick) : null;
      if (pick && !parent) throw new Error(`no task called “${pick}”`);
      const t = await api("/api/task", { action: "create", title: fd.get("title"), parent: parent?.id || null,
        owner: fd.get("owner") || null, channel: fd.get("channel") || null, status: fd.get("status"),
        note: fd.get("note"), requires_review: !!fd.get("rr"),
        thread: m?.channel && fd.get("channel") === m.channel ? (m.thread || m.id) : null,
        origin: m?.id ? originOf(m) : null });
      toast(`created ${t.id}` + (t.owner && t.owner !== "user" && t.status !== "icebox" ? `; ${nameOf(t.owner)} was told` : ""));
      refreshState();
    });
}
$("#new-task").onclick = () => taskModal();
const originOf = (m) => ({ id: m.id, channel: m.channel, thread: m.thread, from: m.from, to: m.to });

// --- the message menu: right-click a message, or its ⋯ ------------------------------

function showMenu(items, x, y) {
  let box = $("#ctx-menu");
  if (!box) { box = document.createElement("div"); box.id = "ctx-menu"; box.className = "popover ctx-menu"; document.body.appendChild(box); }
  S.menuActs = items.filter(Boolean).map((it) => it.act);
  box.innerHTML = items.filter(Boolean).map((it, i) => `<div class="item ${it.cls || ""}" data-act="${i}">${esc(it.label)}${it.hint ? ` <span class="k">${esc(it.hint)}</span>` : ""}</div>`).join("");
  box.classList.remove("hidden");
  const w = box.offsetWidth, h = box.offsetHeight;
  box.style.left = Math.max(4, Math.min(x, innerWidth - w - 4)) + "px";
  box.style.top = Math.max(4, y + h > innerHeight - 4 ? y - h : y) + "px";
}
function hideMenu() { $("#ctx-menu")?.classList.add("hidden"); }
async function copied(text, what) { toast(await copyText(text) ? `${what} copied` : `Could not copy: ${text}`, false); }
const followUpsOf = (id) => (S.state.tasks || []).filter((t) => t.owner === "user" && t.origin?.id === id && !TERMINAL.has(t.status));

function msgMenu(m, x, y, inThread) {
  const mine = m.from === "user" && !m.deleted && (!S.state.member || m.author === S.state.author);
  const open = followUpsOf(m.id);
  const isRoot = !m.thread;
  showMenu([
    { label: "Copy link to message", act: () => copied(msgURL(m), "Link to the message") },
    (m.thread || S.replyCounts[m.id]) && { label: "Copy link to thread", act: () => copied(threadURL(m), "Link to the thread") },
    isRoot && !inThread && m.from !== "supervisor" && { label: S.replyCounts[m.id] ? "Open thread" : "Reply in thread", act: () => openThread(m.id) },
    { label: "Mark unread from here", act: () => markUnread(m) },
    !S.state.member && (open.length
      ? { label: "Done following up", hint: "closes the follow-up", act: () => Promise.all(open.map((t) => doneTask(t.id, true))).then(() => toast("Follow-up closed")) }
      : { label: "Follow up later", hint: "waits on you", act: () => followUp(m) }),
    { label: "Make a task…", act: () => taskModal({ fromMessage: m }) },
    !m.deleted && { label: "Copy text", act: () => copied(m.content || "", "Text") },
    mine && { label: "Edit", act: () => { const b = document.querySelector(`${inThread ? "#drawer-thread" : "#messages"} [data-edit="${CSS.escape(m.id)}"]`); if (b) startEdit(b); } },
    mine && { label: "Delete…", cls: "danger", act: () => deleteMessage(m.id) },
  ], x, y);
}
function viewMenu(v, x, y) {
  const k = viewKey(v);
  showMenu([
    { label: "Mark unread", act: () => { S.marked[`${S.team}|${k}`] = true; save("agychat.marked", S.marked);
        if (S.view && viewKey(S.view) === k) { S.keepUnread = k; toast("Marked unread: it stays bold once you go elsewhere"); }
        renderSidebar(); } },
    { label: "Mark read", act: () => markViewRead(k) },
    { label: "Copy link", act: () => copied(appURL(hashFor(v)), "Link") },
  ], x, y);
}
document.addEventListener("contextmenu", (e) => {
  if (e.shiftKey) return;                       // Shift: the browser's own menu
  const row = e.target.closest(".msg[data-id]");
  if (row?.dataset.id) {
    // On a link, a picture, a text box or a selection, the browser's menu.
    if (e.target.closest("a, img, textarea, input") || String(getSelection()).trim()) return;
    const m = S.msgIndex.get(row.dataset.id); if (!m) return;
    e.preventDefault(); return msgMenu(m, e.clientX, e.clientY, !!row.closest("#drawer"));
  }
  const li = e.target.closest(".left li[data-view]");
  if (li && !li.closest("#needs")) { e.preventDefault(); viewMenu(JSON.parse(li.dataset.view), e.clientX, e.clientY); }
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideMenu(); });
document.addEventListener("scroll", hideMenu, true);

// Unread from a message on: what you had seen is set to just before it. In
// a thread, the thread's; else the view's, which also stays bold (marked)
// until you next open it, even with the view open now.
function markUnread(m) {
  const before = (list) => list.map((x) => x.ts).filter((ts) => ts < m.ts).sort().pop() || "0";
  if (m.thread) {
    S.tseen[tKey(m.thread)] = before(S.thread === m.thread && S.threadMsgs ? S.threadMsgs : [...S.msgIndex.values()].filter((x) => x.id === m.thread || x.thread === m.thread));
    saveThreadsSeen();
    if (S.thread === m.thread) $("#drawer-close").click();
    renderMessages(); renderSidebar();
    return toast("Thread marked unread from that reply");
  }
  const k = keyOf(m) && viewKey(viewOfMsg(m));
  const here = S.view && viewKey(S.view) === k;
  S.seen[`${S.team}|${k}`] = before(here ? S.messages : [...S.msgIndex.values()].filter((x) => viewKey(viewOfMsg(x)) === k));
  S.marked[`${S.team}|${k}`] = true;
  save("agychat.seen", S.seen); save("agychat.marked", S.marked);
  if (here) { S.keepUnread = k; S.base = S.seen[`${S.team}|${k}`]; renderMessages(); }
  renderSidebar();
  toast("Marked unread from here");
}
function markViewRead(k) {
  if (S.latest[k]) S.seen[`${S.team}|${k}`] = S.latest[k];
  for (const [root, ts] of Object.entries(S.threadLatest[k] || {})) if (ts > (S.tseen[tKey(root)] || "")) S.tseen[tKey(root)] = ts;
  delete S.marked[`${S.team}|${k}`]; delete S.atMe[k];
  if (S.keepUnread === k) S.keepUnread = null;
  save("agychat.seen", S.seen); save("agychat.marked", S.marked); saveThreadsSeen();
  renderSidebar();
}

// Follow up later: a task you own, from the message, waiting on you (Waiting
// on you lists it, 💬 opens the message, ✓ closes it). Agents are not told.
async function followUp(m) {
  const first = String(m.content || "").split("\n").find((l) => l.trim()) || "a message";
  try {
    await api("/api/task", { action: "create", owner: "user", title: `Follow up: ${snippet(first, 100)}`,
      note: `${sender(m)}${m.channel ? " in #" + m.channel : ""} at ${m.ts}:\n${m.content || ""}\n\n${msgURL(m)}`,
      origin: originOf(m) });
    toast("Added to Waiting on you: ✓ there when you are done");
    await refreshState();
  } catch (err) { toast(err.message, true); }
}
async function doneTask(id, quiet) {
  try {
    await api("/api/task", { action: "update", id, status: "done" });
    if (!quiet) toast("Done");
    await refreshState();
  } catch (err) { toast(err.message, true); }
}

// A listed model's quota and price, where its runner knows them.
function modelExtras(o) {
  const bits = [];
  if (o.remaining != null) bits.push(`${Math.round(o.remaining * 100)}% left`);
  if (o.resets_at) bits.push(`resets ${fmtTime(Date.parse(o.resets_at) / 1000, o.resets_at, "short")}`);
  if (o.cost) bits.push(`$${o.cost.input}/$${o.cost.output} per M tokens`);
  return bits.length ? esc(` (${bits.join(", ")})`) : "";
}

async function modelModal(agent) {
  const a = S.state.agents.find((x) => x.name === agent); const m = a.model || {};
  let offered = null, runnerLabel = "";
  try { const r = await api(`/api/models?${q({})}`); offered = r.models; runnerLabel = r.runner || ""; } catch {}
  const over = (m.source || "").startsWith("override");
  const current = over ? m.model : "";
  const picker = offered
    ? `<select name="model"><option value="">the roster's choice${m.roster_model ? ` (${esc(m.roster_model)}${m.roster_available === false ? " — NOT offered, turns would fail" : ""})` : " (runner default)"}</option>
        ${offered.map((o) => `<option value="${esc(o.id)}" ${o.id === current ? "selected" : ""}>${esc(o.label)} — ${esc(o.id)}${modelExtras(o)}</option>`).join("")}</select>
       <div class="hint">${offered.length} models offered by the ${esc(runnerLabel)} runner</div>`
    : `<input type="text" name="model" value="${esc(current)}" placeholder="model id">
       <div class="hint">This runner cannot list its models; type the id it expects.</div>`;
  modal(`<h3>${esc(agent)}'s model</h3>
    <div class="hint">now: ${esc(m.model || "runner default")} — ${esc(m.source || "")}${m.available === false ? " — <b>not offered by this runner</b>" : ""}${m.quota && quotaText(m.quota) ? ` — ${esc(quotaText(m.quota))}` : ""}</div>
    <label>Model</label>${picker}
    <label>Until</label><select name="until"><option value="">until I change it</option><option>4h</option><option>1d</option><option selected>3d</option><option>7d</option></select>
    <div class="hint" style="margin-top:8px">Applied at ${esc(agent)}'s next idle moment: it distills, then starts a fresh conversation on the new model.
    When an override expires the agent goes back to the roster's model${m.available === false && !over ? "" : ""}.</div>`,
    async (fd) => { const r = await api("/api/model", { agent, model: fd.get("model") || null, until: fd.get("until") || null });
      toast(r.model ? `${agent} → ${r.model}` : `${agent} back to the roster's model`); refreshState(); });
}

// --- search -----------------------------------------------------------------------------

let searchTimer = null;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer); const v = e.target.value.trim(); const box = $("#search-results");
  if (!v) return box.classList.add("hidden");
  searchTimer = setTimeout(async () => {
    const { hits } = await api(`/api/search?${q({ q: v })}`);
    box.innerHTML = hits.length ? hits.map((h, i) => {
      if (h.type === "message") { const m = h.item;
        const view = m.channel ? { type: "channel", id: m.channel } : { type: "dm", pair: [m.from, m.to].sort() };
        return `<div class="item" data-hit='${esc(JSON.stringify({ view, id: m.id }))}'><span class="k">${m.channel ? "#" + esc(m.channel) : esc(m.from) + " → " + esc(m.to)}</span>${esc((m.content || "").slice(0, 160))}</div>`; }
      if (h.type === "task") return `<div class="item"><span class="k">task · ${esc(h.item.status)}</span>${esc(h.item.title)} — ${esc(h.item.owner || "")}</div>`;
      return `<div class="item"><span class="k">norm</span>${esc(h.item.text)}</div>`;
    }).join("") : `<div class="item">no matches</div>`;
    box.classList.remove("hidden");
  }, 180);
});
$("#search-results").addEventListener("click", async (e) => {
  const it = e.target.closest("[data-hit]"); if (!it) return;
  const { view, id } = JSON.parse(it.dataset.hit); $("#search-results").classList.add("hidden");
  await openView(view);
  const el = document.querySelector(`.msg[data-id="${CSS.escape(id || "")}"]`);
  if (el) { el.scrollIntoView({ block: "center" }); el.classList.add("flash"); } else if (id) openThread(id);
});
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); $("#search").focus(); }
  if (e.key === "Escape") $("#search-results").classList.add("hidden");
});

// --- resizable panes ----------------------------------------------------------------

const LAYOUT = load("agychat.layout", {});
function applyLayout() {
  $(".left").style.width = LAYOUT.left ? LAYOUT.left + "px" : "";
  $("#drawer").style.width = LAYOUT.drawer ? LAYOUT.drawer + "px" : "";
  $(".left .discourse").style.flex = LAYOUT.ops ? `0 0 ${LAYOUT.ops}%` : "";
  $(".left .ops").style.flex = LAYOUT.ops ? "1 1 0" : "";
}
const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
document.addEventListener("mousedown", (e) => {
  const sp = e.target.closest(".splitter"); if (!sp) return;
  e.preventDefault();
  const which = sp.dataset.split; const left = $(".left").getBoundingClientRect();
  sp.classList.add("active"); document.body.classList.add("dragging", which === "ops" ? "row" : "col");
  const move = (ev) => {
    if (which === "left") LAYOUT.left = clamp(ev.clientX - left.left, 180, Math.min(640, innerWidth - 420));
    if (which === "drawer") LAYOUT.drawer = clamp(innerWidth - ev.clientX, 300, Math.min(1000, innerWidth - 480));
    if (which === "ops") LAYOUT.ops = clamp((ev.clientY - left.top) / left.height * 100, 12, 88);
    applyLayout();
  };
  const up = () => { sp.classList.remove("active"); document.body.classList.remove("dragging", "row", "col");
    removeEventListener("mousemove", move); removeEventListener("mouseup", up); save("agychat.layout", LAYOUT); };
  addEventListener("mousemove", move); addEventListener("mouseup", up);
});
document.addEventListener("dblclick", (e) => {
  const sp = e.target.closest(".splitter"); if (!sp) return;
  delete LAYOUT[sp.dataset.split]; save("agychat.layout", LAYOUT); applyLayout();
});
applyLayout();

loadTeams().catch((e) => toast(e.message, true));
