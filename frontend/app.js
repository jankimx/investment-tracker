// -- Config ------------------------------------------------
const API = 'https://web-production-780915.up.railway.app';

// -- State -------------------------------------------------
let state = {
  entries: [], platforms: [], stocks: [],
  summary: null, holdings: [], transactions: [],
  posGrouping: 'none', posSortCol: 'value', posSortDir: 1
};

let growthChart = null;
let projChart   = null;

// -- Init --------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('entry-date').value = today();
  document.getElementById('txn-date').value   = today();
  if (sessionStorage.getItem('auth')) showApp();
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
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts
  });
  const data = await res.json();
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
    if (data.ok) {
      sessionStorage.setItem('auth', '1');
      showApp();
    } else {
      err.textContent = 'Wrong password.';
    }
  } catch {
    err.textContent = 'Cannot reach server.';
  }
}

document.getElementById('login-pw').addEventListener('keydown', e => {
  if (e.key === 'Enter') doLogin();
});

function logout() {
  sessionStorage.removeItem('auth');
  location.reload();
}

function showApp() {
  document.getElementById('login-screen').style.display = 'none';
  document.getElementById('main-app').style.display     = 'block';
  loadAll();
}

// -- Load --------------------------------------------------
async function loadAll() {
  try {
    const [entries, platforms, stocks, summary, holdings] = await Promise.all([
      api('/entries'), api('/platforms'), api('/stocks'),
      api('/summary'), api('/holdings')
    ]);
    state.entries   = entries;
    state.platforms = platforms;
    state.stocks    = stocks;
    state.summary   = summary;
    state.holdings  = holdings;
    updateDataLists();
    renderDashboard();
    renderRecent();
    renderHoldings();
    fetchRefreshStatus();
    updateRefreshInfo();
  } catch (e) {
    console.error('loadAll failed:', e);
    showToast('Failed to load data', 4000);
  }
}

function updateDataLists() {
  document.getElementById('dl-platform').innerHTML =
    state.platforms.map(p => `<option value="${p}">`).join('');
  document.getElementById('dl-stock').innerHTML =
    state.stocks.map(s => `<option value="${s}">`).join('');
  document.getElementById('dl-h-platform').innerHTML =
    state.platforms.map(p => `<option value="${p}">`).join('');
}

// -- Tabs --------------------------------------------------
function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelector(`[data-tab="${tab}"]`).classList.add('active');
  document.getElementById(`tab-${tab}`).classList.add('active');
  if (tab === 'history')     renderHistory();
  if (tab === 'projections') renderProjections();
  if (tab === 'holdings')    renderHoldings(), renderTransactions();
}

// -- Refresh status ----------------------------------------
async function fetchRefreshStatus() {
  try {
    const s  = await api('/refresh-status');
    const el = document.getElementById('refresh-status');
    if (!el) return;
    if (s.refreshed_today) {
      const dt   = new Date(s.last_refresh + 'Z');
      const time = dt.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
      el.innerHTML = `<span class="refresh-status-ok">Refreshed today at ${time}</span>`;
    } else {
      el.innerHTML = `<span class="refresh-status-pending">Not yet refreshed today</span>`;
    }
  } catch { /* silent */ }
}

function updateRefreshInfo() {
  const el = document.getElementById('refresh-info');
  if (!el) return;
  const count = state.holdings.length;
  if (!count) { el.textContent = ''; return; }
  const times = Math.floor(25 / count);
  el.innerHTML = `Each refresh uses <strong style="color:var(--text)">${count}</strong> of 25 daily API calls &mdash; ~${times}x per day`;
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

  setText('m-count', state.entries.length);

  // Platform filter
  const pfSel = document.getElementById('dash-platform');
  const curPF = pfSel.value;
  pfSel.innerHTML = '<option value="all">All platforms</option>' +
    state.platforms.map(p => `<option value="${p}"${p === curPF ? ' selected' : ''}>${p}</option>`).join('');

  const filtered = pfSel.value === 'all' ? state.entries
    : state.entries.filter(e => e.platform === pfSel.value);
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
function renderChart(data, view) {
  const allDates = [...new Set(data.map(e => e.date))].sort();
  if (!allDates.length) {
    if (growthChart) { growthChart.destroy(); growthChart = null; }
    document.getElementById('chart-legend').innerHTML = '';
    return;
  }

  const latestAt = (subset, d) => {
    const combos = {};
    subset.filter(e => e.date <= d).forEach(e => {
      const k = e.platform + '||' + e.stock;
      if (!combos[k] || e.date > combos[k].date) combos[k] = e;
    });
    return Object.values(combos).reduce((s, e) => s + e.value, 0);
  };

  let datasets = [];
  const legend = document.getElementById('chart-legend');

  if (view === 'total') {
    datasets = [{
      label: 'Total', tension: 0, pointRadius: 3, borderWidth: 1.5,
      data: allDates.map(d => Math.round(latestAt(data, d))),
      borderColor: '#4a90e2', backgroundColor: 'rgba(74,144,226,0.06)', fill: true
    }];
    legend.innerHTML = `<span class="legend-item"><span class="legend-dot" style="background:#4a90e2"></span>Total value</span>`;
  } else {
    const keys = view === 'platform'
      ? [...new Set(data.map(e => e.platform))]
      : [...new Set(data.map(e => e.stock))];
    datasets = keys.map(k => {
      const color  = colorFor(k);
      const subset = data.filter(e => (view === 'platform' ? e.platform : e.stock) === k);
      return {
        label: k, tension: 0, pointRadius: 2, borderWidth: 1.5, fill: false,
        data: allDates.map(d => Math.round(latestAt(subset, d))),
        borderColor: color
      };
    });
    legend.innerHTML = datasets.map(d =>
      `<span class="legend-item"><span class="legend-dot" style="background:${d.borderColor}"></span>${d.label}</span>`
    ).join('');
  }

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
  state.entries.forEach(e => {
    const k = e.platform + '||' + e.stock;
    if (k === platform + '||' + stock && !seen[e.date]) {
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
  const latest = {};
  const prev   = {};
  const allDates = [...new Set(state.entries.map(e => e.date))].sort().reverse();
  const todayDate = allDates[0];
  const prevDate  = allDates[1];

  state.entries.forEach(e => {
    const k = e.platform + '||' + e.stock;
    if (e.date === todayDate && !latest[k]) latest[k] = e;
    if (prevDate && e.date === prevDate && !prev[k]) prev[k] = e;
  });

  const totalValue = state.summary?.total_value || 1;

  return Object.values(latest).map(e => {
    const p = prev[e.platform + '||' + e.stock];
    const dailyGain    = e.daily_gain !== undefined && e.daily_gain !== null ? e.daily_gain
                       : (p ? e.value - p.value : null);
    const dailyGainPct = e.daily_gain_pct !== undefined && e.daily_gain_pct !== null ? e.daily_gain_pct
                       : (p && p.value > 0 ? (dailyGain / p.value * 100) : null);
    return {
      platform:      e.platform,
      stock:         e.stock,
      value:         e.value,
      invested:      e.invested || 0,
      shares:        e.shares || null,
      price:         e.price || null,
      totalGain:     e.invested ? e.value - e.invested : null,
      totalGainPct:  (e.invested && e.invested > 0) ? ((e.value - e.invested) / e.invested * 100) : null,
      dailyGain,
      dailyGainPct,
      pctOfPortfolio: (e.value / totalValue * 100)
    };
  });
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
  if (!container || !state.entries.length) return;

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
  if (!state.holdings.length) { el.innerHTML = '<div class="empty">No holdings yet.</div>'; return; }

  const table = document.createElement('table');
  const thead = document.createElement('thead');
  thead.innerHTML = '<tr><th>Platform</th><th>Ticker</th><th class="td-r">Shares</th><th class="td-r">Avg cost/share</th><th class="td-r">Cost basis</th><th></th></tr>';
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  state.holdings.forEach(h => {
    const tr = document.createElement('tr');
    const badge = `<span class="badge ${badgeClass(h.platform)}">${h.platform}</span>`;
    const cb    = h.cost_basis ? fmtDec(h.cost_basis) : '--';
    const total = h.cost_basis ? fmtDec(h.cost_basis * h.shares) : '--';
    tr.innerHTML = `
      <td>${badge}</td>
      <td style="font-weight:500">${h.stock}</td>
      <td class="td-r">${Number(h.shares).toLocaleString('en-US', { maximumFractionDigits: 4 })}</td>
      <td class="td-r">${cb}</td>
      <td class="td-r">${total}</td>
      <td><button class="btn btn-sm btn-danger" data-id="${h.id}">Remove</button></td>
    `;
    tr.querySelector('button').addEventListener('click', () => deleteHolding(h.id));
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  el.innerHTML = '';
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
    if (!txns.length) { el.innerHTML = '<div class="empty">No transactions yet.</div>'; return; }

    const table = document.createElement('table');
    const thead = document.createElement('thead');
    thead.innerHTML = '<tr><th>Date</th><th>Platform</th><th>Ticker</th><th>Action</th><th class="td-r">Shares</th><th class="td-r">Price/share</th><th class="td-r">Total</th><th></th></tr>';
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    txns.forEach(t => {
      const tr    = document.createElement('tr');
      const total = (t.shares * t.price_per_share).toFixed(2);
      const color = t.action === 'buy' ? 'var(--green)' : 'var(--red)';
      const badge = `<span class="badge ${badgeClass(t.platform)}">${t.platform}</span>`;
      tr.innerHTML = `
        <td>${t.date}</td><td>${badge}</td>
        <td style="font-weight:500">${t.stock}</td>
        <td style="color:${color};font-weight:500;text-transform:capitalize">${t.action}</td>
        <td class="td-r">${Number(t.shares).toLocaleString('en-US', { maximumFractionDigits: 4 })}</td>
        <td class="td-r">$${Number(t.price_per_share).toFixed(2)}</td>
        <td class="td-r">$${Number(total).toLocaleString()}</td>
        <td><button class="btn btn-sm btn-danger" data-id="${t.id}">Del</button></td>
      `;
      tr.querySelector('button').addEventListener('click', () => deleteTransaction(t.id));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    el.innerHTML = '';
    el.appendChild(table);
  } catch (e) { console.error('renderTransactions:', e); }
}

async function deleteTransaction(id) {
  if (!confirm('Delete this transaction?')) return;
  try { await api('/transactions/' + id, { method: 'DELETE' }); showToast('Deleted'); renderTransactions(); }
  catch (e) { showToast('Failed', 3000); }
}

// -- Manual entry ------------------------------------------
function renderRecent() {
  const recent = [...state.entries].sort((a, b) => a.date < b.date ? 1 : -1).slice(0, 10);
  const el     = document.getElementById('recent-list');
  if (!el) return;
  if (!recent.length) { el.innerHTML = '<div class="empty">No entries yet.</div>'; return; }

  const table = document.createElement('table');
  const thead = document.createElement('thead');
  thead.innerHTML = '<tr><th>Date</th><th>Platform</th><th>Stock</th><th class="td-r">Value</th><th>Source</th><th></th></tr>';
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  recent.forEach(e => {
    const tr    = document.createElement('tr');
    const badge = `<span class="badge ${badgeClass(e.platform)}">${e.platform}</span>`;
    tr.innerHTML = `
      <td>${e.date}</td><td>${badge}</td><td>${e.stock}</td>
      <td class="td-r">${fmtDec(e.value)}</td>
      <td style="font-size:11px;color:var(--text3)">${e.auto_logged ? 'auto' : 'manual'}</td>
      <td><button class="btn btn-sm btn-danger" data-id="${e.id}">Del</button></td>
    `;
    tr.querySelector('button').addEventListener('click', () => deleteEntry(e.id));
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  el.innerHTML = '';
  el.appendChild(table);
}

async function submitEntry() {
  const date     = document.getElementById('entry-date').value;
  const platform = document.getElementById('entry-platform').value.trim();
  const stock    = document.getElementById('entry-stock').value.trim();
  const value    = document.getElementById('entry-value').value;
  const invested = document.getElementById('entry-invested').value;
  const notes    = document.getElementById('entry-notes').value.trim();
  if (!date || !platform || !stock || !value) { showToast('Fill in date, platform, stock, value'); return; }

  const btn = document.getElementById('entry-btn');
  btn.disabled = true; btn.textContent = 'Saving...';
  try {
    await api('/entries', {
      method: 'POST',
      body: JSON.stringify({ date, platform, stock, value: Number(value), invested: invested ? Number(invested) : 0, notes })
    });
    document.getElementById('entry-value').value    = '';
    document.getElementById('entry-invested').value = '';
    document.getElementById('entry-notes').value    = '';
    showToast('Entry saved');
    await loadAll();
  } catch (e) { showToast('Failed: ' + e.message, 4000); }
  btn.disabled = false; btn.textContent = 'Save entry';
}

async function deleteEntry(id) {
  if (!confirm('Delete this entry?')) return;
  try { await api('/entries/' + id, { method: 'DELETE' }); showToast('Deleted'); await loadAll(); }
  catch (e) { showToast('Failed', 3000); }
}

// -- History -----------------------------------------------
function renderHistory() {
  const hpf  = document.getElementById('hist-platform');
  const hst  = document.getElementById('hist-stock');
  const curP = hpf.value, curS = hst.value;

  hpf.innerHTML = '<option value="all">All</option>' +
    state.platforms.map(p => `<option value="${p}"${p === curP ? ' selected' : ''}>${p}</option>`).join('');
  hst.innerHTML = '<option value="all">All</option>' +
    state.stocks.map(s => `<option value="${s}"${s === curS ? ' selected' : ''}>${s}</option>`).join('');

  let filtered = [...state.entries].sort((a, b) => a.date < b.date ? 1 : -1);
  if (hpf.value !== 'all') filtered = filtered.filter(e => e.platform === hpf.value);
  if (hst.value !== 'all') filtered = filtered.filter(e => e.stock    === hst.value);

  const tbody = document.getElementById('hist-body');
  if (!filtered.length) { tbody.innerHTML = '<tr><td colspan="8" class="empty">No entries match.</td></tr>'; return; }

  tbody.innerHTML = '';
  filtered.forEach(e => {
    const tr    = document.createElement('tr');
    const badge = `<span class="badge ${badgeClass(e.platform)}">${e.platform}</span>`;
    const gain  = e.invested ? fmtGain(e.value - e.invested, null) : null;
    const gainHtml = gain ? `<span class="${gain.cls}">${gain.dollar}</span>` : '--';
    tr.innerHTML = `
      <td>${e.date}</td><td>${badge}</td><td>${e.stock}</td>
      <td class="td-r">${e.shares ? Number(e.shares).toFixed(4) : '--'}</td>
      <td class="td-r">${e.price ? fmtDec(e.price) : '--'}</td>
      <td class="td-r">${fmtDec(e.value)}</td>
      <td class="td-r">${e.invested ? fmtDec(e.invested) : '--'}</td>
      <td class="td-r">${gainHtml}</td>
      <td><button class="btn btn-sm btn-danger" data-id="${e.id}">Del</button></td>
    `;
    tr.querySelector('button').addEventListener('click', () => { deleteEntry(e.id); setTimeout(renderHistory, 400); });
    tbody.appendChild(tr);
  });
}

function exportCSV() {
  const rows = [['Date','Platform','Stock','Shares','Price','Value','Invested','Gain/Loss','Auto']];
  state.entries.forEach(e => {
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

// Init projections on load
renderProjections();
