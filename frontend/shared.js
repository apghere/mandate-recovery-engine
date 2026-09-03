// Tiny shared helpers for the dashboard's 3 screens (Phase 9, docs FR-12).
// No framework, no build step — vanilla fetch() against the same API the
// worker/ingest pipeline writes through, on purpose (dashboard doc note:
// "no separate materialized view, no caching layer").

const STATE_COLORS = {
  DUE: "bg-stone-100 text-stone-600",
  DIAGNOSING: "bg-stone-100 text-stone-600",
  PLANNING: "bg-stone-100 text-stone-600",
  SCHEDULED: "bg-amber-100 text-amber-800",
  EXECUTING: "bg-amber-100 text-amber-800",
  ESCALATING: "bg-orange-100 text-orange-800",
  AWAITING_MANUAL: "bg-orange-100 text-orange-800",
  RECOVERED: "bg-emerald-100 text-emerald-800",
  ABANDONED: "bg-rose-100 text-rose-800",
};

// A small dot, not just color, in front of the label — state should be
// legible even to someone who can't distinguish the hues (docs' own
// "communicate the intelligence of the system" demo principle extends to
// not making that communication color-vision-dependent).
const STATE_DOTS = {
  DUE: "bg-stone-400", DIAGNOSING: "bg-stone-400", PLANNING: "bg-stone-400",
  SCHEDULED: "bg-amber-500", EXECUTING: "bg-amber-500",
  ESCALATING: "bg-orange-500", AWAITING_MANUAL: "bg-orange-500",
  RECOVERED: "bg-emerald-500", ABANDONED: "bg-rose-500",
};

function stateBadge(state) {
  const cls = STATE_COLORS[state] || "bg-stone-100 text-stone-600";
  const dot = STATE_DOTS[state] || "bg-stone-400";
  return `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium ${cls}">
    <span class="w-1.5 h-1.5 rounded-full ${dot}"></span>${state}</span>`;
}

// A compact horizontal probability bar — p_success as a shape, not just
// a number, so a row of slots is scannable at a glance rather than
// requiring the reader to parse four decimals.
function probBar(p) {
  if (p === null || p === undefined) return "";
  const pct = Math.max(0, Math.min(1, Number(p))) * 100;
  const tone = pct >= 60 ? "bg-emerald-500" : pct >= 30 ? "bg-amber-500" : "bg-rose-500";
  return `
    <span class="inline-flex items-center gap-1.5 align-middle">
      <span class="w-12 h-1.5 rounded-full bg-stone-200 overflow-hidden">
        <span class="block h-full ${tone} rounded-full" style="width:${pct.toFixed(0)}%"></span>
      </span>
      <span class="font-mono text-xs text-muted tabular-nums">${(pct).toFixed(0)}%</span>
    </span>`;
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
