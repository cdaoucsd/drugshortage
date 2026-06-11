/* Drug Shortage Tracker frontend.
 * Hash routing: #/ = list view, #/drug/<generic name> = detail view. */

const $ = (sel) => document.querySelector(sel);

const state = { page: 1, perPage: 25 };

const FILTER_INPUTS = {
  q: "#f-q",
  status: "#f-status",
  category: "#f-category",
  posted_from: "#f-posted-from",
  posted_to: "#f-posted-to",
  resolved_from: "#f-resolved-from",
  resolved_to: "#f-resolved-to",
};

function esc(s) {
  const div = document.createElement("div");
  div.textContent = s ?? "";
  return div.innerHTML;
}

function fmtDate(d) {
  return d ? d : "—";
}

function badge(status) {
  const cls = (status || "").toLowerCase() === "current" ? "current" : "resolved";
  return `<span class="badge ${cls}">${esc(status)}</span>`;
}

function filterParams() {
  const params = new URLSearchParams();
  for (const [key, sel] of Object.entries(FILTER_INPUTS)) {
    const val = $(sel).value.trim();
    if (val) params.set(key, val);
  }
  params.set("page", state.page);
  params.set("per_page", state.perPage);
  return params;
}

async function fetchJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
  return resp.json();
}

async function loadStats() {
  try {
    const s = await fetchJSON("/api/stats");
    $("#stat-current-drugs").textContent = s.current_drugs;
    $("#stat-current-records").textContent = s.current_records;
    $("#stat-resolved-records").textContent = s.resolved_records;
    const week = Object.values(s.events_last_7_days || {}).reduce((a, b) => a + b, 0);
    $("#stat-week-events").textContent = week;
    if (s.last_sync) {
      $("#last-sync").textContent = `· last sync ${s.last_sync.slice(0, 16).replace("T", " ")} UTC`;
    }
    $("#stats-bar").hidden = false;
  } catch (e) {
    console.warn("stats unavailable", e);
  }
}

async function loadList() {
  const data = await fetchJSON(`/api/drugs?${filterParams()}`);
  $("#result-meta").textContent = `${data.total} drug${data.total === 1 ? "" : "s"} found`;

  const list = $("#drug-list");
  if (!data.results.length) {
    list.innerHTML = `<div class="empty">No shortages match these filters.</div>`;
  } else {
    list.innerHTML = data.results
      .map(
        (d) => `
      <a class="drug-card" href="#/drug/${encodeURIComponent(d.generic_name)}">
        <h3>${esc(d.generic_name)} ${badge(d.status)}</h3>
        <div class="meta">
          <span>${d.company_count} manufacturer${d.company_count === 1 ? "" : "s"}</span>
          ${d.therapeutic_category ? `<span>${esc(d.therapeutic_category)}</span>` : ""}
          <span>first posted ${fmtDate(d.first_posted)}</span>
          <span>last updated ${fmtDate(d.last_updated)}</span>
        </div>
      </a>`
      )
      .join("");
  }

  const pages = Math.max(1, Math.ceil(data.total / data.per_page));
  $("#pagination").innerHTML =
    pages <= 1
      ? ""
      : `<button id="pg-prev" ${data.page <= 1 ? "disabled" : ""}>← Prev</button>
         <span>Page ${data.page} of ${pages}</span>
         <button id="pg-next" ${data.page >= pages ? "disabled" : ""}>Next →</button>`;
  const prev = $("#pg-prev");
  const next = $("#pg-next");
  if (prev) prev.onclick = () => { state.page--; loadList(); };
  if (next) next.onclick = () => { state.page++; loadList(); };
}

function recordRow(r) {
  return `<tr>
    <td>${esc(r.company_name)}<br><small>${esc(r.contact_info || "")}</small></td>
    <td>${esc(r.presentation || [r.strength, r.dosage_form].filter(Boolean).join(" "))}</td>
    <td>${badge(r.status)}</td>
    <td>${fmtDate(r.initial_posting_date)}</td>
    <td>${fmtDate(r.update_date)}</td>
    <td>${fmtDate(r.resolved_date)}</td>
    <td>${esc(r.availability || r.resolved_note || "")}</td>
  </tr>`;
}

function eventItem(e) {
  const labels = {
    new_shortage: "New shortage",
    updated: "Updated",
    resolved: "Resolved",
    reposted: "Back in shortage",
  };
  let diffHtml = "";
  if (e.diff) {
    const items = Object.entries(e.diff)
      .map(([f, v]) => `<li><b>${esc(f)}</b>: ${esc(String(v.old ?? "—"))} → ${esc(String(v.new ?? "—"))}</li>`)
      .join("");
    diffHtml = `<ul class="diff">${items}</ul>`;
  }
  return `<li>
    <span class="badge event">${labels[e.event_type] || esc(e.event_type)}</span>
    <b>${esc(e.company_name || "")}</b>
    <span class="when">${e.occurred_at.slice(0, 16).replace("T", " ")} UTC</span>
    ${diffHtml}
  </li>`;
}

async function loadDetail(name) {
  const content = $("#detail-content");
  content.innerHTML = `<div class="empty">Loading…</div>`;
  let d;
  try {
    d = await fetchJSON(`/api/drugs/${encodeURIComponent(name)}`);
  } catch {
    content.innerHTML = `<div class="empty">Drug not found.</div>`;
    return;
  }
  const reasons = [...new Set(d.records.map((r) => r.shortage_reason).filter(Boolean))];
  content.innerHTML = `
    <div class="detail-header">
      <h2>${esc(d.generic_name)} ${badge(d.status)}</h2>
      ${reasons.length ? `<p><b>Reason${reasons.length > 1 ? "s" : ""}:</b> ${reasons.map(esc).join(" · ")}</p>` : ""}
    </div>
    <div class="detail-section">
      <h3>Manufacturers &amp; presentations (${d.records.length})</h3>
      <table>
        <thead><tr>
          <th>Manufacturer</th><th>Presentation</th><th>Status</th>
          <th>Posted</th><th>Updated</th><th>Resolved</th><th>Notes</th>
        </tr></thead>
        <tbody>${d.records.map(recordRow).join("")}</tbody>
      </table>
    </div>
    <div class="detail-section">
      <h3>History</h3>
      ${d.events.length
        ? `<ul class="timeline">${d.events.map(eventItem).join("")}</ul>`
        : `<p class="empty">No changes recorded since this drug was first imported.</p>`}
    </div>`;
}

function route() {
  const hash = decodeURIComponent(location.hash || "#/");
  const drugMatch = hash.match(/^#\/drug\/(.+)$/);
  $("#view-list").hidden = Boolean(drugMatch);
  $("#view-detail").hidden = !drugMatch;
  if (drugMatch) {
    loadDetail(drugMatch[1]);
  } else {
    loadList();
  }
}

let debounceTimer;
function onFilterChange() {
  state.page = 1;
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(loadList, 250);
}

document.addEventListener("DOMContentLoaded", () => {
  for (const sel of Object.values(FILTER_INPUTS)) {
    $(sel).addEventListener("input", onFilterChange);
  }
  $("#f-clear").addEventListener("click", () => {
    for (const sel of Object.values(FILTER_INPUTS)) $(sel).value = "";
    onFilterChange();
  });
  $("#filters").addEventListener("submit", (e) => e.preventDefault());
  window.addEventListener("hashchange", route);
  loadStats();
  route();
});
