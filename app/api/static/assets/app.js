// Shared helpers + app shell (sidebar on desktop, top bar + tabs on phones).
(function () {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const fmt = (n, d = 2) => (n === null || n === undefined || n === "" || Number.isNaN(Number(n))) ? "–" : Number(n).toLocaleString(undefined, {minimumFractionDigits: d, maximumFractionDigits: d});
  const dt = (iso) => iso ? new Date(iso).toLocaleString(undefined, {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}) : "–";
  const tm = (iso) => iso ? new Date(iso).toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit"}) : "–";
  const day = (iso) => iso ? new Date(iso).toLocaleDateString(undefined, {weekday: "short", month: "short", day: "numeric"}) : "Unknown";
  const short = (iso) => { if (!iso) return "–"; const d = new Date(iso); return d.toDateString() === new Date().toDateString() ? tm(iso) : dt(iso); };
  const pretty = (code) => String(code ?? "").replace(/_/g, " ").toLowerCase();
  const signed = (n, d = 2, suffix = "") => n === null || n === undefined ? "–" : `<span class="num ${n > 0 ? "good" : n < 0 ? "bad" : ""}">${n > 0 ? "+" : ""}${fmt(n, d)}${suffix}</span>`;
  const codes = (list) => (list || []).map(c => esc(c).replace(/_/g, "_<wbr>")).join(", ");

  async function api(path, opts = {}) {
    const res = await fetch(path, {credentials: "same-origin", ...opts, headers: {"X-Requested-With": "tradlysis", "Content-Type": "application/json", ...(opts.headers || {})}});
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
  }

  const ICONS = {
    overview: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M3 16V9m5 7V4m5 12v-5m4 5V7"/></svg>',
    history: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><circle cx="5" cy="4.5" r="1.8"/><circle cx="5" cy="15.5" r="1.8"/><circle cx="14" cy="10" r="1.8"/><path d="M5 6.3v7.4M5 10h7.2"/></svg>',
    analysis: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="3" y="3" width="14" height="14" rx="2"/><path d="M6.5 13l2.5-3 2 2 3-4.5"/></svg>',
  };
  const PAGES = [["overview", "/", "Overview"], ["history", "/history", "History"], ["analysis", "/analysis", "Analysis"]];

  function statusRows(s) {
    if (!s) return [];
    const eng = s.engine || {}, m = eng.market || {}, c = s.controls || {};
    const kill = c.kill_switch || {}, daily = c.daily_loss_breaker || {}, dd = c.drawdown_breaker || {};
    const halted = kill.active ? "Kill switch on" : daily.tripped ? "Daily limit hit" : dd.tripped ? "Drawdown limit hit" : null;
    return [
      ["Mode", s.mode === "demo" ? "good" : "bad", s.mode === "demo" ? "Demo" : "LIVE"],
      ["Engine", s.engine_alive ? "good" : "bad", s.engine_alive ? "Running" : "Offline"],
      ["Prices", !s.engine_alive ? "off" : m.market_open === false ? "off" : m.stream_connected ? "good" : "bad",
        !s.engine_alive ? "–" : m.market_open === false ? "Market closed" : m.stream_connected ? "Live" : "Down"],
      ["Trading", halted ? "bad" : "good", halted || "Allowed"],
      ["Alerts", eng.telegram_enabled ? "good" : "off", eng.telegram_enabled ? "Telegram" : "Off"],
    ];
  }

  function renderShell(active) {
    const side = document.getElementById("side"), top = document.getElementById("topbar");
    const nav = PAGES.map(([k, href, label]) => `<a href="${href}" ${k === active ? 'aria-current="page"' : ""}>${ICONS[k]}${label}</a>`).join("");
    if (side) side.innerHTML = `
      <div class="brand"><b>Tradlysis</b><span>EUR/USD · trend-pullback experiment</span></div>
      <nav class="nav" aria-label="Main">${nav}</nav>
      <div><div class="side-h">Status</div><div class="status-list" id="side-status"><div class="status-row">Loading…</div></div></div>
      <div class="side-foot" id="side-foot"></div>`;
    if (top) top.innerHTML = `
      <div class="row"><b>Tradlysis</b><div class="pills" id="top-status"></div></div>
      <nav class="tabs" aria-label="Main">${PAGES.map(([k, href, label]) => `<a href="${href}" ${k === active ? 'aria-current="page"' : ""}>${label}</a>`).join("")}</nav>`;
  }

  const listeners = [];
  let lastStatus = null;
  async function refreshStatus() {
    try { lastStatus = await api("/api/status"); } catch (e) { lastStatus = null; }
    const rows = statusRows(lastStatus);
    const side = document.getElementById("side-status"), top = document.getElementById("top-status");
    if (side) side.innerHTML = rows.length ? rows.map(([k, tone, v]) => `<div class="status-row"><span>${k}</span><b><span class="dot d-${tone}"></span>${esc(v)}</b></div>`).join("") : `<div class="status-row">Status unavailable</div>`;
    if (top) top.innerHTML = rows.filter(([k]) => k === "Mode" || k === "Engine" || (k === "Trading" && rows.find(r => r[0] === "Trading")[1] === "bad"))
      .map(([, tone, v]) => `<span class="pill"><span class="dot d-${tone}"></span>${esc(v)}</span>`).join("");
    const foot = document.getElementById("side-foot");
    if (foot && lastStatus) foot.textContent = lastStatus.experiment;
    listeners.forEach(fn => fn(lastStatus));
    return lastStatus;
  }

  window.T = {esc, fmt, dt, tm, day, short, pretty, signed, codes, api, renderShell, refreshStatus,
    onStatus: (fn) => { listeners.push(fn); if (lastStatus) fn(lastStatus); }, get status() { return lastStatus; }};
})();
