/* agy-chat's admin page: agents, norms, memories, update and restart.
   Reads and writes go through the server, which goes through agyteam.actions. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const S = { teams: [], team: null, admin: null, tab: "agents", memories: [], updatePoll: null };

function load(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } }
function save(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} }
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const list = (v) => Array.isArray(v) ? v.join(", ") : (v || "");

async function api(path, body) {
  const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" },
                       body: JSON.stringify({ team: S.team, ...body }) } : {};
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}
const q = (o) => new URLSearchParams({ team: S.team, ...o }).toString();

function toast(text, err) {
  const t = $("#toast"); t.textContent = text; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.add("hidden"), err ? 6000 : 2500);
}

function modal(html, onSubmit) {
  $("#modal-card").innerHTML = `<form>${html}<div class="modal-actions"><button type="button" class="btn ghost" data-cancel>Cancel</button><button class="btn">Save</button></div></form>`;
  $("#modal").classList.remove("hidden");
  const form = $("#modal-card form");
  form.querySelector("[data-cancel]").onclick = () => $("#modal").classList.add("hidden");
  form.onsubmit = async (e) => { e.preventDefault(); try { await onSubmit(new FormData(form)); $("#modal").classList.add("hidden"); } catch (err) { toast(err.message, true); } };
  form.querySelector("input,textarea")?.focus();
}

// --- tabs and team ------------------------------------------------------------------

function showTab(tab) {
  S.tab = tab; save("agychat.adminTab", tab);
  document.querySelectorAll(".admin-tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${tab}`));
  history.replaceState(null, "", `?team=${encodeURIComponent(S.team)}#${tab}`);
  if (tab === "memories") loadMemories();
  if (tab === "update") refreshVersions();
  if (tab === "tasks") showLevels();
}
document.querySelectorAll(".admin-tabs .tab").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));

async function openTeam() {
  save("agychat.team", S.team);
  renderNav("admin", S.team);
  document.title = `${S.team} — admin — agy-chat`;
  S.admin = await api(`/api/admin?${q({})}`);
  renderAgents(); renderNorms();
  $("#mem-agent").innerHTML = S.admin.agents.map((a) => `<option>${esc(a.name)}</option>`).join("");
  showTab(S.tab);
}
$("#team").onchange = (e) => { S.team = e.target.value; openTeam(); };

// --- agents -------------------------------------------------------------------------

// Pictures: click one to choose a file; "remove" clears it.
function picHTML(who) {
  const has = (S.admin.avatars || {})[who];
  const label = who === "user" ? "you" : who;
  return `<span class="pic" data-pic="${esc(who)}" title="${has ? "Change" : "Set"} ${esc(label)}'s picture">${has
    ? `<img src="/api/avatar?${q({ who })}&v=${Date.now()}" alt="">` : esc(label[0] || "?").toUpperCase()}</span>`
    + (has ? ` <button class="link small-link" data-unpic="${esc(who)}">remove</button>` : "");
}
document.addEventListener("click", async (e) => {
  const set = e.target.closest("[data-pic]");
  if (set) { S.picFor = set.dataset.pic; return $("#pic-file").click(); }
  const un = e.target.closest("[data-unpic]");
  if (un) { try { await api("/api/avatar", { who: un.dataset.unpic }); await openTeam(); } catch (err) { toast(err.message, true); } }
});
$("#pic-file").onchange = async (e) => {
  const f = e.target.files[0]; e.target.value = ""; if (!f) return;
  const data = await new Promise((ok) => { const r = new FileReader(); r.onload = () => ok(String(r.result).split(",")[1]); r.readAsDataURL(f); });
  const ext = "." + ((f.name.split(".").pop() || "png").toLowerCase());
  try { await api("/api/avatar", { who: S.picFor, data, ext }); toast("picture saved"); await openTeam(); }
  catch (err) { toast(err.message, true); }
};

// What channel membership costs: posts that wake nobody, carried by later wakes.
function renderQuiet() {
  const q = S.admin.quiet_mail;
  if (!q) { $("#quiet").textContent = "Needs a newer agy-team."; return; }
  if (!q.turns_with_quiet) { $("#quiet").textContent = `No quiet channel mail on ${q.turns} turns in the last ${q.days} days. Recorded from agy-team 3.19.0 on.`; return; }
  const share = q.share == null ? "" : `, ${(q.share * 100).toFixed(1)}% of uncached input`;
  const top = Object.entries(q.channels).slice(0, 8).map(([c, b]) => `${c === "direct" ? "direct" : "#" + c} ~${Math.round(b / 4).toLocaleString()}`).join(" · ");
  $("#quiet").innerHTML = `Last ${q.days} days: ${q.msgs} posts that mentioned nobody reached agents on ${q.turns_with_quiet} of ${q.turns} turns, about ${q.est_tokens.toLocaleString()} tokens${share}. Each then stays in context, re-read at the cached rate, until compaction.<br>Tokens by channel: ${esc(top)}`;
}

function renderAgents() {
  renderQuiet();
  $("#me-pic").innerHTML = picHTML("user");
  $("#agents").innerHTML = S.admin.agents.map((a) => `<tr data-name="${esc(a.name)}">
    <td data-label="Picture">${picHTML(a.name)}</td>
    <td class="name">${esc(a.name)}</td>
    <td data-label="Role"><input type="text" data-f="role" value="${esc(a.role)}"></td>
    <td data-label="Default model"><input type="text" data-f="model" value="${esc(a.model)}" placeholder="runner default"></td>
    <td data-label="Memory model"><input type="text" data-f="memory_model" value="${esc(a.memory_model)}" placeholder="${esc(S.admin.memory_model || "same")}" title="Model for distill, memory review and retro turns"></td>
    <td data-label="Principal"><input type="checkbox" data-f="is_principal" ${a.is_principal ? "checked" : ""} title="Faces the user and gates what reaches them"></td>
    <td data-label="Tools off"><input type="text" data-f="tools_off" value="${esc(list(a.tools_off))}" placeholder="none"></td>
    <td data-label="Alias"><input type="text" data-f="alias" value="${esc(a.alias)}" placeholder="none" title="The name it is shown by, and answers to as @alias"></td>
    <td class="row-actions"><button class="btn small hidden" data-act="save">Save</button>
      <button class="btn small ghost" data-act="remove">Remove</button></td></tr>`).join("")
    || `<tr><td colspan="9" class="sub">No agents on the roster.</td></tr>`;
}
$("#agents").addEventListener("input", (e) => {
  const tr = e.target.closest("tr"); tr.classList.add("dirty"); tr.querySelector("[data-act=save]").classList.remove("hidden");
});
$("#agents").addEventListener("click", async (e) => {
  const btn = e.target.closest("button"); if (!btn) return;
  const tr = btn.closest("tr"); const name = tr.dataset.name;
  try {
    if (btn.dataset.act === "save") {
      const body = { action: "update", name };
      tr.querySelectorAll("[data-f]").forEach((el) => { body[el.dataset.f] = el.type === "checkbox" ? el.checked : el.value; });
      await api("/api/agent", body);
      toast(`${name} saved`);
    } else if (btn.dataset.act === "remove") {
      if (!confirm(`Remove ${name} from the team? A turn it is running is cancelled. Its memories and history stay on disk; adding it back by the same name picks them up.`)) return;
      await api("/api/agent", { action: "remove", name });
      toast(`${name} removed`);
    }
    await openTeam();
  } catch (err) { toast(err.message, true); }
});
$("#add-agent").onsubmit = async (e) => {
  e.preventDefault(); const fd = new FormData(e.target);
  try {
    const r = await api("/api/agent", { action: "add", name: fd.get("name"), role: fd.get("role"),
                                         model: fd.get("model"), all_channels: !!fd.get("all_channels") });
    toast(`${r.name} added` + (r.channels?.length ? ` to ${r.channels.map((c) => "#" + c).join(", ")}` : ""));
    e.target.reset(); await openTeam();
  } catch (err) { toast(err.message, true); }
};

// --- norms --------------------------------------------------------------------------

function renderNorms() {
  $("#norms").value = S.admin.norms || "";
  $("#norms-state").textContent = S.admin.norms ? "" : "No NORMS.md yet.";
  loadCandidates();
}
async function loadCandidates() {
  let groups = [];
  try { groups = (await api(`/api/norm_candidates?${q({})}`)).groups; } catch (err) { toast(err.message, true); }
  S.candidates = groups;
  $("#candidates").innerHTML = groups.map((g, i) => `<div class="card"><div class="head">
      <strong>${esc(g.agents.join(", "))}</strong><span class="sub"> · ${esc(g.terms.slice(0, 6).join(", "))}</span>
      <button class="btn small ghost" data-draft="${i}" style="float:right">Draft</button></div>
      <ul>${g.memories.map((m) => `<li><strong>${esc(m.agent)}</strong>: ${esc(m.name)} <span class="sub">— ${esc(m.description)}</span></li>`).join("")}</ul></div>`).join("")
    || `<p class="sub">None: no lesson is in three or more agents' memories in similar words.</p>`;
}
$("#candidates").addEventListener("click", (e) => {
  const i = e.target.closest("[data-draft]")?.dataset.draft; if (i == null) return;
  const g = S.candidates[+i]; const ta = $("#norms");
  const line = `- ${g.memories[0].description} (learned separately by ${g.agents.join(", ")}: rewrite as the rule you want)`;
  ta.value = ta.value.replace(/\s*$/, "\n") + line + "\n";
  ta.focus(); ta.scrollTop = ta.scrollHeight;
  $("#norms-state").textContent = "unsaved changes"; window.scrollTo(0, 0);
});
$("#norms").oninput = () => { $("#norms-state").textContent = "unsaved changes"; };
$("#save-norms").onclick = async () => {
  try { await api("/api/norms", { text: $("#norms").value }); $("#norms-state").textContent = "saved"; toast("norms saved"); }
  catch (err) { toast(err.message, true); }
};

// --- memories -------------------------------------------------------------------------

const day = (ts) => (ts || "").slice(0, 10);
const recallOf = (m) => S.mem?.recall?.memories?.[m.name] || {};
function memoryMeta(m) {
  const since = m.created ? day(m.created) : m.known_since ? `before ${day(m.known_since)}` : "?";
  const r = recallOf(m);
  const recalled = r.offered ? ` · recalled ${r.offered}×, last ${esc(day(r.last_offered))}` : "";
  const missed = r.missed ? ` · <span class="state-bad">missed ${r.missed}×</span>` : "";
  return `since ${esc(since)} · ${m.revisions || 0} revision${m.revisions === 1 ? "" : "s"} · `
    + `${m.reads ? `${m.reads} read${m.reads === 1 ? "" : "s"}, last ${esc(day(m.last_read))}` : `<span class="state-warn">never read</span>`}`
    + recalled + missed;
}
const PLURALS = { memory: "memories", miss: "misses" };
const plural = (n, w) => `${n} ${n === 1 ? w : PLURALS[w] || w + "s"}`;
function renderRecall() {
  const box = $("#recall"); const r = S.mem?.recall;
  if (!r || (!r.enabled && !r.days.some((d) => d.wakes || d.misses))) {
    box.classList.toggle("hidden", !r);
    if (r) box.innerHTML = `<div class="recall-head"><strong>Recall</strong><span class="sub">Off for this team. With
      <code>"memory_recall": true</code> in the roster, each wake is offered the agent's own memories that bear on it.</span></div>`;
    return;
  }
  box.classList.remove("hidden");
  const days = r.days; const top = Math.max(1, ...days.map((d) => d.wakes));
  const tot = days.reduce((a, d) => ({ wakes: a.wakes + d.wakes, offered: a.offered + d.offered,
    memories: a.memories + d.memories, misses: a.misses + d.misses, errors: a.errors + d.errors }),
    { wakes: 0, offered: 0, memories: 0, misses: 0, errors: 0 });
  const cols = days.map((d) => {
    const tip = `${d.day}: ${plural(d.wakes, "wake")}, ${d.offered} offered memories (${plural(d.memories, "memory")}), `
      + `${plural(d.misses, "miss")}${d.errors ? `, ${plural(d.errors, "judge error")}` : ""}`;
    const h = (n) => `${(64 - 2) * n / top}px`;
    return `<div class="recall-day" title="${esc(tip)}">
      <div class="col">${d.wakes - d.offered ? `<span class="none" style="height:${h(d.wakes - d.offered)}"></span>` : ""}${d.offered ? `<span class="offered" style="height:${h(d.offered)}"></span>` : ""}</div>
      <div class="miss">${d.misses ? `<i></i>${d.misses > 1 ? d.misses : ""}` : ""}</div>
      <div class="lbl">${esc(d.day.slice(8))}</div></div>`;
  }).join("");
  const misses = (r.misses || []).map((x) => `<li><strong>${esc(x.memory || "?")}</strong>
    <span class="sub">${esc(x.ts || "")}${x.learned?.length ? ` · learned: ${esc(x.learned.slice(0, 8).join(", "))}` : ""}</span>
    ${x.correction ? `<div class="sub">“${esc(x.correction.slice(0, 200))}”</div>` : ""}</li>`).join("");
  box.innerHTML = `<div class="recall-head"><strong>Recall, last ${days.length} days</strong>
      <span class="sub">${plural(tot.wakes, "wake")}; ${tot.offered} offered ${plural(tot.memories, "memory")}; ${plural(tot.misses, "miss")}${tot.errors ? `; ${plural(tot.errors, "judge error")}` : ""}${r.enabled ? "" : " · now off"}</span>
      <span class="recall-legend"><span><i style="background:var(--accent)"></i>memories offered</span>
        <span><i style="background:var(--line)"></i>nothing offered</span>
        <span><i class="miss" style="background:var(--bad)"></i>miss</span></span></div>
    <div class="recall-chart" role="img" aria-label="Wakes per day for the last ${days.length} days, split by whether memories were offered">${cols}</div>
    ${misses ? `<details><summary class="sub">Recent misses: a correction restated a memory the agent was not shown</summary><ul class="recall-misses">${misses}</ul></details>` : ""}`;
}
async function loadMemories() {
  const agent = $("#mem-agent").value; if (!agent) { $("#memories").innerHTML = ""; return; }
  let r;
  try { r = await api(`/api/memories?${q({ agent })}`); }
  catch (err) { toast(err.message, true); r = { memories: [], archived: [], reviews: [] }; }
  S.mem = r; renderRecall(); renderMemories();
}
function renderMemories() {
  const r = S.mem || { memories: [], archived: [], reviews: [] }; const agent = $("#mem-agent").value;
  const sorts = {
    name: (a, b) => a.name.localeCompare(b.name),
    least: (a, b) => (a.reads || 0) - (b.reads || 0) || a.name.localeCompare(b.name),
    most: (a, b) => (b.reads || 0) - (a.reads || 0),
    oldest: (a, b) => String(a.created || a.known_since || "").localeCompare(String(b.created || b.known_since || "")),
    revised: (a, b) => (b.revisions || 0) - (a.revisions || 0),
    recalled: (a, b) => (recallOf(b).offered || 0) - (recallOf(a).offered || 0) || a.name.localeCompare(b.name),
    missed: (a, b) => (recallOf(b).missed || 0) - (recallOf(a).missed || 0) || a.name.localeCompare(b.name),
  };
  S.memories = [...r.memories].filter((m) => !$("#mem-unread").checked || !m.reads).sort(sorts[$("#mem-sort").value] || sorts.name);
  const unread = r.memories.filter((m) => !m.reads).length;
  $("#mem-summary").textContent = r.memories.length
    ? `${r.memories.length} memories; ${unread} never read back.` + (r.review_queued ? " A review is queued." : "") : "";
  $("#review-now").disabled = !!r.review_queued || !r.memories.length;
  $("#memories").innerHTML = S.memories.map((m, i) => `<div class="card memory" data-i="${i}">
    <div class="head"><span class="name">${esc(m.name)}</span><span class="desc">${esc(m.description)}</span>
      <button class="btn small ghost" data-act="edit">Edit</button><button class="btn small ghost" data-act="delete">Delete</button></div>
    <div class="meta">${memoryMeta(m)}${m.why ? ` · why: ${esc(m.why)}` : ""}</div>
    <pre>${esc(m.content)}</pre></div>`).join("") || `<p class="sub">${esc(agent)} has saved nothing${$("#mem-unread").checked ? " unread" : ""}.</p>`;
  $("#reviews").innerHTML = (r.reviews || []).map((v) => `<div class="card"><div class="head"><strong>${esc(v.ts)}</strong>
      <span class="sub"> by ${esc(v.by || "?")} · ${v.before} → ${v.after} · ${v.removed?.length || 0} removed, ${v.added?.length || 0} new, ${v.rewritten?.length || 0} rewritten${v.failed ? ' · <span class="state-bad">failed</span>' : ""}</span></div>
      ${v.summary ? `<pre>${esc(v.summary)}</pre>` : ""}</div>`).join("") || `<p class="sub">No reviews yet.</p>`;
  $("#archive").innerHTML = (r.archived || []).map((d) => `<div class="card memory">
      <div class="head"><span class="name">${esc(d.name)}</span><span class="desc">${esc(d.reason)} · ${esc(d.archived)} · by ${esc(d.by)}</span>
      <button class="btn small ghost" data-restore="${esc(d.id)}">Restore</button></div>
      <details><summary class="sub">${esc(d.description)}</summary><pre>${esc(d.content)}</pre></details></div>`).join("")
    || `<p class="sub">Nothing archived.</p>`;
}
$("#mem-sort").onchange = renderMemories;
$("#mem-unread").onchange = renderMemories;
$("#review-now").onclick = async () => {
  const agent = $("#mem-agent").value;
  if (!confirm(`Ask ${agent} to review its memories? It is one turn, run by the supervisor when ${agent} is between turns. Anything it removes is archived.`)) return;
  try { await api("/api/memory", { action: "review", agent }); toast("review queued"); await loadMemories(); }
  catch (err) { toast(err.message, true); }
};
$("#archive").addEventListener("click", async (e) => {
  const id = e.target.closest("[data-restore]")?.dataset.restore; if (!id) return;
  try { await api("/api/memory", { action: "restore", agent: $("#mem-agent").value, id }); toast("restored"); await loadMemories(); }
  catch (err) { toast(err.message, true); }
});
$("#mem-agent").onchange = loadMemories;
function memoryModal(m) {
  const agent = $("#mem-agent").value;
  modal(`<h3>${m ? "Edit" : "New"} memory for ${esc(agent)}</h3>
    <label>Name</label><input type="text" name="name" value="${esc(m?.name)}" ${m ? "readonly" : "required"}>
    <label>One-line description</label><input type="text" name="description" value="${esc(m?.description)}">
    <label>Content</label><textarea name="content" rows="10" required>${esc(m?.content)}</textarea>`,
    async (fd) => {
      await api("/api/memory", { action: "save", agent, name: fd.get("name"), description: fd.get("description"), content: fd.get("content") });
      toast("memory saved"); await loadMemories();
    });
}
$("#new-memory").onclick = () => memoryModal(null);
$("#memories").addEventListener("click", async (e) => {
  const btn = e.target.closest("button"); if (!btn) return;
  const m = S.memories[+btn.closest(".memory").dataset.i]; const agent = $("#mem-agent").value;
  if (btn.dataset.act === "edit") return memoryModal(m);
  if (!confirm(`Delete ${agent}'s memory "${m.name}"? A copy goes to the archive.`)) return;
  try { await api("/api/memory", { action: "delete", agent, name: m.name }); toast("deleted"); await loadMemories(); }
  catch (err) { toast(err.message, true); }
});

// --- tasks: levels and moving off project labels -----------------------------------

async function showLevels() {
  try { const st = await api(`/api/state?${q({})}`);
    $("#level-names").textContent = (st.task_levels || []).map((n, i) => `${i} ${n}`).join(", "); }
  catch (err) { toast(err.message, true); }
}
function migTable(plan, done) {
  if (!plan.length) return `<p class="sub">No project labels left to convert.</p>`;
  return `<table class="grid"><thead><tr><th>Label</th><th>Tasks</th><th>Becomes</th></tr></thead><tbody>${plan.map((r) =>
    `<tr><td>${esc(r.project)}</td><td>${r.tasks}</td><td>${r.parent && !done ? "goes under the existing " + esc(r.parent) : r.closed ? "a closed, archived parent" : "an open project"}</td></tr>`).join("")}</tbody></table>`;
}
$("#mig-preview").onclick = async () => {
  try { const { plan } = await api("/api/task", { action: "migrate_labels", dry_run: true });
    $("#mig-out").innerHTML = migTable(plan); $("#mig-run").disabled = !plan.length; }
  catch (err) { toast(err.message, true); }
};
$("#mig-run").onclick = async () => {
  if (!confirm("Turn these project labels into parent tasks?")) return;
  try { const { plan } = await api("/api/task", { action: "migrate_labels" });
    $("#mig-out").innerHTML = `<p class="sub">Done: ${plan.length} label${plan.length === 1 ? "" : "s"} converted.</p>` + migTable(plan, true);
    $("#mig-run").disabled = true; toast("converted"); }
  catch (err) { toast(err.message, true); }
};

// --- update and restart ---------------------------------------------------------------

function teamState(t) {
  if (!t.running) return `<span class="sub">not running</span>`;
  if (t.draining) return `<span class="state-warn">draining — waiting for ${esc(t.inflight.join(", ") || "the restart")}</span>`;
  return `<span class="state-ok">running</span>${t.version ? ` on ${esc(t.version)}` : ` <span class="state-warn">(older than graceful restart)</span>`}`;
}
async function refreshVersions() {
  let v;
  try { v = await api("/api/versions"); }
  catch { $("#versions").innerHTML = `<span class="state-warn">agy-chat is restarting…</span>`; return false; }
  $("#versions").innerHTML = `<table class="vers"><tr><td>agy-chat</td><td>${esc(v.agychat)}</td></tr>
    <tr><td>agy-team (this app)</td><td>${esc(v.agyteam)}</td></tr>
    ${v.teams.map((t) => `<tr><td>team ${esc(t.name)}</td><td>${teamState(t)}</td><td>${t.running
      ? `<button class="btn small ghost" data-restart="${esc(t.name)}" ${t.draining ? "disabled" : ""}>Restart</button>` : ""}</td></tr>`).join("")}</table>`;
  $("#log").textContent = (v.log || []).join("\n") || "(nothing yet)";
  $("#run-update").disabled = v.update.state === "running" || v.update.state === "restarting";
  return v;
}
$("#versions").addEventListener("click", async (e) => {
  const team = e.target.closest("[data-restart]")?.dataset.restart; if (!team) return;
  const now = $("#now").checked;
  if (!confirm(`Restart ${team}'s supervisor on the installed code? ${now ? "Turns in flight are cancelled and their mail goes back." : "It starts no new turns and restarts when the ones in flight finish."}`)) return;
  try { await api("/api/supervisor", { team, action: "restart", now }); toast(`${team}: restart requested`); refreshVersions(); }
  catch (err) { toast(err.message, true); }
});
$("#check").onclick = async () => {
  const out = $("#check-out"); out.classList.remove("hidden"); out.textContent = "checking…";
  try {
    const r = await api("/api/update", { action: "check" });
    const say = (p) => !p ? "unknown" : p.error ? p.error
      : p.kind === "git" ? `${p.repo}: ${p.behind ? `${p.behind} new commit(s) on ${p.upstream}` : "up to date"}${p.dirty ? " (has local changes: update will refuse)" : ""}`
      : p.kind === "git-tag" ? `${p.repo}: ${p.current || "untagged"} → ${p.target}`
      : p.kind === "pip" ? `pip install from ${p.url}: newest ${p.target}` : "installed some other way";
    out.textContent = `agy-chat: ${say(r.agychat)}\nagy-team: ${r.agyteam_shares_this_env ? "comes with agy-chat" : say(r.agyteam)}`;
  } catch (err) { out.textContent = err.message; }
};
$("#run-update").onclick = async () => {
  const now = $("#now").checked;
  if (!confirm(`Update agy-team and agy-chat, then restart every running team${now ? ", cancelling turns in flight" : " gracefully"}, then agy-chat?`)) return;
  try { await api("/api/update", { action: "run", now }); toast("updating…"); watchUpdate(); }
  catch (err) { toast(err.message, true); }
};
function watchUpdate() {
  clearInterval(S.updatePoll);
  let restarting = false;
  S.updatePoll = setInterval(async () => {
    const v = await refreshVersions();
    if (v && v.update.state === "restarting") restarting = true;
    if (v && restarting && v.update.state === "idle") { clearInterval(S.updatePoll); toast("updated"); setTimeout(() => location.reload(), 600); }
    if (v && v.update.state === "error") { clearInterval(S.updatePoll); toast(v.update.error || "update failed", true); }
  }, 1500);
}

// --- start ----------------------------------------------------------------------------

(async () => {
  try {
    const { teams } = await api("/api/teams");
    S.teams = teams;
    $("#team").innerHTML = teams.map((t) => `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join("");
    const asked = new URLSearchParams(location.search).get("team");
    const want = asked || load("agychat.team", null);
    S.team = teams.some((t) => t.name === want) ? want : teams[0]?.name;
    $("#team").value = S.team;
    S.tab = (location.hash || "").slice(1) || load("agychat.adminTab", "agents");
    if (!["agents", "norms", "memories", "tasks", "update"].includes(S.tab)) S.tab = "agents";
    if (S.team) await openTeam();
    const v = await refreshVersions();
    if (v && v.update.state === "running") watchUpdate();
  } catch (err) { toast(err.message, true); }
})();
