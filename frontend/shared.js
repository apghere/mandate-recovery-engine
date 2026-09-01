// Tiny shared helpers for the dashboard's 3 screens (Phase 9, docs FR-12).
// No framework, no build step -- vanilla fetch() against the same API the
// worker/ingest pipeline writes through, on purpose (dashboard doc note:
// "no separate materialized view, no caching layer").

const STATE_COLORS = {
  DUE: "bg-slate-200 text-slate-700",
  DIAGNOSING: "bg-slate-200 text-slate-700",
  PLANNING: "bg-slate-200 text-slate-700",
  SCHEDULED: "bg-amber-100 text-amber-800",
  EXECUTING: "bg-amber-100 text-amber-800",
  ESCALATING: "bg-orange-100 text-orange-800",
  AWAITING_MANUAL: "bg-orange-100 text-orange-800",
  RECOVERED: "bg-emerald-100 text-emerald-800",
  ABANDONED: "bg-rose-100 text-rose-800",
};

function stateBadge(state) {
  const cls = STATE_COLORS[state] || "bg-slate-200 text-slate-700";
  return `<span class="inline-block px-2 py-0.5 rounded text-xs font-medium ${cls}">${state}</span>`;
}

function fmtRupees(n) {
  return "₹" + Number(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}

async function apiGet(path) {
  const resp = await fetch(path);
  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`${resp.status} ${resp.statusText}: ${body}`);
  }
  return resp.json();
}

async function apiPost(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const t = await resp.text();
    throw new Error(`${resp.status} ${resp.statusText}: ${t}`);
  }
  return resp.json();
}

function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}
