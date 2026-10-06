"use strict";
// LeadLens front end: vanilla JS, no build step. All data comes from /api.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "");

const STAGES = ["Deduplicating", "Enriching websites", "Verifying emails", "Scoring", "Done"];
const TIERS = ["A", "B", "C", "D"];
const STATUSES = ["new", "contacted", "qualified", "disqualified"];

const state = {
  file: null, batches: [], batch: null, leads: [], presets: {},
  filter: { tier: "", email: "", status: "", q: "" }, sort: { key: "score", dir: -1 },
  cursor: -1, visible: [], poll: null, openLeadId: null,
};

const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem("leadlens:" + k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("leadlens:" + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch { /* not json */ }
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 2200);
}

const fmtMoney = (v) => (v == null ? "—" : v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : `$${Math.round(v / 1e3)}K`);
const ago = (ts) => {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString();
};

// ---------------------------------------------------------------- routing
function showImport() {
  stopPoll();
  state.batch = null;
  $("#importView").hidden = false;
  $("#batchView").hidden = true;
  location.hash = "";
  renderBatchList();
}

async function openBatch(id) {
  stopPoll();
  $("#importView").hidden = true;
  $("#batchView").hidden = false;
  location.hash = id;
  state.filter = { tier: "", email: "", status: "", q: "" };
  $("#search").value = ""; $("#emailFilter").value = ""; $("#statusFilter").value = "";
  state.cursor = -1;
  await refreshBatch(id);
}

async function refreshBatch(id) {
  let b;
  try { b = await api(`/api/batches/${id}`); } catch (e) { toast(e.message); return showImport(); }
  state.batch = b;
  $("#batchName").textContent = b.name;
  const preset = state.presets[b.preset]?.label || b.preset;
  $("#batchMeta").textContent = `${preset} · ${b.enrich ? "enriched online" : "offline pass"} · ${ago(b.created_at)}`;
  renderBatchList();

  const busy = b.status === "queued" || b.status === "processing";
  $("#progressCard").hidden = !busy && b.status !== "error";
  $("#results").hidden = busy || b.status === "error";
  $("#icpBtn").disabled = $("#exportBtn").disabled = busy || b.status === "error";

  if (busy || b.status === "error") {
    renderProgress(b);
    if (busy) state.poll = setTimeout(() => refreshBatch(id), 700);
    return;
  }
  state.leads = await api(`/api/batches/${id}/leads`);
  renderResults();
}

function stopPoll() { clearTimeout(state.poll); state.poll = null; }

// ---------------------------------------------------------------- sidebar
async function loadBatches() {
  state.batches = await api("/api/batches");
  renderBatchList();
}

function renderBatchList() {
  const ul = $("#batchList");
  if (!state.batches.length) { ul.innerHTML = `<li class="muted small">No batches yet</li>`; return; }
  ul.innerHTML = state.batches.map((b) => {
    const t = b.stats?.tiers;
    const meta = b.status === "done" ? `${b.stats.unique_leads} leads · ${t?.A ?? 0} A-tier` : b.status === "error" ? "failed" : "processing…";
    return `<li><button data-id="${esc(b.id)}" aria-current="${state.batch?.id === b.id}">
      <span class="b-name">${esc(b.name)}</span><span class="b-meta">${esc(meta)} · ${ago(b.created_at)}</span></button></li>`;
  }).join("");
}

// ---------------------------------------------------------------- import
function setFile(f) {
  state.file = f;
  $("#dropzone").classList.toggle("has-file", !!f);
  $("#dropLabel").textContent = f ? `${f.name} · ${(f.size / 1024).toFixed(0)} KB` : "Drop CSV here or click to browse";
  $("#runBtn").disabled = !f;
}

async function startImport(sample) {
  const err = $("#importError");
  err.hidden = true;
  const fd = new FormData();
  fd.append("preset", $("input[name=preset]:checked").value);
  fd.append("enrich", $("#enrichToggle").checked);
  if (!sample) fd.append("file", state.file);
  $("#runBtn").disabled = $("#sampleBtn").disabled = true;
  try {
    const res = await api(sample ? "/api/batches/sample" : "/api/batches", { method: "POST", body: fd });
    toast(`${res.rows} rows read · ${Object.keys(res.mapped_columns).length} columns mapped`);
    setFile(null);
    await loadBatches();
    openBatch(res.id);
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  } finally {
    $("#runBtn").disabled = !state.file;
    $("#sampleBtn").disabled = false;
  }
}

// ---------------------------------------------------------------- progress
function renderProgress(b) {
  if (b.status === "error") {
    $("#progressStage").textContent = b.stage;
    $("#progressCount").textContent = "";
    $("#progressBar").style.width = "100%";
    $("#progressBar").style.background = "var(--bad)";
    $("#stageList").innerHTML = "";
    return;
  }
  $("#progressBar").style.background = "";
  const idx = Math.max(0, STAGES.indexOf(b.stage));
  const within = b.total ? b.processed / b.total : 0;
  const pct = ((idx + (b.stage === "Enriching websites" ? within : 0.5)) / (STAGES.length - 1)) * 100;
  $("#progressStage").textContent = b.stage === "Queued" ? "Queued…" : `${b.stage}…`;
  $("#progressCount").textContent = b.stage === "Enriching websites" ? `${b.processed} / ${b.total} sites` : "";
  $("#progressBar").style.width = `${Math.min(100, pct)}%`;
  $("#stageList").innerHTML = STAGES.slice(0, -1).map((s, i) =>
    `<li class="${i < idx ? "done" : i === idx ? "current" : ""}">${s}${s === "Enriching websites" && !b.enrich ? " (skipped)" : ""}</li>`).join("");
}

// ---------------------------------------------------------------- results
function renderResults() {
  const s = state.batch.stats;
  const e = s.email || {};
  const deliverable = (e.valid || 0) + (e.unverified || 0);
  const sites = s.sites || {};
  const filled = s.filled_from_site || {};
  const filledTotal = (filled.email || 0) + (filled.phone || 0) + (filled.linkedin || 0);
  const tile = (label, value, sub, cls = "") =>
    `<div class="tile ${cls}"><div class="t-label">${label}</div><div class="t-value">${value}</div><div class="t-sub">${sub}</div></div>`;
  $("#tiles").innerHTML = [
    tile("Tier A leads", s.tiers.A, `${s.tiers.B} more in tier B`, "hero"),
    tile("Unique leads", s.unique_leads, `${s.rows_imported} rows in, ${s.duplicates_removed} duplicates merged`),
    tile("Reachable emails", `${Math.round((deliverable / Math.max(1, s.unique_leads)) * 100)}%`,
      `${e.valid || 0} verified · ${(e.invalid || 0) + (e.missing || 0)} bad or missing`),
    tile("Websites enriched", state.batch.enrich ? sites.ok || 0 : "—",
      state.batch.enrich ? `${sites.blocked || 0} blocked · ${(sites.unreachable || 0) + (sites.error || 0)} down · ${s.cache_hits} cached` : "offline pass"),
    tile("Gaps filled", state.batch.enrich ? filledTotal : "—",
      state.batch.enrich ? `${filled.email || 0} emails, ${filled.phone || 0} phones from sites` : `processed in ${s.seconds}s`),
  ].join("");

  const total = Math.max(1, TIERS.reduce((a, t) => a + s.tiers[t], 0));
  $("#dist").innerHTML = TIERS.filter((t) => s.tiers[t]).map((t) =>
    `<span class="bg-${t}" style="width:${(s.tiers[t] / total) * 100}%" title="Tier ${t}: ${s.tiers[t]}"></span>`).join("");

  renderChips();
  renderRows();
}

function renderChips() {
  const counts = Object.fromEntries(TIERS.map((t) => [t, state.leads.filter((l) => l.tier === t).length]));
  $("#tierChips").innerHTML = [["", "All", state.leads.length], ...TIERS.map((t) => [t, `Tier ${t}`, counts[t]])]
    .map(([v, label, n]) => `<button class="chip" data-tier="${v}" aria-pressed="${state.filter.tier === v}">${label}<span class="n">${n}</span></button>`).join("");
}

function matches(l) {
  const f = state.filter;
  if (f.tier && l.tier !== f.tier) return false;
  if (f.status && l.status !== f.status) return false;
  if (f.email === "ok" && !["valid", "unverified"].includes(l.email_status)) return false;
  if (f.email === "valid" && l.email_status !== "valid") return false;
  if (f.email === "bad" && !["invalid", "missing"].includes(l.email_status)) return false;
  if (f.q) {
    const d = l.data;
    const hay = [d.company, d.domain, d.contact_name, d.title, d.city, d.state, d.industry, d.email].join(" ").toLowerCase();
    if (!hay.includes(f.q)) return false;
  }
  return true;
}

function signals(l) {
  const d = l.data, e = l.enrichment || {}, out = [];
  const yr = new Date().getFullYear();
  if (d.founded) out.push([`${yr - d.founded} yrs`, yr - d.founded >= 15 ? "good" : ""]);
  if (/owner|founder|proprietor/i.test(d.title || "")) out.push(["Owner-run", "good"]);
  if (e.hiring) out.push(["Hiring", "good"]);
  if (e.site_age_years >= 3) out.push([`Site ©${e.copyright_year}`, "warn"]);
  if (e.status === "blocked") out.push(["Bot-walled", "warn"]);
  if (e.status === "unreachable") out.push(["Site down", "warn"]);
  (e.tech || []).slice(0, 2).forEach((t) => out.push([t, ""]));
  if (l.dup_count) out.push([`${l.dup_count + 1}× merged`, ""]);
  return out.map(([t, c]) => `<span class="sig ${c}">${esc(t)}</span>`).join("");
}

const EMAIL_PILL = { valid: "pill-ok", unverified: "pill-muted", risky: "pill-warn", invalid: "pill-bad", missing: "pill-bad" };

function renderRows() {
  const { key, dir } = state.sort;
  state.visible = state.leads.filter(matches).sort((a, b) => {
    const va = key === "company" ? (a.data.company || "").toLowerCase() : a.score;
    const vb = key === "company" ? (b.data.company || "").toLowerCase() : b.score;
    return va < vb ? -dir : va > vb ? dir : 0;
  });
  $$(".leads th.sortable").forEach((th) => th.setAttribute("aria-sort", th.dataset.sort === key ? (dir < 0 ? "descending" : "ascending") : "none"));
  $("#resultCount").textContent = `${state.visible.length} of ${state.leads.length}`;
  $("#emptyRows").hidden = state.visible.length > 0;
  $("#leadRows").innerHTML = state.visible.map((l, i) => {
    const d = l.data;
    const loc = [d.city, d.state].filter(Boolean).join(", ");
    const flags = l.email_flags.length ? ` title="${esc(l.email_flags.join(", "))}"` : "";
    return `<tr data-id="${l.id}" class="${i === state.cursor ? "cursor" : ""} ${l.status === "disqualified" ? "dq" : ""}">
      <td><span class="score tier-${l.tier}">${l.score}<small>${l.tier}</small></span></td>
      <td><div class="c-name">${esc(d.company || d.domain || "—")}</div>
          <div class="c-sub">${esc(d.domain || "no website")}${loc ? " · " + esc(loc) : ""}</div>
          <div class="c-sub">${esc(d.industry || "")}</div></td>
      <td><div>${esc(d.contact_name || "—")}</div><div class="c-sub">${esc(d.title || "")}</div></td>
      <td><div class="c-sub" style="color:var(--text)">${esc(d.email || "—")}</div>
          <span class="pill ${EMAIL_PILL[l.email_status]}"${flags}>${esc(l.email_status)}${l.email_flags.length ? " · " + esc(l.email_flags[0]) : ""}</span></td>
      <td class="signals">${signals(l)}</td>
      <td><select data-status="${l.id}" aria-label="Lead status">${STATUSES.map((s) => `<option ${s === l.status ? "selected" : ""}>${s}</option>`).join("")}</select></td>
    </tr>`;
  }).join("");
}

async function setStatus(id, status) {
  await api(`/api/leads/${id}`, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify({ status }) });
  const l = state.leads.find((x) => x.id === id);
  if (l) l.status = status;
  renderRows();
  if (state.openLeadId === id) renderLead(l);
  toast(`Marked ${status}`);
}

// ---------------------------------------------------------------- lead drawer
function openDrawer(id) {
  $("#scrim").hidden = false;
  const d = $(id);
  d.classList.add("open");
  d.setAttribute("aria-hidden", "false");
  setTimeout(() => d.querySelector("[data-close]")?.focus(), 50);
}
function closeDrawers() {
  $$(".drawer").forEach((d) => { d.classList.remove("open"); d.setAttribute("aria-hidden", "true"); });
  $("#scrim").hidden = true;
  state.openLeadId = null;
}

function renderLead(l) {
  state.openLeadId = l.id;
  const d = l.data, e = l.enrichment || {};
  $("#dTitle").textContent = d.company || d.domain;
  const site = d.domain ? `https://${d.domain}` : "";
  $("#dSite").textContent = d.domain || "no website";
  $("#dSite").href = site || "#";

  const factors = l.reasons.map((r) => `
    <div class="factor">
      <div class="factor-top"><b>${esc(r.label)}</b><span class="muted">${r.points} / ${r.max}</span></div>
      <div class="bar"><div class="bg-${r.value >= 0.75 ? "A" : r.value >= 0.45 ? "B" : r.value >= 0.25 ? "C" : "D"}" style="width:${r.value * 100}%"></div></div>
      <div class="why">${esc(r.why)}</div>
    </div>`).join("");

  const kv = (pairs) => `<dl class="kv">${pairs.filter(([, v]) => v).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;
  const socials = Object.entries(e.socials || {}).map(([k, u]) => safeUrl(u) ? `<a href="${esc(safeUrl(u))}" target="_blank" rel="noopener">${esc(k)}</a>` : "").join(" · ");
  const webStatus = { ok: "Live", blocked: "Blocked by bot protection", disallowed: "robots.txt disallows", unreachable: "Unreachable", error: "Error", skipped: "Not enriched (offline pass)", "no domain": "No website on record" }[e.status] || e.status;
  const isSales = state.batch.preset === "sales";
  const sender = store.get("sender", "");
  const offering = store.get("offering", "");

  $("#dBody").innerHTML = `
    <div class="d-score"><span class="score tier-${l.tier}">${l.score}</span>
      <div><strong>Tier ${l.tier}</strong><div class="muted small">${l.tier === "A" ? "Work this lead first" : l.tier === "B" ? "Strong fit, worth outreach" : l.tier === "C" ? "Partial fit, nurture" : "Poor fit or unreachable"}</div></div></div>
    <h3>Why this score</h3>${factors}
    <h3>Contact</h3>${kv([
      ["Name", esc(d.contact_name)], ["Title", esc(d.title)],
      ["Email", d.email ? `${esc(d.email)} <span class="pill ${EMAIL_PILL[l.email_status]}">${esc(l.email_status)}</span>` : ""],
      ["Email notes", esc(l.email_flags.join(", "))], ["Phone", esc(d.phone)],
      ["LinkedIn", safeUrl(d.linkedin) ? `<a href="${esc(safeUrl(d.linkedin))}" target="_blank" rel="noopener">profile</a>` : ""],
    ])}
    <h3>Company</h3>${kv([
      ["Industry", esc(d.industry)], ["Employees", esc(d.employees_raw || (d.employees ? Math.round(d.employees) : ""))],
      ["Revenue", esc(d.revenue_raw || (d.revenue ? fmtMoney(d.revenue) : ""))], ["Founded", esc(d.founded)],
      ["Location", esc([d.city, d.state, d.country].filter(Boolean).join(", "))],
      ["Duplicates", l.dup_count ? `${l.dup_count} merged into this record` : ""],
    ])}
    <h3>Website</h3>${kv([
      ["Status", esc(webStatus) + (e.note ? ` <span class="muted small">(${esc(e.note)})</span>` : "") + (e.cached ? ` <span class="pill pill-muted">cached</span>` : "")],
      ["Title", esc(e.title)], ["About", esc(e.description)], ["Tech", esc((e.tech || []).join(", "))],
      ["Emails found", esc((e.emails || []).join(", "))], ["Phones found", esc((e.phones || []).join(", "))],
      ["Socials", socials], ["Last updated", e.copyright_year ? `© ${e.copyright_year}` : ""],
      ["Hiring", e.hiring ? "Yes, careers page found" : ""],
    ])}
    <h3>Pipeline</h3>
    <div class="status-row">${STATUSES.map((s) => `<button class="chip" data-set-status="${s}" aria-pressed="${l.status === s}">${s}</button>`).join("")}</div>
    <h3>First-touch email</h3>
    <div class="opener-box">
      <div class="two">
        <input type="text" id="senderName" placeholder="Your name & firm" value="${esc(sender)}" aria-label="Sender name">
        ${isSales ? `<input type="text" id="offering" placeholder="What you sell (one line)" value="${esc(offering)}" aria-label="Your offering">` : "<span></span>"}
      </div>
      <div class="actions" style="margin-top:0">
        <button class="btn btn-primary" id="genOpener">${l.opener ? "Regenerate" : "Draft email"}</button>
        <button class="btn" id="copyOpener" ${l.opener ? "" : "disabled"}>Copy</button>
        <span class="muted small" id="openerSource"></span>
      </div>
      <textarea id="openerText" placeholder="A short, personalized opener grounded in this lead's signals will appear here." ${l.opener ? "" : "hidden"}>${esc(l.opener || "")}</textarea>
    </div>`;
}

async function generateOpener(l) {
  const btn = $("#genOpener");
  btn.disabled = true;
  btn.textContent = "Drafting…";
  const sender = $("#senderName").value.trim() || "[Your name]";
  const offering = $("#offering")?.value.trim() || "";
  store.set("sender", $("#senderName").value.trim());
  if ($("#offering")) store.set("offering", offering);
  try {
    const res = await api(`/api/leads/${l.id}/opener`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ sender, offering }),
    });
    l.opener = res.text;
    $("#openerText").hidden = false;
    $("#openerText").value = res.text;
    $("#copyOpener").disabled = false;
    $("#openerSource").textContent = res.source === "ai" ? "AI-written from this lead's signals" : "Template (add ANTHROPIC_API_KEY for AI drafts)";
  } catch (e) { toast(e.message); }
  btn.disabled = false;
  btn.textContent = "Regenerate";
}

// ---------------------------------------------------------------- ICP drawer
const WEIGHT_LABELS = {
  firmographic_fit: "Firmographic fit", industry_fit: "Industry fit", succession_signal: "Succession signal",
  growth_signal: "Growth signal", decision_maker: "Decision maker", reachability: "Reachability", data_quality: "Data quality",
};

function fillIcp() {
  const icp = state.batch.icp, f = $("#icpForm");
  f.industries.value = icp.industries.join(", ");
  f.exclude_keywords.value = (icp.exclude_keywords || []).join(", ");
  f.emp_lo.value = icp.employees[0]; f.emp_hi.value = icp.employees[1];
  f.rev_lo.value = icp.revenue[0] / 1e6; f.rev_hi.value = icp.revenue[1] / 1e6;
  f.min_age_years.value = icp.min_age_years || 0;
  f.titles.value = icp.titles.join(", ");
  const all = { ...Object.fromEntries(Object.keys(WEIGHT_LABELS).map((k) => [k, 0])), ...icp.weights };
  $("#weights").innerHTML = Object.entries(all).map(([k, v]) => `
    <div class="weight-row"><label for="w_${k}">${WEIGHT_LABELS[k]}</label>
      <input type="range" id="w_${k}" name="w_${k}" min="0" max="40" step="5" value="${v}">
      <output for="w_${k}">${v}</output></div>`).join("");
}

async function applyIcp(ev) {
  ev.preventDefault();
  const f = $("#icpForm");
  const list = (s) => s.split(",").map((x) => x.trim().toLowerCase()).filter(Boolean);
  const weights = Object.fromEntries(Object.keys(WEIGHT_LABELS).map((k) => [k, +f[`w_${k}`].value]));
  const body = {
    industries: list(f.industries.value), exclude_keywords: list(f.exclude_keywords.value),
    employees: [+f.emp_lo.value, +f.emp_hi.value], revenue: [+f.rev_lo.value * 1e6, +f.rev_hi.value * 1e6],
    min_age_years: +f.min_age_years.value, titles: list(f.titles.value), weights,
  };
  try {
    const res = await api(`/api/batches/${state.batch.id}/icp`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    closeDrawers();
    await refreshBatch(state.batch.id);
    await loadBatches();
    toast(`Re-scored: ${res.tiers.A} A · ${res.tiers.B} B · ${res.tiers.C} C · ${res.tiers.D} D`);
  } catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------- export
function exportSelection() {
  const tiers = $$("#exportTiers .chip").filter((c) => c.getAttribute("aria-pressed") === "true").map((c) => c.dataset.t);
  const exclude = $("#excludeInvalid").checked;
  const n = state.leads.filter((l) => tiers.includes(l.tier) && !(exclude && (l.email_status === "invalid" || l.status === "disqualified"))).length;
  return { tiers, exclude, n };
}
function updateExportCount() {
  const { n } = exportSelection();
  $("#exportCount").textContent = `${n} lead${n === 1 ? "" : "s"} will be exported.`;
  $("#doExport").disabled = n === 0;
}
function openExport() {
  $("#exportTiers").innerHTML = TIERS.map((t) => `<button type="button" class="chip" data-t="${t}" aria-pressed="${t === "A" || t === "B"}">Tier ${t}</button>`).join("");
  updateExportCount();
  $("#exportDialog").returnValue = "";  // Esc keeps the previous value otherwise
  $("#exportDialog").showModal();
}
function doExport() {
  const { tiers, exclude } = exportSelection();
  const fmt = $("input[name=fmt]:checked").value;
  const url = `/api/batches/${state.batch.id}/export?format=${fmt}&tiers=${tiers.join(",")}&exclude_invalid=${exclude}`;
  const a = document.createElement("a");
  a.href = url; a.download = "";
  document.body.appendChild(a); a.click(); a.remove();
  toast("Export downloading");
}

// ---------------------------------------------------------------- wiring
function bind() {
  $("#brandLink").addEventListener("click", (e) => { e.preventDefault(); showImport(); });
  $("#newImportBtn").addEventListener("click", showImport);
  $("#batchList").addEventListener("click", (e) => { const b = e.target.closest("button[data-id]"); if (b) openBatch(b.dataset.id); });

  const dz = $("#dropzone");
  $("#fileInput").addEventListener("change", (e) => setFile(e.target.files[0] || null));
  ["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) setFile(f); });
  $("#runBtn").addEventListener("click", () => startImport(false));
  $("#sampleBtn").addEventListener("click", () => startImport(true));
  $$("input[name=preset]").forEach((r) => r.addEventListener("change", () => store.set("preset", r.value)));
  $("#enrichToggle").addEventListener("change", (e) => store.set("enrich", e.target.checked));

  $("#tierChips").addEventListener("click", (e) => {
    const c = e.target.closest(".chip"); if (!c) return;
    state.filter.tier = c.dataset.tier; state.cursor = -1; renderChips(); renderRows();
  });
  $("#emailFilter").addEventListener("change", (e) => { state.filter.email = e.target.value; renderRows(); });
  $("#statusFilter").addEventListener("change", (e) => { state.filter.status = e.target.value; renderRows(); });
  $("#search").addEventListener("input", (e) => { state.filter.q = e.target.value.trim().toLowerCase(); state.cursor = -1; renderRows(); });
  $$(".leads th.sortable").forEach((th) => th.addEventListener("click", () => {
    const k = th.dataset.sort;
    state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : k === "score" ? -1 : 1 };
    renderRows();
  }));

  $("#leadRows").addEventListener("change", (e) => {
    const s = e.target.closest("select[data-status]");
    if (s) setStatus(+s.dataset.status, s.value);
  });
  $("#leadRows").addEventListener("click", (e) => {
    if (e.target.closest("select")) return;
    const tr = e.target.closest("tr[data-id]"); if (!tr) return;
    state.cursor = state.visible.findIndex((l) => l.id === +tr.dataset.id);
    renderRows();
    renderLead(state.visible[state.cursor]);
    openDrawer("#leadDrawer");
  });
  $("#dBody").addEventListener("click", (e) => {
    const l = state.leads.find((x) => x.id === state.openLeadId);
    if (!l) return;
    if (e.target.id === "genOpener") generateOpener(l);
    if (e.target.id === "copyOpener") navigator.clipboard.writeText($("#openerText").value).then(() => toast("Copied to clipboard"));
    const s = e.target.closest("[data-set-status]");
    if (s) setStatus(l.id, s.dataset.setStatus);
  });

  $("#icpBtn").addEventListener("click", () => { fillIcp(); openDrawer("#icpDrawer"); });
  $("#icpForm").addEventListener("submit", applyIcp);
  $("#weights").addEventListener("input", (e) => { if (e.target.type === "range") e.target.nextElementSibling.value = e.target.value; });

  $("#exportBtn").addEventListener("click", openExport);
  $("#exportTiers").addEventListener("click", (e) => {
    const c = e.target.closest(".chip"); if (!c) return;
    c.setAttribute("aria-pressed", c.getAttribute("aria-pressed") !== "true"); updateExportCount();
  });
  $("#excludeInvalid").addEventListener("change", updateExportCount);
  $("#exportDialog").addEventListener("close", () => { if ($("#exportDialog").returnValue === "ok") doExport(); });

  $("#scrim").addEventListener("click", closeDrawers);
  $$("[data-close]").forEach((b) => b.addEventListener("click", closeDrawers));

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") return closeDrawers();
    if ($("#batchView").hidden || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
    if ($("#leadDrawer").classList.contains("open") || $("#icpDrawer").classList.contains("open")) {
      if (!["j", "k"].includes(e.key)) return;
    }
    if (e.key === "j" || e.key === "k") {
      const n = state.visible.length; if (!n) return;
      state.cursor = Math.max(0, Math.min(n - 1, state.cursor + (e.key === "j" ? 1 : -1)));
      renderRows();
      $("#leadRows tr.cursor")?.scrollIntoView({ block: "nearest" });
      if ($("#leadDrawer").classList.contains("open")) renderLead(state.visible[state.cursor]);
    } else if (e.key === "Enter" && state.cursor >= 0) {
      renderLead(state.visible[state.cursor]); openDrawer("#leadDrawer");
    }
  });
}

async function init() {
  bind();
  const preset = store.get("preset", "acquisition");
  const radio = $(`input[name=preset][value="${preset}"]`);
  if (radio) radio.checked = true;
  $("#enrichToggle").checked = store.get("enrich", true);
  try {
    const [presets, health] = await Promise.all([api("/api/presets"), api("/api/health")]);
    state.presets = presets;
    const pill = $("#aiPill");
    pill.textContent = health.ai ? "✦ AI drafts on" : "Template drafts";
    pill.className = "pill " + (health.ai ? "pill-accent" : "pill-muted");
    pill.title = health.ai ? "Outreach drafts are AI-written" : "Set ANTHROPIC_API_KEY to enable AI-written outreach";
    await loadBatches();
  } catch (e) { toast("Cannot reach server: " + e.message); }
  const id = location.hash.slice(1);
  if (id) openBatch(id); else showImport();
}

init();
