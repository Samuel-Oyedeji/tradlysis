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

  // The experiment the pages show: ?experiment= in the URL, else the last one picked in this
  // browser, else the API's default (the first enabled experiment).
  const STORE_KEY = "tradlysis.experiment";
  const store = {
    get() { try { return localStorage.getItem(STORE_KEY); } catch (e) { return null; } },
    set(v) { try { v ? localStorage.setItem(STORE_KEY, v) : localStorage.removeItem(STORE_KEY); } catch (e) { /* private mode */ } },
  };
  let experiment = new URLSearchParams(location.search).get("experiment") || store.get() || null;
  const ACCOUNT_WIDE = /^\/api\/(config|experiments|backtest)(\/|\?|$)/;
  const withExperiment = (path) => {
    if (!experiment || !path.startsWith("/api/") || ACCOUNT_WIDE.test(path) || /[?&]experiment=/.test(path)) return path;
    return `${path}${path.includes("?") ? "&" : "?"}experiment=${encodeURIComponent(experiment)}`;
  };
  function selectExperiment(slug, path) {
    store.set(slug);
    const url = new URL(path || location.href, location.href);
    url.searchParams.delete("experiment");
    location.href = url.toString();
  }

  async function api(path, opts = {}) {
    const res = await fetch(withExperiment(path), {credentials: "same-origin", ...opts, headers: {"X-Requested-With": "tradlysis", "Content-Type": "application/json", ...(opts.headers || {})}});
    if (!res.ok) {
      const text = await res.text();
      let detail = text; try { detail = JSON.parse(text).detail || text; } catch (e) { /* not JSON */ }
      const err = new Error(`${res.status} ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
      err.status = res.status; err.detail = detail;
      throw err;
    }
    return res.json();
  }

  const ICONS = {
    overview: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M3 16V9m5 7V4m5 12v-5m4 5V7"/></svg>',
    history: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><circle cx="5" cy="4.5" r="1.8"/><circle cx="5" cy="15.5" r="1.8"/><circle cx="14" cy="10" r="1.8"/><path d="M5 6.3v7.4M5 10h7.2"/></svg>',
    analysis: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="3" y="3" width="14" height="14" rx="2"/><path d="M6.5 13l2.5-3 2 2 3-4.5"/></svg>',
    experiments: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="3" y="3" width="6" height="6" rx="1.2"/><rect x="11" y="3" width="6" height="6" rx="1.2"/><rect x="3" y="11" width="6" height="6" rx="1.2"/><rect x="11" y="11" width="6" height="6" rx="1.2"/></svg>',
    backtest: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M3.5 10a6.5 6.5 0 1 0 2-4.7"/><path d="M3 3.5v3.2h3.2"/><path d="M10 6.5V10l2.5 1.6"/></svg>',
    config: '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M4 5h12M4 10h12M4 15h12"/><circle cx="8" cy="5" r="1.7" fill="var(--nav-bg, #fff)"/><circle cx="13" cy="10" r="1.7" fill="var(--nav-bg, #fff)"/><circle cx="7" cy="15" r="1.7" fill="var(--nav-bg, #fff)"/></svg>',
  };
  const PAGES = [["overview", "/", "Overview"], ["history", "/history", "History"], ["analysis", "/analysis", "Analysis"],
    ["experiments", "/experiments", "Experiments"], ["backtest", "/backtest", "Backtest"], ["config", "/config", "Config"]];
  // Pages about one experiment show the picker; the account-wide ones don't.
  const PER_EXPERIMENT = new Set(["overview", "history", "analysis"]);
  const pair = (inst) => String(inst || "").replace("_", "/");

  function statusRows(s) {
    if (!s) return [];
    const eng = s.engine || {}, m = s.market || {}, c = s.controls || {};
    const kill = c.kill_switch || {}, daily = c.daily_loss_breaker || {}, dd = c.drawdown_breaker || {};
    const waiting = s.engine_alive && s.engine_state === "WAITING";
    const idle = s.engine_alive && !waiting && !s.running;
    const halted = kill.active ? "Kill switch on" : daily.tripped ? "Daily limit hit" : dd.tripped ? "Drawdown limit hit"
      : idle ? "Not running" : null;
    const live = s.engine_alive && s.running;
    return [
      ["Mode", s.mode === "demo" ? "good" : "bad", s.mode === "demo" ? "Demo" : "LIVE"],
      ["Engine", !s.engine_alive || waiting ? "bad" : "good", !s.engine_alive ? "Offline" : waiting ? "Waiting" : "Running"],
      ["Prices", !live ? "off" : m.market_open === false ? "off" : m.stream_connected ? "good" : "bad",
        !live ? "–" : m.market_open === false ? "Market closed" : m.stream_connected ? "Live" : "Down"],
      ["Trading", halted ? (idle ? "off" : "bad") : "good", halted || "Allowed"],
      ["Alerts", eng.telegram_enabled ? "good" : "off", eng.telegram_enabled ? "Telegram" : "Off"],
    ];
  }

  function pickerHtml(id) {
    return `<label class="exp-pick"><span class="side-h">Experiment</span><select id="${id}" aria-label="Experiment"><option>Loading…</option></select></label>`;
  }

  function fillPickers(s) {
    const list = (s && s.experiments) || [];
    document.querySelectorAll(".exp-pick select").forEach(sel => {
      sel.innerHTML = list.map(e => `<option value="${esc(e.slug)}" ${e.slug === s.experiment ? "selected" : ""}>${esc(e.name)}${e.enabled ? "" : " (off)"}</option>`).join("");
      sel.onchange = () => selectExperiment(sel.value);
    });
    const info = s && s.experiment_info;
    const sub = document.getElementById("brand-sub");
    if (sub && info && document.querySelector(".exp-pick")) sub.textContent = `${pair(info.instrument)} · ${pretty(info.strategy)}${info.enabled ? "" : " · off"}`;
  }

  function renderShell(active) {
    const side = document.getElementById("side"), top = document.getElementById("topbar");
    const picker = PER_EXPERIMENT.has(active);
    const nav = PAGES.map(([k, href, label]) => `<a href="${href}" ${k === active ? 'aria-current="page"' : ""}>${ICONS[k]}${label}</a>`).join("");
    if (side) side.innerHTML = `
      <div class="brand"><b>Tradlysis</b><span id="brand-sub">${picker ? "" : "All experiments"}</span></div>
      ${picker ? pickerHtml("exp-side") : ""}
      <nav class="nav" aria-label="Main">${nav}</nav>
      <div><div class="side-h">Status</div><div class="status-list" id="side-status"><div class="status-row">Loading…</div></div></div>
      <div class="side-foot" id="side-foot"></div>`;
    if (top) top.innerHTML = `
      <div class="row"><b>Tradlysis</b><div class="pills" id="top-status"></div></div>
      ${picker ? `<div class="row">${pickerHtml("exp-top")}</div>` : ""}
      <nav class="tabs" aria-label="Main">${PAGES.map(([k, href, label]) => `<a href="${href}" ${k === active ? 'aria-current="page"' : ""}>${label}</a>`).join("")}</nav>`;
  }

  const listeners = [];
  let lastStatus = null;
  async function refreshStatus() {
    try { lastStatus = await api("/api/status"); } catch (e) {
      lastStatus = null;
      if (e.status === 404 && experiment) { store.set(null); experiment = null; return refreshStatus(); }  // deleted/renamed
    }
    if (lastStatus && !experiment) experiment = lastStatus.experiment;
    fillPickers(lastStatus);
    const rows = statusRows(lastStatus);
    const side = document.getElementById("side-status"), top = document.getElementById("top-status");
    if (side) side.innerHTML = rows.length ? rows.map(([k, tone, v]) => `<div class="status-row"><span>${k}</span><b><span class="dot d-${tone}"></span>${esc(v)}</b></div>`).join("") : `<div class="status-row">Status unavailable</div>`;
    if (top) top.innerHTML = rows.filter(([k]) => k === "Mode" || k === "Engine" || (k === "Trading" && rows.find(r => r[0] === "Trading")[1] === "bad"))
      .map(([, tone, v]) => `<span class="pill"><span class="dot d-${tone}"></span>${esc(v)}</span>`).join("");
    const foot = document.getElementById("side-foot");
    if (foot && lastStatus) {
      const waiting = lastStatus.engine_state === "WAITING" && lastStatus.engine_reason;
      foot.textContent = waiting ? `Engine waiting: ${lastStatus.engine_reason}`
        : lastStatus.config_pending ? "Config saved · engine restarting" : lastStatus.experiment;
    }
    listeners.forEach(fn => fn(lastStatus));
    return lastStatus;
  }

  window.T = {esc, fmt, dt, tm, day, short, pretty, signed, codes, api, pair, renderShell, refreshStatus, selectExperiment,
    onStatus: (fn) => { listeners.push(fn); if (lastStatus) fn(lastStatus); }, get status() { return lastStatus; },
    get experiment() { return experiment; }};
})();
