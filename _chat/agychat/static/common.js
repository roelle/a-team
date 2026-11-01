/* Shared by every agy-chat page: preferences, times, and one renderer for
   markdown and the things people write in it -- links, go links, paths,
   @mentions, #channels and task ids. No framework, no build. */
"use strict";

// --- preferences (per browser) ------------------------------------------------------

const PREFS_KEY = "agychat.prefs";
const Prefs = Object.assign({ tz: "", hour12: true, golinks: "http://go/" },
  (() => { try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || {}; } catch { return {}; } })());
function savePrefs() { try { localStorage.setItem(PREFS_KEY, JSON.stringify(Prefs)); } catch {} }
const browserTZ = () => Intl.DateTimeFormat().resolvedOptions().timeZone;
const tzOf = () => Prefs.tz || browserTZ();

// --- time ------------------------------------------------------------------------------
//
// The server sends each record's instant as "t" (seconds); "ts" is the
// record's own local time with no zone, kept as a fallback.

function instant(t, ts) {
  if (typeof t === "number") return new Date(t * 1000);
  if (ts) { const d = new Date(String(ts).replace(" ", "T")); if (!isNaN(d)) return d; }
  return null;
}
function fmtTime(t, ts, style = "full") {
  const d = instant(t, ts); if (!d) return ts || "";
  const opt = { timeZone: tzOf(), hour: "numeric", minute: "2-digit", hour12: !!Prefs.hour12 };
  if (style === "full") Object.assign(opt, { weekday: "short", year: "numeric", month: "short", day: "numeric", timeZoneName: "short" });
  if (style === "short") Object.assign(opt, { month: "short", day: "numeric" });
  if (style === "date") { delete opt.hour; delete opt.minute; Object.assign(opt, { weekday: "short", year: "numeric", month: "short", day: "numeric" }); }
  try { return new Intl.DateTimeFormat(undefined, opt).format(d); } catch { return d.toLocaleString(); }
}

// --- rendering --------------------------------------------------------------------------

const escHTML = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const unesc = (s) => String(s).replace(/&(amp|lt|gt|quot|#39);/g, (_, e) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" }[e]));

// What a page knows that the renderer does not. Each returns null when it
// does not recognise the thing, and the text is left as it was written.
const RenderCtx = {
  mention: (name) => null,          // -> {label, title, cls}
  channel: (name) => false,         // -> exists?
  task: (id) => null,               // -> {title, status}
  taskURL: (id) => `/board#task=${encodeURIComponent(id)}`,
  channelURL: (name) => `/#channel=${encodeURIComponent(name)}`,
  fileURL: (path) => `/api/file?path=${encodeURIComponent(path)}`,
};

// In the order they win when two start at the same place. Text arrives
// escaped, so the lookbehinds also keep "&#39;" from reading as a channel.
const INLINE_RULES = [
  ["code", /`([^`\n]+)`/g],
  ["img", /!\[([^\]\n]*)\]\(([^)\s]+)\)/g],
  ["link", /\[([^\]\n]+)\]\(([^)\s]+)\)/g],
  ["url", /https?:\/\/[^\s<]*[^\s<.,;:!?)\]'"]/g],
  ["bold", /\*\*([^*\n]+)\*\*/g],
  ["strike", /~~([^~\n]+)~~/g],
  ["italic", /(?<![\w*])\*([^*\s][^*\n]*)\*(?![\w*])|(?<![\w&#])_([^_\s][^_\n]*)_(?!\w)/g],
  ["golink", /(?<![\w\/.\-])go\/[\w\-][\w\-.\/#?=]*[\w\-\/]/g],
  ["path", /(?<![\w\/.:\-])(?:~\/[\w.\-]+(?:\/[\w.\-]+)*\/?|\.{1,2}\/[\w.\-]+(?:\/[\w.\-]+)*\/?|\/[\w.~\-]+(?:\/[\w.\-]+)+\/?|[\w\-][\w.\-]*(?:\/[\w.\-]+)+\.[A-Za-z0-9]{1,8})/g],
  ["mention", /(?<![\w@&#])@([A-Za-z0-9](?:[\w-]|\.(?=\w))*)/g],
  ["channel", /(?<![\w&#\/])#([a-z0-9][a-z0-9_-]{0,39})(?![\w-])/g],
  ["task", /\btask_[0-9a-f]{6,}\b/g],
];

function href(u) {
  const raw = unesc(u);
  if (/^https?:\/\//i.test(raw)) return escHTML(raw);
  if (/^go\//.test(raw)) return escHTML(Prefs.golinks.replace(/\/?$/, "/") + raw.slice(3));
  return null;
}

function inlineHTML(text, ctx = RenderCtx) {
  let out = "", pos = 0;
  while (pos < text.length) {
    let best = null;
    for (const [kind, re] of INLINE_RULES) {
      re.lastIndex = pos; const m = re.exec(text);
      if (m && (!best || m.index < best.m.index)) best = { kind, m };
    }
    if (!best) { out += text.slice(pos); break; }
    const { kind, m } = best;
    out += text.slice(pos, m.index);
    out += renderToken(kind, m, ctx) ?? m[0];
    pos = m.index + m[0].length;
  }
  return out;
}

function renderToken(kind, m, ctx) {
  switch (kind) {
    case "code": return `<code>${m[1]}</code>`;
    case "bold": return `<strong>${inlineHTML(m[1], ctx)}</strong>`;
    case "strike": return `<del>${inlineHTML(m[1], ctx)}</del>`;
    case "italic": return `<em>${inlineHTML(m[1] ?? m[2], ctx)}</em>`;
    case "url": return `<a href="${href(m[0])}" target="_blank" rel="noopener">${m[0]}</a>`;
    case "golink": return `<a class="golink" href="${href(m[0])}" target="_blank" rel="noopener" title="${escHTML(href(m[0]) ? unesc(href(m[0])) : "")}">${m[0]}</a>`;
    case "link": {
      const h = href(m[2]);
      if (h) return `<a href="${h}" target="_blank" rel="noopener">${inlineHTML(m[1], ctx)}</a>`;
      return `<span class="pill-file" data-path="${m[2]}">${inlineHTML(m[1], ctx)}</span>`;
    }
    case "img": {
      const h = href(m[2]);
      const src = h || escHTML(ctx.fileURL(unesc(m[2])));
      return `<img class="inline-img" src="${src}" alt="${m[1]}" ${h ? "" : `data-path="${m[2]}"`} loading="lazy">`;
    }
    case "path": return `<span class="pill-file" data-path="${m[0]}">${m[0]}</span>`;
    case "mention": {
      const who = ctx.mention(unesc(m[1]));
      if (!who) return null;
      return `<span class="mention${who.cls ? " " + who.cls : ""}" title="${escHTML(who.title || "")}">@${escHTML(who.label)}</span>`;
    }
    case "channel":
      if (!ctx.channel(m[1])) return null;
      return `<a class="chan-link" href="${escHTML(ctx.channelURL(m[1]))}" data-view='${escHTML(JSON.stringify({ type: "channel", id: m[1] }))}'>#${m[1]}</a>`;
    case "task": {
      const t = ctx.task(m[0]);
      if (!t) return null;
      return `<a class="task-ref" href="${escHTML(ctx.taskURL(m[0]))}" title="${escHTML(m[0])} · ${escHTML(t.status || "")}"><span class="st ${escHTML(t.status || "")}"></span>${escHTML(t.title)}</a>`;
    }
  }
  return null;
}

// Block markdown: fenced code, headings, tables, quotes, lists, rules, paragraphs.
function markdownHTML(src, ctx = RenderCtx) {
  const out = []; const lines = escHTML(src).split("\n"); let i = 0;
  const inl = (s) => inlineHTML(s, ctx);
  const isTableRow = (l) => /^\s*\|.*\|\s*$/.test(l);
  const isSep = (l) => /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/.test(l || "") && /-/.test(l || "");
  const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  const startsBlock = (l) => l.startsWith("```") || /^#{1,6} /.test(l) || /^\s*([-*]|\d+[.)]) /.test(l) || /^&gt; ?/.test(l) || isTableRow(l) || /^\s*(---|\*\*\*)\s*$/.test(l);
  while (i < lines.length) {
    const l = lines[i];
    if (l.startsWith("```")) {
      const buf = []; i++;
      while (i < lines.length && !lines[i].startsWith("```")) buf.push(lines[i++]);
      i++; out.push(`<pre><code>${buf.join("\n")}</code></pre>`); continue;
    }
    if (isTableRow(l) && isSep(lines[i + 1])) {
      const head = cells(l); i += 2; const rows = [];
      while (i < lines.length && isTableRow(lines[i])) rows.push(cells(lines[i++]));
      out.push(`<div class="md-table"><table><thead><tr>${head.map((c) => `<th>${inl(c)}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inl(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    const h = l.match(/^(#{1,6}) (.*)$/);
    if (h) { out.push(`<div class="md-h md-h${Math.min(h[1].length, 4)}">${inl(h[2])}</div>`); i++; continue; }
    if (/^\s*(---|\*\*\*)\s*$/.test(l)) { out.push("<hr>"); i++; continue; }
    if (/^&gt; ?/.test(l)) {
      const buf = [];
      while (i < lines.length && /^&gt; ?/.test(lines[i])) buf.push(unesc(lines[i++].replace(/^&gt; ?/, "")));
      out.push(`<blockquote>${markdownHTML(buf.join("\n"), ctx)}</blockquote>`); continue;
    }
    if (/^\s*([-*]|\d+[.)]) /.test(l)) {
      const ordered = /^\s*\d+[.)] /.test(l); const items = [];
      while (i < lines.length && (ordered ? /^\s*\d+[.)] / : /^\s*[-*] /).test(lines[i])) {
        const raw = lines[i++].replace(/^\s*([-*]|\d+[.)]) /, "");
        const box = raw.match(/^\[( |x|X)\] (.*)$/);
        items.push(box ? `<li class="check"><input type="checkbox" disabled ${box[1] !== " " ? "checked" : ""}> ${inl(box[2])}</li>` : `<li>${inl(raw)}</li>`);
      }
      out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`); continue;
    }
    if (!l.trim()) { i++; continue; }
    const para = [];
    while (i < lines.length && lines[i].trim() && !(para.length && startsBlock(lines[i]))) para.push(inl(lines[i++]));
    out.push(`<p>${para.join("<br>")}</p>`);
  }
  return out.join("");
}

// --- the preferences dialog ---------------------------------------------------------------

function openPrefs(onSaved) {
  let zones = [];
  try { zones = Intl.supportedValuesOf("timeZone"); } catch { zones = [browserTZ()]; }
  if (!zones.includes("UTC")) zones = ["UTC", ...zones];
  const dlg = document.createElement("div");
  dlg.className = "modal";
  dlg.innerHTML = `<div class="modal-card"><form>
    <h3>Preferences</h3>
    <div class="hint">Kept in this browser.</div>
    <label>Time zone</label>
    <select name="tz"><option value="">This computer's (${escHTML(browserTZ())})</option>
      ${zones.map((z) => `<option ${z === Prefs.tz ? "selected" : ""}>${escHTML(z)}</option>`).join("")}</select>
    <label>Clock</label>
    <select name="hour12"><option value="1" ${Prefs.hour12 ? "selected" : ""}>12-hour (3:04 PM)</option><option value="0" ${Prefs.hour12 ? "" : "selected"}>24-hour (15:04)</option></select>
    <label>Go links</label>
    <input type="text" name="golinks" value="${escHTML(Prefs.golinks)}" placeholder="http://go/">
    <div class="hint">"go/name" in a message links to this address + name.</div>
    <div class="hint" style="margin-top:8px">Now: ${escHTML(fmtTime(Date.now() / 1000))}</div>
    <div class="modal-actions"><button type="button" class="btn ghost" data-cancel>Cancel</button><button class="btn">Save</button></div>
  </form></div>`;
  document.body.appendChild(dlg);
  const form = dlg.querySelector("form");
  dlg.querySelector("[data-cancel]").onclick = () => dlg.remove();
  form.onsubmit = (e) => {
    e.preventDefault(); const fd = new FormData(form);
    Prefs.tz = fd.get("tz") || ""; Prefs.hour12 = fd.get("hour12") === "1";
    Prefs.golinks = (fd.get("golinks") || "http://go/").trim();
    savePrefs(); dlg.remove(); if (onSaved) onSaved();
  };
}

// --- one app: the navigation every page shares --------------------------------------------

const PAGES = [["chat", "Chat", "/"], ["board", "Board", "/board"], ["wiki", "Wiki", "/wiki"], ["admin", "Admin", "/admin"]];
function renderNav(current, team, onPrefs) {
  const el = document.getElementById("app-nav"); if (!el) return;
  const t = team ? `?team=${encodeURIComponent(team)}` : "";
  el.innerHTML = PAGES.map(([key, label, path]) =>
    `<a class="nav-link${key === current ? " active" : ""}" href="${path}${t}">${label}</a>`).join("")
    + `<button class="icon top-icon nav-prefs" title="Preferences: time zone, clock, go links">⚙</button>`;
  el.querySelector(".nav-prefs").onclick = () => openPrefs(onPrefs);
}
