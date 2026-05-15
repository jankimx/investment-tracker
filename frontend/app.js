// -- Config ------------------------------------------------
// API URL can be overridden by setting localStorage.api_url (handy for local dev).
const DEFAULT_API = 'https://web-production-780915.up.railway.app';
const API = (localStorage.getItem('api_url') || DEFAULT_API).replace(/\/+$/, '');
const REQUEST_TIMEOUT_MS = 60000; // analyzer can take 30-60s; everything else is fast

// -- State -------------------------------------------------
let state = {
  entries: [], positions: [], chartRows: [],
  platforms: [], stocks: [],
  summary: null, holdings: [], transactions: [],
  posGrouping: 'none', posSortCol: 'value', posSortDir: 1
};

let growthChart = null;
let projChart   = null;

// -- Init --------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('bal-date').value = today();
  document.getElementById('txn-date').value = today();
  if (sessionStorage.getItem('token')) showApp();
  renderProjections();
});

// -- Utilities ---------------------------------------------
const today = () => new Date().toISOString().split('T')[0];

const fmt = v => '$' + Math.round(Math.abs(v)).toLocaleString();

const fmtDec = v => '$' + Math.abs(Number(v)).toLocaleString('en-US', {
  minimumFractionDigits: 2, maximumFractionDigits: 2
});

const fmtGain = (v, pct) => {
  if (v === null || v === undefined) return { dollar: '--', pct: '--' };
  const sign = v >= 0 ? '+' : '-';
  const cls  = v >= 0 ? 'positive' : 'negative';
  return {
    dollar: sign + fmtDec(v),
    pct:    pct !== null && pct !== undefined ? sign + Math.abs(pct).toFixed(2) + '%' : '',
    cls
  };
};

function badgeClass(s) {
  let h = 0;
  for (const c of s) h = (h * 31 + c.charCodeAt(0)) % 6;
  return 'b' + h;
}

// Build a <span class="badge ..."> with the platform name as textContent so
// user-controlled values can never become HTML.
function badgeEl(name) {
  const span = document.createElement('span');
  span.className = `badge ${badgeClass(name)}`;
  span.textContent = name;
  return span;
}

// <td> with text content; pass cls for className.
function tdText(text, cls) {
  const td = document.createElement('td');
  td.textContent = text == null ? '' : String(text);
  if (cls) td.className = cls;
  return td;
}

// <td> wrapping a single child element.
function tdNode(node, cls) {
  const td = document.createElement('td');
  if (node) td.appendChild(node);
  if (cls) td.className = cls;
  return td;
}

const COLORS = ['#4a90e2','#23d160','#ffb347','#c875ff','#ff6b7a','#00d2d3','#f9ca24','#6ab04c'];
function colorFor(s) {
  let h = 0;
  for (const c of s) h = (h * 31 + c.charCodeAt(0)) % COLORS.length;
  return COLORS[h];
}

function showToast(msg, dur = 2500) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), dur);
}

// -- API ---------------------------------------------------
async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  const token = sessionStorage.getItem('token');
  if (token) headers['Authorization'] = 'Bearer ' + token;

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
  let res;
  try {
    res = await fetch(API + path, { ...opts, headers, signal: ctrl.signal });
  } catch (e) {
    clearTimeout(timer);
    if (e.name === 'AbortError') throw new Error('Request timed out');
    throw e;
  }
  clearTimeout(timer);

  // Server says our session is dead — drop to login.
  if (res.status === 401 && path !== '/auth') {
    sessionStorage.removeItem('token');
    location.reload();
    throw new Error('Session expired');
  }

  let data = {};
  try { data = await res.json(); } catch { /* empty body is fine */ }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// -- Auth --------------------------------------------------
async function doLogin() {
  const pw  = document.getElementById('login-pw').value;
  const err = document.getElementById('login-err');
  err.textContent = '';
  try {
    const data = await api('/auth', {
      method: 'POST',
      body: JSON.stringify({ password: pw })
    });
    if (data.ok && data.token) {
      sessionStorage.setItem('token', data.token);
      showApp();
    } else {
      err.textContent = 'Wrong password.';
    }
  } catch (e) {
    err.textContent = e.message === 'Request timed out' ? 'Server not responding.' : 'Cannot reach server.';
  }
}

document.getElementById('login-pw').addEventListener('keydown', e => {
  if (e.key === 'Enter') doLogin();
});

async function logout() {
  stopAutoPoll();
  try { await api('/auth/logout', { method: 'POST' }); } catch { /* best effort */ }
  sessionStorage.removeItem('token');
  location.reload();
}

function showApp() {
  document.getElementById('login-screen').style.display = 'none';
  document.getElementById('main-app').style.display     = 'block';
  loadAll();
  startAutoPoll();
}

// -- Auto-poll ---------------------------------------------
// Polls /summary + co. every AUTO_POLL_MS while the tab is visible. Pauses
// when the tab is hidden (Page Visibility API) and does an immediate refresh
// on return. Backend cron also pushes new prices every minute, so 60s here
// keeps the UI within ~60s of the latest FMP fetch.
const AUTO_POLL_MS = 60_000;
let _autoPollInterval = null;

function startAutoPoll() {
  stopAutoPoll();
  _autoPollInterval = setInterval(() => {
    if (document.hidden) return;
    loadAll();
  }, AUTO_POLL_MS);
  document.addEventListener('visibilitychange', _onVisibility);
}

function stopAutoPoll() {
  if (_autoPollInterval) { clearInterval(_autoPollInterval); _autoPollInterval = null; }
  document.removeEventListener('visibilitychange', _onVisibility);
}

function _onVisibility() {
  // When the user comes back to the tab, immediately refresh so they don't
  // see stale numbers while waiting for the next interval tick.
  if (!document.hidden) loadAll();
}

// -- Load --------------------------------------------------
//
// Only fetch what the Dashboard needs on first paint. Other tabs lazy-load
// their data when first opened (and re-fetch after mutations via
// invalidate*() helpers).
//
// state.loaded.* tracks which secondary fetches have completed so we don't
// re-hit the API on every tab click.
state.loaded = { entries: false, stocks: false, transactions: false, balances: false };

async function loadAll() {
  // Mutations call loadAll(); when they do, any cached secondary data is now
  // stale and should re-fetch on its next tab open.
  invalidateAll();
  try {
    const [positions, chartData, platforms, summary, holdings, refreshStatus] = await Promise.all([
      api('/positions'),
      api('/chart-data?days=90'),
      api('/platforms'),
      api('/summary'),
      api('/holdings'),
      api('/refresh-status').catch(() => null),
    ]);
    state.positions = positions;
    state.chartRows = (chartData && chartData.rows) || [];
    state.platforms = platforms;
    state.summary   = summary;
    state.holdings  = holdings;
    updateDataLists();
    renderDashboard();
    applyRefreshStatus(refreshStatus);
  } catch (e) {
    console.error('loadAll failed:', e);
    showToast('Failed to load data', 4000);
  }
}

// On-demand fetchers for secondary data. Each is idempotent: it caches in
// state.* and only re-fetches if forced.
async function ensureStocks(force = false) {
  if (state.loaded.stocks && !force) return state.stocks;
  state.stocks = await api('/stocks');
  state.loaded.stocks = true;
  updateDataLists();
  return state.stocks;
}
function invalidateAll() {
  // Called after mutations: clear secondary caches so the next tab open
  // sees fresh data. Dashboard data is refetched via loadAll() directly.
  state.loaded = { entries: false, stocks: false, transactions: false, balances: false };
}

function fillDatalist(id, values) {
  const dl = document.getElementById(id);
  if (!dl) return;
  dl.replaceChildren(...values.map(v => {
    const o = document.createElement('option');
    o.value = v;
    return o;
  }));
}

function updateDataLists() {
  fillDatalist('dl-platform', state.platforms);
  fillDatalist('dl-stock', state.stocks);
  fillDatalist('dl-h-platform', state.platforms);
}

// -- Tabs --------------------------------------------------
async function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelector(`[data-tab="${tab}"]`).classList.add('active');
  document.getElementById(`tab-${tab}`).classList.add('active');

  if (tab === 'projections') {
    renderProjections();
  } else if (tab === 'holdings') {
    await ensureStocks();
    renderHoldings();
    renderTransactions();
  } else if (tab === 'balances') {
    await ensureStocks();
    renderBalances();
  } else if (tab === 'analyze') {
    initAnalyzeTab();
  }
}

// -- Refresh status ----------------------------------------
function applyRefreshStatus(s) {
  const el = document.getElementById('refresh-status');
  if (!el) return;
  el.replaceChildren();
  if (!s) return;
  const span = document.createElement('span');
  if (s.refreshed_today) {
    const dt   = new Date(s.last_refresh + 'Z');
    const time = dt.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
    span.className   = 'refresh-status-ok';
    span.textContent = `Refreshed today at ${time} (${s.count} prices)`;
  } else {
    span.className   = 'refresh-status-pending';
    span.textContent = 'Not yet refreshed today';
  }
  el.appendChild(span);

  // Persist any errors from the last attempt right under the status, so we
  // don't lose them when the toast disappears.
  const errs = s.errors || [];
  if (errs.length) {
    const errBox = document.createElement('div');
    errBox.style.cssText = 'flex-basis:100%;font-size:11px;color:#ff6b6b;margin-top:4px;';
    const summary = `Last refresh errors (${errs.length}): `;
    errBox.textContent = summary + errs.slice(0, 3).map(e => `${e.stock}: ${e.error}`).join(' | ');
    el.appendChild(errBox);
  }
}

async function manualRefresh() {
  const btn = document.getElementById('btn-refresh-now');
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = 'Refreshing…';
  try {
    await api('/refresh-prices', { method: 'POST' });
    // Poll briefly until the lock clears, then reload.
    let lastStatus = null;
    for (let i = 0; i < 30; i++) {
      await new Promise(r => setTimeout(r, 1000));
      const s = await api('/refresh-status').catch(() => null);
      if (s) lastStatus = s;
      if (s && !s.in_progress) break;
    }
    await loadAll();
    if (lastStatus && (lastStatus.errors || []).length > 0) {
      const first = lastStatus.errors[0];
      showToast(`Refresh error: ${first.error || JSON.stringify(first)}`, 8000);
    } else if (lastStatus && lastStatus.count === 0) {
      showToast('Refresh returned 0 prices', 6000);
    } else {
      showToast(`Refreshed ${lastStatus ? lastStatus.count : ''} prices`, 2500);
    }
  } catch (e) {
    showToast(e.message || 'Refresh failed', 4000);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

// -- Dashboard ---------------------------------------------
function renderDashboard() {
  const s = state.summary;
  if (!s) return;

  setText('m-total',    fmtDec(s.total_value));
  setText('m-invested', fmtDec(s.total_invested));

  const tg = fmtGain(s.total_gain, s.total_invested > 0 ? (s.total_gain / s.total_invested * 100) : null);
  setGain('m-gain', 'm-gainpct', tg);

  if (s.daily_gain !== null && s.daily_gain !== undefined) {
    const dg = fmtGain(s.daily_gain, s.daily_gain_pct);
    setGain('m-daily', 'm-dailypct', dg);
  } else {
    setText('m-daily', '--');
    setText('m-dailypct', 'Not enough data');
    document.getElementById('m-daily').className = 'metric-value';
  }

  // Platform filter
  const pfSel = document.getElementById('dash-platform');
  const curPF = pfSel.value;
  pfSel.replaceChildren();
  const allOpt = document.createElement('option');
  allOpt.value = 'all';
  allOpt.textContent = 'All platforms';
  pfSel.appendChild(allOpt);
  state.platforms.forEach(p => {
    const o = document.createElement('option');
    o.value = p;
    o.textContent = p;
    if (p === curPF) o.selected = true;
    pfSel.appendChild(o);
  });

  const chartRows = state.chartRows || [];
  const filtered = pfSel.value === 'all' ? chartRows
    : chartRows.filter(e => e.platform === pfSel.value);
  renderChart(filtered, document.getElementById('dash-view').value);
  renderPositionsTable();
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function setGain(valId, pctId, g) {
  const ve = document.getElementById(valId);
  const pe = document.getElementById(pctId);
  if (ve) { ve.textContent = g.dollar; ve.className = 'metric-value ' + (g.cls || ''); }
  if (pe) pe.textContent = g.pct;
}

// -- Chart -------------------------------------------------

// Build a series of cumulative totals, one value per date in `allDates`.
// Walks `subset` once: for each entry we update a running map of "latest
// value per (platform||stock)", then sum the map at every date boundary.
// O(N + D) instead of the previous O(N * D).
function buildSeries(subset, allDates) {
  const sorted = [...subset].sort((a, b) => a.date < b.date ? -1 : a.date > b.date ? 1 : 0);
  const latest = new Map();
  let running = 0;
  let cursor = 0;
  const out = new Array(allDates.length);

  for (let di = 0; di < allDates.length; di++) {
    const d = allDates[di];
    while (cursor < sorted.length && sorted[cursor].date <= d) {
      const e = sorted[cursor++];
      const k = e.platform + '||' + e.stock;
      const prev = latest.get(k);
      if (prev === undefined) { latest.set(k, e.value); running += e.value; }
      else { running += (e.value - prev); latest.set(k, e.value); }
    }
    out[di] = Math.round(running);
  }
  return out;
}

function renderLegend(legendEl, datasets) {
  legendEl.replaceChildren();
  datasets.forEach(d => {
    const item = document.createElement('span');
    item.className = 'legend-item';
    const dot = document.createElement('span');
    dot.className = 'legend-dot';
    dot.style.background = d.borderColor;
    item.appendChild(dot);
    item.appendChild(document.createTextNode(d.label));
    legendEl.appendChild(item);
  });
}

function renderChart(data, view) {
  const allDates = [...new Set(data.map(e => e.date))].sort();
  const legend = document.getElementById('chart-legend');
  if (!allDates.length) {
    if (growthChart) { growthChart.destroy(); growthChart = null; }
    legend.replaceChildren();
    return;
  }

  let datasets = [];

  if (view === 'total') {
    datasets = [{
      label: 'Total value', tension: 0, pointRadius: 3, borderWidth: 1.5,
      data: buildSeries(data, allDates),
      borderColor: '#4a90e2', backgroundColor: 'rgba(74,144,226,0.06)', fill: true
    }];
  } else {
    const keyFn = view === 'platform' ? e => e.platform : e => e.stock;
    const groups = new Map();
    data.forEach(e => {
      const k = keyFn(e);
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(e);
    });
    datasets = [...groups.entries()].map(([k, subset]) => ({
      label: k, tension: 0, pointRadius: 2, borderWidth: 1.5, fill: false,
      data: buildSeries(subset, allDates),
      borderColor: colorFor(k)
    }));
  }

  renderLegend(legend, datasets);

  if (growthChart) growthChart.destroy();
  growthChart = new Chart(document.getElementById('growth-chart'), {
    type: 'line',
    data: { labels: allDates, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => c.dataset.label + ': ' + fmtDec(c.parsed.y) } }
      },
      scales: {
        x: { ticks: { maxTicksLimit: 6, font: { size: 10 }, color: '#555560' }, grid: { display: false } },
        y: { ticks: { callback: v => '$' + Math.round(v / 1000) + 'k', font: { size: 10 }, color: '#555560', maxTicksLimit: 5 }, grid: { color: 'rgba(255,255,255,0.04)' } }
      }
    }
  });
}

// -- Positions table ---------------------------------------
function setGrouping(g, btn) {
  state.posGrouping = g;
  document.querySelectorAll('.toggle-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderPositionsTable();
}

function sortPositions(col) {
  if (state.posSortCol === col) state.posSortDir *= -1;
  else { state.posSortCol = col; state.posSortDir = 1; }
  renderPositionsTable();
}

function buildSparkline(platform, stock) {
  const pts = [];
  const seen = {};
  // chartRows is the slim 90-day window; covers sparkline needs without
  // shipping every historical entry the way state.entries used to.
  (state.chartRows || []).forEach(e => {
    if (e.platform === platform && e.stock === stock && !seen[e.date]) {
      seen[e.date] = true;
      pts.push({ date: e.date, value: e.value });
    }
  });
  pts.sort((a, b) => a.date < b.date ? -1 : 1);
  const last7 = pts.slice(-7);
  if (last7.length < 2) return '<span style="color:var(--text3)">--</span>';

  const vals = last7.map(p => p.value);
  const min  = Math.min(...vals), max = Math.max(...vals);
  const range = max - min || 1;
  const W = 56, H = 22, pad = 2;
  const points = vals.map((v, i) => {
    const x = pad + (i / (vals.length - 1)) * (W - pad * 2);
    const y = pad + (1 - (v - min) / range) * (H - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const color = vals[vals.length - 1] >= vals[0] ? 'var(--green)' : 'var(--red)';
  return `<svg class="sparkline" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
    <polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

function buildPositionRows() {
  // Source of truth for "what positions exist right now" = holdings, not
  // entries. Filtering against this set hides ghost rows for positions you
  // no longer hold (the entries history sticks around but stops rendering).
  const liveKeys = new Set(
    state.holdings.map(h => (h.platform || '') + '||' + (h.stock || '').toUpperCase())
  );

  const totalValue = state.summary?.total_value || 1;
  const positions  = state.positions || [];

  return positions
    .filter(e => liveKeys.has((e.platform || '') + '||' + (e.stock || '').toUpperCase()))
    .map(e => ({
      platform:       e.platform,
      stock:          e.stock,
      value:          e.value,
      invested:       e.invested || 0,
      shares:         e.shares || null,
      price:          e.price || null,
      totalGain:      e.invested ? e.value - e.invested : null,
      totalGainPct:   (e.invested && e.invested > 0) ? ((e.value - e.invested) / e.invested * 100) : null,
      dailyGain:      e.daily_gain ?? null,
      dailyGainPct:   e.daily_gain_pct ?? null,
      pctOfPortfolio: (e.value / totalValue * 100),
    }));
}

function sortRows(rows) {
  const { posSortCol: col, posSortDir: dir } = state;
  const num = (a, b, key) => ((b[key] || 0) - (a[key] || 0)) * dir;
  const str = (a, b, key) => a[key].localeCompare(b[key]) * dir;
  const fns = {
    value: (a, b) => num(a, b, 'value'),
    totalGain: (a, b) => num(a, b, 'totalGain'),
    totalGainPct: (a, b) => num(a, b, 'totalGainPct'),
    dailyGain: (a, b) => num(a, b, 'dailyGain'),
    dailyGainPct: (a, b) => num(a, b, 'dailyGainPct'),
    pctOfPortfolio: (a, b) => num(a, b, 'pctOfPortfolio'),
    stock: (a, b) => str(a, b, 'stock'),
    platform: (a, b) => str(a, b, 'platform')
  };
  return [...rows].sort(fns[col] || fns.value);
}

function makeGainTd(val, pct) {
  const td1 = document.createElement('td');
  const td2 = document.createElement('td');
  td1.className = td2.className = 'td-r';
  if (val === null || val === undefined) {
    td1.textContent = '--'; td2.textContent = '--';
  } else {
    const g = fmtGain(val, pct);
    td1.textContent = g.dollar; td1.classList.add(g.cls);
    td2.textContent = g.pct;    td2.classList.add(g.cls);
  }
  return [td1, td2];
}

function buildTr(r) {
  const tr = document.createElement('tr');

  const tdBadge = document.createElement('td');
  const badge = document.createElement('span');
  badge.className = `badge ${badgeClass(r.platform)}`;
  badge.textContent = r.platform;
  tdBadge.appendChild(badge);

  const tdStock = document.createElement('td');
  tdStock.style.fontWeight = '500';
  tdStock.textContent = r.stock;

  const tdSpark = document.createElement('td');
  tdSpark.innerHTML = buildSparkline(r.platform, r.stock);

  const tdVal = document.createElement('td'); tdVal.className = 'td-r'; tdVal.textContent = fmtDec(r.value);
  const tdInv = document.createElement('td'); tdInv.className = 'td-r'; tdInv.textContent = r.invested ? fmtDec(r.invested) : '--';
  const tdPct = document.createElement('td'); tdPct.className = 'td-r'; tdPct.textContent = r.pctOfPortfolio.toFixed(2) + '%';

  const [tdTG, tdTGP]  = makeGainTd(r.totalGain, r.totalGainPct);
  const [tdDG, tdDGP]  = makeGainTd(r.dailyGain, r.dailyGainPct);

  [tdBadge, tdStock, tdSpark, tdVal, tdInv, tdTG, tdTGP, tdDG, tdDGP, tdPct].forEach(td => tr.appendChild(td));
  return tr;
}

function buildAggregateTr(label, groupKey, grp, totalValue) {
  const tr = document.createElement('tr');

  const value     = grp.reduce((s, r) => s + r.value, 0);
  const invested  = grp.reduce((s, r) => s + r.invested, 0);
  const tg        = invested > 0 ? value - invested : null;
  const tgPct     = (invested > 0) ? (tg / invested * 100) : null;
  const dgRows    = grp.filter(r => r.dailyGain !== null);
  const dg        = dgRows.length ? dgRows.reduce((s, r) => s + r.dailyGain, 0) : null;
  const dgPrev    = dgRows.reduce((s, r) => s + (r.value - r.dailyGain), 0);
  const dgPct     = (dg !== null && dgPrev > 0) ? (dg / dgPrev * 100) : null;
  const pct       = value / totalValue * 100;

  const td1 = document.createElement('td'); td1.colSpan = 3;
  if (groupKey === 'platform') {
    const badge = document.createElement('span');
    badge.className = `badge ${badgeClass(label)}`;
    badge.textContent = label;
    td1.appendChild(badge);
  } else {
    td1.textContent = label; td1.style.fontWeight = '500';
  }

  const tdVal = document.createElement('td'); tdVal.className = 'td-r'; tdVal.textContent = fmtDec(value);
  const tdInv = document.createElement('td'); tdInv.className = 'td-r'; tdInv.textContent = invested ? fmtDec(invested) : '--';
  const tdPct = document.createElement('td'); tdPct.className = 'td-r'; tdPct.textContent = pct.toFixed(2) + '%';

  const [tdTG, tdTGP] = makeGainTd(tg, tgPct);
  const [tdDG, tdDGP] = makeGainTd(dg, dgPct);

  [td1, tdVal, tdInv, tdTG, tdTGP, tdDG, tdDGP, tdPct].forEach(td => tr.appendChild(td));
  return tr;
}

function renderPositionsTable() {
  const container = document.getElementById('positions-container');
  if (!container || !(state.positions || []).length) return;

  const { posSortCol, posSortDir, posGrouping } = state;
  const totalValue = state.summary?.total_value || 1;

  // Build thead
  const cols = [
    { key: 'platform', label: 'Platform', left: true },
    { key: 'stock', label: 'Stock', left: true },
    { key: null, label: '7D' },
    { key: 'value', label: 'Value' },
    { key: null, label: 'Cost basis' },
    { key: 'totalGain', label: 'Total gain' },
    { key: 'totalGainPct', label: 'Total %' },
    { key: 'dailyGain', label: 'Daily gain' },
    { key: 'dailyGainPct', label: 'Daily %' },
    { key: 'pctOfPortfolio', label: '% Portfolio' }
  ];

  const table = document.createElement('table');
  table.className = 'pos-table';

  const thead = document.createElement('thead');
  const htr   = document.createElement('tr');
  cols.forEach(c => {
    const th = document.createElement('th');
    if (!c.left) th.className = 'td-r';
    if (c.key) {
      th.setAttribute('data-col', c.key);
      const arrow = posSortCol === c.key ? (posSortDir === 1 ? ' &#9660;' : ' &#9650;') : '';
      if (posSortCol === c.key) th.classList.add('sorted');
      th.innerHTML = c.label + arrow;
      th.addEventListener('click', () => sortPositions(c.key));
    } else {
      th.textContent = c.label;
    }
    htr.appendChild(th);
  });
  thead.appendChild(htr);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  const rows  = buildPositionRows();
  const sorted = sortRows(rows);

  if (posGrouping === 'none') {
    sorted.forEach(r => tbody.appendChild(buildTr(r)));
  } else {
    const key    = posGrouping === 'platform' ? 'platform' : 'stock';
    const groups = {};
    sorted.forEach(r => { if (!groups[r[key]]) groups[r[key]] = []; groups[r[key]].push(r); });
    // Sort groups by total value descending
    Object.entries(groups)
      .sort((a, b) => b[1].reduce((s, r) => s + r.value, 0) - a[1].reduce((s, r) => s + r.value, 0))
      .forEach(([label, grp]) => tbody.appendChild(buildAggregateTr(label, key, grp, totalValue)));
  }

  table.appendChild(tbody);
  container.innerHTML = '';
  container.appendChild(table);
}

// -- Holdings ----------------------------------------------
function renderHoldings() {
  const el = document.getElementById('holdings-list');
  if (!el) return;
  el.replaceChildren();

  // Holdings tab is for transaction-tracked positions only. Value-only
  // balances live on the Balances tab; they'd render meaninglessly here
  // (no ticker, no shares).
  const txnRows = (state.holdings || []).filter(h => h.source !== 'balance');

  if (!txnRows.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = 'No holdings yet.';
    el.appendChild(empty);
    return;
  }

  const table = document.createElement('table');
  const thead = document.createElement('thead');
  const htr   = document.createElement('tr');
  ['Platform','Ticker','Shares','Avg cost/share','Cost basis',''].forEach((label, i) => {
    const th = document.createElement('th');
    th.textContent = label;
    if (i >= 2 && i <= 4) th.className = 'td-r';
    htr.appendChild(th);
  });
  thead.appendChild(htr);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  txnRows.forEach(h => {
    const tr = document.createElement('tr');
    tr.appendChild(tdNode(badgeEl(h.platform)));

    const tickerTd = tdText(h.stock);
    tickerTd.style.fontWeight = '500';
    tr.appendChild(tickerTd);

    tr.appendChild(tdText(Number(h.shares).toLocaleString('en-US', { maximumFractionDigits: 4 }), 'td-r'));
    tr.appendChild(tdText(h.cost_basis ? fmtDec(h.cost_basis) : '--', 'td-r'));
    tr.appendChild(tdText(h.cost_basis ? fmtDec(h.cost_basis * h.shares) : '--', 'td-r'));

    const btn = document.createElement('button');
    btn.className = 'btn btn-sm btn-danger';
    btn.textContent = 'Remove';
    btn.addEventListener('click', () => deleteHolding(h.id));
    tr.appendChild(tdNode(btn));

    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  el.appendChild(table);
}

async function saveTransaction() {
  const platform = document.getElementById('txn-platform').value.trim();
  const stock    = document.getElementById('txn-stock').value.trim().toUpperCase();
  const action   = document.getElementById('txn-action').value;
  const shares   = document.getElementById('txn-shares').value;
  const price    = document.getElementById('txn-price').value;
  const date     = document.getElementById('txn-date').value;
  if (!platform || !stock || !shares || !price) { showToast('Fill in all fields'); return; }

  const btn = document.getElementById('txn-btn');
  btn.disabled = true; btn.textContent = 'Saving...';
  try {
    await api('/transactions', {
      method: 'POST',
      body: JSON.stringify({ platform, stock, action, shares: Number(shares), price_per_share: Number(price), date: date || today() })
    });
    document.getElementById('txn-shares').value = '';
    document.getElementById('txn-price').value  = '';
    document.getElementById('txn-preview').textContent = '';
    showToast(`${action === 'buy' ? 'Bought' : 'Sold'} ${shares} shares of ${stock}`);
    await loadAll();
  } catch (e) { showToast('Failed: ' + e.message, 4000); }
  btn.disabled = false; btn.textContent = 'Save transaction';
}

function updateTxnPreview() {
  const shares = document.getElementById('txn-shares').value;
  const price  = document.getElementById('txn-price').value;
  const stock  = document.getElementById('txn-stock').value.trim().toUpperCase();
  const action = document.getElementById('txn-action').value;
  const el     = document.getElementById('txn-preview');
  if (shares && price && stock) {
    const total = (Number(shares) * Number(price)).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
    el.textContent = `${action === 'buy' ? 'Buying' : 'Selling'} ${shares} shares of ${stock} at $${Number(price).toFixed(2)} = ${total}`;
  } else {
    el.textContent = '';
  }
}

async function deleteHolding(id) {
  if (!confirm('Remove this holding?')) return;
  try { await api('/holdings/' + id, { method: 'DELETE' }); showToast('Removed'); await loadAll(); }
  catch (e) { showToast('Failed: ' + e.message, 4000); }
}

async function renderTransactions() {
  try {
    const txns = await api('/transactions');
    const el   = document.getElementById('txn-history');
    if (!el) return;
    el.replaceChildren();
    if (!txns.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'No transactions yet.';
      el.appendChild(empty);
      return;
    }

    const table = document.createElement('table');
    const thead = document.createElement('thead');
    const htr = document.createElement('tr');
    ['Date','Platform','Ticker','Action','Shares','Price/share','Total',''].forEach((label, i) => {
      const th = document.createElement('th');
      th.textContent = label;
      if (i >= 4 && i <= 6) th.className = 'td-r';
      htr.appendChild(th);
    });
    thead.appendChild(htr);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    txns.forEach(t => {
      const tr    = document.createElement('tr');
      // Shares are signed: positive = buy, negative = sell. Fall back to the
      // legacy action field if a record predates the migration.
      const signedShares = Number(t.shares);
      const isBuy = t.action ? (t.action === 'buy') : (signedShares > 0);
      const absShares = Math.abs(signedShares);
      const total = (absShares * Number(t.price_per_share)).toFixed(2);

      tr.appendChild(tdText(t.date));
      tr.appendChild(tdNode(badgeEl(t.platform)));

      const tickerTd = tdText(t.stock);
      tickerTd.style.fontWeight = '500';
      tr.appendChild(tickerTd);

      const actionTd = tdText(isBuy ? 'Buy' : 'Sell');
      actionTd.style.color = isBuy ? 'var(--green)' : 'var(--red)';
      actionTd.style.fontWeight = '500';
      tr.appendChild(actionTd);

      tr.appendChild(tdText(absShares.toLocaleString('en-US', { maximumFractionDigits: 4 }), 'td-r'));
      tr.appendChild(tdText('$' + Number(t.price_per_share).toFixed(2), 'td-r'));
      tr.appendChild(tdText('$' + Number(total).toLocaleString(), 'td-r'));

      const btn = document.createElement('button');
      btn.className = 'btn btn-sm btn-danger';
      btn.textContent = 'Del';
      btn.addEventListener('click', () => deleteTransaction(t.id));
      tr.appendChild(tdNode(btn));

      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    el.appendChild(table);
  } catch (e) { console.error('renderTransactions:', e); }
}

async function deleteTransaction(id) {
  if (!confirm('Delete this transaction?')) return;
  try { await api('/transactions/' + id, { method: 'DELETE' }); showToast('Deleted'); renderTransactions(); }
  catch (e) { showToast('Failed', 3000); }
}


async function submitBalance() {
  const name     = document.getElementById('bal-name').value.trim();
  const value    = document.getElementById('bal-value').value;
  const date     = document.getElementById('bal-date').value;
  const invested = document.getElementById('bal-invested').value;
  const note     = document.getElementById('bal-note').value.trim();
  if (!name || !value || !date) {
    showToast('Fill in name, value, and date');
    return;
  }

  const btn = document.getElementById('bal-btn');
  btn.disabled = true; btn.textContent = 'Saving...';
  try {
    await api('/balances', {
      method: 'POST',
      body: JSON.stringify({
        platform: name,
        date,
        value:    Number(value),
        invested: invested ? Number(invested) : null,
        note,
      }),
    });
    document.getElementById('bal-value').value    = '';
    document.getElementById('bal-invested').value = '';
    document.getElementById('bal-note').value     = '';
    showToast('Balance saved');
    await loadAll();
    renderBalances();
  } catch (e) {
    showToast('Failed: ' + e.message, 4000);
  }
  btn.disabled = false; btn.textContent = 'Save balance';
}

async function renderBalances() {
  const el = document.getElementById('balances-list');
  if (!el) return;
  el.replaceChildren();
  try {
    const rows = await api('/balances');
    if (!rows.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'No balances yet.';
      el.appendChild(empty);
      return;
    }
    const table = document.createElement('table');
    const thead = document.createElement('thead');
    const htr   = document.createElement('tr');
    ['Date','Name','Value','Invested','Note',''].forEach((label, i) => {
      const th = document.createElement('th');
      th.textContent = label;
      if (i === 2 || i === 3) th.className = 'td-r';
      htr.appendChild(th);
    });
    thead.appendChild(htr);
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    rows.forEach(b => {
      const tr = document.createElement('tr');
      tr.appendChild(tdText(b.date));
      tr.appendChild(tdNode(badgeEl(b.platform)));
      tr.appendChild(tdText(b.value ? fmtDec(b.value) : '--', 'td-r'));
      tr.appendChild(tdText(b.invested ? fmtDec(b.invested) : '--', 'td-r'));
      const noteTd = tdText(b.note || '');
      noteTd.style.fontSize = '11px';
      noteTd.style.color = 'var(--text3)';
      tr.appendChild(noteTd);
      const btn = document.createElement('button');
      btn.className = 'btn btn-sm btn-danger';
      btn.textContent = 'Del';
      btn.addEventListener('click', () => deleteBalance(b.id));
      tr.appendChild(tdNode(btn));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    el.appendChild(table);
  } catch (e) {
    const err = document.createElement('div');
    err.className = 'empty';
    err.textContent = 'Could not load balances: ' + e.message;
    el.appendChild(err);
  }
}

async function deleteBalance(id) {
  if (!confirm('Delete this balance entry?')) return;
  try { await api('/balances/' + id, { method: 'DELETE' }); showToast('Deleted'); await loadAll(); renderBalances(); }
  catch (e) { showToast('Failed: ' + e.message, 4000); }
}

async function deleteEntry(id) {
  if (!confirm('Delete this entry?')) return;
  try { await api('/entries/' + id, { method: 'DELETE' }); showToast('Deleted'); await loadAll(); }
  catch (e) { showToast('Failed', 3000); }
}

async function exportCSV() {
  // state.entries is capped at 200 for page-load performance; fetch the full
  // history (up to the API's hard cap) only when the user explicitly exports.
  let allEntries = state.entries;
  try {
    allEntries = await api('/entries?limit=2000');
  } catch (e) {
    showToast('Export using cached entries only', 3000);
  }
  const rows = [['Date','Platform','Stock','Shares','Price','Value','Invested','Gain/Loss','Auto']];
  allEntries.forEach(e => {
    const gain = e.invested ? (e.value - e.invested).toFixed(2) : '';
    rows.push([e.date, e.platform, e.stock, e.shares || '', e.price || '', e.value, e.invested || '', gain, e.auto_logged ? 'yes' : 'no']);
  });
  const csv = rows.map(r => r.map(v => `"${v}"`).join(',')).join('\n');
  const a   = document.createElement('a');
  a.href     = 'data:text/csv,' + encodeURIComponent(csv);
  a.download = 'investments.csv';
  a.click();
}

// -- Projections -------------------------------------------
function compound(principal, monthly, rate, years) {
  const r = rate / 100 / 12;
  let v   = principal;
  const out = [{ year: 0, value: principal, contributed: principal }];
  for (let m = 1; m <= years * 12; m++) {
    v = v * (1 + r) + monthly;
    if (m % 12 === 0) out.push({ year: m / 12, value: v, contributed: principal + monthly * m });
  }
  return out;
}

function useCurrentTotal() {
  if (state.summary) {
    document.getElementById('proj-start').value = Math.round(state.summary.total_value);
    renderProjections();
  }
}

function renderProjections() {
  const start   = Number(document.getElementById('proj-start').value) || 0;
  const monthly = Number(document.getElementById('proj-monthly').value) || 0;
  const rate    = Number(document.getElementById('proj-rate').value);
  const years   = Number(document.getElementById('proj-years').value);
  const low     = Number(document.getElementById('proj-low').value);
  const high    = Number(document.getElementById('proj-high').value);

  const base  = compound(start, monthly, rate, years);
  const pess  = compound(start, monthly, low, years);
  const opt   = compound(start, monthly, high, years);
  const labels = base.map(d => d.year === 0 ? 'Now' : 'Year ' + d.year);

  if (projChart) projChart.destroy();
  projChart = new Chart(document.getElementById('proj-chart'), {
    type: 'line',
    data: { labels, datasets: [
      { label: 'Base', data: base.map(d => Math.round(d.value)), borderColor: '#4a90e2', backgroundColor: 'rgba(74,144,226,0.05)', fill: true, tension: 0, pointRadius: 2, borderWidth: 1.5 },
      { label: 'Optimistic', data: opt.map(d => Math.round(d.value)), borderColor: '#23d160', fill: false, tension: 0, pointRadius: 2, borderWidth: 1.5, borderDash: [5, 3] },
      { label: 'Pessimistic', data: pess.map(d => Math.round(d.value)), borderColor: '#ff4757', fill: false, tension: 0, pointRadius: 2, borderWidth: 1.5, borderDash: [3, 3] },
      { label: 'Contributed', data: base.map(d => Math.round(d.contributed)), borderColor: '#555560', fill: false, tension: 0, pointRadius: 0, borderWidth: 1, borderDash: [2, 4] }
    ]},
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => c.dataset.label + ': ' + fmtDec(c.parsed.y) } } },
      scales: {
        x: { ticks: { font: { size: 10 }, color: '#555560', maxTicksLimit: 10 }, grid: { display: false } },
        y: { ticks: { callback: v => '$' + Math.round(v / 1000) + 'k', font: { size: 10 }, color: '#555560', maxTicksLimit: 6 }, grid: { color: 'rgba(255,255,255,0.04)' } }
      }
    }
  });

  document.getElementById('proj-body').innerHTML = base.map((d, i) => `
    <tr>
      <td>${d.year === 0 ? 'Now' : 'Year ' + d.year}</td>
      <td class="td-r negative">${fmtDec(pess[i].value)}</td>
      <td class="td-r" style="font-weight:500">${fmtDec(d.value)}</td>
      <td class="td-r positive">${fmtDec(opt[i].value)}</td>
      <td class="td-r" style="color:var(--text3)">${fmtDec(d.contributed)}</td>
    </tr>
  `).join('');
}

// -- Stock Analyzer ----------------------------------------

function initAnalyzeTab() {
  loadRecentAnalyses();
}

async function loadRecentAnalyses() {
  try {
    const recent = await api('/recent-analyses');
    if (!recent || !recent.length) return;
    const wrap = document.getElementById('analyze-recent-wrap');
    const grid = document.getElementById('analyze-recent-grid');
    if (!wrap || !grid) return;
    wrap.style.display = 'block';
    grid.innerHTML = '';
    recent.slice(0, 8).forEach(function(a) {
      const color = analyzeScoreColor(a.overall_score || 0);
      const card = document.createElement('div');
      card.className = 'analyze-recent-card';
      const sym = a.symbol || '';
      card.onclick = function() { quickAnalyze(sym); };

      const symEl = el('div', sym, 'font-size:14px;font-weight:700;margin-bottom:2px');
      const nameEl = el('div', a.company_name || '', 'font-size:10px;color:var(--text3);margin-bottom:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis');
      const scoreEl = el('div', String(a.overall_score || '--'), 'font-size:20px;font-weight:300;color:' + color);
      const verdictEl = el('div', analyzeVerdictLabel(a.verdict), 'font-size:10px;color:var(--text3);margin-top:2px');

      card.appendChild(symEl);
      card.appendChild(nameEl);
      card.appendChild(scoreEl);
      card.appendChild(verdictEl);
      grid.appendChild(card);
    });
  } catch(e) { /* silent */ }
}

function quickAnalyze(symbol) {
  const input = document.getElementById('analyze-input');
  if (input) input.value = symbol;
  runAnalysis();
}

async function runAnalysis() {
  const input = document.getElementById('analyze-input');
  const symbol = (input ? input.value : '').trim().toUpperCase();
  if (!symbol) return;

  const area = document.getElementById('analyze-report-area');
  const prompt = document.getElementById('analyze-start-prompt');
  if (prompt) prompt.style.display = 'none';
  if (area) {
    area.innerHTML = '';
    const wrap = document.createElement('div');
    wrap.style.cssText = 'max-width:360px;margin:60px auto;padding:0 16px';

    const spinner = document.createElement('div');
    spinner.style.cssText = 'width:28px;height:28px;margin:0 auto 20px;border:2px solid var(--border2);border-top-color:var(--green);border-radius:50%;animation:spin 0.8s linear infinite';
    wrap.appendChild(spinner);

    const steps = [
      { id: 's1', label: 'Fetching financial data from FMP...' },
      { id: 's2', label: 'Calculating quality score...' },
      { id: 's3', label: 'Calculating value score...' },
      { id: 's4', label: 'Checking for red flags...' },
      { id: 's5', label: 'Generating analysis with Claude AI...' },
      { id: 's6', label: 'Building your report...' },
    ];

    steps.forEach(function(s, i) {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--border)';
      row.id = s.id;

      const dot = document.createElement('div');
      dot.style.cssText = 'width:8px;height:8px;border-radius:50%;background:var(--border2);flex-shrink:0;transition:background 0.3s';
      dot.id = s.id + '-dot';

      const label = el('div', s.label, 'font-size:12px;color:var(--text3);transition:color 0.3s');
      label.id = s.id + '-label';

      row.appendChild(dot);
      row.appendChild(label);
      wrap.appendChild(row);
    });

    const note = el('div', 'This typically takes 30-60 seconds', 'font-size:11px;color:var(--text3);margin-top:16px;text-align:center');
    wrap.appendChild(note);
    area.appendChild(wrap);

    // Animate steps
    let currentStep = 0;
    const stepTimes = [0, 3000, 5000, 7000, 10000, 45000];
    function activateStep(i) {
      // Mark previous as done
      if (i > 0) {
        const prevDot = document.getElementById(steps[i-1].id + '-dot');
        const prevLabel = document.getElementById(steps[i-1].id + '-label');
        if (prevDot) prevDot.style.background = 'var(--green)';
        if (prevLabel) prevLabel.style.color = 'var(--green)';
      }
      // Activate current
      if (i < steps.length) {
        const dot = document.getElementById(steps[i].id + '-dot');
        const label = document.getElementById(steps[i].id + '-label');
        if (dot) dot.style.background = 'var(--yellow)';
        if (label) label.style.color = 'var(--text)';
        if (i + 1 < steps.length) {
          setTimeout(function() { activateStep(i + 1); }, stepTimes[i + 1] - stepTimes[i]);
        }
      }
    }
    activateStep(0);
  }

  try {
    const result = await api('/analyze/' + symbol);
    renderAnalysisReport(result);
    loadRecentAnalyses();
  } catch(e) {
    if (area) {
      area.innerHTML = '';
      const card = document.createElement('div');
      card.className = 'card';
      card.style.cssText = 'text-align:center;padding:32px';
      card.appendChild(el('div', e.message || 'Analysis failed', 'color:var(--red);font-size:13px;margin-bottom:8px'));
      card.appendChild(el('div', 'Check the ticker symbol and try again', 'font-size:11px;color:var(--text3)'));
      area.appendChild(card);
    }
  }
}

// -- Utilities ---------------------------------------------

function el(tag, text, css) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  if (css) e.style.cssText = css;
  return e;
}

function analyzeScoreColor(score) {
  if (score >= 70) return 'var(--green)';
  if (score >= 45) return '#ffb347';
  return 'var(--red)';
}

function analyzeVerdictLabel(verdict) {
  const map = {
    worth_investigating: 'Worth investigating',
    mixed: 'Mixed signals',
    significant_concerns: 'Significant concerns'
  };
  return map[verdict] || 'Mixed signals';
}

function analyzeConfBadge(conf) {
  const el = document.createElement('span');
  el.className = 'analyze-conf';
  if (conf === 'high') { el.classList.add('conf-high'); el.textContent = 'High confidence'; }
  else if (conf === 'medium') { el.classList.add('conf-medium'); el.textContent = 'Medium confidence'; }
  else { el.classList.add('conf-low'); el.textContent = 'Low confidence'; }
  return el;
}

function sectionLabel(text) {
  const d = document.createElement('div');
  d.className = 'analyze-section-label';
  d.textContent = text;
  return d;
}

function card(children, extraStyle) {
  const d = document.createElement('div');
  d.className = 'card';
  if (extraStyle) d.style.cssText = extraStyle;
  if (Array.isArray(children)) children.forEach(function(c) { if (c) d.appendChild(c); });
  else if (children) d.appendChild(children);
  return d;
}

// -- Main Report Renderer ----------------------------------

function renderAnalysisReport(result) {
  const area = document.getElementById('analyze-report-area');
  if (!area) return;
  area.innerHTML = '';

  const { report, cached, age_hours } = result;
  const { scores, narrative } = report;
  const profile = scores.profile;
  const q = scores.quality;
  const v = scores.value;
  const trap = scores.value_trap;
  const color = analyzeScoreColor(scores.overall_score);

  // -- Cached notice -------------------------------------
  if (cached) {
    const notice = document.createElement('div');
    notice.style.cssText = 'display:flex;align-items:center;justify-content:space-between;font-size:11px;color:var(--text3);background:var(--bg2);border-radius:var(--radius);padding:8px 12px;margin-bottom:12px';
    notice.appendChild(el('span', 'Cached analysis (' + Math.round(age_hours) + 'h ago)'));
    const refreshBtn = document.createElement('button');
    refreshBtn.className = 'btn btn-sm';
    refreshBtn.textContent = 'Refresh';
    refreshBtn.onclick = function() { refreshAnalysis(profile.symbol); };
    notice.appendChild(refreshBtn);
    area.appendChild(notice);
  }

  // -- Header --------------------------------------------
  const header = document.createElement('div');
  header.style.cssText = 'display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:10px';

  const companyInfo = document.createElement('div');
  companyInfo.appendChild(el('div', profile.name, 'font-size:20px;font-weight:700;letter-spacing:-0.5px'));
  companyInfo.appendChild(el('div', profile.symbol + ' . ' + (profile.sector || ''), 'font-size:12px;color:var(--text2);margin-top:2px'));
  const verdictColor = scores.verdict === 'worth_investigating' ? 'var(--green)' :
                       scores.verdict === 'significant_concerns' ? 'var(--red)' : '#ffb347';
  companyInfo.appendChild(el('div', analyzeVerdictLabel(scores.verdict), 'margin-top:8px;font-size:12px;font-weight:600;color:' + verdictColor));

  const scoreWrap = document.createElement('div');
  scoreWrap.style.textAlign = 'right';
  scoreWrap.appendChild(el('div', String(scores.overall_score), 'font-size:48px;font-weight:300;letter-spacing:-2px;line-height:1;color:' + color));
  scoreWrap.appendChild(el('div', 'Overall Score', 'font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:0.08em'));

  header.appendChild(companyInfo);
  header.appendChild(scoreWrap);
  area.appendChild(header);

  // -- Business -----------------------------------------
  area.appendChild(sectionLabel('The Business'));
  const bizCard = document.createElement('div');
  bizCard.className = 'card';
  bizCard.appendChild(el('p', narrative.business || '', 'font-size:13px;line-height:1.75;color:var(--text2)'));
  area.appendChild(bizCard);

  // -- Value Trap ----------------------------------------
  area.appendChild(sectionLabel('Value Trap Check'));
  const trapCls = trap.risk_level === 'high' ? 'trap-high' : trap.risk_level === 'medium' ? 'trap-medium' : 'trap-low';
  const trapColor = trap.risk_level === 'high' ? 'var(--red)' : trap.risk_level === 'medium' ? '#ffb347' : 'var(--green)';
  const trapCard = document.createElement('div');
  trapCard.className = 'analyze-trap-card ' + trapCls;
  trapCard.appendChild(el('div', trap.risk_label, 'font-size:13px;font-weight:600;color:' + trapColor + ';margin-bottom:6px'));
  trapCard.appendChild(el('div', trap.explanation, 'font-size:12px;color:var(--text2);line-height:1.5;margin-bottom:10px'));
  trap.signals.forEach(function(s) {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:8px;font-size:12px;margin-bottom:4px';
    const icon = document.createElement('span');
    icon.textContent = s.status === 'triggered' ? 'x' : s.status === 'clear' ? 'v' : '?';
    icon.style.color = s.status === 'triggered' ? 'var(--red)' : s.status === 'clear' ? 'var(--green)' : 'var(--text3)';
    row.appendChild(icon);
    row.appendChild(el('span', s.signal));
    row.appendChild(el('span', '-- ' + s.detail, 'color:var(--text3);font-size:11px'));
    trapCard.appendChild(row);
  });
  area.appendChild(trapCard);

  // -- Quality Score -------------------------------------
  area.appendChild(sectionLabel('Quality Score'));
  const qCard = document.createElement('div');
  qCard.className = 'card';
  appendScoreBar(qCard, 'Quality', q.score, 100);
  if (narrative.quality_narrative) {
    qCard.appendChild(el('p', narrative.quality_narrative, 'font-size:12px;color:var(--text2);line-height:1.6;margin-bottom:14px'));
  }
  renderQualitySignals(qCard, q.components);
  area.appendChild(qCard);

  // -- Value Score ---------------------------------------
  area.appendChild(sectionLabel('Value Score'));
  const vCard = document.createElement('div');
  vCard.className = 'card';
  appendScoreBar(vCard, 'Value', v.score, 100);
  if (narrative.value_narrative) {
    vCard.appendChild(el('p', narrative.value_narrative, 'font-size:12px;color:var(--text2);line-height:1.6;margin-bottom:14px'));
  }
  renderValueSignals(vCard, v.components);
  area.appendChild(vCard);

  // -- Portfolio Fit -------------------------------------
  if (narrative.portfolio_fit) {
    area.appendChild(sectionLabel('Portfolio Fit'));
    const pfCard = document.createElement('div');
    pfCard.className = 'card';
    pfCard.style.cssText = 'background:rgba(91,141,239,0.06);border-color:rgba(91,141,239,0.15)';
    pfCard.appendChild(el('div', 'Based on your portfolio', 'font-size:10px;color:var(--blue);font-weight:600;text-transform:uppercase;letter-spacing:0.08em;margin-bottom:8px'));
    pfCard.appendChild(el('p', narrative.portfolio_fit, 'font-size:13px;color:var(--text2);line-height:1.7'));
    area.appendChild(pfCard);
  }

  // -- Red Flags -----------------------------------------
  if (scores.red_flags && scores.red_flags.length) {
    area.appendChild(sectionLabel('Red Flags'));
    scores.red_flags.forEach(function(f) {
      const fc = document.createElement('div');
      fc.className = 'analyze-flag-card';
      fc.appendChild(el('div', '! ' + f.title, 'font-size:12px;font-weight:600;color:var(--red);margin-bottom:5px'));
      fc.appendChild(el('div', f.detail, 'font-size:12px;color:var(--text2);line-height:1.5'));
      fc.appendChild(el('div', f.why_it_matters, 'font-size:11px;color:var(--text3);margin-top:6px;line-height:1.5'));
      if (f.historical_example) {
        fc.appendChild(el('div', f.historical_example, 'font-size:11px;color:var(--text3);margin-top:5px;font-style:italic'));
      }
      area.appendChild(fc);
    });
  }

  // -- Insider -------------------------------------------
  const ins = scores.insider;
  const insColor = (ins.signal === 'cluster_buying' || ins.signal === 'some_buying') ? 'var(--green)' :
                   (ins.signal === 'cluster_selling' || ins.signal === 'some_selling') ? 'var(--red)' : 'var(--text3)';
  const insLabels = { cluster_buying: 'Cluster Buying', cluster_selling: 'Cluster Selling', some_buying: 'Some Buying', some_selling: 'Some Selling', neutral: 'Neutral' };
  area.appendChild(sectionLabel('Signals We Weight Lightly'));
  const insCard = document.createElement('div');
  insCard.className = 'card';
  const insHeader = document.createElement('div');
  insHeader.style.marginBottom = '10px';
  insHeader.appendChild(el('span', 'Insider Activity: ' + (insLabels[ins.signal] || 'Neutral'), 'font-size:12px;font-weight:600;color:' + insColor));
  insHeader.appendChild(analyzeConfBadge(ins.confidence));
  insCard.appendChild(insHeader);
  insCard.appendChild(el('p', ins.detail, 'font-size:12px;color:var(--text2);line-height:1.5;margin-bottom:8px'));
  insCard.appendChild(el('p', ins.important_caveat, 'font-size:11px;color:var(--text3);line-height:1.5;font-style:italic'));
  area.appendChild(insCard);

  // -- Verdict -------------------------------------------
  area.appendChild(sectionLabel('The Verdict'));
  const verdictCard = document.createElement('div');
  verdictCard.className = 'card';
  const verdictEl = document.createElement('div');
  verdictEl.className = 'analyze-verdict-text';
  verdictEl.innerHTML = formatVerdict(narrative.verdict || '');
  verdictCard.appendChild(verdictEl);
  area.appendChild(verdictCard);

  // -- Learning ------------------------------------------
  area.appendChild(sectionLabel('What You Learned'));
  const learnCard = document.createElement('div');
  learnCard.className = 'card';
  renderLearning(learnCard, narrative.learning || '');
  area.appendChild(learnCard);

  // -- Ask a Question ------------------------------------
  area.appendChild(sectionLabel('Ask a Question'));
  const askCard = document.createElement('div');
  askCard.className = 'card';
  const askInput = document.createElement('input');
  askInput.className = 'analyze-ask-input';
  askInput.placeholder = 'e.g. What would change your verdict on this?';
  const askBtn = document.createElement('button');
  askBtn.className = 'btn btn-sm';
  askBtn.textContent = 'Ask';
  const askAnswer = document.createElement('div');
  askAnswer.style.cssText = 'display:none;margin-top:12px;font-size:13px;color:var(--text2);line-height:1.6;background:var(--bg3);border-radius:var(--radius);padding:12px';

  const sym = profile.symbol;
  askBtn.onclick = function() { submitAnalyzeQuestion(sym, askInput, askAnswer); };
  askInput.onkeydown = function(e) { if (e.key === 'Enter') submitAnalyzeQuestion(sym, askInput, askAnswer); };

  askCard.appendChild(askInput);
  askCard.appendChild(askBtn);
  askCard.appendChild(askAnswer);
  area.appendChild(askCard);

  // -- Disclaimer ----------------------------------------
  const disc = el('div', '', 'font-size:11px;color:var(--text3);text-align:center;padding:16px 0;line-height:1.6');
  disc.innerHTML = 'Educational analysis only &mdash; not financial advice.<br>Value investing requires patience measured in years, not months.';
  area.appendChild(disc);
}

// -- Score Bar ---------------------------------------------

function appendScoreBar(parent, label, score, max) {
  const color = analyzeScoreColor(score * (100 / max));
  const wrap = document.createElement('div');
  wrap.style.cssText = 'display:flex;align-items:center;gap:12px;margin-bottom:14px';
  wrap.appendChild(el('div', label, 'flex:1;font-size:14px;font-weight:600'));
  const barWrap = document.createElement('div');
  barWrap.style.cssText = 'flex:2;height:4px;background:var(--bg3);border-radius:2px';
  const bar = document.createElement('div');
  bar.style.cssText = 'height:4px;border-radius:2px;width:' + Math.min(100, score) + '%;background:' + color;
  barWrap.appendChild(bar);
  const scoreEl = document.createElement('div');
  scoreEl.style.cssText = 'font-size:18px;font-weight:300;color:' + color + ';width:50px;text-align:right';
  const num = document.createElement('span');
  num.textContent = String(score);
  const denom = el('span', '/' + max, 'font-size:11px;color:var(--text3)');
  scoreEl.appendChild(num);
  scoreEl.appendChild(denom);
  wrap.appendChild(barWrap);
  wrap.appendChild(scoreEl);
  parent.appendChild(wrap);
}

// -- Signal Rows -------------------------------------------

function makeSignalRow(label, sub, score, max, conf, details, analogy, caveat, id) {
  const color = analyzeScoreColor(score * (100 / max));
  const wrap = document.createElement('div');

  const row = document.createElement('div');
  row.className = 'analyze-signal-row';
  row.onclick = function() { toggleAnalyzeSignal(id); };

  const dot = document.createElement('div');
  dot.className = 'analyze-signal-dot';
  dot.style.background = color;

  const info = document.createElement('div');
  info.style.flex = '1';
  info.appendChild(el('div', label, 'font-size:13px'));
  info.appendChild(el('div', sub, 'font-size:11px;color:var(--text2)'));

  const scoreEl = el('div', score + '/' + max, 'font-size:13px;font-weight:500;color:' + color);
  const chev = el('div', 'v', 'color:var(--text3);font-size:10px;margin-left:4px');

  row.appendChild(dot);
  row.appendChild(info);
  row.appendChild(scoreEl);
  row.appendChild(analyzeConfBadge(conf));
  row.appendChild(chev);
  wrap.appendChild(row);

  const detail = document.createElement('div');
  detail.className = 'analyze-signal-detail';
  detail.id = id;

  const grid = document.createElement('div');
  grid.className = 'analyze-detail-grid';
  details.forEach(function(d) {
    grid.appendChild(el('div', d[0], null));
    grid.children[grid.children.length - 1].className = 'analyze-detail-key';
    const vEl = el('div', d[1], null);
    vEl.className = 'analyze-detail-val';
    grid.appendChild(vEl);
  });
  detail.appendChild(grid);

  if (caveat) {
    detail.appendChild(el('div', caveat, 'margin-top:8px;padding:8px 10px;background:rgba(255,165,0,0.08);border-radius:4px;font-size:11px;color:#ffb347'));
  }

  detail.appendChild(el('div', analogy, 'padding:9px 11px;background:var(--bg2);border-left:2px solid var(--blue);border-radius:4px;font-size:11px;color:var(--text2);line-height:1.55;margin-top:8px'));
  wrap.appendChild(detail);
  return wrap;
}

function renderQualitySignals(parent, components) {
  const defs = [
    { key: 'roic', label: 'Business Efficiency', sub: 'Return on Invested Capital (ROIC)',
      analogy: 'Think of ROIC like how efficiently a restaurant turns its tables. Above 15% consistently means the business compounds wealth for its owners.',
      details: function(d) { return [['Avg ROIC', d.avg_roic !== undefined ? d.avg_roic + '%' : '--'], ['Years above 15%', d.years_above_15pct + '/' + d.total_years], ['Trend', d.trend || '--']]; } },
    { key: 'gross_margin', label: 'Competitive Advantage', sub: 'Gross margin stability (moat)',
      analogy: 'Stable or expanding margins mean customers keep paying full price. A sign competitors cannot easily take their business.',
      details: function(d) { return [['Avg gross margin', d.avg_margin !== undefined ? d.avg_margin + '%' : '--'], ['Trend', d.trend || '--'], ['Value trap flag', d.value_trap_flag ? 'Yes' : 'No']]; } },
    { key: 'debt_safety', label: 'Financial Safety', sub: 'Debt and interest coverage',
      analogy: 'Low debt and strong interest coverage means the company can survive a bad year without going bankrupt.',
      details: function(d) { return [['Debt-to-equity', d.debt_to_equity !== undefined ? d.debt_to_equity + 'x' : '--'], ['Interest coverage', d.interest_coverage ? d.interest_coverage + 'x' : 'No debt'], ['Current ratio', d.current_ratio !== undefined ? d.current_ratio + 'x' : '--']]; } },
    { key: 'owner_earnings', label: 'Real Cash Generation', sub: 'Owner earnings (Buffett metric)',
      analogy: 'Owner earnings strip accounting tricks away to show how much cash you could actually take home as an owner of this business.',
      details: function(d) { return [['Avg owner earnings', d.avg_owner_earnings !== undefined ? '$' + d.avg_owner_earnings + 'B' : '--'], ['Trend', d.trend || '--'], ['Positive years', d.positive_years + '/' + d.total_years]]; } },
    { key: 'capital_allocation', label: 'Management Quality', sub: 'Buybacks and shareholder treatment',
      analogy: 'Great managers buy back stock when it is cheap and avoid diluting shareholders with excessive stock compensation.',
      details: function(d) { return [['Share count change', d.share_count_change_pct !== undefined ? d.share_count_change_pct + '%' : '--'], ['Shares reduced?', d.shares_reduced === true ? 'Yes' : d.shares_reduced === false ? 'No' : 'Unknown'], ['Avg SBC % revenue', d.avg_sbc_pct_of_revenue !== undefined ? d.avg_sbc_pct_of_revenue + '%' : '--']]; } }
  ];

  defs.forEach(function(s, i) {
    const d = components[s.key] || {};
    parent.appendChild(makeSignalRow(s.label, s.sub, d.score || 0, d.max || 20, d.confidence, s.details(d), s.analogy, null, 'aqsig-' + i));
  });
}

function renderValueSignals(parent, components) {
  const ne = components.normalized_earnings || {};
  const fcf = components.fcf_yield || {};
  const dcf = components.dcf || {};

  const defs = [
    { id: 'avne', label: 'Price vs History', sub: 'Normalized P/E (10-yr avg earnings)',
      score: ne.score || 0, max: ne.max || 30, conf: ne.confidence, caveat: ne.caveat,
      analogy: 'Averaging 10 years of earnings smooths out good and bad years. Like judging a farmer by average harvests, not just one season.',
      details: [['Normalized EPS', ne.normalized_eps !== undefined ? '$' + ne.normalized_eps : '--'], ['Normalized P/E', ne.current_pe_normalized !== undefined ? ne.current_pe_normalized + 'x' : '--'], ['Current price', ne.current_price !== undefined ? '$' + ne.current_price : '--'], ['Assessment', ne.valuation || '--']] },
    { id: 'avfcf', label: 'Cash Return (FCF Yield)', sub: 'Free cash flow vs market cap',
      score: fcf.score || 0, max: fcf.max || 30, conf: fcf.confidence, caveat: null,
      analogy: 'FCF yield tells you how much cash you are buying per dollar invested. A 5% yield beats most bonds with ownership upside.',
      details: [['FCF yield', fcf.fcf_yield_pct !== undefined ? fcf.fcf_yield_pct + '%' : '--'], ['Avg FCF (5yr)', fcf.avg_fcf_billions !== undefined ? '$' + fcf.avg_fcf_billions + 'B' : '--'], ['FCF trend', fcf.fcf_trend || '--']] },
    { id: 'avdcf', label: 'Intrinsic Value (DCF)', sub: 'Discounted cash flow estimate',
      score: dcf.score || 0, max: dcf.max || 40, conf: dcf.confidence, caveat: dcf.caveat,
      analogy: 'A DCF estimates what all future cash flows are worth today. Treat it as a rough compass, not a GPS. Small assumption changes move the number significantly.',
      details: [['DCF estimate', dcf.dcf_estimate !== undefined ? '$' + dcf.dcf_estimate : '--'], ['Range', (dcf.dcf_range_low && dcf.dcf_range_high) ? '$' + dcf.dcf_range_low + ' - $' + dcf.dcf_range_high : '--'], ['Current price', dcf.current_price !== undefined ? '$' + dcf.current_price : '--'], ['Discount/premium', dcf.discount_pct !== undefined ? dcf.discount_pct + '%' : '--']] }
  ];

  defs.forEach(function(s) {
    parent.appendChild(makeSignalRow(s.label, s.sub, s.score, s.max, s.conf, s.details, s.analogy, s.caveat, s.id));
  });
}

function toggleAnalyzeSignal(id) {
  const d = document.getElementById(id);
  if (d) d.classList.toggle('open');
}

// -- Verdict & Learning ------------------------------------

function formatVerdict(text) {
  if (!text) return '';
  const escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  return escaped.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
}

function renderLearning(parent, text) {
  const sections = text.split('CONCEPT:').filter(function(s) { return s.trim(); });
  if (!sections.length) {
    parent.appendChild(el('p', text, 'font-size:12px;color:var(--text2)'));
    return;
  }
  sections.forEach(function(s) {
    const lines = s.trim().split('\n').filter(function(l) { return l.trim(); });
    const concept = lines[0] ? lines[0].trim() : '';
    const whatLine = lines.find(function(l) { return l.startsWith('WHAT:'); });
    const whyLine = lines.find(function(l) { return l.startsWith('WHY HERE:'); });
    const what = whatLine ? whatLine.replace('WHAT:', '').trim() : '';
    const why = whyLine ? whyLine.replace('WHY HERE:', '').trim() : '';

    const item = document.createElement('div');
    item.className = 'analyze-learn-item';
    item.appendChild(el('div', concept, null));
    item.children[0].className = 'analyze-learn-concept';
    item.appendChild(el('div', what, null));
    item.children[1].className = 'analyze-learn-text';
    if (why) {
      const whyEl = el('div', why, null);
      whyEl.className = 'analyze-learn-text';
      whyEl.style.cssText = 'color:var(--text3);margin-top:3px;font-size:12px;color:var(--text2);line-height:1.55';
      item.appendChild(whyEl);
    }
    parent.appendChild(item);
  });
}

// -- Ask & Refresh -----------------------------------------

async function submitAnalyzeQuestion(symbol, inputEl, answerEl) {
  const question = inputEl ? inputEl.value.trim() : '';
  if (!question) return;
  if (answerEl) { answerEl.style.display = 'block'; answerEl.textContent = 'Thinking...'; }
  try {
    const res = await api('/analyze/' + symbol + '/question', {
      method: 'POST',
      body: JSON.stringify({ question: question })
    });
    if (answerEl) answerEl.textContent = res.answer;
  } catch(e) {
    if (answerEl) answerEl.textContent = 'Could not generate answer. Try again.';
  }
}

async function refreshAnalysis(symbol) {
  const area = document.getElementById('analyze-report-area');
  if (area) {
    area.innerHTML = '';
    area.appendChild(el('div', 'Refreshing analysis...', 'text-align:center;padding:40px;color:var(--text3)'));
  }
  try {
    const result = await api('/analyze/' + symbol + '?refresh=true');
    renderAnalysisReport(result);
  } catch(e) {
    if (area) {
      area.innerHTML = '';
      area.appendChild(el('div', e.message || 'Refresh failed', 'text-align:center;padding:32px;color:var(--red)'));
    }
  }
}
