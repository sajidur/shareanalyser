'use strict';
/* BD Stock Tracker - single-page dashboard (vanilla JS, no build step). */
const LWC = window.LightweightCharts;

const S = {
  cleanup: [],
  routeToken: 0,
  route: '',
  quotes: null,
  quotesAt: 0,
  status: null,
  flagLabels: {},
  alertKinds: null,
  commissionRate: 0.4,
  overlays: { sma20: true, sma50: true, sma200: true, bb: false },
  rangeLevel: 50,
  stockRange: 126,
  market: { q: '', sector: '', cat: '', type: '', traded: true, sort: 'value_mn', dir: -1 },
  screener: { preset: 'bullish', sector: '', cat: '', minValue: 1, excludeZ: true, equity: true, sort: 'score', dir: -1 },
  lastLiveRender: 0,
};

/* ------------------------------------------------------------------ utils */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ESC[c]);
const NF = {};
const nf = d => NF[d] || (NF[d] = new Intl.NumberFormat('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d }));
const isNum = v => typeof v === 'number' && isFinite(v);
const fmt = (v, d = 2) => (isNum(v) ? nf(d).format(v) : '—');
const fmtInt = v => fmt(v, 0);
const fmtPct = (v, d = 2) => (isNum(v) ? (v > 0 ? '+' : '') + nf(d).format(v) + '%' : '—');
const fmtSigned = (v, d = 2) => (isNum(v) ? (v > 0 ? '+' : '') + nf(d).format(v) : '—');
const dir = v => (!isNum(v) || v === 0 ? 'flat' : v > 0 ? 'up' : 'down');
const arrow = v => (!isNum(v) || v === 0 ? '' : v > 0 ? '▲ ' : '▼ ');
const chg = (v, d = 2) => (isNum(v) ? `<span class="${dir(v)}">${arrow(v)}${fmtPct(v, d)}</span>` : '<span class="muted">—</span>');
/* DSE reports values in Tk million; Bangladeshi convention is crore (1 crore = 10 million). */
const crore = mn => (isNum(mn) ? `${nf(Math.abs(mn) >= 1000 ? 0 : 2).format(mn / 10)} cr` : '—');
const compact = v => {
  if (!isNum(v)) return '—';
  const a = Math.abs(v);
  if (a >= 1e7) return nf(2).format(v / 1e7) + ' cr';
  if (a >= 1e5) return nf(2).format(v / 1e5) + ' lakh';
  return nf(0).format(v);
};
const tk = (v, d = 2) => (isNum(v) ? 'Tk ' + fmt(v, d) : '—');
const tkSigned = (v, d = 0) => (isNum(v) ? `<span class="${dir(v)}">${v > 0 ? '+' : v < 0 ? '−' : ''}Tk ${fmt(Math.abs(v), d)}</span>` : '—');
const toDate = s => new Date(String(s).length <= 10 ? s + 'T00:00:00' : s);
const fmtDate = (s, opts = { day: 'numeric', month: 'short', year: 'numeric' }) => {
  if (!s) return '—';
  const d = toDate(s);
  return isNaN(d) ? esc(s) : d.toLocaleDateString('en-GB', opts);
};
const fmtTime = s => {
  if (!s) return '—';
  const d = new Date(s);
  return isNaN(d) ? esc(s) : d.toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
};
const pad2 = n => String(n).padStart(2, '0');
const today = () => { const d = new Date(); return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`; };
const LABEL_CLASS = { 'Strong Bullish': 'sb', Bullish: 'b', Bearish: 'be', 'Strong Bearish': 'sbe' };
const signal = (label, score) =>
  label
    ? `<span class="label ${LABEL_CLASS[label] || ''}" title="Technical score ${isNum(score) ? fmtSigned(score, 0) : ''} (−100 to +100)">${esc(label)}${isNum(score) ? ` <span class="num">${fmtSigned(score, 0)}</span>` : ''}</span>`
    : '<span class="muted">—</span>';
const catBadge = c => (c ? `<span class="badge cat-${esc(c)}" title="DSE category ${esc(c)}">${esc(c)}</span>` : '');
const IDX_NAME = { DSEX: 'Broad index', DS30: 'Blue-chip 30', DSES: 'Shariah index' };
const SHORT_FLAG = {
  golden_cross: 'Golden cross', death_cross: 'Death cross', macd_bull_cross: 'MACD ↑', macd_bear_cross: 'MACD ↓',
  oversold: 'Oversold', overbought: 'Overbought', vol_spike: 'Vol spike', new_52w_high: '52w high', new_52w_low: '52w low',
  near_52w_high: 'Near 52w high', near_52w_low: 'Near 52w low', above_sma200: '> 200 DMA', bb_above: '> upper BB',
  bb_below: '< lower BB', upper_circuit: 'Upper circuit', lower_circuit: 'Lower circuit', z_category: 'Z category',
};
const POS_FLAGS = new Set(['golden_cross', 'macd_bull_cross', 'new_52w_high', 'near_52w_high', 'above_sma200', 'oversold', 'bb_below']);
const NEG_FLAGS = new Set(['death_cross', 'macd_bear_cross', 'new_52w_low', 'near_52w_low', 'overbought', 'bb_above', 'lower_circuit']);
const WARN_FLAGS = new Set(['z_category', 'upper_circuit', 'vol_spike']);
const chip = f => `<span class="chip ${POS_FLAGS.has(f) ? 'pos' : NEG_FLAGS.has(f) ? 'neg' : WARN_FLAGS.has(f) ? 'warn' : ''}" title="${esc(S.flagLabels[f] || f)}">${esc(SHORT_FLAG[f] || f)}</span>`;
const chips = (flags, max = 4) => {
  if (!flags || !flags.length) return '';
  const shown = flags.slice(0, max).map(chip).join('');
  return `<div class="chips">${shown}${flags.length > max ? `<span class="chip" title="${esc(flags.slice(max).map(f => SHORT_FLAG[f] || f).join(', '))}">+${flags.length - max}</span>` : ''}</div>`;
};
const stockLink = code => `<a href="#/stock/${encodeURIComponent(code)}" class="code">${esc(code)}</a>`;
const empty = (title, text = '') => `<div class="empty"><strong>${esc(title)}</strong>${text}</div>`;

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), kind === 'err' ? 7000 : 3500);
}

async function api(path, { method = 'GET', body } = {}) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  else if (method !== 'GET') opts.body = '{}';
  const r = await fetch('/api' + path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { /* non-JSON */ }
  if (!r.ok) {
    const err = new Error((data && data.error) || `Request failed (HTTP ${r.status})`);
    err.status = r.status;
    err.dbError = !!(data && data.db_error);
    throw err;
  }
  return data;
}

async function getQuotes(force = false) {
  if (!force && S.quotes && Date.now() - S.quotesAt < 60000) return S.quotes;
  const d = await api('/quotes');
  S.quotes = d;
  S.quotesAt = Date.now();
  Object.assign(S.flagLabels, d.flag_labels || {});
  const dl = $('#codes');
  if (dl.dataset.n !== String(d.rows.length) || dl.dataset.named !== String(d.rows.filter(r => r.name).length)) {
    dl.innerHTML = d.rows.map(r => `<option value="${esc(r.code)}">${esc(r.name || r.sector || '')}</option>`).join('');
    dl.dataset.n = String(d.rows.length);
    dl.dataset.named = String(d.rows.filter(r => r.name).length);
  }
  return d;
}

/* ------------------------------------------------------------------ charts */
function colors() {
  const cs = getComputedStyle(document.documentElement);
  const v = n => cs.getPropertyValue(n).trim();
  return {
    bg: v('--surface'), text: v('--text-2'), muted: v('--muted'), grid: v('--grid'), axis: v('--axis'),
    up: v('--up-fill'), down: v('--down-fill'), flat: v('--flat-fill'), accent: v('--accent'),
    s1: v('--s1'), s2: v('--s2'), s3: v('--s3'), s4: v('--s4'), s5: v('--s5'), s7: v('--s7'),
  };
}
function alpha(hex, a) {
  const h = hex.replace('#', '');
  if (h.length !== 6) return hex;
  return `rgba(${parseInt(h.slice(0, 2), 16)}, ${parseInt(h.slice(2, 4), 16)}, ${parseInt(h.slice(4, 6), 16)}, ${a})`;
}
function makeChart(el, opts = {}) {
  const c = colors();
  const base = {
    autoSize: true,
    layout: { background: { type: 'solid', color: c.bg }, textColor: c.muted, fontSize: 11, fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif', attributionLogo: false },
    grid: { vertLines: { visible: false }, horzLines: { color: c.grid } },
    rightPriceScale: { borderColor: c.axis, minimumWidth: 68 },
    timeScale: { borderColor: c.axis, rightOffset: 2 },
    crosshair: { mode: LWC.CrosshairMode.Normal },
    localization: { locale: 'en-GB' },
  };
  const merged = { ...base, ...opts };
  for (const k of ['layout', 'grid', 'rightPriceScale', 'timeScale']) merged[k] = { ...base[k], ...(opts[k] || {}) };
  const chart = LWC.createChart(el, merged);
  S.cleanup.push(() => chart.remove());
  return chart;
}

/* ------------------------------------------------------------------ tables */
function sortRows(rows, cols, sort, d) {
  const col = cols.find(c => c.key === sort);
  const val = col && col.val ? col.val : r => r[sort];
  return rows.slice().sort((a, b) => {
    const x = val(a), y = val(b);
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return (typeof x === 'string' ? x.localeCompare(y) : x - y) * d;
  });
}
function tableHtml(cols, rows, st, emptyMsg = 'Nothing to show') {
  const head = cols
    .map(c => `<th class="${c.align || ''} ${c.sort === false || !st ? '' : 'sortable'}" data-k="${c.key}" ${c.title ? `title="${esc(c.title)}"` : ''}>${c.label}${st && st.sort === c.key ? `<span class="arr">${st.dir > 0 ? '↑' : '↓'}</span>` : ''}</th>`)
    .join('');
  const body = rows
    .map(r => `<tr class="${r.code ? 'click' : ''}" ${r.code ? `data-code="${esc(r.code)}"` : ''}>${cols.map(c => `<td class="${c.align || ''} ${c.cls || ''}">${c.html ? c.html(r) : esc(r[c.key] ?? '—')}</td>`).join('')}</tr>`)
    .join('');
  return `<table class="t"><thead><tr>${head}</tr></thead><tbody>${body || `<tr><td colspan="${cols.length}">${empty(emptyMsg)}</td></tr>`}</tbody></table>`;
}
function mountTable(container, cols, rows, st, opts = {}) {
  const draw = () => {
    let list = st && st.sort ? sortRows(rows, cols, st.sort, st.dir) : rows;
    if (opts.limit) list = list.slice(0, opts.limit);
    container.innerHTML = tableHtml(cols, list, st, opts.empty);
  };
  container.onclick = e => {
    const th = e.target.closest('th.sortable');
    if (th && st) {
      const k = th.dataset.k;
      if (st.sort === k) st.dir *= -1;
      else { st.sort = k; st.dir = (cols.find(c => c.key === k) || {}).align === 'r' ? -1 : 1; }
      draw();
      return;
    }
    if (e.target.closest('button, a, input, select')) return;
    const tr = e.target.closest('tr[data-code]');
    if (tr && !opts.noNav) location.hash = '#/stock/' + encodeURIComponent(tr.dataset.code);
  };
  draw();
  return draw;
}
const range52 = r => {
  if (!isNum(r.low52) || !isNum(r.high52) || r.high52 <= r.low52) return '<span class="muted">—</span>';
  const px = r.ltp || r.close;
  const p = Math.max(0, Math.min(100, ((px - r.low52) / (r.high52 - r.low52)) * 100));
  return `<div class="range52" title="52-week range ${fmt(r.low52)} – ${fmt(r.high52)}; now ${fmt(px)}"><div class="m" style="left:${p}%"></div></div>`;
};
const pos52 = r => (isNum(r.low52) && isNum(r.high52) && r.high52 > r.low52 ? ((r.ltp || r.close) - r.low52) / (r.high52 - r.low52) : null);
const rsiHtml = v => (isNum(v) ? `<span class="${v < 30 ? 'up' : v > 70 ? 'down' : ''}" title="${v < 30 ? 'Oversold' : v > 70 ? 'Overbought' : 'RSI 14'}">${fmt(v, 1)}</span>` : '<span class="muted">—</span>');
const COL = {
  star: { key: 'watch', label: '<span title="Watchlist">★</span>', sort: false, html: r => `<button class="star ${r.watch ? 'on' : ''}" data-star="${esc(r.code)}" title="${r.watch ? 'Remove from' : 'Add to'} watchlist" aria-label="Toggle watchlist">★</button>` },
  code: { key: 'code', label: 'Code', html: r => `<span class="code">${esc(r.code)}</span> ${catBadge(r.category)}<span class="nm">${esc(r.name || '')}</span>` },
  sector: { key: 'sector', label: 'Sector', html: r => `<span class="small">${esc(r.sector || '—')}</span>` },
  ltp: { key: 'ltp', label: 'LTP', align: 'r', val: r => r.ltp || r.close, html: r => fmt(r.ltp || r.close) },
  chg: { key: 'change_pct', label: 'Change', align: 'r', html: r => (r.volume ? chg(r.change_pct) : '<span class="muted small">no trade</span>') },
  volume: { key: 'volume', label: 'Volume', align: 'r', html: r => compact(r.volume) },
  value: { key: 'value_mn', label: 'Value (mn)', align: 'r', title: 'Traded value, Tk million', html: r => fmt(r.value_mn, 2) },
  trades: { key: 'trades', label: 'Trades', align: 'r', html: r => fmtInt(r.trades) },
  rsi: { key: 'rsi', label: 'RSI', align: 'r', html: r => rsiHtml(r.rsi) },
  signal: { key: 'score', label: 'Signal', html: r => signal(r.label, r.score) },
  pe: { key: 'trailing_pe', label: 'P/E', align: 'r', title: 'Trailing P/E from DSE', html: r => fmt(r.trailing_pe, 1) },
  flags: { key: 'flags', label: 'Signals', sort: false, html: r => chips(r.flags) },
  r1w: { key: 'ret_1w', label: '1W', align: 'r', html: r => chg(r.ret_1w, 1) },
  r1m: { key: 'ret_1m', label: '1M', align: 'r', html: r => chg(r.ret_1m, 1) },
  volx: { key: 'vol_ratio', label: 'Vol ×', align: 'r', title: 'Volume vs 20-day average', html: r => (isNum(r.vol_ratio) ? fmt(r.vol_ratio, 1) + '×' : '—') },
  range: { key: 'pos52', label: '52-wk range', val: pos52, align: 'c', html: range52 },
};

/* ------------------------------------------------------------------ shared bits */
function breadthHtml(b) {
  const a = b.advanced || 0, d = b.declined || 0, u = b.unchanged || 0, t = a + d + u;
  if (!t) return '<div class="muted small">No breadth data</div>';
  const w = n => (n / t) * 100;
  return `<div class="stack" role="img" aria-label="${a} advanced, ${u} unchanged, ${d} declined">
      <div style="width:${w(a)}%;background:var(--up-fill)"></div><div style="width:${w(u)}%;background:var(--flat-fill)"></div><div style="width:${w(d)}%;background:var(--down-fill)"></div></div>
    <div class="legend"><span><i style="background:var(--up-fill)"></i>▲ Advanced <b>${a}</b></span><span><i style="background:var(--flat-fill)"></i>Unchanged <b>${u}</b></span><span><i style="background:var(--down-fill)"></i>▼ Declined <b>${d}</b></span></div>`;
}
function signalBreadth(labels) {
  const order = [['Strong Bullish', 'var(--up-fill)'], ['Bullish', 'color-mix(in srgb, var(--up-fill) 45%, var(--surface))'], ['Neutral', 'var(--flat-fill)'], ['Bearish', 'color-mix(in srgb, var(--down-fill) 45%, var(--surface))'], ['Strong Bearish', 'var(--down-fill)']];
  const total = order.reduce((s, [k]) => s + (labels[k] || 0), 0);
  if (!total) return '';
  return `<h3 class="mt">Technical picture · ${total} stocks</h3><div class="stack mt" role="img" aria-label="Signal distribution">${order.map(([k, c]) => `<div style="width:${((labels[k] || 0) / total) * 100}%;background:${c}" title="${k}: ${labels[k] || 0}"></div>`).join('')}</div>
    <div class="legend">${order.map(([k, c]) => `<span><i style="background:${c}"></i>${k} <b>${labels[k] || 0}</b></span>`).join('')}</div>`;
}
function sectorBars(sectors, limit = 50) {
  if (!sectors || !sectors.length) return '<div class="muted small">No sector data</div>';
  const list = sectors.slice(0, limit);
  const max = Math.max(0.5, ...list.map(s => Math.abs(s.avg_change)));
  return `<div class="hbars">${list
    .map(s => {
      const w = (Math.abs(s.avg_change) / max) * 50;
      const style = s.avg_change >= 0 ? `left:50%;width:${w}%` : `left:${50 - w}%;width:${w}%`;
      return `<div class="hbar" title="${esc(s.sector)}: avg ${fmtPct(s.avg_change)} · ▲${s.advanced} ▼${s.declined} · turnover Tk ${crore(s.turnover_mn)} (${fmt(s.turnover_share, 1)}%)">
        <span class="n">${esc(s.sector)}</span><div class="track"><div class="axis0"></div><div class="fill ${s.avg_change >= 0 ? 'pos' : 'neg'}" style="${style}"></div></div><span class="val">${chg(s.avg_change)}</span></div>`;
    })
    .join('')}</div>`;
}
function errorHtml(e) {
  if (e.dbError || e.status === 503) {
    return `<div class="err-box"><h2>Can't use the MySQL database</h2><p>${esc(e.message)}</p>
      <ol class="steps"><li>Check that the <b>MySQL80</b> Windows service is running.</li>
      <li>In the project folder run <code>python run.py setup</code> and enter your MySQL user and password.</li>
      <li>Restart the dashboard with <code>python run.py serve</code>.</li></ol></div>`;
  }
  return `<div class="err-box"><h2>Something went wrong</h2><pre>${esc(e.message)}</pre></div>`;
}

/* ------------------------------------------------------------------ router */
const ROUTES = {
  '': viewDashboard, market: viewMarket, stock: viewStock, screener: viewScreener, predict: viewPredict, accuracy: viewAccuracy, bd: viewBD, watchlist: viewWatchlist,
  portfolio: viewPortfolio, alerts: viewAlerts, reports: viewReports, settings: viewSettings,
};
const LIVE_ROUTES = new Set(['', 'market', 'watchlist', 'screener']);

function runCleanup() {
  while (S.cleanup.length) {
    try { S.cleanup.pop()(); } catch (e) { /* already disposed */ }
  }
}
async function router(opts = {}) {
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  const name = ROUTES[parts[0]] ? parts[0] : '';
  S.route = name;
  $$('#nav a').forEach(a => a.classList.toggle('active', a.dataset.route === name));
  const token = ++S.routeToken;
  const view = $('#view');
  const y = window.scrollY;
  if (!opts.quiet) {
    runCleanup();
    view.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
    window.scrollTo(0, 0);
  }
  const ctx = {
    alive: () => token === S.routeToken,
    render: html => { runCleanup(); view.innerHTML = html; if (opts.keepScroll) window.scrollTo(0, y); },
  };
  try {
    await ROUTES[name](view, parts.slice(1), ctx);
    if (LIVE_ROUTES.has(name)) S.lastLiveRender = Date.now();
  } catch (e) {
    if (ctx.alive()) { runCleanup(); view.innerHTML = errorHtml(e); }
    console.error(e);
  }
}
const rerender = () => router({ quiet: true, keepScroll: true });

/* ------------------------------------------------------------------ status, banner, live updates */
function renderStatus(st) {
  const pill = $('#status-pill');
  const m = st && st.market;
  if (!m) {
    pill.className = 'status-pill';
    $('.txt', pill).textContent = st ? 'No market data yet' : 'Server unreachable';
    return;
  }
  pill.className = 'status-pill' + (m.is_open ? ' open' : '');
  const t = m.fetched_at ? new Date(m.fetched_at).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }) : '';
  $('.txt', pill).innerHTML = m.is_open
    ? `Market open <span class="txt-long">· updated ${t}</span>`
    : `Closed <span class="txt-long">· ${fmtDate(m.trade_date, { day: 'numeric', month: 'short' })} session</span>`;
  const mon = st.monitor;
  $('#monitor-info').innerHTML = mon
    ? mon.error
      ? `Monitor: last check failed (${esc(mon.error).slice(0, 80)})`
      : `Monitor on · checked ${fmtTime(mon.at)}${mon.next_in_s ? ` · next in ${Math.round(mon.next_in_s / 60)} min` : ''}`
    : 'Background monitor off';
}
function renderBanner(st) {
  const b = $('#banner');
  const jobs = st.jobs || {};
  const local = ['backfill', 'daily', 'analyze', 'predict', 'simulate', 'ai_news', 'ai_profiles'].map(k => jobs[k]).find(j => j && j.running);
  const external = st.backfill_progress && st.backfill_progress.running ? st.backfill_progress : null;
  if (local || external) {
    const p = local ? local.progress : external;
    const name = local ? local.name : 'backfill';
    const pct = p && p.total ? Math.round((p.done / p.total) * 100) : null;
    const what = name === 'backfill' ? 'Downloading about one year of price history from DSE'
      : name === 'daily' ? (p ? 'End-of-day run: filling missing price history' : 'Running end-of-day analysis')
      : name === 'predict' ? 'Training and testing the prediction model'
      : name === 'simulate' ? 'Rebuilding the walk-forward forecast history (about 2 minutes)'
      : name === 'ai_news' ? 'Local AI is rating announcements (about 7 seconds each)'
      : name === 'ai_profiles' ? 'Local AI is running company health checks (2–3 minutes each)'
      : 'Re-running analysis';
    b.innerHTML = `<div class="banner info"><div class="msg"><strong>${what}…</strong> ${p ? `${p.done} / ${p.total} <span class="muted">${esc(p.item || '')}</span>` : ''}</div>${pct != null ? `<div class="progress"><div style="width:${pct}%"></div></div>` : ''}</div>`;
    S.jobWasRunning = true;
    return;
  }
  if (S.jobWasRunning) {
    S.jobWasRunning = false;
    S.quotes = null;
    const failed = Object.values(jobs).find(j => j.error);
    toast(failed ? `Background job failed: ${failed.error}` : 'Background job finished', failed ? 'err' : '');
    rerender();
  }
  const h = st.history || {};
  const covered = h.codes_with_history || 0;
  if (st.instruments && covered < st.instruments * 0.5) {
    b.innerHTML = `<div class="banner warn"><div class="msg"><strong>Price history covers ${covered} of ${st.instruments} instruments.</strong> Indicators and signals need history. Download about one year for every instrument from DSE (about 15–20 minutes, runs in the background).</div><button class="btn btn-primary" data-job="backfill">Download history</button></div>`;
    return;
  }
  b.innerHTML = '';
}
async function refreshStatus() {
  try {
    const st = await api('/status');
    S.status = st;
    renderStatus(st);
    renderBanner(st);
    return st;
  } catch (e) {
    renderStatus(null);
    $('#banner').innerHTML = `<div class="banner err"><div class="msg">${e.dbError || e.status === 503 ? `<strong>Database unavailable.</strong> ${esc(e.message)} — run <code>python run.py setup</code>.` : `<strong>Can't reach the tracker server.</strong> Is <code>python run.py serve</code> running?`}</div></div>`;
    return null;
  }
}
async function statusLoop() {
  const st = await refreshStatus();
  const running = st && (Object.values(st.jobs || {}).some(j => j.running) || !!(st.backfill_progress && st.backfill_progress.running));
  const open = st && st.market && st.market.is_open;
  const busy = $('#modal').open || (document.activeElement && $('#view').contains(document.activeElement) && /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName));
  if (open && !running && !document.hidden && !busy && LIVE_ROUTES.has(S.route) && Date.now() - S.lastLiveRender > 55000) {
    S.quotes = null;
    rerender();
  }
  clearTimeout(S.statusTimer);
  S.statusTimer = setTimeout(statusLoop, running ? 3000 : 60000);
}
async function startJob(name, body) {
  try {
    const r = await api('/jobs/' + name, { method: 'POST', body: body || {} });
    toast(r.started === false ? 'That job is already running' : 'Started in the background');
    clearTimeout(S.statusTimer);
    statusLoop();
  } catch (e) { toast(e.message, 'err'); }
}
async function refreshNow() {
  const btn = $('#refresh-btn');
  btn.classList.add('spin');
  btn.disabled = true;
  try {
    await api('/jobs/refresh', { method: 'POST' });
    S.quotes = null;
    await refreshStatus();
    rerender();
    toast('Prices updated from DSE');
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    btn.classList.remove('spin');
    btn.disabled = false;
  }
}
async function toggleWatch(code, btn) {
  const on = btn.classList.contains('on');
  try {
    if (on) await api('/watchlist/' + encodeURIComponent(code), { method: 'DELETE' });
    else await api('/watchlist', { method: 'POST', body: { code } });
    btn.classList.toggle('on', !on);
    if (S.quotes) { const r = S.quotes.rows.find(x => x.code === code); if (r) r.watch = !on; }
    toast(on ? `${code} removed from watchlist` : `${code} added to watchlist`);
  } catch (e) { toast(e.message, 'err'); }
}

/* ------------------------------------------------------------------ modals */
function openModal(html, onMount) {
  const m = $('#modal');
  m.innerHTML = html;
  $$('[data-close]', m).forEach(b => b.addEventListener('click', () => m.close()));
  m.showModal();
  if (onMount) onMount(m);
}
async function openTxModal(pre = {}) {
  const q = await getQuotes().catch(() => null);
  const priceFor = code => { const r = q && q.rows.find(x => x.code === code); return r ? r.ltp || r.close : null; };
  const side = pre.side || 'BUY';
  openModal(
    `<form id="tx-form" novalidate>
      <div class="modal-h"><h2>Add transaction</h2><button type="button" class="x-btn" data-close aria-label="Close">✕</button></div>
      <div class="modal-b">
        <div class="seg" id="tx-side"><button type="button" data-v="BUY" class="${side === 'BUY' ? 'on' : ''}">Buy</button><button type="button" data-v="SELL" class="${side === 'SELL' ? 'on' : ''}">Sell</button></div>
        <div class="row"><label class="field"><span>Trading code</span><input name="code" list="codes" required value="${esc(pre.code || '')}" autocapitalize="characters"></label>
          <label class="field"><span>Trade date</span><input type="date" name="trade_date" value="${today()}" required></label></div>
        <div class="row"><label class="field"><span>Quantity (shares)</span><input type="number" name="qty" min="1" step="1" required value="${esc(pre.qty || '')}"></label>
          <label class="field"><span>Price per share (Tk)</span><input type="number" name="price" min="0" step="0.01" required value="${esc(pre.price ?? priceFor(pre.code) ?? '')}"></label></div>
        <label class="field"><span>Commission &amp; fees (Tk)</span><input type="number" name="fee" min="0" step="0.01" placeholder="Leave blank to use ${fmt(S.commissionRate)}% of value"><span class="small muted" id="tx-hint"></span></label>
        <label class="field"><span>Note (optional)</span><input name="note" maxlength="255"></label>
        <div class="form-err" id="tx-err"></div>
      </div>
      <div class="modal-f"><button type="button" class="btn" data-close>Cancel</button><button class="btn btn-primary" type="submit">Save transaction</button></div>
    </form>`,
    m => {
      const f = $('#tx-form', m);
      let curSide = side;
      $$('#tx-side button', m).forEach(b => b.addEventListener('click', () => { curSide = b.dataset.v; $$('#tx-side button', m).forEach(x => x.classList.toggle('on', x === b)); }));
      const hint = () => {
        const v = Number(f.qty.value) * Number(f.price.value);
        $('#tx-hint', m).textContent = v > 0 ? `Trade value ${tk(v)} · auto commission ${tk((v * S.commissionRate) / 100)}` : '';
      };
      f.qty.addEventListener('input', hint);
      f.price.addEventListener('input', hint);
      f.code.addEventListener('change', () => { f.code.value = f.code.value.trim().toUpperCase(); const p = priceFor(f.code.value); if (p && !f.price.value) { f.price.value = p; hint(); } });
      hint();
      f.addEventListener('submit', async e => {
        e.preventDefault();
        const body = { side: curSide, code: f.code.value.trim().toUpperCase(), trade_date: f.trade_date.value, qty: f.qty.value, price: f.price.value, fee: f.fee.value, note: f.note.value };
        try {
          await api('/transactions', { method: 'POST', body });
          m.close();
          toast(`${body.side === 'BUY' ? 'Bought' : 'Sold'} ${body.qty} ${body.code} recorded`);
          rerender();
        } catch (err) { $('#tx-err', m).textContent = err.message; }
      });
      (pre.code ? f.qty : f.code).focus();
    }
  );
}
async function loadAlertKinds() {
  if (!S.alertKinds) S.alertKinds = (await api('/alerts')).kinds;
  return S.alertKinds;
}
function alertFormFields(kinds, pre = {}) {
  const opts = Object.entries(kinds).map(([k, v]) => `<option value="${k}" ${pre.kind === k ? 'selected' : ''}>${esc(v.label)}</option>`).join('');
  return `<div class="row"><label class="field"><span>Trading code</span><input name="code" list="codes" required value="${esc(pre.code || '')}"></label>
      <label class="field"><span>Condition</span><select name="kind">${opts}</select></label></div>
    <div class="row"><label class="field" id="al-th"><span>Threshold <span id="al-unit" class="muted"></span></span><input type="number" step="any" name="threshold" value="${esc(pre.threshold ?? '')}"></label>
      <label class="field"><span>Note (optional)</span><input name="note" maxlength="255"></label></div>
    <label class="check"><input type="checkbox" name="repeat" checked> Keep active after it triggers (fires at most once per day)</label>`;
}
function bindAlertForm(f, kinds) {
  const sync = () => {
    const k = kinds[f.kind.value] || {};
    $('#al-th', f).style.visibility = k.needs ? 'visible' : 'hidden';
    $('#al-unit', f).textContent = k.unit ? `(${k.unit})` : '';
    f.threshold.required = !!k.needs;
  };
  f.kind.addEventListener('change', sync);
  sync();
  return () => ({ code: f.code.value.trim().toUpperCase(), kind: f.kind.value, threshold: f.threshold.value, note: f.note.value, repeat: f.repeat.checked });
}
async function openAlertModal(pre = {}) {
  let kinds;
  try { kinds = await loadAlertKinds(); } catch (e) { toast(e.message, 'err'); return; }
  openModal(
    `<form id="al-form" novalidate><div class="modal-h"><h2>New alert</h2><button type="button" class="x-btn" data-close aria-label="Close">✕</button></div>
      <div class="modal-b">${alertFormFields(kinds, pre)}<div class="form-err" id="al-err"></div></div>
      <div class="modal-f"><button type="button" class="btn" data-close>Cancel</button><button class="btn btn-primary" type="submit">Create alert</button></div></form>`,
    m => {
      const f = $('#al-form', m);
      const read = bindAlertForm(f, kinds);
      f.addEventListener('submit', async e => {
        e.preventDefault();
        try {
          await api('/alerts', { method: 'POST', body: read() });
          m.close();
          toast('Alert created');
          rerender();
        } catch (err) { $('#al-err', m).textContent = err.message; }
      });
      f.threshold.focus();
    }
  );
}

/* ================================================================== VIEWS */

/* ---------------- dashboard */
async function viewDashboard(el, args, ctx) {
  const d = await api('/market');
  if (!ctx.alive()) return;
  const st = d.state;
  if (!st) {
    ctx.render(`<div class="card">${empty('No market data yet', '<p>Click the refresh button (top right) to fetch the latest prices from DSE.</p>')}</div>`);
    return;
  }
  const idx = Object.fromEntries((st.indices || []).map(i => [i.key, i]));
  const t = st.totals || {};
  const mh = d.market_history || [];
  const prev = mh.filter(r => r.trade_date < st.trade_date).slice(-1)[0];
  const turnChg = prev && prev.turnover_mn && t.turnover ? (t.turnover / prev.turnover_mn - 1) * 100 : null;
  const idxTile = k => {
    const i = idx[k];
    return `<div class="tile idx"><div class="k"><span>${k}</span><span class="muted">${IDX_NAME[k]}</span></div><div class="v">${i ? fmt(i.value) : '—'}</div>
      <div class="d">${i ? `<span class="${dir(i.change)}">${arrow(i.change)}${fmtSigned(i.change)} (${fmtPct(i.percent)})</span>` : '&nbsp;'}</div></div>`;
  };
  const mover = rows => tableHtml([COL.code, COL.ltp, COL.chg, { key: 'value_mn', label: 'Value', align: 'r', html: r => crore(r.value_mn) }], rows, null, 'No trades yet');
  const pf = d.portfolio || {};
  ctx.render(`
    <div class="page-h"><div><h1>Market overview</h1>
      <div class="sub">Dhaka Stock Exchange · ${fmtDate(st.trade_date, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })} · ${st.is_open ? '<b class="up">Live session</b>' : 'Session closed'}${st.trade_time ? ` · DSE time ${esc(st.trade_time)}` : ''}</div></div>
      <div class="actions">${d.last_report ? `<a class="btn" href="#/reports/${esc(d.last_report)}">Latest daily report</a>` : ''}</div></div>
    <div class="tiles">
      ${['DSEX', 'DS30', 'DSES'].map(idxTile).join('')}
      <div class="tile"><div class="k">Turnover</div><div class="v">Tk ${crore(t.turnover)}</div><div class="d">${turnChg != null ? `${chg(turnChg, 1)} vs previous session` : '&nbsp;'}</div></div>
      <div class="tile"><div class="k">Volume</div><div class="v">${compact(t.volume)}</div><div class="d">${fmtInt(t.trades)} trades</div></div>
    </div>
    <div class="grid g-main">
      <div class="card fill">
        <div class="card-h"><h2>Index trend</h2><div class="seg" id="idx-seg">${['DSEX', 'DS30', 'DSES'].map((k, i) => `<button data-k="${k}" class="${i ? '' : 'on'}">${k}</button>`).join('')}</div></div>
        <div class="chart-box dash grow" id="idx-chart"></div>
        <div class="pane-label">Daily turnover (Tk crore)</div>
        <div class="chart-box bars" id="turn-chart"></div>
        ${(d.index_history.DSEX || []).length < 60 ? '<div class="card-b small muted">Index history starts with DSE\'s last 30 sessions and grows every day the tracker runs.</div>' : ''}
      </div>
      <div class="card">
        <div class="card-h"><h2>Market breadth</h2></div>
        <div class="card-b">${breadthHtml(st.breadth || {})}${signalBreadth(d.signal_breadth || {})}</div>
        <div class="card-h" style="border-top:1px solid var(--border)"><h2>Sectors</h2><span class="small muted">average % change</span></div>
        <div class="card-b">${sectorBars(d.sectors)}</div>
      </div>
    </div>
    <div class="grid g3">
      <div class="card"><div class="card-h"><h2>Top movers</h2><div class="seg" id="mv-seg"><button data-k="g" class="on">Gainers</button><button data-k="l">Losers</button><button data-k="t">Turnover</button></div></div>
        <div class="table-wrap" id="mv-g">${mover(d.gainers)}</div><div class="table-wrap" id="mv-l" hidden>${mover(d.losers)}</div><div class="table-wrap" id="mv-t" hidden>${mover(d.turnover)}</div></div>
      <div class="card"><div class="card-h"><h2>Watchlist</h2><a href="#/watchlist" class="small">Manage</a></div>
        <div class="table-wrap">${d.watchlist.length ? tableHtml([COL.code, COL.ltp, COL.chg, COL.signal], d.watchlist, null) : empty('No stocks on your watchlist', '<p class="small">Use the ★ in the Market table or on a stock page.</p>')}</div></div>
      <div class="card"><div class="card-h"><h2>Portfolio</h2><a href="#/portfolio" class="small">Open</a></div>
        <div class="card-b">${pf.positions ? `<div class="kv" style="grid-template-columns:1fr">
            <div><span>Market value</span><span>${tk(pf.market_value, 0)}</span></div>
            <div><span>Today</span><span>${tkSigned(pf.day_change)} ${isNum(pf.day_change_pct) ? `(${chg(pf.day_change_pct)})` : ''}</span></div>
            <div><span>Unrealised P/L</span><span>${tkSigned(pf.unrealized)} ${isNum(pf.unrealized_pct) ? `(${chg(pf.unrealized_pct)})` : ''}</span></div></div>`
          : `<p class="muted small">No holdings yet. <a href="#/portfolio">Add your first transaction</a>.</p>`}
          <h3 class="mt">Recent alerts</h3>
          ${d.events.length ? `<ul class="list-plain">${d.events.slice(0, 6).map(e => `<li>${stockLink(e.code)} ${esc(e.message)} <span class="muted small">· ${fmtTime(e.triggered_at)}</span></li>`).join('')}</ul>` : '<p class="muted small">No alerts triggered recently. <a href="#/alerts">Set up alerts</a>.</p>'}
        </div></div>
    </div>
    <div class="card"><div class="card-h"><h2>Latest company news</h2><span class="small muted">DSE disclosures, collected through the day</span></div>
      <div class="card-b">${newsListHtml(d.news, d.tag_labels, true)}</div></div>`);

  $$('#mv-seg button').forEach(b => b.addEventListener('click', () => {
    $$('#mv-seg button').forEach(x => x.classList.toggle('on', x === b));
    ['g', 'l', 't'].forEach(k => { $('#mv-' + k).hidden = k !== b.dataset.k; });
  }));
  $$('.card .table-wrap').forEach(w => mountRowNav(w));

  const c = colors();
  const ic = makeChart($('#idx-chart'), { timeScale: { timeVisible: false } });
  const area = ic.addAreaSeries({ lineColor: c.s1, topColor: alpha(c.s1, 0.22), bottomColor: alpha(c.s1, 0.02), lineWidth: 2, priceLineVisible: false });
  const showIdx = k => { area.setData(d.index_history[k] || []); ic.timeScale().fitContent(); };
  showIdx('DSEX');
  $$('#idx-seg button').forEach(b => b.addEventListener('click', () => { $$('#idx-seg button').forEach(x => x.classList.toggle('on', x === b)); showIdx(b.dataset.k); }));
  const tc = makeChart($('#turn-chart'));
  const hist = tc.addHistogramSeries({ color: alpha(c.s1, 0.75), priceLineVisible: false, priceFormat: { type: 'price', precision: 0, minMove: 1 } });
  hist.setData(mh.filter(r => isNum(r.turnover_mn)).map(r => ({ time: r.trade_date, value: r.turnover_mn / 10 })));
  tc.timeScale().fitContent();
}
function mountRowNav(container) {
  container.addEventListener('click', e => {
    if (e.target.closest('button, a, input')) return;
    const tr = e.target.closest('tr[data-code]');
    if (tr) location.hash = '#/stock/' + encodeURIComponent(tr.dataset.code);
  });
}

/* ---------------- market */
async function viewMarket(el, args, ctx) {
  const d = await getQuotes();
  if (!ctx.alive()) return;
  const f = S.market;
  const rows = d.rows;
  const sectors = [...new Set(rows.map(r => r.sector).filter(Boolean))].sort();
  const types = [...new Set(rows.map(r => r.asset_type).filter(Boolean))].sort();
  const st = d.state || {};
  ctx.render(`
    <div class="page-h"><div><h1>Market</h1><div class="sub">${rows.length} DSE instruments · prices ${st.is_open ? 'live, updated' : 'as of'} ${fmtTime(st.fetched_at)}${d.signal_date ? ` · signals from ${fmtDate(d.signal_date)} close` : ''}</div></div></div>
    <div class="card">
      <div class="card-h toolbar">
        <input id="m-q" placeholder="Filter by code or name" value="${esc(f.q)}" style="width:200px">
        <select id="m-sector"><option value="">All sectors</option>${sectors.map(s => `<option ${f.sector === s ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select>
        <select id="m-cat"><option value="">All categories</option>${['A', 'B', 'N', 'Z'].map(c => `<option value="${c}" ${f.cat === c ? 'selected' : ''}>Category ${c}</option>`).join('')}</select>
        <select id="m-type"><option value="">All types</option>${types.map(t => `<option ${f.type === t ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>
        <label class="check"><input type="checkbox" id="m-traded" ${f.traded ? 'checked' : ''}> Traded today</label>
        <span class="grow"></span><span class="small muted" id="m-count"></span>
      </div>
      <div class="table-wrap tall" id="m-table"></div>
    </div>`);
  const cols = [COL.star, COL.code, COL.sector, COL.ltp, COL.chg, COL.volume, COL.value, COL.trades, COL.rsi, COL.signal, COL.pe];
  const tableEl = $('#m-table');
  const filtered = () => {
    const q = f.q.trim().toLowerCase();
    return rows.filter(r =>
      (!q || r.code.toLowerCase().includes(q) || (r.name || '').toLowerCase().includes(q)) &&
      (!f.sector || r.sector === f.sector) && (!f.cat || r.category === f.cat) && (!f.type || r.asset_type === f.type) &&
      (!f.traded || r.volume > 0));
  };
  const draw = () => {
    const list = filtered();
    mountTable(tableEl, cols, list, f);
    $('#m-count').textContent = `${list.length} shown`;
  };
  $('#m-q').addEventListener('input', e => { f.q = e.target.value; draw(); });
  $('#m-sector').addEventListener('change', e => { f.sector = e.target.value; draw(); });
  $('#m-cat').addEventListener('change', e => { f.cat = e.target.value; draw(); });
  $('#m-type').addEventListener('change', e => { f.type = e.target.value; draw(); });
  $('#m-traded').addEventListener('change', e => { f.traded = e.target.checked; draw(); });
  draw();
}

/* ---------------- screener */
const hasFlag = f => r => (r.flags || []).includes(f);
const PRESETS = [
  ['all', 'All', () => true],
  ['bullish', 'Bullish score', r => r.score >= 15],
  ['strong', 'Strong bullish', r => r.score >= 40],
  ['bearish', 'Bearish score', r => isNum(r.score) && r.score <= -15],
  ['oversold', 'Oversold (RSI < 30)', r => isNum(r.rsi) && r.rsi < 30],
  ['overbought', 'Overbought (RSI > 70)', r => r.rsi > 70],
  ['golden', 'Golden cross', hasFlag('golden_cross')],
  ['macd', 'MACD bullish cross', hasFlag('macd_bull_cross')],
  ['volspike', 'Volume spike', hasFlag('vol_spike')],
  ['high52', 'New 52-week high', hasFlag('new_52w_high')],
  ['nearlow', 'Near 52-week low', r => hasFlag('near_52w_low')(r) || hasFlag('new_52w_low')(r)],
  ['above200', 'Above 200-day avg', hasFlag('above_sma200')],
  ['value', 'Low P/E (< 15)', r => r.trailing_pe > 0 && r.trailing_pe < 15],
  ['circuit', 'Near upper circuit', hasFlag('upper_circuit')],
];
async function viewScreener(el, args, ctx) {
  const d = await getQuotes();
  if (!ctx.alive()) return;
  const f = S.screener;
  const sectors = [...new Set(d.rows.map(r => r.sector).filter(Boolean))].sort();
  ctx.render(`
    <div class="page-h"><div><h1>Screener</h1><div class="sub">${d.signal_date ? `Technical signals from the ${fmtDate(d.signal_date)} close; prices are the latest snapshot.` : 'Signals appear after the first end-of-day analysis.'}</div></div></div>
    <div class="card">
      <div class="card-b"><div class="chips" id="presets">${PRESETS.map(([k, l]) => `<button class="chip ${f.preset === k ? 'on' : ''}" data-k="${k}">${esc(l)}</button>`).join('')}</div></div>
      <div class="card-h toolbar" style="border-top:1px solid var(--border)">
        <select id="s-sector"><option value="">All sectors</option>${sectors.map(s => `<option ${f.sector === s ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select>
        <select id="s-cat"><option value="">All categories</option>${['A', 'B', 'N', 'Z'].map(c => `<option value="${c}" ${f.cat === c ? 'selected' : ''}>Category ${c}</option>`).join('')}</select>
        <label class="check">Min. traded value
          <select id="s-min">${[0, 1, 5, 10, 50].map(v => `<option value="${v}" ${f.minValue === v ? 'selected' : ''}>${v ? `Tk ${v} mn` : 'Any'}</option>`).join('')}</select></label>
        <label class="check"><input type="checkbox" id="s-eq" ${f.equity ? 'checked' : ''}> Equities only</label>
        <label class="check"><input type="checkbox" id="s-z" ${f.excludeZ ? 'checked' : ''}> Exclude Z category</label>
        <span class="grow"></span><span class="small muted" id="s-count"></span>
      </div>
      <div class="table-wrap tall" id="s-table"></div>
    </div>
    <p class="disclaimer">Screens are mechanical filters on chart indicators. Check company disclosures, fundamentals and news on DSE before making decisions.</p>`);
  const cols = [COL.star, COL.code, COL.sector, COL.ltp, COL.chg, COL.value, COL.rsi, COL.volx, COL.r1m, COL.range, COL.signal, COL.flags];
  const draw = () => {
    const pf = (PRESETS.find(p => p[0] === f.preset) || PRESETS[0])[2];
    const list = d.rows.filter(r =>
      pf(r) && (!f.sector || r.sector === f.sector) && (!f.cat || r.category === f.cat) &&
      (!f.excludeZ || r.category !== 'Z') && (!f.equity || r.asset_type === 'EQ') && (r.value_mn || 0) >= f.minValue);
    mountTable($('#s-table'), cols, list, f, { empty: 'No stocks match this screen' });
    $('#s-count').textContent = `${list.length} match${list.length === 1 ? '' : 'es'}`;
  };
  $('#presets').addEventListener('click', e => {
    const b = e.target.closest('button[data-k]');
    if (!b) return;
    f.preset = b.dataset.k;
    $$('#presets button').forEach(x => x.classList.toggle('on', x === b));
    draw();
  });
  $('#s-sector').addEventListener('change', e => { f.sector = e.target.value; draw(); });
  $('#s-cat').addEventListener('change', e => { f.cat = e.target.value; draw(); });
  $('#s-min').addEventListener('change', e => { f.minValue = Number(e.target.value); draw(); });
  $('#s-eq').addEventListener('change', e => { f.equity = e.target.checked; draw(); });
  $('#s-z').addEventListener('change', e => { f.excludeZ = e.target.checked; draw(); });
  draw();
}

/* ---------------- stock detail */
async function viewStock(el, args, ctx) {
  const code = (args[0] || '').toUpperCase();
  if (!code) { location.hash = '#/market'; return; }
  const d = await api('/stock/' + encodeURIComponent(code));
  if (!ctx.alive()) return;
  Object.assign(S.flagLabels, d.flag_labels || {});
  S.commissionRate = d.commission_rate ?? S.commissionRate;
  const inst = d.instrument, q = d.quote || {}, a = d.analysis, info = d.info || {}, ex = info.extra || {};
  const last = d.candles[d.candles.length - 1];
  const price = q.ltp || q.close || (last && last.close);
  const change = isNum(q.ycp) && isNum(price) && q.volume ? price - q.ycp : null;
  const pct = q.volume ? q.change_pct : null;
  const pos = d.position;

  const scoreHtml = a ? (() => {
    const w = Math.abs(a.score) / 2;
    const style = a.score >= 0 ? `left:50%;width:${w}%` : `left:${50 - w}%;width:${w}%`;
    return `<div class="score-head"><span class="big num">${fmtSigned(a.score, 0)}</span>${signal(a.label)}</div>
      <div class="score-track" role="img" aria-label="Technical score ${a.score} of 100"><div class="zero"></div><div class="fill ${a.score >= 0 ? 'pos' : 'neg'}" style="${style}"></div></div>
      <div class="score-scale"><span>−100 bearish</span><span>0</span><span>bullish +100</span></div>
      <ul class="reasons">${a.reasons.map(r => `<li><span class="pts ${dir(r.points)}">${r.points ? fmtSigned(r.points, 0) : '·'}</span><span>${esc(r.text)}</span></li>`).join('')}</ul>
      ${a.flags.length ? `<div class="mt">${chips(a.flags, 20)}</div>` : ''}`;
  })() : empty('Not enough history', '<p class="small">At least 20 sessions are needed for indicators.</p>');

  const ret = k => `<div class="tile"><div class="k">${k.toUpperCase()}</div><div class="v" style="font-size:17px">${a ? chg(a['ret_' + k], 1) : '—'}</div></div>`;
  const pb = isNum(info.nav) && info.nav > 0 && isNum(price) ? price / info.nav : null;
  const pattern = (ex.sharePattern || []).slice(-1)[0];
  const SH = [['sponsor', 'Sponsor/Director', 'var(--s1)'], ['government', 'Government', 'var(--s2)'], ['institution', 'Institution', 'var(--s3)'], ['foreign', 'Foreign', 'var(--s4)'], ['public', 'Public', 'var(--s5)']];
  const fin = (ex.multiYearFinancials || []).slice(-5).reverse();
  const divs = (ex.dividendHistory || []).slice(0, 6);

  ctx.render(`
    <div class="stock-h">
      <div class="id"><h1>${esc(code)} ${catBadge(inst.category)}</h1>
        <div class="meta">${esc(inst.name || '')}${inst.sector ? ` · ${esc(inst.sector)}` : ''}${inst.board && inst.board !== 'PUBLIC' ? ` · ${esc(inst.board)} board` : ''}</div></div>
      <div><div class="px num">${fmt(price)}</div>
        <div class="pxd">${q.volume ? `<span class="${dir(change)}">${arrow(change)}${fmtSigned(change)} (${fmtPct(pct)})</span>` : '<span class="muted">Not traded</span>'}
          <span class="muted small"> · ${d.live_bar ? 'live · ' : ''}${fmtDate(q.trade_date)}</span></div></div>
      <div class="actions">
        <button class="btn ${d.watch ? 'on' : ''}" id="s-watch" data-on="${d.watch ? 1 : 0}">★ ${d.watch ? 'Watching' : 'Watch'}</button>
        <button class="btn" id="s-alert">+ Alert</button>
        <button class="btn" id="s-tx">+ Transaction</button>
        <a class="btn" href="#/predict/${encodeURIComponent(code)}">Prediction</a>
        <button class="btn" id="s-refresh" title="Re-download price history and fundamentals from DSE">↻ Update</button>
        <a class="btn" href="https://www.dse.com.bd/company/${encodeURIComponent(code)}" target="_blank" rel="noopener">DSE page ↗</a>
      </div>
    </div>
    ${d.warning ? `<div class="banner warn" style="margin:0"><div class="msg">${esc(d.warning)}</div></div>` : ''}
    <div class="tiles">
      <div class="tile"><div class="k">Day range</div><div class="v" style="font-size:17px">${fmt(q.low)} – ${fmt(q.high)}</div><div class="d">Open ${fmt(q.open)} · Prev ${fmt(q.ycp)}</div></div>
      <div class="tile"><div class="k">Volume</div><div class="v" style="font-size:17px">${compact(q.volume)}</div><div class="d">${a && isNum(a.vol_ratio) ? `${fmt(a.vol_ratio, 1)}× 20-day avg` : '&nbsp;'}</div></div>
      <div class="tile"><div class="k">Value · Trades</div><div class="v" style="font-size:17px">Tk ${crore(q.value_mn)}</div><div class="d">${fmtInt(q.trades)} trades</div></div>
      <div class="tile"><div class="k">52-week range</div><div class="v" style="font-size:17px">${a ? `${fmt(a.low52)} – ${fmt(a.high52)}` : '—'}</div><div class="d">${a ? range52({ ...a, ltp: price }) : ''}</div></div>
    </div>
    <div class="grid g-main">
      <div class="card">
        <div class="card-h"><h2>Price chart</h2>
          <div class="seg" id="rng">${[['3M', 63], ['6M', 126], ['1Y', 250], ['2Y', 500]].map(([l, n]) => `<button data-n="${n}" class="${S.stockRange === n ? 'on' : ''}">${l}</button>`).join('')}</div>
          <div class="seg" id="ovl">
            <button data-k="sma20"><span class="key"><i style="background:var(--s1)"></i>SMA 20</span></button>
            <button data-k="sma50"><span class="key"><i style="background:var(--s2)"></i>SMA 50</span></button>
            <button data-k="sma200"><span class="key"><i style="background:var(--s7)"></i>SMA 200</span></button>
            <button data-k="bb"><span class="key"><i class="dash"></i>Bollinger</span></button>
          </div></div>
        <div class="chart-box" id="c-main"><div class="ohlc" id="ohlc"></div></div>
        <div class="pane-label"><span class="key"><i style="background:var(--s1)"></i>RSI (14)</span><span class="muted">dashed lines: 70 overbought · 30 oversold</span></div>
        <div class="chart-box sm" id="c-rsi"></div>
        <div class="pane-label"><span class="key"><i style="background:var(--s1)"></i>MACD (12, 26, 9)</span><span class="key"><i style="background:var(--s2)"></i>Signal</span><span class="muted">bars: histogram</span></div>
        <div class="chart-box md" id="c-macd"></div>
      </div>
      <div class="grid" style="align-content:start">
        <div class="card"><div class="card-h"><h2>Technical summary</h2><span class="small muted">${a ? `${a.sessions} sessions` : ''}</span></div><div class="card-b">${scoreHtml}</div></div>
        <div class="card"><div class="card-h"><h2>Key levels</h2></div><div class="card-b">
          ${a ? `<div class="kv">
            <div><span>SMA 20</span><span>${fmt(a.sma20)}</span></div><div><span>SMA 50</span><span>${fmt(a.sma50)}</span></div>
            <div><span>SMA 200</span><span>${fmt(a.sma200)}</span></div><div><span>RSI 14</span><span>${rsiHtml(a.rsi)}</span></div>
            <div><span>Bollinger ↑</span><span>${fmt(a.bb_upper)}</span></div><div><span>Bollinger ↓</span><span>${fmt(a.bb_lower)}</span></div>
            <div><span>ATR 14</span><span>${fmt(a.atr)} <span class="muted">(${fmt(a.atr_pct, 1)}%)</span></span></div><div><span>Avg vol 20d</span><span>${compact(a.avg_vol20)}</span></div></div>
            <h3 class="mt">Pivot points (from last session)</h3>
            <div class="table-wrap"><table class="t"><thead><tr>${['S2', 'S1', 'P', 'R1', 'R2'].map(k => `<th class="r">${k}</th>`).join('')}</tr></thead>
              <tbody><tr>${['S2', 'S1', 'P', 'R1', 'R2'].map(k => `<td class="r">${fmt(a.pivots[k])}</td>`).join('')}</tr></tbody></table></div>` : '<p class="muted">—</p>'}
        </div></div>
      </div>
    </div>
    <div class="tiles">${['1w', '1m', '3m', '6m', '1y'].map(ret).join('')}</div>
    <div class="grid g3">
      <div class="card"><div class="card-h"><h2>Fundamentals</h2><span class="small muted">${info.updated_at ? 'from DSE · ' + fmtDate(info.updated_at) : ''}</span></div><div class="card-b">
        ${info.code ? `<div class="kv" style="grid-template-columns:1fr">
          <div><span>P/E (DSE)</span><span>${fmt(info.pe, 2)}</span></div>
          <div><span>Trailing P/E</span><span>${fmt(inst.trailing_pe, 2)}</span></div>
          <div><span>EPS (Tk)</span><span>${fmt(info.eps)}</span></div>
          <div><span>NAV per share</span><span>${fmt(info.nav)}</span></div>
          <div><span>Price / NAV</span><span>${fmt(pb)}</span></div>
          <div><span>Dividend yield</span><span>${isNum(info.dividend_yield) ? fmt(info.dividend_yield) + '%' : '—'}</span></div>
          <div><span>Market cap</span><span>${isNum(info.market_cap) ? 'Tk ' + fmt(info.market_cap / 1e7, 0) + ' cr' : '—'}</span></div>
          <div><span>Free float</span><span>${isNum(info.free_float) ? fmt(info.free_float, 1) + '%' : '—'}</span></div>
          <div><span>Paid-up capital</span><span>${isNum(info.paid_up_capital) ? 'Tk ' + fmt(info.paid_up_capital / 1e7, 1) + ' cr' : '—'}</span></div>
          <div><span>Face value</span><span>${fmt(info.face_value)}</span></div>
          <div><span>Listed since</span><span>${esc(info.listing_year || '—')}</span></div>
          <div><span>Year end · last AGM</span><span>${esc(info.year_end || '—')} · ${esc(ex.agmDate || '—')}</span></div></div>`
          : '<p class="muted small">Fundamentals load from the DSE company page. Use ↻ Update.</p>'}
      </div></div>
      <div class="card"><div class="card-h"><h2>Earnings &amp; dividends</h2></div><div class="card-b flush">
        ${fin.length ? `<div class="table-wrap"><table class="t"><thead><tr><th>Year</th><th class="r">EPS</th><th class="r">NAV</th><th class="r">Profit (mn)</th><th class="r">P/E</th></tr></thead>
          <tbody>${fin.map(f => `<tr><td>${esc(f.year)}</td><td class="r">${fmt(f.epsBasic)}</td><td class="r">${fmt(f.nav)}</td><td class="r">${fmt(f.profit, 1)}</td><td class="r">${fmt(f.pe, 1)}</td></tr>`).join('')}</tbody></table></div>` : ''}
        ${divs.length ? `<div class="table-wrap"><table class="t"><thead><tr><th>Dividend year</th><th class="r">Cash %</th><th class="r">Stock %</th><th class="r">Yield</th></tr></thead>
          <tbody>${divs.map(v => `<tr><td>${esc(v.year)}</td><td class="r">${fmt(v.cash, 1)}</td><td class="r">${fmt(v.stock, 1)}</td><td class="r">${isNum(v.yieldPct) ? fmt(v.yieldPct) + '%' : '—'}</td></tr>`).join('')}</tbody></table></div>` : ''}
        ${!fin.length && !divs.length ? '<div class="card-b muted small">No financial history available.</div>' : ''}
      </div></div>
      <div class="card"><div class="card-h"><h2>Shareholding</h2><span class="small muted">${pattern ? esc(pattern.date) : ''}</span></div><div class="card-b">
        ${pattern && pattern.pattern ? `<div class="stack" role="img" aria-label="Shareholding pattern">${SH.map(([k, , c]) => `<div style="width:${pattern.pattern[k] || 0}%;background:${c}" title="${k}: ${pattern.pattern[k] || 0}%"></div>`).join('')}</div>
          <div class="legend">${SH.map(([k, l, c]) => `<span><i style="background:${c}"></i>${l} <b>${fmt(pattern.pattern[k] || 0, 2)}%</b></span>`).join('')}</div>` : '<p class="muted small">Not available.</p>'}
        <h3 class="mt">Your position</h3>
        ${pos ? `<div class="kv" style="grid-template-columns:1fr">
            <div><span>Shares</span><span>${fmtInt(pos.qty)}</span></div><div><span>Average cost</span><span>${tk(pos.avg_cost)}</span></div>
            <div><span>Market value</span><span>${tk(pos.market_value, 0)}</span></div>
            <div><span>Unrealised P/L</span><span>${tkSigned(pos.unrealized)} (${chg(pos.unrealized_pct)})</span></div></div>` : '<p class="muted small">You don\'t hold this stock.</p>'}
        ${d.transactions.length ? `<ul class="list-plain small">${d.transactions.slice(0, 6).map(t => `<li class="sp"><span>${fmtDate(t.trade_date)} · <b class="${t.side === 'BUY' ? 'up' : 'down'}">${t.side}</b> ${fmtInt(t.qty)} @ ${fmt(t.price)}</span><button class="x-btn" data-del-tx="${t.id}" title="Delete transaction" aria-label="Delete transaction">✕</button></li>`).join('')}</ul>` : ''}
        <h3 class="mt">Alerts</h3>
        ${d.alerts.length ? `<ul class="list-plain small">${d.alerts.map(al => `<li class="sp"><span>${esc(al.description)} ${al.active ? '' : '<span class="muted">(paused)</span>'}</span><button class="x-btn" data-del-alert="${al.id}" title="Delete alert" aria-label="Delete alert">✕</button></li>`).join('')}</ul>` : '<p class="muted small">No alerts. <a href="#" id="s-alert2">Create one</a>.</p>'}
      </div></div>
    </div>`);

  $('#s-watch').addEventListener('click', async e => {
    const b = e.currentTarget;
    const on = b.dataset.on === '1';
    try {
      await (on ? api('/watchlist/' + encodeURIComponent(code), { method: 'DELETE' }) : api('/watchlist', { method: 'POST', body: { code } }));
      b.dataset.on = on ? '0' : '1';
      b.classList.toggle('on', !on);
      b.textContent = `★ ${on ? 'Watch' : 'Watching'}`;
      S.quotes = null;
    } catch (err) { toast(err.message, 'err'); }
  });
  $('#s-alert').addEventListener('click', () => openAlertModal({ code, kind: 'price_above', threshold: price ? Math.round(price * 1.05 * 10) / 10 : '' }));
  const a2 = $('#s-alert2');
  if (a2) a2.addEventListener('click', e => { e.preventDefault(); openAlertModal({ code, kind: 'price_above' }); });
  $('#s-tx').addEventListener('click', () => openTxModal({ code, price }));
  $('#s-refresh').addEventListener('click', async e => {
    const b = e.currentTarget;
    b.disabled = true;
    b.textContent = 'Updating…';
    try { const r = await api(`/stock/${encodeURIComponent(code)}/refresh`, { method: 'POST' }); toast(`Downloaded ${r.rows} sessions for ${code}`); rerender(); }
    catch (err) { toast(err.message, 'err'); b.disabled = false; b.textContent = '↻ Update'; }
  });
  mountStockCharts(d);
}

function mountStockCharts(d) {
  const n = d.candles.length;
  if (!n) { $('#c-main').innerHTML = empty('No price history yet'); return; }
  const c = colors();
  const main = makeChart($('#c-main'), { timeScale: { visible: false } });
  const candle = main.addCandlestickSeries({ upColor: c.up, downColor: c.down, wickUpColor: c.up, wickDownColor: c.down, borderVisible: false, priceFormat: { type: 'price', precision: 2, minMove: 0.01 } });
  candle.setData(d.candles);
  main.priceScale('right').applyOptions({ scaleMargins: { top: 0.1, bottom: 0.24 } });
  const vol = main.addHistogramSeries({ priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false });
  main.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  vol.setData(d.volumes.map(v => ({ time: v.time, value: v.value, color: alpha(v.up ? c.up : c.down, 0.4) })));
  const line = (data, color, extra = {}) => {
    const s = main.addLineSeries({ color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, ...extra });
    s.setData(data);
    return s;
  };
  const ov = {
    sma20: [line(d.series.sma20, c.s1)],
    sma50: [line(d.series.sma50, c.s2)],
    sma200: [line(d.series.sma200, c.s7)],
    bb: [line(d.series.bb_upper, c.muted, { lineWidth: 1, lineStyle: LWC.LineStyle.Dashed }), line(d.series.bb_lower, c.muted, { lineWidth: 1, lineStyle: LWC.LineStyle.Dashed })],
  };
  const applyOverlays = () => {
    for (const [k, series] of Object.entries(ov)) series.forEach(s => s.applyOptions({ visible: !!S.overlays[k] }));
    $$('#ovl button').forEach(b => b.classList.toggle('on', !!S.overlays[b.dataset.k]));
  };
  applyOverlays();
  $$('#ovl button').forEach(b => b.addEventListener('click', () => { S.overlays[b.dataset.k] = !S.overlays[b.dataset.k]; applyOverlays(); }));

  const rsiC = makeChart($('#c-rsi'), { timeScale: { visible: false }, rightPriceScale: { scaleMargins: { top: 0.04, bottom: 0.04 } } });
  const rsiS = rsiC.addLineSeries({ color: c.s1, lineWidth: 2, priceLineVisible: false, autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }) });
  rsiS.setData(d.series.rsi);
  [70, 30].forEach(p => rsiS.createPriceLine({ price: p, color: c.muted, lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true }));

  const macdC = makeChart($('#c-macd'));
  const hist = macdC.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false, priceFormat: { type: 'price', precision: 2, minMove: 0.01 } });
  hist.setData(d.series.macd_hist.map(p => (p.value == null ? { time: p.time } : { time: p.time, value: p.value, color: alpha(p.value >= 0 ? c.up : c.down, 0.5) })));
  const macdLine = macdC.addLineSeries({ color: c.s1, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  macdLine.setData(d.series.macd);
  const sigLine = macdC.addLineSeries({ color: c.s2, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  sigLine.setData(d.series.macd_signal);

  // keep the three panes scrolled/zoomed together
  const charts = [main, rsiC, macdC];
  let syncing = false;
  charts.forEach(src => src.timeScale().subscribeVisibleLogicalRangeChange(r => {
    if (!r || syncing) return;
    syncing = true;
    charts.forEach(dst => { if (dst !== src) dst.timeScale().setVisibleLogicalRange(r); });
    syncing = false;
  }));

  // hover: OHLC legend + crosshair mirrored into the indicator panes
  const legend = i => {
    const b = d.candles[i];
    const prev = i > 0 ? d.candles[i - 1].close : null;
    const p = prev ? (b.close / prev - 1) * 100 : null;
    const val = k => { const pt = d.series[k][i]; return pt && pt.value != null ? fmt(pt.value) : '—'; };
    $('#ohlc').innerHTML = `<span>${fmtDate(b.time, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' })}</span><span>O <b>${fmt(b.open)}</b></span><span>H <b>${fmt(b.high)}</b></span><span>L <b>${fmt(b.low)}</b></span><span>C <b>${fmt(b.close)}</b></span><span class="${dir(p)}">${fmtPct(p)}</span><span>Vol <b>${compact(d.volumes[i].value)}</b></span>${S.overlays.sma20 ? `<span>SMA20 <b>${val('sma20')}</b></span>` : ''}${S.overlays.sma50 ? `<span>SMA50 <b>${val('sma50')}</b></span>` : ''}${S.overlays.sma200 ? `<span>SMA200 <b>${val('sma200')}</b></span>` : ''}<span>RSI <b>${val('rsi')}</b></span>`;
  };
  legend(n - 1);
  main.subscribeCrosshairMove(p => {
    let i = p && p.logical != null ? Math.round(p.logical) : null;
    if (i == null || i < 0 || i >= n || !p.point) i = null;
    legend(i == null ? n - 1 : i);
    if (i == null) { rsiC.clearCrosshairPosition(); macdC.clearCrosshairPosition(); return; }
    const t = d.candles[i].time;
    const rv = d.series.rsi[i].value, mv = d.series.macd[i].value;
    if (rv != null) rsiC.setCrosshairPosition(rv, t, rsiS); else rsiC.clearCrosshairPosition();
    if (mv != null) macdC.setCrosshairPosition(mv, t, macdLine); else macdC.clearCrosshairPosition();
  });

  const setRange = bars => { main.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - bars) - 0.5, to: n + 1.5 }); };
  setRange(S.stockRange);
  $$('#rng button').forEach(b => b.addEventListener('click', () => {
    S.stockRange = Number(b.dataset.n);
    $$('#rng button').forEach(x => x.classList.toggle('on', x === b));
    setRange(S.stockRange);
  }));
}

/* ---------------- prediction */
const NEWS_TAG_CLASS = { earnings: '', dividend: 'pos', no_dividend: 'neg', negative: 'neg', positive: 'pos', record_date: 'warn', spot: 'warn', price_limit: 'warn' };
function newsListHtml(items, tagLabels = {}, showCode = false) {
  if (!items || !items.length) return '<p class="muted small">No announcements stored yet.</p>';
  return `<ul class="list-plain">${items.map(n => {
    const tone = isNum(n.tone) && n.tone !== 0 ? `<span class="chip ${n.tone > 0 ? 'pos' : 'neg'}" title="Tone score used by the model">${n.tone > 0 ? '▲ positive' : '▼ negative'}</span>` : '';
    const eps = isNum(n.eps_growth) ? `<span class="chip ${n.eps_growth > 0 ? 'pos' : n.eps_growth < 0 ? 'neg' : ''}">EPS ${fmtPct(n.eps_growth * 100, 0)} vs last year</span>` : '';
    const ai = isNum(n.ai_impact) && (n.ai_reason || '') !== 'Routine notice'
      ? `<span class="chip ${n.ai_impact > 0 ? 'pos' : n.ai_impact < 0 ? 'neg' : ''}" title="${esc(n.ai_reason || '')}">AI ${n.ai_impact > 0 ? '+' : ''}${n.ai_impact}</span>` : '';
    const tags = (n.tags || []).filter(t => t !== 'earnings').map(t => `<span class="chip ${NEWS_TAG_CLASS[t] || ''}">${esc(tagLabels[t] || t)}</span>`).join('');
    return `<li><div class="sp" style="align-items:flex-start"><div>${showCode ? `${stockLink(n.code)} ` : ''}<b>${esc(n.summary)}</b>
        <div class="small muted">${fmtDate(n.news_date, { day: 'numeric', month: 'short', year: 'numeric' })}${n.news_time ? ' · ' + esc(n.news_time) : ''}${n.type ? ' · ' + esc(n.type) : ''}${n.record_date ? ` · record date ${fmtDate(n.record_date, { day: 'numeric', month: 'short' })}` : ''}</div></div>
        <div class="chips" style="justify-content:flex-end">${ai}${eps}${tone}${tags}</div></div>
      ${n.ai_reason && n.ai_reason !== 'Routine notice' ? `<div class="small muted">AI: ${esc(n.ai_reason)}</div>` : ''}
      ${n.body && n.body !== n.summary ? `<details class="small mt"><summary class="muted" style="cursor:pointer">Full text</summary><p style="white-space:pre-wrap;margin:6px 0 0">${esc(n.body)}</p></details>` : ''}</li>`;
  }).join('')}</ul>`;
}

function inputGroupsHtml(hz) {
  const blocks = ['5', '20'].map(h => [h, (hz[h] || {}).input_groups || []]).filter(([, g]) => g.length);
  if (!blocks.length) return '';
  return `<h3 class="mt">What each input group adds (tested on unseen data)</h3>
    <p class="small muted">The model is re-tested with each group removed. Positive = ranks stocks better with this group; negative = the group added noise in these tests. Ranking skill is the rank correlation between forecast and actual return (0 = none).</p>
    <div class="grid g2">${blocks.map(([h, groups]) => `<div class="table-wrap"><table class="t"><thead><tr><th>${esc(hz[h].label)}</th><th class="r">Inputs</th><th class="r">Ranking skill</th><th class="r">Direction</th></tr></thead><tbody>
      ${groups.map(g => `<tr><td>${esc(g.label)}</td><td class="r">${g.inputs}</td><td class="r"><span class="${dir(g.adds_rank_ic)}">${fmtSigned(g.adds_rank_ic, 3)}</span></td><td class="r">${fmtSigned(g.adds_accuracy, 1)} pts</td></tr>`).join('')}
    </tbody></table></div>`).join('')}</div>`;
}

const pct0 = p => (isNum(p) ? `${Math.round(p * 100)}%` : '—');
const timeKey = t => (typeof t === 'string' ? t : t && t.year ? `${t.year}-${pad2(t.month)}-${pad2(t.day)}` : String(t));
const fromClose = (v, close) => (isNum(v) && close ? fmtPct((v / close - 1) * 100, 1) : '—');

function reliabilityTable(hz, live) {
  const rows = Object.entries(hz).filter(([, v]) => v.available);
  if (!rows.length) return '<p class="muted small">No test results yet.</p>';
  const cell = (hit, target, width) => (isNum(hit) ? `${fmt(hit, 0)}% <span class="muted small">/ ${target}</span>${isNum(width) ? `<div class="muted small">width ${fmt(width, 1)}%</div>` : ''}` : '—');
  return `<div class="table-wrap"><table class="t"><thead><tr><th>Horizon</th><th class="r" title="Price ended inside the 50% range; target 50%">Inside 50% range</th><th class="r" title="Target 68%">Inside 68% range</th><th class="r" title="Target 90%">Inside 90% range</th>
      <th class="r">Direction model right</th><th class="r" title="Always guessing the most common direction in the same test windows">Always-same-guess</th><th class="r">Live forecasts checked</th><th>Status</th></tr></thead><tbody>
    ${rows.map(([h, v]) => {
      const b = v.backtest || {};
      const l = (live || {})[h] || {};
      return `<tr><td>${esc(v.label)}</td>
        <td class="r">${cell(b.range50_hit, 50, b.width50)}</td>
        <td class="r">${cell(b.range68_hit, 68, b.width68)}</td>
        <td class="r">${cell(b.range90_hit, 90, b.width90)}</td>
        <td class="r">${isNum(b.direction_accuracy) ? fmt(b.direction_accuracy, 0) + '%' : '—'}</td>
        <td class="r">${isNum(b.naive_accuracy) ? fmt(b.naive_accuracy, 0) + '%' : '—'}</td>
        <td class="r">${l.checked ? `${fmtInt(l.checked)} <span class="muted small">(range ${fmt(l.range68_hit, 0)}%, direction ${fmt(l.direction_accuracy, 0)}%)</span>` : '<span class="muted small">builds up daily</span>'}</td>
        <td>${v.extrapolated ? '<span class="label">Estimate (untestable yet)</span>' : v.reliable ? '<span class="label b">Direction reliable</span>' : b.range68_hit >= 60 ? '<span class="label">Ranges only</span>' : '<span class="label be">Low confidence</span>'}</td></tr>`;
    }).join('')}</tbody></table></div>`;
}

const RANGE_NAMES = { 30: 'Core range', 50: 'Most likely range', 68: 'Likely range', 90: 'Wide range' };
const sigBadge = (s, big = false) => (s ? `<span class="sig ${esc(s).replace(' ', '_')}${big ? ' big' : ''}">${esc(s)}</span>` : '');
const SIGNAL_HELP = {
  BUY: 'Uptrend pullback with reward:risk ≥ 1.5 and positive historical expected value',
  ACCUMULATE: 'Quality/value share suited to buying a fixed amount regularly',
  HOLD: 'You hold it and the trend is intact; keep the trailing stop',
  'TAKE PROFIT': 'You hold it and it is stretched (at target / RSI > 75 / above upper band)',
  SELL: 'You hold it: trend stop hit (not a quality share) or strong-evidence bad news such as a Z downgrade',
  REDUCE: 'You hold it and the trend is down: consider selling part',
  AVOID: 'Downtrend, seriously negative news or too illiquid',
  WAIT: 'No clear setup right now',
};
function mdLite(text) {
  const lines = esc(text || '').replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').split(/\r?\n/);
  let html = '', inList = false;
  for (const raw of lines) {
    const l = raw.trim();
    if (/^[-*•]\s+/.test(l)) {
      if (!inList) { html += '<ul>'; inList = true; }
      html += `<li>${l.replace(/^[-*•]\s+/, '')}</li>`;
    } else {
      if (inList) { html += '</ul>'; inList = false; }
      if (l) html += `<p>${l}</p>`;
    }
  }
  return html + (inList ? '</ul>' : '');
}

function tradePlanHtml(one) {
  const p = one.plan;
  if (!p) return '';
  const rec = one.buy_record || {};
  const cell = (k, v, d = '') => `<div class="cell"><div class="k">${k}</div><div class="v">${v}</div>${d ? `<div class="d">${d}</div>` : ''}</div>`;
  const pct = x => (isNum(x) ? fmtPct(x, 1) : '');
  const hold = p.holding;
  const sizeCell = isNum(p.shares) && p.shares > 0
    ? cell('Position size', `${fmtInt(p.shares)} shares`, `≈ Tk ${fmtInt(p.position_value)} · loses ≈ Tk ${fmtInt(p.risk_amount)} if stopped`)
    : cell('Position size', '—', '<a href="#/settings">Set your capital &amp; risk % in Settings</a>');
  const intr = one.intraday;
  return `<div class="card"><div class="card-h"><h2>Trade plan</h2>${sigBadge(p.signal, true)}<span class="small muted">${esc(SIGNAL_HELP[p.signal] || '')}</span></div>
    <div class="card-b">
      <ul class="reasons" style="margin:0 0 12px">${p.reasons.map(r => `<li><span class="pts">•</span><span>${esc(r)}</span></li>`).join('')}</ul>
      ${hold ? `<div class="note" style="margin-bottom:12px"><b>You hold ${fmtInt(hold.qty)} shares</b> at avg ${fmt(hold.avg_cost)} (${chg(hold.unrealized_pct)}). Trailing stop: <b>${fmt(hold.trailing_stop)}</b>: sell if the price closes below it.</div>` : ''}
      ${intr ? `<div class="note" style="margin-bottom:12px"><b>Live:</b> today's close is likely <b>${fmt(intr.low50)} – ${fmt(intr.high50)}</b> (50%), almost surely ${fmt(intr.low90)} – ${fmt(intr.high90)} (90%). Price now ${fmt(intr.ltp)}, ${intr.time_left_pct}% of the session left: the range narrows as the close approaches.</div>` : ''}
      <div class="plan-grid">
        ${cell('Buy zone (limit)', p.buy_zone ? `${fmt(p.buy_zone[0])} – ${fmt(p.buy_zone[1])}` : '—', 'next session: ~50% / ~70% chance of a fill')}
        ${cell('Stop-loss', `<span class="down">${fmt(p.stop)}</span>`, `${pct(p.stop_pct)} · price − 1.5 × ATR`)}
        ${cell('Target 1 (≈1 week)', `<span class="up">${fmt(p.t1)}</span>`, `${pct(p.t1_pct)} · reward:risk 1:${fmt(p.rr1, 1)}`)}
        ${cell('Target 2 (≈1 month)', `<span class="up">${fmt(p.t2)}</span>`, `${pct(p.t2_pct)} · reward:risk 1:${fmt(p.rr2, 1)}`)}
        ${cell('Historical win rate', isNum(p.win_rate) ? `${fmt(p.win_rate, 0)}%` : '—', isNum(p.stop_rate) ? `T2 before stop · stop first ${fmt(p.stop_rate, 0)}% (20 sessions)` : '')}
        ${cell('Expected value', isNum(p.ev_pct) ? `<span class="${dir(p.ev_pct)}">${fmtPct(p.ev_pct, 2)}</span>` : '—', 'average result per trade with this stop/target')}
        ${cell('Sell zone (limit)', p.sell_zone ? `${fmt(p.sell_zone[0])} – ${fmt(p.sell_zone[1])}` : '—', 'next session: ~50% / ~30% chance price reaches it')}
        ${cell('Support · Resistance', `${fmt(p.support)} · ${fmt(p.resistance)}`, '20-day low · 20-day high')}
        ${sizeCell}
      </div>
      ${planDatesHtml(p)}
      <p class="small muted mt">Win rate and expected value come from every liquid DSE share in the past year: how often a rise of this size (in units of the share's own volatility) came before a fall of this size. The BUY rule's own record: ${rec.trades ? `${fmtInt(rec.trades)} past trades, target first ${fmt(rec.win_rate, 0)}%, stopped ${fmt(rec.stop_rate, 0)}%, average ${fmtPct(rec.avg_trade, 2)} per trade vs ${fmtPct(rec.baseline_trade, 2)} for random entries (about ${fmt(rec.avg_days, 0)} sessions held).` : 'building up.'}</p>
    </div></div>`;
}

function planDatesHtml(p) {
  const d = p.dates;
  if (!d) return '';
  const D = x => fmtDate(x, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
  const R = r => (r ? `${fmt(r[0])} – ${fmt(r[1])}` : '—');
  const s = d.short, l = d.long;
  return `<div class="grid g2 mt" style="gap:12px">
    <div class="note"><b>Short-term trade · ${fmtDate(s.from, { day: 'numeric', month: 'short' })} – ${fmtDate(s.to, { day: 'numeric', month: 'short', year: 'numeric' })}</b>
      <ul class="steps" style="margin:6px 0 0;padding-left:18px">
        <li>Buy: next session, <b>${D(s.entry)}</b>, in the buy zone ${p.buy_zone ? `${fmt(p.buy_zone[0])} – ${fmt(p.buy_zone[1])}` : ''}; place the stop at <b>${fmt(p.stop)}</b></li>
        <li>Target 1 <b>${fmt(p.t1)}</b> by about <b>${D(s.t1_by)}</b> (1 week; likely range ${R(s.range_1w)})</li>
        <li>Target 2 <b>${fmt(p.t2)}</b> by about <b>${D(s.t2_by)}</b> (1 month; likely range ${R(s.range_1m)})</li>
        <li>If neither the stop nor Target 2 is hit by <b>${D(s.exit_by)}</b>, exit or re-check the plan${s.typical_exit ? `. Past trades of this kind ended after about ${fmt(s.typical_hold_sessions, 0)} sessions (around ${D(s.typical_exit)})` : ''}</li>
      </ul></div>
    <div class="note"><b>Long-term (regular buying) · ${fmtDate(l.from, { month: 'short', year: 'numeric' })} – ${fmtDate(l.to, { month: 'short', year: 'numeric' })}</b>
      <ul class="steps" style="margin:6px 0 0;padding-left:18px">
        <li>Accumulate in equal parts from <b>${D(l.accumulate_from)}</b> to <b>${D(l.accumulate_to)}</b> (about 3 months) instead of buying all at once</li>
        <li>3 months (${D(l.accumulate_to)}): likely ${R(l.range_3m)}</li>
        <li>Review on <b>${D(l.review)}</b> (6 months): likely ${R(l.range_6m)} <span class="muted">(estimate)</span></li>
        <li>1-year horizon <b>${D(l.horizon)}</b>: likely ${R(l.range_1y)}, wider ${R(l.range_1y_68)} (50% / 68%, <span class="muted">estimate</span>)</li>
      </ul>
      <div class="small muted mt">${p.long_score != null ? `Long-term quality/value score: <b>${fmt(p.long_score, 0)}/100</b>${p.long_score >= 70 ? ' (suited to regular buying)' : ''}.` : 'Not in the long-term universe (needs category A, profitable, liquid).'}</div></div>
  </div>`;
}

const AREA_LABEL = { governance: 'Governance', financial_health: 'Financial health', growth: 'Growth', dividend_reliability: 'Dividend reliability', news_flow: 'News flow', political_exposure: 'Political / state risk' };
function aiProfileBody(p, available) {
  return p ? `<div class="hbars">${Object.keys(AREA_LABEL).map(k => {
      if (!isNum(p[k])) return `<div class="hbar" style="grid-template-columns:140px minmax(0,1fr) 40px"><span class="n">${AREA_LABEL[k]}</span><span class="small muted">not scored yet: re-check</span><span class="val">—</span></div>`;
      const v = p[k], w = Math.abs(v) / 2 * 50;
      const style = v >= 0 ? `left:50%;width:${w}%` : `left:${50 - w}%;width:${w}%`;
      return `<div class="hbar" style="grid-template-columns:140px minmax(0,1fr) 40px"><span class="n">${AREA_LABEL[k]}</span><div class="track"><div class="axis0"></div><div class="fill ${v >= 0 ? 'pos' : 'neg'}" style="${style}"></div></div><span class="val ${dir(v)}">${v > 0 ? '+' : ''}${v}</span></div>`;
    }).join('')}</div>
    <div class="grid g2 mt" style="gap:12px">
      <div><h3>Positives</h3><ul class="steps" style="padding-left:18px;margin:4px 0">${(p.positives || []).map(x => `<li>${esc(x)}</li>`).join('') || '<li class="muted">none listed</li>'}</ul></div>
      <div><h3>Risks</h3><ul class="steps" style="padding-left:18px;margin:4px 0">${(p.risks || []).map(x => `<li>${esc(x)}</li>`).join('') || '<li class="muted">none listed</li>'}</ul></div>
    </div>
    ${p.political_capped ? `<p class="small muted mt">The model scored political / state risk ${p.political_capped.model_said > 0 ? '+' : ''}${p.political_capped.model_said}; the filings (takeover, write-off or court order) cap it at ${p.political_capped.cap}.</p>` : ''}
    <p class="mt">${esc(p.summary || '')}</p>
    <p class="small muted">${esc(p.model || '')} · ${fmtDate(p.as_of)} · scores −2 (serious problems) to +2 (clearly strong), from the company's DSE announcements and financial history</p>`
    : `<p class="muted small">${available ? 'No health check yet. It runs automatically for your holdings, watchlist and signals; or click "Check now" (about 2–3 minutes).' : 'Local AI not available: start Ollama.'}</p>`;
}
function aiProfileHtml(p, code, available) {
  return `<div class="card"><div class="card-h"><h2>AI company health check</h2><span class="small muted">local LLM reading the filings · not back-tested</span>
      <button class="btn btn-sm" id="ai-prof" data-code="${esc(code)}" ${available ? '' : 'disabled'}>${p ? 'Re-check' : 'Check now'}</button></div>
    <div class="card-b" id="ai-prof-out">${aiProfileBody(p, available)}</div></div>`;
}

function aiCardHtml(one) {
  const ai = one.ai || {};
  const n = one.ai_note;
  return `<div class="card"><div class="card-h"><h2>AI analyst note</h2><span class="small muted">local Ollama · ${esc(ai.model || '')} · written from the numbers on this page</span>
      <button class="btn btn-sm" id="ai-gen" ${ai.available ? '' : 'disabled'}>${n ? 'Rewrite' : 'Write note'}</button></div>
    <div class="card-b"><div id="ai-out" class="ai-note">${n ? mdLite(n.note) + `<p class="small muted">${esc(n.model || '')} · ${fmtTime(n.created_at)}</p>` : ai.available ? '<p class="muted small">Click "Write note" for a plain-language summary (takes about 30 seconds on your PC).</p>' : `<p class="muted small">Local AI not available${ai.error ? ': ' + esc(ai.error) : ''}. Start Ollama with the ${esc(ai.model || 'gemma3:4b')} model.</p>`}</div>
    <p class="small muted">The AI only explains; it does not make the ranges or signals. Small local models can misread facts, so check the numbers above.</p></div></div>`;
}
const band = (h, level) => [h['low' + level] ?? h.low68, h['high' + level] ?? h.high68];

function horizonTile(h, close) {
  const bt = h.backtest || {};
  const lvl = S.rangeLevel;
  const [lo, hi] = band(h, lvl);
  const hit = bt[`range${lvl}_hit`];
  const weak = isNum(hit) && hit < lvl - 12;
  const others = [50, 68, 90].filter(x => x !== lvl).map(x => { const [a, b] = band(h, x); return `${x}%: ${fmt(a)} – ${fmt(b)}`; }).join(' · ');
  const bd = h.bd || {}, ct = h.combined_test || {};
  const bdApplied = bd.scale > 0 && Math.abs(bd.shift_pct || 0) >= 0.05;
  const bdLine = !h.bd ? ''
    : bdApplied
      ? `<div class="d small"><span class="${bd.shift_pct >= 0 ? 'up' : 'down'}">${arrow(bd.shift_pct)}Bangladesh factors move it ${fmtPct(bd.shift_pct, 1)}</span> <span class="muted">(base midpoint ${fmt(bd.base_mid)}; in tests the push was right ${fmt(((ct.tests || {})['1.0'] || {}).right_way, 0)}% of the time)</span></div>`
      : Math.abs(bd.bd_pct || 0) >= 0.05
        ? `<div class="d small muted">Bangladesh factors measured ${fmtPct(bd.bd_pct, 1)}, not applied here: ${esc(ct.reason || 'not proven at this horizon')}</div>`
        : '<div class="d small muted">No active Bangladesh factor moves this share</div>';
  const dirLine = h.extrapolated
    ? '<span class="muted">Estimate: 3-month range extended with √time; cannot be tested with one year of history</span>'
    : h.reliable
      ? `<span class="${h.prob_up >= 0.5 ? 'up' : 'down'}">${arrow(h.prob_up - 0.5)}${pct0(h.prob_up)} chance of rising</span>`
      : '<span class="muted">Statistical direction: no reliable signal</span>';
  const dateFmt = h.h >= 60 ? { day: 'numeric', month: 'short', year: 'numeric' } : { weekday: 'short', day: 'numeric', month: 'short' };
  return `<div class="tile">
    <div class="k"><span>${esc(h.label)}</span><span class="muted">${h.target_date ? fmtDate(h.target_date, dateFmt) : ''}</span></div>
    <div class="v range num">${fmt(lo)} – ${fmt(hi)}</div>
    <div class="d">${RANGE_NAMES[lvl]} (${lvl}%) · ${fromClose(lo, close)} to ${fromClose(hi, close)}</div>
    <div class="d small">${isNum(hit) ? `Held ${fmt(hit, 0)}% of the time in tests · ` : h.extrapolated ? '<span class="chip warn">Estimate</span> ' : ''}midpoint ${fmt(h.median)}</div>
    <div class="d small muted">${others}</div>
    ${bdLine}
    <div class="d">${dirLine}</div>
    ${isNum(h.hist_up) ? `<div class="d muted small">Past year: higher after ${h.h === 1 ? 'one session' : esc(h.label.toLowerCase())} ${pct0(h.hist_up)} of the time</div>` : ''}
    ${weak ? `<div class="d"><span class="chip warn" title="In tests the price ended inside this ${lvl}% range only ${fmt(hit, 0)}% of the time">Low confidence</span></div>` : ''}
  </div>`;
}

function termRow(key, title, span, hs, close) {
  if (!hs.length) return '';
  const when = span ? `${fmtDate(span.from, { day: 'numeric', month: 'short', year: 'numeric' })} – ${fmtDate(span.to, { day: 'numeric', month: 'short', year: 'numeric' })}` : '';
  return `<div class="sp" style="margin-top:4px"><h2>${title} <span class="muted small" style="font-weight:500">· ${when}</span></h2>
      <span class="small muted">${key === 'short' ? 'for trading: next session to about one month' : 'for investing: 3 months to 1 year (6-month and 1-year ranges are estimates)'}</span></div>
    <div class="tiles">${hs.map(h => horizonTile(h, close)).join('')}</div>`;
}

function combinedHtml(fc) {
  const hs = fc.horizons.filter(h => !h.extrapolated && h.bd);
  if (!hs.length) return '';
  const close = fc.close;
  // one row per factor (company events by kind), in a fixed order
  const order = ['event', 'record', 'regime', 'spec', 'state', 'politics'];
  const rows = new Map();
  hs.forEach(h => (h.bd.parts || []).forEach(p => {
    const id = p.key + (p.kind ? ':' + p.kind : '');
    if (!rows.has(id)) rows.set(id, { p, by: {} });
    rows.get(id).by[h.h] = p;
  }));
  const sorted = [...rows.values()].sort((a, b) => order.indexOf(a.p.key) - order.indexOf(b.p.key));
  const noteShort = n => !n ? '' : n.startsWith('overlaps') ? 'overlaps a stronger event: not added' : n;
  const cell = p => !p ? '<td class="r muted">—</td>'
    : `<td class="r" title="Measured ${fmtPct(p.raw_pct, 2)} before the evidence weight ${isNum(p.w) ? fmt(p.w, 2) : '0'}">${p.used ? chg(p.pct, 2) : `<span class="muted">${fmtPct(p.pct, 2)}</span>`}<div class="small muted">measured ${fmtPct(p.raw_pct, 1)}</div>${p.note ? `<div class="small muted">${esc(noteShort(p.note))}</div>` : ''}</td>`;
  const th = hs.map(h => `<th class="r">${esc(h.label)}<div class="small muted" style="font-weight:400">${h.target_date ? fmtDate(h.target_date, { day: 'numeric', month: 'short' }) : ''}</div></th>`).join('');
  const pctOf = v => (isNum(v) && close ? (v / close - 1) * 100 : null);
  return `<div class="card"><div class="card-h"><h2>Combined prediction: how every factor adds up</h2><span class="small muted">base model + statistical model + Bangladesh factors · each part measured on DSE history</span></div>
    <div class="table-wrap"><table class="t"><thead><tr><th>Factor</th><th>Now</th>${th}<th>Evidence</th></tr></thead><tbody>
      <tr><td><b>Base model</b><div class="small muted">volatility model + measured shape of DSE moves</div></td><td class="small">last close ${fmt(close)}</td>
        ${hs.map(h => `<td class="r">${fmt(h.bd.base_mid)}<div class="small muted">${fmtPct(pctOf(h.bd.base_mid), 1)}</div></td>`).join('')}<td class="small muted">ranges held as often as promised in tests</td></tr>
      <tr><td>Statistical model (68 inputs)</td><td class="small">${hs.some(h => h.bd.model && h.bd.model.used) ? 'passed the reliability test' : '<span class="muted">failed the reliability test: not used</span>'}</td>
        ${hs.map(h => cell({ pct: h.bd.model ? h.bd.model.pct : 0, raw_pct: h.bd.model ? h.bd.model.pct : 0, used: h.bd.model && h.bd.model.used, w: h.bd.model && h.bd.model.used ? 1 : 0 })).join('')}<td class="small muted">gate: rank-IC, AUC, top-10</td></tr>
      ${sorted.map(({ p, by }) => `<tr><td>${esc(p.label)}</td><td class="small wrap">${esc(p.state || '')}</td>${hs.map(h => cell(by[h.h])).join('')}
        <td>${evBadge(p.evidence)}<div class="small muted">${isNum(p.n) ? `${fmtInt(p.n)} past cases` : ''}${isNum(p.t) ? ` · t ${fmt(p.t, 1)}` : ''}${isNum(p.w) ? ` · weight ${fmt(p.w, 2)}` : ''}</div></td></tr>`).join('')
        || `<tr><td colspan="${hs.length + 3}" class="muted small">No active Bangladesh factor for this share right now (no recent company event, no record date ahead, normal pump risk, not state-owned).</td></tr>`}
      <tr><td><b>Bangladesh factors total</b></td><td class="small muted">sum of the used parts</td>${hs.map(h => `<td class="r">${chg(h.bd.bd_pct, 2)}</td>`).join('')}<td></td></tr>
      ${hs.some(h => Math.abs(h.bd.politics_pct || 0) > 0) ? `<tr><td><b>Your political records added</b></td><td class="small muted">switched on in Settings · outside the test</td>${hs.map(h => `<td class="r">${chg(h.bd.politics_pct, 2)}</td>`).join('')}<td></td></tr>` : ''}
      <tr><td><b>Strength applied</b></td><td class="small muted">chosen daily on unseen data (Bangladesh factors)</td>${hs.map(h => { const c = h.combined_test || {}; return `<td class="r" title="${esc(c.reason || '')}"><b>${fmt((h.bd.scale || 0) * 100, 0)}%</b></td>`; }).join('')}<td></td></tr>
      <tr class="total"><td><b>Final midpoint</b></td><td class="small muted">base × e<sup>shift</sup></td>${hs.map(h => `<td class="r"><b>${fmt(h.median)}</b><div class="small">${chg(pctOf(h.median), 1)}</div></td>`).join('')}<td></td></tr>
    </tbody></table></div>
    <div class="card-b small">
      <p><b>How it is combined.</b> Final midpoint = base midpoint × e<sup>shift</sup>, where shift = statistical model (only if it passes its test) + strength × Σ Bangladesh factor parts. The whole range moves with the midpoint; its width comes from the base model. Each part is what happened to shares after the same kind of situation in the past, compared with the market (median), multiplied by an evidence weight that is 0 for weak evidence (t &lt; 1) and 1 for strong evidence with many cases.</p>
      <ul class="steps" style="padding-left:18px">${hs.map(h => { const c = h.combined_test || {}; const t1 = (c.tests || {})['1.0'] || {}, t0 = (c.tests || {})['0.0'] || {}; return `<li><b>${esc(h.label)}:</b> strength ${fmt((h.bd.scale || 0) * 100, 0)}%. ${esc(c.reason || '')}${isNum(c.affected) ? ` · ${fmtInt(c.affected)} share-days moved by the factors in the test windows` : ''}${isNum(t0.err) && isNum(t1.err) ? ` · typical miss of the midpoint ${fmt(t0.err, 1)}% without vs ${fmt(t1.err, 1)}% with the factors` : ''}</li>`; }).join('')}</ul>
      <p class="muted">Coloured numbers are added to the forecast; grey numbers are measured but not added (weak evidence so the weight is 0, the event falls after this horizon, or it overlaps a stronger event of the same share).
        The 6-month and 1-year ranges use the 3-month shift (factor effects are measured over at most 20 sessions). Political records you add yourself are shown in the Bangladesh factors card but not added here, because they are chosen with hindsight.</p>
    </div></div>`;
}

function rangeForDate(fc, iso) {
  const c = fc.cone;
  if (!iso || !c.length) return null;
  const i = c.findIndex(p => String(p.time) >= iso);
  return i < 0 ? null : { ...c[i], n: i + 1 };
}

function formulaHtml(m) {
  const hz = m.horizons || {};
  const meth = { baseline: '60-day volatility × √days', vol: 'volatility model', vol_binned: 'volatility model, scaled per volatility bucket',
    vol_bd: 'volatility model + Bangladesh/political inputs', vol_bd_binned: 'volatility model + Bangladesh/political inputs, scaled per bucket' };
  const groups = {};
  (m.inputs || []).forEach(x => { (groups[x.group] = groups[x.group] || []).push(x.label); });
  const f = s => `<code style="white-space:normal">${s}</code>`;
  return `<div class="card"><div class="card-h"><h2>How the forecast is calculated</h2><span class="small muted">recomputed every trading day after the close</span></div><div class="card-b small">
    <ol class="steps" style="padding-left:18px">
      <li><b>Expected size of the move (σ̂).</b> A ridge regression predicts how big the move will be, from ${(m.vol_inputs || []).length || 17} volatility inputs (${esc((m.vol_inputs || []).join(', '))}):<br>
        ${f('ln(|ln(P<sub>t+h</sub>/P<sub>t</sub>)| + 0.002) = b₀ + Σ bⱼ·vⱼ   ⇒   σ̂<sub>h</sub> = e<sup>b₀ + Σ bⱼ·vⱼ</sup>')}<br>
        Range method in use: ${Object.entries(hz).filter(([, v]) => v.available && !v.extrapolated).map(([, v]) => `${esc(v.label)} = ${esc(meth[v.range_method] || v.range_method)}`).join('; ')}. Picked daily by the lowest interval score (width + 2/α × miss) on unseen data.<br>
        Bangladesh/political inputs the range model can add (${esc((m.vol_bd_inputs || []).join(', '))}): effect on the interval score ${Object.entries(hz).filter(([, v]) => v.available && !v.extrapolated && isNum(v.bd_range_gain_pct)).map(([, v]) => `${esc(v.label)} ${v.bd_range_gain_pct > 0 ? '+' : ''}${fmt(v.bd_range_gain_pct, 1)}%`).join(', ') || 'not tested yet'} (negative = better; used only where they win).
        Long term: ${f('σ̂<sub>6m, 1y</sub> = σ̂<sub>3m</sub> × √(h / 60)')} with the 3-month shape (an estimate until enough history exists to test it).</li>
      <li><b>Measured shape of DSE moves.</b> z = ln(P<sub>t+h</sub>/P<sub>t</sub>) / σ̂<sub>h</sub> over all past stock-days; q<sub>p</sub> = p-th percentile of z (fat tails and circuit-limit jumps included).</li>
      <li><b>Price range</b> at confidence c (50%, 68%, 90%):<br>
        ${f('Low = P<sub>t</sub> · e<sup>q<sub>(1−c)/2</sub> · σ̂<sub>h</sub></sup>')}  ${f('High = P<sub>t</sub> · e<sup>q<sub>(1+c)/2</sub> · σ̂<sub>h</sub></sup>')}  ${f('Midpoint = P<sub>t</sub> · e<sup>q<sub>0.5</sub> · σ̂<sub>h</sub></sup>')}<br>
        e.g. the 50% range uses the 25th and 75th percentiles of z; the 90% range uses the 5th and 95th.</li>
      <li><b>Bangladesh factors merged in (the combined prediction).</b> Every band edge is moved by one shift:<br>
        ${f('shift<sub>h</sub> = model<sub>h</sub> + s<sub>h</sub> · Σ<sub>k</sub> part<sub>k</sub>(h)')}   ${f('Final = P<sub>t</sub> · e<sup>q · σ̂<sub>h</sub> + shift<sub>h</sub></sup>')}<br>
        Parts, each measured only from past cases whose outcome was known before the forecast date (share vs the equal-weight market, median):
        <b>company event</b> (DSE query, rumour, insider deal, Z downgrade/upgrade, margin ban, regulator, government, central-bank takeover, court order, political disruption, write-off, circuit day) ${f('E(t + h − b) − E(t − b)')}, E = cumulative effect 1, 5 and 20 sessions after past events of that kind (linear between, flat after 20), b = the event's session; only the strongest active event counts because events overlap.
        <b>Record date</b>: the same curve from the session before past record dates (applied when one falls inside the horizon).
        <b>Market regime</b>: ${f('β · (market 20-session move after days in this regime − average) · min(h, 20)/20')}.
        <b>Pump-and-dump risk</b> and <b>state ownership</b>: ${f('(group\'s 20-session result − all shares) · min(h, 20)/20')}.<br>
        Evidence weight on every part: ${f('w = n/(n + 20) · clip((|t| − 1)/1.5, 0, 1)')} (0 if the median and trimmed mean disagree). model<sub>h</sub> = statistical model − base midpoint, only while it passes the reliability gate.
        Strength s<sub>h</sub> ∈ {0, 0.5, 1} is chosen daily on the same 3 unseen windows: used only if the shifted 68% range is ≥ 1% better (interval score) on the share-days the factors moved and the shift pointed the right way more than half the time.
        Now: ${Object.entries(hz).filter(([, v]) => v.available && !v.extrapolated && v.combined).map(([, v]) => `${esc(v.label)} ${fmt((v.combined.scale || 0) * 100, 0)}%`).join(', ') || 'not tested yet'}.</li>
      <li><b>Direction</b> (shown only when it passes the test below): inputs are clipped to the 1st–99th percentile and standardised, zᵢ = (xᵢ − meanᵢ) / sdᵢ, then<br>
        ${f('P(up) = 1 / (1 + e<sup>−(w₀ + Σ wᵢ·zᵢ)</sup>)')}   (logistic regression, L2 penalty)   and   ${f('E[ln(P<sub>t+h</sub>/P<sub>t</sub>)] = β₀ + Σ βᵢ·zᵢ')}   (ridge, λ = 0.1·n)</li>
      <li><b>Reliability gate:</b> in 3 rolling unseen windows of 15 sessions, rank correlation between forecast and actual return &gt; 0.03 in every window, AUC &gt; 0.53, and the model's top 10 beat the average liquid share. Otherwise: "no reliable signal".</li>
      <li><b>Trade plan:</b> ${f('Stop = P − 1.5 × ATR14')} (holders: ${f('trailing stop = highest close since buying − 2.5 × ATR')}), ${f('T1 = top of 1-week 50% range')}, ${f('T2 = top of 1-month 68% range')}, ${f('Reward:risk = (T − P) / (P − Stop)')}.<br>
        ${f('Win rate = share of past stock-days where the high reached +u·σ before the low reached −d·σ within 20 sessions')}, with u = ln(T2/P)/σ, d = ln(P/Stop)/σ, σ = 60-day daily volatility. ${f('EV = mean(+u if target first, −d if stop first, else 20-day return) · σ')}.<br>
        Buy zone = P · e<sup>(30th / 50th percentile of next-session low in σ)·σ</sup> (about 70% / 50% chance of a fill). ${f('Shares = min(Capital × Risk% / (Entry − Stop), Capital × Max% / Entry)')}.</li>
      <li><b>Signal:</b> BUY = uptrend pullback (price &gt; SMA50, SMA20 &gt; SMA50, RSI 40–65, ≤ 5% above SMA20) + reward:risk ≥ 1.5 + EV &gt; 0 + liquid + not category Z. AVOID/SELL = price &lt; falling SMA50, score ≤ −40, death cross, new 52-week low, seriously negative news, or the trailing stop hit. ACCUMULATE = long-term score ≥ 70.</li>
      <li><b>AI news score</b> (local Ollama model, see Settings): each non-routine announcement rated −2 … +2; the model input is the 30-day sum. It is tested like every other input.</li>
      <li><b>Long-term score:</b> ${f('100 × (0.25·Valuation + 0.25·Dividends + 0.20·Growth + 0.30·Stability)')}, each a percentile rank among category-A, profitable, liquid companies.</li>
    </ol>
    <h3 class="mt">The ${(m.inputs || []).length} direction-model inputs</h3>
    <div class="grid g2" style="gap:6px 16px">${Object.entries(groups).map(([g, labels]) => `<div><b>${esc((m.group_labels || {})[g] || g)}</b> <span class="muted">(${labels.length})</span>: ${esc(labels.join(', '))}</div>`).join('')}</div>
    <p class="muted mt">All inputs are point-in-time: fundamentals count only from ~120 days after the fiscal year end, shareholding from ~30 days after its date, news from its announcement date. Ranges cannot be made narrower without the price falling outside them more often; the hit rates above show the trade-off.</p>
  </div></div>`;
}

function driversHtml(h) {
  const ds = (h && h.drivers) || [];
  if (!ds.length) return '<p class="muted small">No drivers.</p>';
  const max = Math.max(...ds.map(d => Math.abs(d.impact)), 1e-6);
  return `<div class="hbars">${ds.map(d => {
    const w = (Math.abs(d.impact) / max) * 50;
    const style = d.impact >= 0 ? `left:50%;width:${w}%` : `left:${50 - w}%;width:${w}%`;
    return `<div class="hbar" style="grid-template-columns:minmax(0,1.3fr) minmax(0,1fr) 130px" title="${esc(d.label)}: ${esc(d.value)}">
      <span class="n">${esc(d.label)}</span><div class="track"><div class="axis0"></div><div class="fill ${d.impact >= 0 ? 'pos' : 'neg'}" style="${style}"></div></div>
      <span class="val small">${d.impact >= 0 ? '▲' : '▼'} ${esc(d.value)}</span></div>`;
  }).join('')}</div>`;
}

function stockPredictionHtml(one) {
  const inst = one.instrument;
  const head = `<div class="stock-h"><div class="id"><h1>${esc(inst.code)} ${catBadge(inst.category)}</h1><div class="meta">${esc(inst.name || '')}${inst.sector ? ' · ' + esc(inst.sector) : ''}</div></div>
    ${one.levels ? `<div><div class="px num">${fmt(one.levels.close)}</div><div class="pxd muted small">last close · ${fmtDate(one.levels.date)}</div></div>` : ''}
    <div class="actions"><a class="btn" href="#/stock/${encodeURIComponent(inst.code)}">Stock page</a><button class="btn" id="pr-alert">+ Alert</button></div></div>`;
  const fc = one.forecast;
  if (!fc) return `${head}<div class="card">${empty('No forecast for this instrument', `<p>${esc(one.reason || '')}</p>`)}</div>`;
  const lv = one.levels || {}, an = one.analysis || {}, close = fc.close;
  const m1 = fc.horizons.find(h => h.h === 20) || fc.horizons[0];
  const pv = lv.pivots || {};
  const row = (k, v) => `<div><span>${k}</span><span>${v}</span></div>`;
  const at = v => (isNum(v) ? `${fmt(v)} <span class="muted small">(${fromClose(v, close)})</span>` : '—');
  const track = one.track || [];
  const L = S.rangeLevel;
  const outer = L === 90 ? 68 : 90;
  return `${head}
    ${tradePlanHtml(one)}
    ${bdStockCardHtml(one)}
    <div class="toolbar"><span class="small muted">Range shown:</span>
      <div class="seg" id="lvl-seg">${[30, 50, 68, 90].map(l => `<button data-l="${l}" class="${L === l ? 'on' : ''}">${l}%${l === 30 ? ' · tightest' : l === 90 ? ' · safest' : ''}</button>`).join('')}</div>
      <span class="small muted">A narrower range is missed more often; each tile shows how often it held in tests.</span></div>
    ${termRow('short', 'Short term', fc.date_ranges && fc.date_ranges.short, fc.horizons.filter(h => h.term === 'short'), close)}
    ${termRow('long', 'Long term', fc.date_ranges && fc.date_ranges.long, fc.horizons.filter(h => h.term === 'long'), close)}
    ${combinedHtml(fc)}
    <div class="card"><div class="card-h"><h2>Range for a date</h2><span class="small muted">pick any trading day up to one year ahead</span></div>
      <div class="card-b toolbar"><input type="date" id="pick-date" min="${esc(fc.cone[0].time)}" max="${esc(fc.cone[fc.cone.length - 1].time)}" value="${esc(fc.cone[Math.min(9, fc.cone.length - 1)].time)}">
        <div id="pick-out" class="small" style="flex:1;min-width:260px"></div></div></div>
    <div class="grid g-main">
      <div class="card fill">
        <div class="card-h"><h2>Forecast cone</h2><div class="seg" id="cone-seg"><button data-v="short" class="${S.coneView !== 'long' ? 'on' : ''}">Short term</button><button data-v="long" class="${S.coneView === 'long' ? 'on' : ''}">Long term</button></div><span class="small muted">trading days Sun–Thu, DSE holidays skipped (next year projected)</span></div>
        <div class="pane-label"><span class="key"><i style="background:var(--s1)"></i>Price</span><span class="key"><i class="dash-s1"></i>Midpoint path</span><span class="key"><i style="background:var(--s2)"></i>${RANGE_NAMES[L]} (${L}%)</span><span class="key"><i class="dash"></i>${RANGE_NAMES[outer]} (${outer}%)</span></div>
        <div class="chart-box grow" id="pc" style="min-height:340px"><div class="ohlc" id="pc-legend"></div></div>
      </div>
      <div class="card"><div class="card-h"><h2>Planning levels</h2></div><div class="card-b">
        <div class="kv" style="grid-template-columns:1fr">
          ${row('Resistance R2 · R1', `${fmt(pv.R2)} · ${fmt(pv.R1)}`)}
          ${row('20-day high · 60-day high', `${fmt(lv.high20)} · ${fmt(lv.high60)}`)}
          ${row('Last close', `<b>${fmt(close)}</b>`)}
          ${row('Support S1 · S2', `${fmt(pv.S1)} · ${fmt(pv.S2)}`)}
          ${row('20-day low · 60-day low', `${fmt(lv.low20)} · ${fmt(lv.low60)}`)}
          ${row('Volatility stop (1.5 × ATR)', at(lv.stop))}
          ${row('1-month upside (top of likely range)', at(lv.target))}
          ${row('Reward : risk', isNum(lv.reward_risk) ? `1 : ${fmt(lv.reward_risk, 1)}` : '—')}
        </div>
        <p class="mt">${one.setup ? '<span class="ok-mark">✓</span> Matches the short-term setup (uptrend + pullback)' : '<span class="no-mark">✗</span> Not in the short-term buy setup right now'}</p>
        <p class="small muted">Technical signal: ${signal(an.label, an.score)} · RSI ${fmt(an.rsi, 1)} · 1-month ${fmtPct(an.ret_1m, 1)}</p>
      </div></div>
    </div>
    <div class="grid g2">
      <div class="card"><div class="card-h"><h2>What the model sees</h2><div class="seg" id="drv-seg">${fc.horizons.filter(h => !h.extrapolated).map(h => `<button data-h="${h.h}" class="${h.h === m1.h ? 'on' : ''}">${esc(h.label)}</button>`).join('')}</div></div>
        <div class="card-b"><div id="drv-lean" class="note"></div><div id="drv" class="mt"></div>
        <p class="small muted mt">Bars show which inputs push the model toward up (▲) or down (▼). Treat this as a description of the chart, not a forecast, unless the horizon is marked reliable.</p></div></div>
      <div class="card"><div class="card-h"><h2>This stock's past forecasts vs actual</h2><span class="small muted">checked every trading day</span></div>
        <div class="card-b flush">${track.length ? (() => {
          const lvl = S.rangeLevel === 50 && track.some(t => t.in50 != null) ? 50 : S.rangeLevel === 90 ? 68 : 68;
          const inT = t => (lvl === 50 ? t.in50 : t.in68);
          return `<div class="table-wrap" style="max-height:420px;overflow-y:auto"><table class="t"><thead><tr><th>Checked</th><th>For</th><th class="r">Close then</th><th class="r">${lvl}% range</th><th class="r">Actual</th><th class="c">In range</th><th>Type</th></tr></thead><tbody>
          ${track.map(t => `<tr><td>${fmtDate(t.target_date, { day: 'numeric', month: 'short' })}</td><td>${t.horizon === 1 ? 'Next session' : '1 week'}</td><td class="r">${fmt(t.close)}</td><td class="r">${fmt(t['low' + lvl] ?? t.low68)} – ${fmt(t['high' + lvl] ?? t.high68)}</td><td class="r">${fmt(t.actual)} <span class="${dir(t.actual_ret)} small">${fmtPct(t.actual_ret, 1)}</span></td><td class="c">${inT(t) == null ? '—' : inT(t) ? '<span class="ok-mark">✓</span>' : '<span class="no-mark">✗</span>'}</td><td class="small muted">${SRC_LABEL[t.source] || ''}</td></tr>`).join('')}
          </tbody></table></div>
          <div class="card-b small muted">Inside the ${lvl}% range ${track.filter(inT).length} of ${track.length} times shown (target about ${lvl}%). <a href="#/accuracy">All stocks →</a></div>`;
        })() : '<div class="card-b muted small">Forecasts are saved every trading day. Their outcomes appear here once each horizon has passed.</div>'}</div></div>
    </div>
    ${aiProfileHtml(one.ai_profile, one.instrument.code, (one.ai || {}).available)}
    ${aiCardHtml(one)}
    <div class="card"><div class="card-h"><h2>News &amp; announcements</h2><span class="small muted">from DSE · used by the model (tone, earnings surprise, dividend, record date, AI score)</span></div>
      <div class="card-b">${newsListHtml(one.news, one.tag_labels)}</div></div>`;
}

function mountPredictChart(one) {
  const fc0 = one.forecast;
  if (!fc0 || !one.history.length) return;
  const longView = S.coneView === 'long';
  // short term: last 3 months of prices + the next 20 sessions; long term: everything + 1 year ahead
  const fc = { ...fc0, cone: longView ? fc0.cone : fc0.cone.slice(0, 20) };
  const history = longView ? one.history : one.history.slice(-60);
  const c = colors();
  const ch = makeChart($('#pc'));
  const price = ch.addLineSeries({ color: c.s1, lineWidth: 2, priceLineVisible: false });
  price.setData(history);
  const last = history[history.length - 1];
  const line = (key, color, extra = {}) => {
    const s = ch.addLineSeries({ color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, ...extra });
    s.setData([{ time: last.time, value: last.value }, ...fc.cone.map(p => ({ time: p.time, value: p[key] }))]);
    return s;
  };
  const L = S.rangeLevel, O = L === 90 ? 68 : 90;
  line('median', c.s1, { lineStyle: LWC.LineStyle.Dashed });
  line('high' + L, c.s2, { lineWidth: 2 });
  line('low' + L, c.s2, { lineWidth: 2 });
  line('high' + O, c.muted, { lineWidth: 1, lineStyle: LWC.LineStyle.Dashed });
  line('low' + O, c.muted, { lineWidth: 1, lineStyle: LWC.LineStyle.Dashed });
  ch.timeScale().fitContent();
  const cone = new Map(fc.cone.map(p => [String(p.time), p]));
  const hist = new Map(history.map(p => [String(p.time), p.value]));
  const legend = t => {
    const el = $('#pc-legend');
    if (!el) return;
    const k = t ? timeKey(t) : null;
    const p = k && cone.get(k);
    if (p) el.innerHTML = `<span>${fmtDate(k, { weekday: 'short', day: 'numeric', month: 'short' })}</span><span>midpoint <b>${fmt(p.median)}</b></span><span>${L}% <b>${fmt(p['low' + L])} – ${fmt(p['high' + L])}</b></span><span>${O}% <b>${fmt(p['low' + O])} – ${fmt(p['high' + O])}</b></span>`;
    else if (k && hist.has(k)) el.innerHTML = `<span>${fmtDate(k, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' })}</span><span>close <b>${fmt(hist.get(k))}</b></span>`;
    else el.innerHTML = `<span>Last close <b>${fmt(last.value)}</b> · ${fmtDate(last.time)}</span>`;
  };
  legend(null);
  ch.subscribeCrosshairMove(p => legend(p && p.point ? p.time : null));
}

function signalTable(rows, emptyMsg) {
  return tableHtml([
    { key: 'code', label: 'Code', html: r => `<span class="code">${esc(r.code)}</span> ${catBadge(r.category)}<span class="nm">${esc(r.name || '')}</span>` },
    { key: 'signal', label: 'Signal', html: r => sigBadge(r.signal) },
    { key: 'close', label: 'Price', align: 'r', html: r => fmt(r.close) },
    { key: 'zone', label: 'Buy zone', align: 'r', html: r => (r.buy_zone ? `${fmt(r.buy_zone[0])} – ${fmt(r.buy_zone[1])}` : '—') },
    { key: 'stop', label: 'Stop', align: 'r', html: r => `<span class="down">${fmt((r.holding && r.holding.trailing_stop) || r.stop)}</span>` },
    { key: 't1', label: 'T1', align: 'r', html: r => `<span class="up">${fmt(r.t1)}</span>` },
    { key: 't2', label: 'T2', align: 'r', html: r => `<span class="up">${fmt(r.t2)}</span>` },
    { key: 'rr2', label: 'R:R', align: 'r', html: r => (isNum(r.rr2) ? `1:${fmt(r.rr2, 1)}` : '—') },
    { key: 'win', label: 'Win %', align: 'r', title: 'Historical chance T2 is reached before the stop (20 sessions)', html: r => (isNum(r.win_rate) ? fmt(r.win_rate, 0) + '%' : '—') },
    { key: 'ev', label: 'EV / trade', align: 'r', title: 'Historical average result per trade with this stop and target', html: r => (isNum(r.ev_pct) ? `<span class="${dir(r.ev_pct)}">${fmtPct(r.ev_pct, 2)}</span>` : '—') },
    { key: 'shares', label: 'Shares', align: 'r', html: r => (isNum(r.shares) && r.shares ? fmtInt(r.shares) : '—') },
    { key: 'exit', label: 'Exit / review by', title: 'Short-term: if neither stop nor T2 is hit by this date', html: r => (r.dates ? fmtDate(r.dates.short.exit_by, { day: 'numeric', month: 'short' }) : '—') },
    { key: 'why', label: 'Why', cls: 'wrap', html: r => `<span class="small">${esc((r.reasons || [])[0] || '')}</span>` },
  ], rows, null, emptyMsg);
}

function signalsCardHtml(pl) {
  if (!pl || !pl.date) return '';
  const c = pl.counts || {};
  const rec = (pl.tables || {}).buy_record || {}, srec = (pl.tables || {}).sell_record || {};
  const tabs = [['buy', `BUY (${c.BUY || 0})`], ['acc', `ACCUMULATE (${c.ACCUMULATE || 0})`], ['hold', `Your holdings (${pl.holdings.length})`], ['watch', `Watchlist (${pl.watch.length})`], ['avoid', `Avoid: BD risk (${(pl.avoid_bd || []).length})`]];
  return `<div class="card" id="sig-card"><div class="card-h"><h2>Today's buy / sell signals</h2>${pl.regime ? `<span class="label ${REGIME_CLASS[pl.regime] || ''}" title="Market regime">${esc(pl.regime)} market</span>` : ''}
      <div class="seg" id="sig-seg">${tabs.map(([k, l], i) => `<button data-k="${k}" class="${i ? '' : 'on'}">${l}</button>`).join('')}</div></div>
    <div class="card-b small"><div class="note">${rec.trades ? `<b>BUY rule record (past year):</b> ${fmtInt(rec.trades)} trades, target reached first ${fmt(rec.win_rate, 0)}%, stopped ${fmt(rec.stop_rate, 0)}%, average <b>${fmtPct(rec.avg_trade, 2)}</b> per trade vs ${fmtPct(rec.baseline_trade, 2)} for random entries with the same exits. ` : ''}${srec.signals ? `<b>SELL rule:</b> 20 sessions after a sell signal shares averaged ${fmtPct(srec.fwd20, 2)} vs ${fmtPct(srec.all20, 2)} for all shares.` : ''} Win rate is below 50% by design: winners are bigger than losers, so <b>always place the stop</b>.${pl.settings && pl.settings.capital ? '' : ' <a href="#/settings">Set your capital and risk %</a> to get share quantities.'}</div></div>
    <div class="table-wrap" id="sig-buy">${signalTable(pl.buy, 'No BUY signals today — normal in a weak market')}</div>
    <div class="table-wrap" id="sig-acc" hidden>${signalTable(pl.accumulate, 'No candidates')}</div>
    <div class="table-wrap" id="sig-hold" hidden>${signalTable(pl.holdings, 'No holdings — add transactions on the Portfolio page')}</div>
    <div class="table-wrap" id="sig-watch" hidden>${signalTable(pl.watch, 'Your watchlist is empty')}</div>
    <div class="table-wrap" id="sig-avoid" hidden>${signalTable(pl.avoid_bd || [], 'No Bangladesh-specific red flags today')}</div></div>`;
}

function picksHtml(pk, meta) {
  const rule = (meta.setup_rule || {})['20'] || {};
  const rule1w = (meta.setup_rule || {})['5'] || {};
  const n = (x, i) => `<span class="muted">${i + 1}</span>`;
  const shortRows = pk.short || [], longRows = pk.long || [];
  const shortTable = tableHtml([
    { key: 'rank', label: '#', html: (r) => n(r, shortRows.indexOf(r)) },
    { key: 'code', label: 'Code', html: r => `<span class="code">${esc(r.code)}</span> ${catBadge(r.category)}<span class="nm">${esc(r.name || '')} · ${esc(r.sector || '')}</span>` },
    { key: 'close', label: 'Price', align: 'r', html: r => fmt(r.close) },
    { key: 'score', label: 'Trend signal', html: r => signal(r.label, r.score) },
    { key: 'rsi', label: 'RSI', align: 'r', html: r => fmt(r.rsi, 0) },
    { key: 'above', label: 'vs 20-day avg', align: 'r', html: r => fmtPct(r.above_sma20_pct, 1) },
    { key: 'r1w', label: '1-week range', align: 'r', html: r => (r.range_1w ? `${fmt(r.range_1w[0])} – ${fmt(r.range_1w[1])}` : '—') },
    { key: 'r1m', label: '1-month range', align: 'r', html: r => `${fmt(r.range_1m[0])} – ${fmt(r.range_1m[1])}` },
    { key: 'stop', label: 'Stop', align: 'r', html: r => `<span class="down">${fmt(r.stop)}</span>` },
    { key: 'target', label: 'Target', align: 'r', html: r => `<span class="up">${fmt(r.target)}</span>` },
    { key: 'rr', label: 'Reward:risk', align: 'r', html: r => (isNum(r.reward_risk) ? `1 : ${fmt(r.reward_risk, 1)}` : '—') },
    { key: 'hold', label: 'Hold until', html: r => (r.hold_until ? fmtDate(r.hold_until, { day: 'numeric', month: 'short' }) : '—') },
  ], shortRows, null, 'No share matches the setup today — that is normal in a weak market.');
  const longTable = tableHtml([
    { key: 'rank', label: '#', html: (r) => n(r, longRows.indexOf(r)) },
    { key: 'code', label: 'Code', html: r => `<span class="code">${esc(r.code)}</span> ${catBadge(r.category)}<span class="nm">${esc(r.name || '')} · ${esc(r.sector || '')}</span>` },
    { key: 'close', label: 'Price', align: 'r', html: r => fmt(r.close) },
    { key: 'score', label: 'Score', html: r => `<div class="sp" title="Valuation ${r.components.valuation} · Dividends ${r.components.dividends} · Growth ${r.components.growth} · Stability ${r.components.stability}"><div class="meter"><div style="width:${r.score}%"></div></div><b class="num">${fmt(r.score, 0)}</b></div>` },
    { key: 'pe', label: 'P/E', align: 'r', html: r => fmt(r.pe, 1) },
    { key: 'pb', label: 'P/NAV', align: 'r', html: r => fmt(r.pb, 2) },
    { key: 'dy', label: 'Div. yield', align: 'r', html: r => (isNum(r.dividend_yield) ? fmt(r.dividend_yield, 1) + '%' : '—') },
    { key: 'dyrs', label: 'Paid (5 yrs)', align: 'r', html: r => esc(r.dividend_years || '—') },
    { key: 'eps', label: 'EPS growth', align: 'r', html: r => (isNum(r.eps_growth) ? chg(r.eps_growth, 0) + '<span class="muted small">/yr</span>' : '—') },
    { key: 'vol', label: 'Volatility', align: 'r', html: r => fmt(r.volatility, 0) + '%' },
    { key: 'trend', label: 'Trend', html: r => (r.trend ? '<span class="up">Up</span>' : '<span class="muted">Mixed</span>') },
    { key: 'r3m', label: '3-month likely range', align: 'r', html: r => (r.range_3m ? `${fmt(r.range_3m[0])} – ${fmt(r.range_3m[1])}` : '—') },
    { key: 'r1y', label: '1-year range (50%, est.)', align: 'r', html: r => (r.range_1y ? `${fmt(r.range_1y[0])} – ${fmt(r.range_1y[1])}` : '—') },
  ], longRows, null, 'No candidates yet.');
  const dr = pk.date_ranges || {};
  const D = x => fmtDate(x, { day: 'numeric', month: 'short', year: 'numeric' });
  return `<div class="card-h"><h2>Top 10 ideas</h2><div class="seg" id="pk-seg"><button data-k="short" class="on">Short term${dr.short ? ` · ${fmtDate(dr.short.from, { day: 'numeric', month: 'short' })} – ${fmtDate(dr.short.to, { day: 'numeric', month: 'short' })}` : ' (1–4 weeks)'}</button><button data-k="long" class="">Long term${dr.long ? ` · to ${fmtDate(dr.long.to, { month: 'short', year: 'numeric' })}` : ''}</button></div></div>
    ${dr.long ? `<div class="card-b small muted" style="padding-bottom:0">Short term: trades held up to <b>${D(dr.short.to)}</b>. Long term: accumulate until <b>${D(dr.long.accumulate_to)}</b>, review on <b>${D(dr.long.review)}</b>, 1-year horizon <b>${D(dr.long.to)}</b>.</div>` : ''}
    <div id="pk-short">
      <div class="card-b"><div class="note"><b>Setup:</b> liquid A/B/N shares (≥ Tk ${fmt(pk.liquid_mn, 0)} mn traded per day) in an uptrend (price above the 50-day average, 20-day above 50-day) that have pulled back: RSI 40–65 and no more than 5% above the 20-day average. Ranked by ${esc(pk.short_ranked_by || 'trend strength')}. Stop = 1.5 × daily range (ATR) below the price; target = top of the 1-month likely range.<br>
        <b>Track record (past year):</b> ${rule.days ? `picks beat the average liquid share in ${fmt(rule.beat_rate, 0)}% of 1-month periods, by ${fmtPct(rule.avg_excess)} on average (1 week: ${fmt(rule1w.beat_rate, 0)}%, ${fmtPct(rule1w.avg_excess)}). A small edge, not a sure thing: use the stop.` : 'not enough history yet.'}
        ${pk.short_candidates > (pk.short || []).length ? ` ${pk.short_candidates} shares match today; the top 10 are shown.` : ''}</div></div>
      <div class="table-wrap" id="pk-short-t">${shortTable}</div>
    </div>
    <div id="pk-long" hidden>
      <div class="card-b"><div class="note"><b>For buying a fixed amount regularly</b> (e.g. every month) rather than timing entries. Category-A, profitable (EPS &gt; 0), liquid companies, each scored against the other ${fmtInt(pk.long_candidates)} candidates: valuation (P/E, P/NAV) 25%, dividends (yield and how many of the last 5 years paid) 25%, earnings growth 20%, stability (low volatility and long-term uptrend) 30%. At most 3 per sector, so the list stays diversified.<br>
        <b>Not backtested:</b> DSE only provides current fundamentals, so this is a quality/value screen. Check each company's latest reports before investing.</div></div>
      <div class="table-wrap" id="pk-long-t">${longTable}</div>
    </div>`;
}

async function viewPredict(el, args, ctx) {
  const code = (args[0] || '').toUpperCase();
  const [base, one, pl] = await Promise.all([
    api('/predict'), code ? api('/predict/' + encodeURIComponent(code)) : Promise.resolve(null),
    code ? Promise.resolve(null) : api('/plans').catch(() => null)]);
  if (!ctx.alive()) return;
  getQuotes().catch(() => {});
  const m = base.meta || {};
  if (!m.trained_at) {
    ctx.render(`<div class="page-h"><div><h1>Prediction</h1></div></div>
      <div class="card">${empty('The forecast model has not been trained yet', '<p>It trains automatically after each end-of-day run.</p><button class="btn btn-primary" data-job="predict">Train now</button>')}</div>`);
    return;
  }
  const hz = m.horizons || {};
  const anyReliable = Object.values(hz).some(v => v.reliable);
  ctx.render(`
    <div class="page-h"><div><h1>Prediction</h1>
      <div class="sub">Forecasts from the ${fmtDate(m.trade_date)} close · rebuilt every trading day after the close (last ${fmtTime(m.trained_at)}) · ${(m.inputs || []).length} inputs incl. news &amp; fundamentals · ${fmtInt(m.sessions)} sessions of ${fmtInt(m.stocks)} shares · <a href="#/accuracy">checked against real prices daily</a></div></div>
      <div class="actions"><button class="btn" data-job="predict">Retrain now</button></div></div>
    <div class="card"><form class="card-b toolbar" id="pf" autocomplete="off">
      <input name="code" list="codes" placeholder="Enter a trading code, e.g. BRACBANK" value="${esc(code)}" style="width:260px" required>
      <button class="btn btn-primary">Show prediction</button>
      ${base.watchlist.length ? `<span class="small muted">Watchlist:</span><div class="chips">${base.watchlist.map(c => `<a class="chip" href="#/predict/${encodeURIComponent(c)}">${esc(c)}</a>`).join('')}</div>` : ''}
    </form></div>
    ${one ? stockPredictionHtml(one) : paperCardHtml(pl && pl.record) + signalsCardHtml(pl)}
    <div class="card" id="picks">${picksHtml(base.picks || {}, m)}</div>
    <div class="card"><div class="card-h"><h2>How reliable are these forecasts?</h2><span class="small muted">tested on ${hz['5'] && hz['5'].backtest ? `${hz['5'].backtest.windows} unseen windows, ${fmtDate(hz['5'].backtest.test_from)} – ${fmtDate(hz['5'].backtest.test_to)}` : 'recent unseen data'}</span></div>
      <div class="card-b">
        <div class="note">${anyReliable
          ? '<b>The direction model has passed its tests for some horizons</b> (marked below), so a chance-of-rising figure is shown for those.'
          : '<b>Price ranges: yes. Up/down calls: not yet.</b> The likely ranges have held about as often as promised for 1 session to 1 month. The up/down model has not beaten simple guessing on data it had not seen, so its direction is hidden. It is retrained and re-tested every trading day and will switch on automatically if it proves itself. The 3-month range is low confidence until more history builds up.'}</div>
        <div class="mt">${reliabilityTable(hz, base.live)}</div>
        ${inputGroupsHtml(hz)}
        <p class="small muted mt">Inputs: ${(m.inputs || []).length} in ${Object.keys(m.group_labels || {}).length} groups · news history for ${fmtInt(m.news_coverage)} of ${fmtInt(m.stocks)} shares (${fmtInt(m.news_items)} announcements) · fundamentals for ${fmtInt(m.fund_coverage)}. <a href="#/accuracy">See the daily accuracy check →</a></p>
      </div></div>
    ${formulaHtml(m)}
    <p class="disclaimer">Forecasts and lists are statistical estimates from past prices and published fundamentals, made for your own planning. They are not investment advice and can be wrong, especially around news, corporate actions and market-wide shocks.</p>`);

  $('#pf').addEventListener('submit', e => {
    e.preventDefault();
    const v = e.target.code.value.trim().toUpperCase();
    if (v) location.hash = '#/predict/' + encodeURIComponent(v);
  });
  $$('#pk-seg button').forEach(b => b.addEventListener('click', () => {
    $$('#pk-seg button').forEach(x => x.classList.toggle('on', x === b));
    $('#pk-short').hidden = b.dataset.k !== 'short';
    $('#pk-long').hidden = b.dataset.k !== 'long';
  }));
  ['#pk-short-t', '#pk-long-t'].forEach(sel => $(sel).addEventListener('click', e => {
    const tr = e.target.closest('tr[data-code]');
    if (tr) location.hash = '#/predict/' + encodeURIComponent(tr.dataset.code);
  }));
  $$('#lvl-seg button').forEach(b => b.addEventListener('click', () => { S.rangeLevel = Number(b.dataset.l); rerender(); }));
  $$('#cone-seg button').forEach(b => b.addEventListener('click', () => { S.coneView = b.dataset.v; rerender(); }));
  const pick = $('#pick-date');
  if (pick && one && one.forecast) {
    const show = () => {
      const p = rangeForDate(one.forecast, pick.value);
      const close = one.forecast.close;
      $('#pick-out').innerHTML = p
        ? `<b>${fmtDate(p.time, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' })}</b> (trading day ${p.n}${p.n > 60 ? ', estimate' : ''}): most likely <b>${fmt(p.low50)} – ${fmt(p.high50)}</b> (50%, ${fromClose(p.low50, close)} to ${fromClose(p.high50, close)}) · 68%: ${fmt(p.low68)} – ${fmt(p.high68)} · 90%: ${fmt(p.low90)} – ${fmt(p.high90)} · midpoint ${fmt(p.median)}`
        : '<span class="muted">Pick a date within the next year.</span>';
    };
    pick.addEventListener('change', show);
    show();
  }
  $$('#sig-seg button').forEach(b => b.addEventListener('click', () => {
    $$('#sig-seg button').forEach(x => x.classList.toggle('on', x === b));
    ['buy', 'acc', 'hold', 'watch', 'avoid'].forEach(k => { const el2 = $('#sig-' + k); if (el2) el2.hidden = k !== b.dataset.k; });
  }));
  const sc = $('#sig-card');
  if (sc) sc.addEventListener('click', e => { const tr = e.target.closest('tr[data-code]'); if (tr) location.hash = '#/predict/' + encodeURIComponent(tr.dataset.code); });
  const profBtn = $('#ai-prof');
  if (profBtn && one) profBtn.addEventListener('click', async () => {
    profBtn.disabled = true;
    profBtn.textContent = 'Checking…';
    $('#ai-prof-out').innerHTML = '<p class="muted small">The local model is reading the filings (about 2–3 minutes)…</p>';
    try {
      const r = await api('/ai/profile/' + encodeURIComponent(one.instrument.code), { method: 'POST' });
      $('#ai-prof-out').innerHTML = aiProfileBody(r.profile, true);
      profBtn.textContent = 'Re-check';
    } catch (err) {
      $('#ai-prof-out').innerHTML = `<p class="down small">${esc(err.message)}</p>`;
      profBtn.textContent = 'Check now';
    } finally { profBtn.disabled = false; }
  });
  const gen = $('#ai-gen');
  if (gen && one) gen.addEventListener('click', async () => {
    gen.disabled = true;
    gen.textContent = 'Writing…';
    $('#ai-out').innerHTML = '<p class="muted small">The local model is writing (about 30 seconds)…</p>';
    try {
      const r = await api('/ai/note/' + encodeURIComponent(one.instrument.code), { method: 'POST' });
      $('#ai-out').innerHTML = mdLite(r.note.note) + `<p class="small muted">${esc(r.note.model || '')} · ${fmtTime(r.note.created_at)}</p>`;
      gen.textContent = 'Rewrite';
    } catch (err) {
      $('#ai-out').innerHTML = `<p class="down small">${esc(err.message)}</p>`;
      gen.textContent = 'Write note';
    } finally { gen.disabled = false; }
  });
  if (one && one.forecast) {
    const fc = one.forecast;
    const showDrivers = hVal => {
      const h = fc.horizons.find(x => x.h === hVal) || fc.horizons[0];
      const bt = h.backtest || {};
      $('#drv').innerHTML = driversHtml(h);
      $('#drv-lean').innerHTML = h.reliable
        ? `<b>${esc(h.label)}: ${pct0(h.prob_up)} chance of rising.</b> This horizon passed its out-of-sample tests.`
        : `<b>${esc(h.label)}: the model leans ${pct0(h.prob_up)} up, but this is not reliable.</b> In tests it was right ${fmt(bt.direction_accuracy, 0)}% of the time, versus ${fmt(bt.naive_accuracy, 0)}% for always guessing the most common direction.`;
      $$('#drv-seg button').forEach(b => b.classList.toggle('on', Number(b.dataset.h) === h.h));
    };
    $$('#drv-seg button').forEach(b => b.addEventListener('click', () => showDrivers(Number(b.dataset.h))));
    showDrivers(20);
    mountPredictChart(one);
  }
  const al = $('#pr-alert');
  if (al && one) al.addEventListener('click', () => openAlertModal({ code: one.instrument.code, kind: 'price_below', threshold: one.levels && one.levels.stop ? one.levels.stop : '' }));
}

/* ---------------- accuracy (forecast vs real prices, checked daily) */
const SRC_LABEL = { live: 'Live', backtest: 'Walk-forward' };
const accTarget = { in68: 68, in90: 90, direction: 50 };

async function viewAccuracy(el, args, ctx) {
  const h = Number(args[0]) || 1;
  const date = args[1] || '';
  const [d, paper] = await Promise.all([api(`/accuracy?h=${h}${date ? '&date=' + encodeURIComponent(date) : ''}`), api('/signals/log').catch(() => null)]);
  if (!ctx.alive()) return;
  S.paperLog = paper;
  const checks = d.checks || [];
  if (!checks.length) {
    ctx.render(`<div class="page-h"><div><h1>Forecast accuracy</h1></div></div>
      <div class="card">${empty('No forecasts have been checked yet', '<p>Forecasts are checked against real closing prices after each trading day. You can also rebuild a walk-forward history for the last 40 sessions now.</p><button class="btn btn-primary" data-job="simulate">Build history now</button>')}</div>`);
    return;
  }
  const horizons = d.horizons || {};
  const hChecks = checks.filter(c => c.horizon === h);
  const tile = hh => {
    const t = (d.totals || {})[`${hh}_live`] || (d.totals || {})[`${hh}_backtest`];
    if (!t) return '';
    return `<a class="tile" href="#/accuracy/${hh}" style="text-decoration:none;color:inherit;${hh === h ? 'box-shadow:0 0 0 2px var(--accent)' : ''}">
      <div class="k"><span>${esc(horizons[hh] || hh)}</span><span class="muted">${SRC_LABEL[t.source]}</span></div>
      <div class="v">${isNum(t.in50) ? fmt(t.in50, 0) : fmt(t.in68, 0)}% <span class="small muted">in ${isNum(t.in50) ? '50%' : '68%'} range</span></div>
      <div class="d">${isNum(t.in50) ? `target 50% · avg width ${fmt(t.width50, 1)}%` : 'target 68%'}</div>
      <div class="d">68% range: ${fmt(t.in68, 0)}%${isNum(t.width68) ? ` (width ${fmt(t.width68, 1)}%)` : ''} · 90%: ${fmt(t.in90, 0)}%</div>
      <div class="d">direction right ${fmt(t.direction, 0)}% · avg miss ${fmt(t.mae, 1)}%</div>
      <div class="d muted small">${fmtInt(t.forecasts)} forecasts over ${t.days} days</div></a>`;
  };
  const det = d.details || {};
  const rows = det.rows || [];
  const mine = new Set([...(d.watchlist || []), ...(d.holdings || [])]);
  const f = S.accFilter || 'all';
  const shown = rows.filter(r => f === 'all' || (f === 'mine' && mine.has(r.code)) || (f === 'miss' && !inKey(r)))
    .sort((a, b) => Math.abs(b.error_pct) - Math.abs(a.error_pct));
  const dateOpts = [...new Set(hChecks.map(c => String(c.check_date)))].reverse();
  const lv = S.rangeLevel === 90 ? 90 : S.rangeLevel === 68 ? 68 : 50;
  const inKey = r => (lv === 50 && r.in50 != null ? r.in50 : lv === 90 ? r.in90 : r.in68);
  const inside = rows.length ? rows.filter(inKey).length / rows.length * 100 : null;
  ctx.render(`
    <div class="page-h"><div><h1>Forecast accuracy</h1>
      <div class="sub">Every trading day, forecasts whose time is up are compared with the real closing price. <b>Live</b> = forecast made on that day. <b>Walk-forward</b> = re-created for earlier days using only data available at the time.</div></div>
      <div class="actions"><button class="btn" data-job="simulate">Rebuild walk-forward history</button></div></div>
    <div class="tiles">${Object.keys(horizons).map(Number).map(tile).join('')}</div>
    <div class="toolbar"><span class="small muted">Range level:</span><div class="seg" id="acc-lvl">${[50, 68, 90].map(l => `<button data-l="${l}" class="${lv === l ? 'on' : ''}">${l}%</button>`).join('')}</div></div>
    <div class="grid g2">
      <div class="card"><div class="card-h"><h2>Inside the ${lv}% range, by day</h2><span class="small muted">${esc(horizons[h])} · dashed line = ${lv}% target</span></div>
        <div class="pane-label"><span class="key"><i style="background:var(--s1)"></i>Live</span><span class="key"><i class="dash"></i>Walk-forward</span></div>
        <div class="chart-box dash" id="acc-range"></div></div>
      <div class="card"><div class="card-h"><h2>Direction right, by day</h2><span class="small muted">dashed line = 50% (coin flip)</span></div>
        <div class="pane-label"><span class="key"><i style="background:var(--s1)"></i>Live</span><span class="key"><i class="dash"></i>Walk-forward</span></div>
        <div class="chart-box dash" id="acc-dir"></div></div>
    </div>
    <div class="card"><div class="card-h"><h2>Stock by stock</h2>
        <select id="acc-date">${dateOpts.map(x => `<option value="${x}" ${String(det.check_date) === x ? 'selected' : ''}>${fmtDate(x, { weekday: 'short', day: 'numeric', month: 'short' })}</option>`).join('')}</select>
        <div class="seg" id="acc-f"><button data-f="all" class="${f === 'all' ? 'on' : ''}">All</button><button data-f="mine" class="${f === 'mine' ? 'on' : ''}">Watchlist &amp; holdings</button><button data-f="miss" class="${f === 'miss' ? 'on' : ''}">Misses only</button></div></div>
      <div class="card-b small">${det.made_on ? `${esc(horizons[h])} forecasts made on <b>${fmtDate(det.made_on)}</b>, checked against the close on <b>${fmtDate(det.check_date)}</b>: ${rows.length} stocks, <b>${fmt(inside, 0)}%</b> ended inside the ${lv}% range (target ${lv}%). Sorted by biggest miss.` : 'No details.'}</div>
      <div class="table-wrap tall" id="acc-t"></div></div>
    <div class="card"><div class="card-h"><h2>Daily checks</h2><span class="small muted">${esc(horizons[h])}</span></div><div class="table-wrap tall" id="acc-days"></div></div>
    ${paper ? `${paperCardHtml(paper)}<div class="card"><div class="card-h"><h2>Paper trades (every BUY signal)</h2><span class="small muted">filled if the next session reaches the entry limit; then stop, Target 2 or the exit date, whichever comes first</span></div><div class="table-wrap tall" id="paper-t"></div></div>` : ''}
    <p class="disclaimer">"Inside likely range" should be close to 68% and "inside wide range" close to 90% if the ranges are honest. Direction near 50% means the up/down model has no edge, which is why its calls stay hidden on the Prediction page until it does better.</p>`);

  mountTable($('#acc-t'), [
    { key: 'code', label: 'Code', html: r => `<span class="code">${esc(r.code)}</span> ${catBadge(r.category)}<span class="nm">${esc(r.name || '')}</span>` },
    { key: 'close', label: 'Close then', align: 'r', html: r => fmt(r.close) },
    { key: 'range', label: `${lv}% range`, align: 'r', html: r => { const lo = r['low' + lv] ?? r.low68, hi = r['high' + lv] ?? r.high68; return `${fmt(lo)} – ${fmt(hi)}`; } },
    { key: 'median', label: 'Midpoint', align: 'r', html: r => fmt(r.median) },
    { key: 'actual', label: 'Actual', align: 'r', html: r => `<b>${fmt(r.actual)}</b>${r.traded ? '' : ' <span class="muted small">no trade</span>'}` },
    { key: 'actual_ret', label: 'Moved', align: 'r', html: r => chg(r.actual_ret) },
    { key: 'error_pct', label: 'Miss vs midpoint', align: 'r', val: r => Math.abs(r.error_pct), html: r => fmtPct(r.error_pct) },
    { key: 'in50', label: 'In 50%', align: 'c', html: r => (r.in50 == null ? '—' : r.in50 ? '<span class="ok-mark">✓</span>' : '<span class="no-mark">✗</span>') },
    { key: 'in68', label: 'In 68%', align: 'c', html: r => (r.in68 ? '<span class="ok-mark">✓</span>' : '<span class="no-mark">✗</span>') },
    { key: 'in90', label: 'In 90%', align: 'c', html: r => (r.in90 ? '<span class="ok-mark">✓</span>' : '<span class="no-mark">✗</span>') },
    { key: 'dir_hit', label: 'Direction', align: 'c', html: r => (r.dir_hit == null ? '<span class="muted">flat</span>' : r.dir_hit ? '<span class="ok-mark">✓</span>' : '<span class="no-mark">✗</span>') },
    { key: 'source', label: 'Type', html: r => `<span class="small muted">${SRC_LABEL[r.source] || r.source}</span>` },
  ], shown, null, f === 'mine' ? 'None of your watchlist or holdings were forecast for this day' : 'No rows');
  $('#acc-t').addEventListener('click', e => { const tr = e.target.closest('tr[data-code]'); if (tr) location.hash = '#/predict/' + encodeURIComponent(tr.dataset.code); });
  mountTable($('#acc-days'), [
    { key: 'check_date', label: 'Checked on', html: r => `<a href="#/accuracy/${h}/${esc(r.check_date)}">${fmtDate(r.check_date, { weekday: 'short', day: 'numeric', month: 'short' })}</a>` },
    { key: 'made_on', label: 'Forecast made', html: r => fmtDate(r.made_on, { day: 'numeric', month: 'short' }) },
    { key: 'source', label: 'Type', html: r => SRC_LABEL[r.source] || r.source },
    { key: 'n', label: 'Stocks', align: 'r', html: r => fmtInt(r.n) },
    { key: 'in50', label: 'In 50%', align: 'r', html: r => (isNum(r.in50) ? fmt(r.in50, 0) + '%' : '—') },
    { key: 'width50', label: '50% width', align: 'r', html: r => (isNum(r.width50) ? fmt(r.width50, 1) + '%' : '—') },
    { key: 'in68', label: 'In 68%', align: 'r', html: r => `<span class="${r.in68 >= 60 ? '' : 'down'}">${fmt(r.in68, 0)}%</span>` },
    { key: 'in90', label: 'In 90%', align: 'r', html: r => fmt(r.in90, 0) + '%' },
    { key: 'dir', label: 'Direction right', align: 'r', val: r => (r.dir_n ? r.dir_hits / r.dir_n : null), html: r => (r.dir_n ? fmt((r.dir_hits / r.dir_n) * 100, 0) + '%' : '—') },
    { key: 'mae_pct', label: 'Avg miss', align: 'r', html: r => fmt(r.mae_pct, 2) + '%' },
    { key: 'bias_pct', label: 'Bias', align: 'r', title: 'Average (actual − midpoint): negative = prices ended lower than the midpoint', html: r => fmtPct(r.bias_pct) },
  ], hChecks, { sort: 'check_date', dir: -1 });

  const pt = $('#paper-t');
  if (pt && paper) {
    const ST = { pending: 'waiting for fill', open: 'open', target: 'target ✓', stopped: 'stopped ✗', expired: 'time exit', missed: 'not filled' };
    mountTable(pt, [
      { key: 'signal_date', label: 'Signal', html: r => fmtDate(r.signal_date, { day: 'numeric', month: 'short' }) },
      { key: 'code', label: 'Code', html: r => `<span class="code">${esc(r.code)}</span><span class="nm">${esc(r.name || '')}</span>` },
      { key: 'entry_limit', label: 'Entry limit', align: 'r', html: r => fmt(r.entry_limit) },
      { key: 'stop', label: 'Stop', align: 'r', html: r => `<span class="down">${fmt(r.stop)}</span>` },
      { key: 't2', label: 'Target 2', align: 'r', html: r => `<span class="up">${fmt(r.t2)}</span>` },
      { key: 'exit_by', label: 'Exit by', html: r => fmtDate(r.exit_by, { day: 'numeric', month: 'short' }) },
      { key: 'status', label: 'Status', html: r => `<span class="${r.status === 'target' ? 'up' : r.status === 'stopped' ? 'down' : 'muted'}">${ST[r.status] || esc(r.status)}</span>` },
      { key: 'entry_price', label: 'Filled at', align: 'r', html: r => fmt(r.entry_price) },
      { key: 'exit_price', label: 'Exit / last', align: 'r', html: r => fmt(r.exit_price) },
      { key: 'return_pct', label: 'Result', align: 'r', html: r => chg(r.return_pct) },
      { key: 'sessions', label: 'Sessions', align: 'r', html: r => fmtInt(r.sessions) },
    ], paper.rows || [], { sort: 'signal_date', dir: -1 }, { empty: 'No signals logged yet' });
  }
  $('#acc-date').addEventListener('change', e => { location.hash = `#/accuracy/${h}/${e.target.value}`; });
  $$('#acc-f button').forEach(b => b.addEventListener('click', () => { S.accFilter = b.dataset.f; rerender(); }));

  const c = colors();
  const daily = (elId, valueOf, target) => {
    const ch = makeChart($(elId), { rightPriceScale: { scaleMargins: { top: 0.1, bottom: 0.1 } } });
    const fmtP = { type: 'custom', formatter: v => `${v.toFixed(0)}%` };
    const series = {
      live: ch.addLineSeries({ color: c.s1, lineWidth: 2, priceFormat: fmtP, priceLineVisible: false }),
      backtest: ch.addLineSeries({ color: c.muted, lineWidth: 2, lineStyle: LWC.LineStyle.Dashed, priceFormat: fmtP, priceLineVisible: false }),
    };
    let withData = series.backtest;
    for (const [src, s] of Object.entries(series)) {
      const pts = hChecks.filter(x => x.source === src && valueOf(x) != null).map(x => ({ time: String(x.check_date), value: valueOf(x) }));
      s.setData(pts);
      if (pts.length && src === 'live') withData = s;
    }
    withData.createPriceLine({ price: target, color: c.axis, lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true });
    ch.timeScale().fitContent();
  };
  $$('#acc-lvl button').forEach(b => b.addEventListener('click', () => { S.rangeLevel = Number(b.dataset.l); rerender(); }));
  daily('#acc-range', x => (lv === 50 ? x.in50 : lv === 90 ? x.in90 : x.in68), lv);
  daily('#acc-dir', x => (x.dir_n ? (x.dir_hits / x.dir_n) * 100 : null), 50);
}

/* ---------------- Bangladesh factors */
const REGIME_CLASS = { Calm: 'b', Euphoric: '', Nervous: 'be', Stress: 'sbe' };
const evBadge = e => `<span class="label ${e === 'strong' ? 'sbe' : e === 'moderate' ? 'be' : ''}">${esc(e || 'n/a')}</span>`;
const BD_KIND_LABEL = { dse_query: 'DSE query', rumour: 'Rumour clarification', sponsor_buy: 'Sponsor buy', sponsor_sell: 'Sponsor sell',
  to_z: 'To Z', from_z: 'From Z', margin_ban: 'Margin ban', regulator: 'Regulator', govt: 'Government/state',
  state_control: 'State / regulator control', political_hit: 'Political disruption', court_hold: 'Court order' };
const BD_NEG_KINDS = ['to_z', 'margin_ban', 'rumour', 'sponsor_sell', 'state_control', 'political_hit'];

function groupRow(g) {
  const a = g.all || {}, u = g.uneasy || {};
  return `<tr><td class="wrap">${esc(g.label)}${g.hindsight ? ' <span class="chip warn" title="The group was chosen knowing what happened, so its past result looks more certain than it is">hindsight</span>' : ''}</td>
    <td class="r">${fmtInt(g.companies)}</td><td class="r">${chg(a.ex20, 1)}</td><td class="r">${isNum(a.pos20) ? fmt(a.pos20, 0) + '%' : '—'}</td>
    <td class="r">${chg(u.ex20, 1)}</td><td class="r">${isNum(u.pos20) ? fmt(u.pos20, 0) + '%' : '—'}</td></tr>`;
}

function politicalCardHtml(d) {
  const p = d.political || {};
  const groups = Object.values(p.groups || {});
  const links = d.links || [];
  const evs = d.political_events || [];
  return `<div class="card"><div class="card-h"><h2>Political &amp; state influence</h2><span class="small muted">from DSE filings, shareholding and prices · plus your own records</span></div>
    <div class="card-b">
      <h3>How these groups of shares did</h3>
      <div class="table-wrap"><table class="t"><thead><tr><th>Group</th><th class="r">Companies</th><th class="r">Next 20 sessions vs market</th><th class="r">Beat market</th><th class="r">… when the market was Nervous/Stress</th><th class="r">Beat</th></tr></thead>
        <tbody>${groups.map(groupRow).join('') || `<tr><td colspan="6">${empty('Not measured yet')}</td></tr>`}</tbody></table></div>
      <p class="small muted">Median result of every 5th session, share vs the equal-weight market. "Fell hardest on sell-off days" uses the whole year to pick the shares, so it is marked hindsight; the predictor uses a point-in-time version of it.
        Sell-off day = market down ${fmt(Math.abs(p.shock_day_pct || 1), 0)}% or more (${fmtInt(p.shock_days)} days so far).</p>
    </div>
    <div class="grid g2" style="gap:0">
      <div class="card-b"><h3>Fall hardest on sell-off days</h3><div class="table-wrap" id="shock-t"></div>
        <p class="small muted">1.0× = falls like the market; 2.0× = twice as much. Shares with at least 10 sell-off days.</p></div>
      <div class="card-b"><h3>State-owned / state-backed shares</h3><div class="table-wrap tall" id="govt-t"></div></div>
    </div>
    <div class="card-b"><h3>Takeovers, court orders and political disruption in filings (last year)</h3>
      ${evs.length ? `<ul class="list-plain small" style="max-height:340px;overflow:auto">${evs.map(e => `<li class="sp"><span>${stockLink(e.code)} <span class="muted">${esc(e.name || '')}</span></span><span><span class="chip ${BD_NEG_KINDS.includes(e.type) ? 'neg' : 'warn'}">${esc(BD_KIND_LABEL[e.type] || e.type)}</span> ${fmtDate(e.date)}</span></li>`).join('')}</ul>` : '<p class="muted small">None found.</p>'}
    </div>
    <div class="card-b"><h3>Who governed when</h3>
      <p class="small muted">Used to work out what each recorded link meant on each date: linked to the party in government, to a party that lost power (for 5 years), or to a party out of power. <b>Check the last period</b> and edit it if needed. Now in government: <b>${esc(d.ruling_now || '—')}</b>.</p>
      <div class="table-wrap"><table class="t" id="tl-t"><thead><tr><th>Government</th><th>Party</th><th>From</th><th>To</th><th></th></tr></thead><tbody>
        ${(d.timeline || []).map((t, i) => `<tr><td class="wrap">${esc(t.label)}</td><td>${esc((d.parties || {})[t.party] || 'no party')}</td><td>${fmtDate(t.from)}</td><td>${t.to ? fmtDate(t.to) : '<span class="muted">now</span>'}</td><td><button class="x-btn" data-del-period="${i}" title="Delete" aria-label="Delete period">✕</button></td></tr>`).join('')}
      </tbody></table></div>
      <form class="modal-b mt" id="tl-form" novalidate><div class="row">
        <label class="field"><span>Party in government</span><select name="party"><option value="">No party (interim / caretaker)</option>${Object.entries(d.parties || {}).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join('')}</select></label>
        <label class="field"><span>From</span><input type="date" name="from" required></label>
        <label class="field"><span>To (empty = now)</span><input type="date" name="to"></label>
        <label class="field"><span>Name</span><input name="label" maxlength="80" placeholder="e.g. BNP government"></label></div>
        <div class="sp"><div class="form-err" id="tl-err"></div><button class="btn">Add period</button></div></form>
    </div>
    ${powerChangesHtml(p.power_changes || [])}
    <div class="card-b"><h3>Your records: political links of owners, sponsors and directors</h3>
      <p class="small muted">Record what you know from news you trust, always with a source: an owner who is an MP or minister of a party, a close relative or business partner of one, a party financier, or owners under investigation. The tool never guesses anyone's party. Each record's meaning follows who governs (table above). Companies linked to a party that <b>lost power</b>, or whose owners are <b>under investigation</b>, are kept out of ACCUMULATE; all records show in the trade plan, and you can add them to the forecast in Settings. Records stay on this PC.</p>
      <div class="table-wrap"><table class="t"><thead><tr><th>Code</th><th>Person</th><th>Role · party</th><th>Position now</th><th>Since – until</th><th>Source</th><th></th></tr></thead><tbody>
        ${links.map(l => `<tr><td>${stockLink(l.code)}<div class="small muted">${esc(l.name || '')}</div></td><td class="wrap">${esc(l.person || '—')}</td>
          <td class="wrap">${l.party ? `${esc(l.role_label || l.role || '')} · <b>${esc(l.party_label)}</b>` : esc((d.alignments || {})[l.exposure] || l.exposure)}</td>
          <td class="wrap"><span class="chip ${['ousted', 'under_action'].includes(l.align_now) ? 'neg' : l.align_now === 'ruling' ? 'pos' : 'warn'}">${esc(l.align_label)}</span></td>
          <td class="small">${l.since ? fmtDate(l.since) : '—'} – ${l.until ? fmtDate(l.until) : 'now'}</td>
          <td class="wrap small">${/^https?:\/\//.test(l.source) ? `<a href="${esc(l.source)}" target="_blank" rel="noopener noreferrer">${esc(l.source.slice(0, 60))}</a>` : esc(l.source)}${l.note ? `<div class="muted">${esc(l.note)}</div>` : ''}</td>
          <td><button class="x-btn" data-del-link="${l.id}" title="Delete" aria-label="Delete record">✕</button></td></tr>`).join('') || `<tr><td colspan="7">${empty('No records yet')}</td></tr>`}
      </tbody></table></div>
      <form class="modal-b mt" id="link-form" novalidate>
        <div class="row"><label class="field"><span>Trading code</span><input name="code" required placeholder="e.g. BEXIMCO" style="text-transform:uppercase"></label>
          <label class="field"><span>Person (owner / sponsor / director)</span><input name="person" maxlength="120"></label></div>
        <div class="row"><label class="field"><span>Party</span><select name="party"><option value="">— no party (choose a situation) —</option>${Object.entries(d.parties || {}).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join('')}</select></label>
          <label class="field"><span>Role</span><select name="role"><option value="">—</option>${Object.entries(d.roles || {}).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join('')}</select></label>
          <label class="field"><span>Or a situation without a party</span><select name="situation"><option value="">—</option>${Object.entries(d.situations || {}).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join('')}</select></label></div>
        <div class="row"><label class="field"><span>Since (optional)</span><input type="date" name="since"></label>
          <label class="field"><span>Until (optional)</span><input type="date" name="until"></label>
          <label class="field"><span>Source (required)</span><input name="source" maxlength="300" required placeholder="newspaper + date, or a link"></label></div>
        <label class="field"><span>Note</span><input name="note" maxlength="500"></label>
        <div class="sp"><div class="form-err" id="link-err"></div><button class="btn btn-primary">Add record</button></div>
      </form>
    </div></div>`;
}

function powerChangesHtml(changes) {
  if (!changes.length) return '';
  return `<div class="card-b"><h3>After each change of government (inside the price history)</h3>
    <div class="table-wrap"><table class="t"><thead><tr><th>Change</th><th>Group (your records, position after the change)</th><th class="r">Companies</th><th class="r">Next 20 sessions vs market</th><th class="r">Next 60</th></tr></thead><tbody>
      ${changes.map(c => {
        const gs = Object.entries(c.groups || {});
        const rows = [['All shares', c.all], ...gs.map(([, g]) => [g.label, g])];
        return rows.map(([lab, g], i) => `<tr>${i === 0 ? `<td rowspan="${rows.length}" class="wrap"><b>${esc(c.label)}</b><div class="small muted">${fmtDate(c.date)}</div></td>` : ''}<td class="wrap">${esc(lab)}</td><td class="r">${fmtInt(g.n)}</td><td class="r">${chg(g.ex20, 1)}</td><td class="r">${chg(g.ex60, 1)}</td></tr>`).join('');
      }).join('')}
    </tbody></table></div>
    <p class="small muted">Median result of each share from the last session before the change. With few records the groups are small; add records to see how linked companies reacted when power changed.</p></div>`;
}

function politicalStockHtml(p) {
  if (!p) return '';
  const sh = p.shock, base = p.baseline || {};
  const ev = p.events || [];
  return `<h3 class="mt">Political &amp; state influence</h3>
    <div class="plan-grid">
      <div class="cell"><div class="k">Government shareholding</div><div class="v">${isNum(p.govt_hold) ? fmt(p.govt_hold, 1) + '%' : 'none'}</div><div class="d">from the DSE shareholding report</div></div>
      <div class="cell"><div class="k">Fall on sell-off days</div><div class="v ${sh && sh.beta >= 1.5 ? 'down' : ''}">${sh ? fmt(sh.beta, 2) + '× market' : '—'}</div><div class="d">${sh ? `on ${sh.days} days the market fell ${fmt(Math.abs(p.shock_day_pct || 1), 0)}%+ (political / macro shocks); higher than ${fmt(sh.pct_rank, 0)}% of shares` : 'not enough trading history'}</div></div>
      <div class="cell"><div class="k">Political link (your records)</div><div class="v ${['ousted', 'under_action'].includes(p.alignment) ? 'down' : ''}">${p.alignment ? esc(p.alignment_label) : 'none recorded'}</div><div class="d">${(p.links || []).map(l => esc(l.exposure_label)).join(' · ') || '<a href="#/bd">add one on the BD factors page</a>'}${p.ruling_party ? ` · in government now: ${esc(p.ruling_party)}` : ''}</div></div>
    </div>
    ${(p.groups || []).length ? `<div class="table-wrap mt"><table class="t"><thead><tr><th>Group this share is in</th><th class="r">Companies</th><th class="r">Next 20 vs market</th><th class="r">Beat market</th><th class="r">… in Nervous/Stress</th><th class="r">Beat</th></tr></thead><tbody>
      ${p.groups.map(groupRow).join('')}${base.all ? groupRow({ label: 'All shares (for comparison)', companies: base.companies, all: base.all, uneasy: base.uneasy }) : ''}</tbody></table></div>` : ''}
    ${ev.length ? `<div class="small muted mt">In this company's filings in the last 12 months:</div><ul class="list-plain small">${ev.map(e => `<li class="sp"><span><b>${fmtDate(e.date)}</b> · ${esc(e.label)}</span><span>${isNum(e.ex20) ? `typically ${fmtPct(e.ex20, 1)} vs market over 20 sessions (n=${e.n})` : ''} ${evBadge(e.evidence)}</span></li>`).join('')}</ul>`
      : '<p class="small muted mt">No central bank / regulator takeover, court order or political disruption in this company\'s filings in the last year.</p>'}`;
}

function bdStockCardHtml(one) {
  const b = one.bd || {};
  const spec = b.speculation;
  const reg = b.regime || {};
  const rs = (b.regime_stats || {})[reg.regime] || {};
  const events = b.recent_events || [];
  const sb = (b.spec_stats || {}).High || {};
  return `<div class="card"><div class="card-h"><h2>Bangladesh factors</h2><a class="small" href="#/bd">How these are measured →</a></div><div class="card-b">
    <div class="plan-grid">
      <div class="cell"><div class="k">Market regime now</div><div class="v"><span class="label ${REGIME_CLASS[reg.regime] || ''}">${esc(reg.regime || '—')}</span></div><div class="d">${isNum(rs.fwd20) ? `after such days the market moved ${fmtPct(rs.fwd20, 1)} over 20 sessions (up ${fmt(rs.pos20, 0)}% of the time)` : ''}</div></div>
      <div class="cell"><div class="k">Pump-and-dump risk</div><div class="v ${spec && spec.score >= 60 ? 'down' : ''}">${spec ? `${spec.score}/100` : '—'}</div><div class="d">${spec && spec.why.length ? esc(spec.why.join(' · ')) : 'no speculative pattern'}</div></div>
      <div class="cell"><div class="k">High-risk shares historically</div><div class="v">${isNum(sb.ex20) ? fmtPct(sb.ex20, 1) : '—'}</div><div class="d">typical 20-session result vs market; 1 in 10 fell ${isNum(sb.p10) ? fmt(Math.abs(sb.p10), 0) : '—'}%+, 1 in 10 gained ${isNum(sb.p90) ? fmt(sb.p90, 0) : '—'}%+</div></div>
    </div>
    <h3 class="mt">Recent Bangladesh-specific events for this share</h3>
    ${events.length ? `<ul class="list-plain small">${events.map(e => `<li class="sp"><span><b>${fmtDate(e.date, { day: 'numeric', month: 'short' })}</b> · ${esc(e.label)}</span><span>${isNum(e.ex20) ? `typically ${fmtPct(e.ex20, 1)} vs market over 20 sessions (n=${e.n})` : ''} ${evBadge(e.evidence)}</span></li>`).join('')}</ul>` : '<p class="muted small">None: no DSE query, rumour clarification, insider declaration, category change, margin ban or regulator/government news recently (political events count for up to 6 months).</p>'}
    ${politicalStockHtml(b.political)}
  </div></div>`;
}

async function viewBD(el, args, ctx) {
  const d = await api('/bd');
  if (!ctx.alive()) return;
  if (!d.built_at) {
    ctx.render(`<div class="page-h"><div><h1>Bangladesh factors</h1></div></div><div class="card">${empty('Not built yet', '<p>They are measured after each end-of-day run.</p><button class="btn btn-primary" data-job="daily" data-force="1">Run end-of-day now</button>')}</div>`);
    return;
  }
  const reg = d.regime_now || {};
  const used = s => s.direction === 'negative' && s.evidence !== 'weak';
  const studies = Object.entries(d.studies || {});
  const stats = d.regime_stats || {};
  ctx.render(`
    <div class="page-h"><div><h1>Bangladesh factors</h1>
      <div class="sub">Measured on DSE prices ${fmtDate(d.history_from)} – ${fmtDate(d.history_to)} · rebuilt every trading day after the close · updated ${fmtTime(d.built_at)}</div></div></div>
    <div class="note">Political power cannot be measured directly from any data. This page measures its <b>footprint</b>: market-wide stress, central bank / regulator takeovers, court orders, companies reporting political disruption, government shareholding, how hard each share falls on sell-off days, and how shares behaved after rumours, insider deals and category changes. Only effects with real evidence change the buy/sell signals. The tool never guesses anyone's political affiliation; you can record links you know about (with a source) and they are measured below.</div>
    <div class="grid g-main">
      <div class="card"><div class="card-h"><h2>Market regime</h2><span class="label ${REGIME_CLASS[reg.regime] || ''}">${esc(reg.regime || '—')}</span><span class="small muted">equal-weight market of all shares</span></div>
        <div class="chart-box dash" id="bd-reg"></div>
        <div class="card-b small muted"><div class="legend" style="margin:0 0 6px"><span><i style="background:var(--up-fill);border-radius:50%"></i>Calm</span><span><i style="background:var(--s7);border-radius:50%"></i>Euphoric</span><span><i style="background:var(--s2);border-radius:50%"></i>Nervous</span><span><i style="background:var(--down-fill);border-radius:50%"></i>Stress</span></div>
          Dots mark where a regime began (lasting 3+ sessions). Stress = sharp fall, drawdown ≥ 8% or volatility spike; Nervous = falling with weak breadth or drying turnover; Euphoric = strong rally on heavy turnover; Calm = otherwise.</div></div>
      <div class="card"><div class="card-h"><h2>What followed each regime</h2></div><div class="card-b">
        <div class="kv" style="grid-template-columns:1fr">
          <div><span>Now</span><span>${esc(reg.regime || '—')} · 20-session market ${fmtPct((reg.m20 || 0) * 100, 1)} · drawdown ${fmtPct((reg.drawdown || 0) * 100, 1)} · breadth ${fmt((reg.breadth5 || 0) * 100, 0)}% rising · turnover ${fmt(reg.turnover_ratio, 2)}× normal</span></div>
        </div>
        <div class="table-wrap mt"><table class="t"><thead><tr><th>Regime</th><th class="r">Days</th><th class="r">Market next 20 sessions</th><th class="r">Rose</th></tr></thead><tbody>
          ${['Calm', 'Euphoric', 'Nervous', 'Stress'].filter(k => stats[k]).map(k => `<tr><td><span class="label ${REGIME_CLASS[k]}">${k}</span></td><td class="r">${stats[k].days}</td><td class="r">${chg(stats[k].fwd20, 1)}</td><td class="r">${fmt(stats[k].pos20, 0)}%</td></tr>`).join('')}
        </tbody></table></div>
        <p class="small muted">In Stress, new BUY signals are held back (WAIT) because the market usually kept falling. One year of data contains only a few independent episodes, so treat this as a guide.</p>
      </div></div>
    </div>
    <div class="card"><div class="card-h"><h2>What happened after Bangladesh-specific events</h2><span class="small muted">share vs the equal-weight market · median of all cases</span></div>
      <div class="table-wrap"><table class="t"><thead><tr><th>Event</th><th class="r">Cases</th><th class="r" title="Share's return in the 20 sessions before the event">Run-up before</th><th class="r">Next session</th><th class="r">Next 5</th><th class="r">Next 20</th><th class="r">Beat market</th><th>Evidence</th><th>Used in signals</th></tr></thead><tbody>
        ${studies.map(([k, s]) => `<tr><td>${esc(s.label)}</td><td class="r">${fmtInt(s.n)}</td><td class="r">${chg(s.pre20, 1)}</td><td class="r">${chg(s.ex1, 1)}</td><td class="r">${chg(s.ex5, 1)}</td><td class="r" title="Trimmed mean ${fmtPct(s.wmean20, 1)} · raw mean ${fmtPct(s.mean20, 1)}">${chg(s.ex20, 1)}</td><td class="r">${isNum(s.pos20) ? fmt(s.pos20, 0) + '%' : '—'}</td><td>${evBadge(s.evidence)}</td><td class="small">${used(s) ? (k === 'record_date' ? 'Wait until after the record date' : 'Avoid new buys; holders: sell if strong') : '<span class="muted">no</span>'}</td></tr>`).join('')}
      </tbody></table></div>
      <div class="card-b small muted">"Evidence" needs the median and the outlier-trimmed mean to agree, a typical move of at least 1%, and a t-statistic ≥ 1.5 (moderate) or ≥ 2.5 (strong). Circuit days and DSE queries show huge <i>average</i> moves caused by a few shares that multiplied; the typical share did not, so they are not used as buy signals.</div></div>
    ${politicalCardHtml(d)}
    <div class="grid g2">
      <div class="card"><div class="card-h"><h2>Political &amp; regulatory events</h2><span class="small muted">market reaction (equal-weight)</span></div>
        <div class="table-wrap"><table class="t"><thead><tr><th>Date</th><th>Event</th><th class="r">5 days before</th><th class="r">Next day</th><th class="r">Next 5</th><th class="r">Next 20</th><th></th></tr></thead><tbody>
          ${(d.events || []).map(e => `<tr><td>${fmtDate(e.date)}</td><td class="wrap"><b>${esc(e.title)}</b><div class="small muted">${esc(e.kind)} · ${esc(e.source)}${e.note ? ' · ' + esc(e.note) : ''}</div></td><td class="r">${chg((e.reaction || {}).pre5, 1)}</td><td class="r">${chg((e.reaction || {}).post1, 1)}</td><td class="r">${chg((e.reaction || {}).post5, 1)}</td><td class="r">${chg((e.reaction || {}).post20, 1)}</td><td>${e.id ? `<button class="x-btn" data-del-event="${e.id}" title="Delete" aria-label="Delete event">✕</button>` : ''}</td></tr>`).join('') || `<tr><td colspan="7">${empty('No events yet')}</td></tr>`}
        </tbody></table></div>
        <form class="card-b modal-b" id="ev-form" style="padding:14px 16px" novalidate>
          <h3>Record an event (political, regulatory, economic)</h3>
          <div class="row"><label class="field"><span>Date</span><input type="date" name="date" required></label>
            <label class="field"><span>Kind</span><select name="kind"><option value="political">Political</option><option value="regulatory">Regulatory (BSEC, DSE, Bangladesh Bank)</option><option value="economic">Economic (budget, rates, FX)</option><option value="other">Other</option></select></label></div>
          <label class="field"><span>What happened</span><input name="title" maxlength="200" placeholder="e.g. Hartal / blockade, new BSEC chairman, policy rate change" required></label>
          <div class="row"><label class="field"><span>Your expected impact</span><select name="impact"><option value="">—</option><option value="-2">−2 very negative</option><option value="-1">−1 negative</option><option value="0">0 neutral</option><option value="1">+1 positive</option><option value="2">+2 very positive</option></select></label>
            <label class="field"><span>Note</span><input name="note" maxlength="500"></label></div>
          <div class="sp"><div class="form-err" id="ev-err"></div><button class="btn btn-primary">Add event</button></div>
          <p class="small muted">The market's reaction around each event is measured from DSE prices, so over time you see which kinds of events really moved the market.</p>
        </form></div>
      <div class="card"><div class="card-h"><h2>Pump-and-dump risk</h2><span class="small muted">score 0–100</span></div><div class="card-b">
        <div class="table-wrap"><table class="t"><thead><tr><th>Risk</th><th class="r">Cases</th><th class="r">Typical next 20 vs market</th><th class="r">1 in 10 fell</th><th class="r">1 in 10 gained</th></tr></thead><tbody>
          ${['Low', 'Elevated', 'High'].filter(k => (d.spec_stats || {})[k]).map(k => { const s = d.spec_stats[k]; return `<tr><td>${k}</td><td class="r">${fmtInt(s.samples)}</td><td class="r">${chg(s.ex20, 1)}</td><td class="r down">${fmtPct(s.p10, 0)}</td><td class="r up">${fmtPct(s.p90, 0)}</td></tr>`; }).join('')}
        </tbody></table></div>
        <p class="small muted">Points: small company 20, free float &lt; 30% 15, category Z 15, +30% in a month 20, 2+ circuit days 15, volume 3× normal 10, recent DSE query 15. High-risk shares are a lottery: most lag, a few explode. BUY signals skip them.</p>
        <h3 class="mt">Highest risk today</h3>
        <div class="table-wrap" id="spec-t"></div>
      </div></div>
    </div>
    <div class="card"><div class="card-h"><h2>Rumours, insider deals &amp; regulator news (last 30 days)</h2></div><div class="card-b">
      ${(d.recent_flagged || []).length ? `<ul class="list-plain">${d.recent_flagged.map(n => `<li><div class="sp" style="align-items:flex-start"><span>${stockLink(n.code)} <b>${esc(n.summary)}</b> <span class="small muted">${fmtDate(n.news_date, { day: 'numeric', month: 'short' })}</span></span><span class="chips">${n.kinds.map(k => `<span class="chip ${BD_NEG_KINDS.includes(k) ? 'neg' : k === 'sponsor_buy' || k === 'from_z' ? 'pos' : 'warn'}">${esc(BD_KIND_LABEL[k] || k)}</span>`).join('')}</span></div><div class="small muted">${esc((n.body || '').slice(0, 220))}</div></li>`).join('')}</ul>` : '<p class="muted">None.</p>'}
    </div></div>`);
  mountTable($('#spec-t'), [
    { key: 'code', label: 'Code', html: r => `<span class="code">${esc(r.code)}</span><span class="nm">${esc(r.name || '')}</span>` },
    { key: 'score', label: 'Risk', align: 'r', html: r => `<b class="${r.score >= 60 ? 'down' : ''}">${r.score}</b>` },
    { key: 'r20', label: '1 month', align: 'r', html: r => chg(r.r20, 1) },
    { key: 'why', label: 'Why', cls: 'wrap', html: r => `<span class="small">${esc(r.why.join(' · '))}</span>` },
  ], d.spec_top || [], { sort: 'score', dir: -1 }, { empty: 'No high-risk shares today' });
  mountTable($('#shock-t'), [
    { key: 'code', label: 'Code', html: r => `<span class="code">${esc(r.code)}</span><span class="nm">${esc(r.name || '')}</span>` },
    { key: 'beta', label: 'Fall vs market', align: 'r', html: r => `<b class="${r.beta >= 1.5 ? 'down' : ''}">${fmt(r.beta, 2)}×</b>` },
    { key: 'avg', label: 'Avg on those days', align: 'r', html: r => chg(r.avg, 1) },
    { key: 'days', label: 'Days', align: 'r', html: r => fmtInt(r.days) },
  ], d.shock_top || [], { sort: 'beta', dir: -1 }, { empty: 'Not enough history' });
  mountTable($('#govt-t'), [
    { key: 'code', label: 'Code', html: r => `<span class="code">${esc(r.code)}</span><span class="nm">${esc(r.name || '')}</span>` },
    { key: 'govt_hold', label: 'Government', align: 'r', html: r => fmt(r.govt_hold, 1) + '%' },
    { key: 'beta', label: 'Fall on sell-offs', align: 'r', html: r => isNum(r.beta) ? fmt(r.beta, 2) + '×' : '—' },
  ], d.govt_top || [], { sort: 'govt_hold', dir: -1 }, { empty: 'No state-owned shares found' });
  const saveTimeline = async periods => {
    await api('/bd/timeline', { method: 'POST', body: { periods } });
    toast('Timeline saved; political positions are being re-measured');
    setTimeout(rerender, 3000);
  };
  const tf = $('#tl-form');
  if (tf) tf.addEventListener('submit', async e => {
    e.preventDefault();
    try { await saveTimeline([...(d.timeline || []), Object.fromEntries(new FormData(tf).entries())]); }
    catch (err) { $('#tl-err').textContent = err.message; }
  });
  const tt = $('#tl-t');
  if (tt) tt.addEventListener('click', async e => {
    const b = e.target.closest('[data-del-period]');
    if (!b || !confirm('Delete this period?')) return;
    try { await saveTimeline((d.timeline || []).filter((_, i) => i !== Number(b.dataset.delPeriod))); }
    catch (err) { toast(err.message, 'err'); }
  });
  const lf = $('#link-form');
  if (lf) lf.addEventListener('submit', async e => {
    e.preventDefault();
    try {
      await api('/bd/links', { method: 'POST', body: Object.fromEntries(new FormData(lf).entries()) });
      toast('Recorded; group results are being re-measured');
      setTimeout(rerender, 3000);
    } catch (err) { $('#link-err').textContent = err.message; }
  });
  const f = $('#ev-form');
  f.addEventListener('submit', async e => {
    e.preventDefault();
    try {
      await api('/bd/events', { method: 'POST', body: { date: f.date.value, kind: f.kind.value, title: f.title.value, impact: f.impact.value, note: f.note.value } });
      toast('Event added; market reaction is being measured');
      setTimeout(rerender, 2500);
    } catch (err) { $('#ev-err').textContent = err.message; }
  });
  const hist = d.regime_history || [];
  if (hist.length) {
    const c = colors();
    const ch = makeChart($('#bd-reg'));
    const s = ch.addLineSeries({ color: c.s1, lineWidth: 2, priceLineVisible: false, priceFormat: { type: 'price', precision: 3, minMove: 0.001 } });
    s.setData(hist.map(h => ({ time: String(h.date), value: h.index })));
    const colorOf = { Stress: c.down, Nervous: c.s2, Euphoric: c.s7, Calm: c.up };
    // mark regime changes that lasted at least 3 sessions (colour dots; legend below the chart)
    const markers = [];
    let cur = null;
    hist.forEach((h, i) => {
      const lasts = hist.slice(i, i + 3).every(x => x.regime === h.regime);
      if (h.regime !== cur && lasts) {
        markers.push({ time: String(h.date), position: 'aboveBar', color: colorOf[h.regime] || c.muted, shape: 'circle', size: 1 });
        cur = h.regime;
      }
    });
    s.setMarkers(markers);
    ch.timeScale().fitContent();
  }
}

function paperCardHtml(rec) {
  if (!rec) return '';
  const cnt = rec.counts || {};
  return `<div class="card"><div class="card-h"><h2>Live paper-trading record</h2><span class="small muted">every BUY signal followed with real prices since ${fmtDate(rec.since)}</span><a class="small" href="#/accuracy">Full log →</a></div><div class="card-b">
    <div class="plan-grid">
      <div class="cell"><div class="k">Closed trades</div><div class="v">${fmtInt(rec.closed || 0)}</div><div class="d">${fmtInt(cnt.open || 0)} open · ${fmtInt(cnt.pending || 0)} waiting for a fill · ${fmtInt(cnt.missed || 0)} not filled</div></div>
      <div class="cell"><div class="k">Target reached</div><div class="v">${isNum(rec.win_rate) ? fmt(rec.win_rate, 0) + '%' : '—'}</div><div class="d">stopped ${isNum(rec.stop_rate) ? fmt(rec.stop_rate, 0) + '%' : '—'}</div></div>
      <div class="cell"><div class="k">Average per trade</div><div class="v">${isNum(rec.avg_return) ? `<span class="${dir(rec.avg_return)}">${fmtPct(rec.avg_return, 2)}</span>` : '—'}</div><div class="d">after commission both ways</div></div>
      <div class="cell"><div class="k">Open trades now</div><div class="v">${isNum(rec.open_avg_return) ? `<span class="${dir(rec.open_avg_return)}">${fmtPct(rec.open_avg_return, 2)}</span>` : '—'}</div><div class="d">average, marked to the last close</div></div>
    </div>
    <p class="small muted mt">This is the honest test: signals are logged before the outcome is known. Watch it for several weeks before trusting real money to the signals.</p></div></div>`;
}

/* ---------------- watchlist */
async function viewWatchlist(el, args, ctx) {
  const d = await api('/watchlist');
  if (!ctx.alive()) return;
  Object.assign(S.flagLabels, d.flag_labels || {});
  getQuotes().catch(() => {});
  ctx.render(`
    <div class="page-h"><div><h1>Watchlist</h1><div class="sub">Stocks you follow. They appear in the daily report and on the dashboard.</div></div>
      <form class="actions" id="w-add" autocomplete="off"><input name="code" list="codes" placeholder="Trading code, e.g. BXPHARMA" required style="width:220px"><button class="btn btn-primary">Add</button></form></div>
    <div class="card"><div class="table-wrap" id="w-table"></div></div>`);
  const cols = [COL.code, COL.ltp, COL.chg, COL.volume, COL.rsi, COL.signal, COL.r1w, COL.r1m, COL.range, COL.flags,
    { key: 'note', label: 'Note', html: r => `<span class="small">${esc(r.note || '')}</span>` },
    { key: 'x', label: '', sort: false, html: r => `<button class="x-btn" data-unwatch="${esc(r.code)}" title="Remove" aria-label="Remove ${esc(r.code)}">✕</button>` }];
  mountTable($('#w-table'), cols, d.rows, { sort: 'code', dir: 1 }, { empty: 'Your watchlist is empty — add a trading code above' });
  $('#w-add').addEventListener('submit', async e => {
    e.preventDefault();
    const code = e.target.code.value.trim().toUpperCase();
    try { await api('/watchlist', { method: 'POST', body: { code } }); toast(`${code} added`); S.quotes = null; rerender(); }
    catch (err) { toast(err.message, 'err'); }
  });
}

/* ---------------- portfolio */
async function viewPortfolio(el, args, ctx) {
  const [d, pl, rv] = await Promise.all([api('/portfolio'), api('/plans').catch(() => null), api('/ai/portfolio').catch(() => ({}))]);
  if (!ctx.alive()) return;
  S.commissionRate = d.commission_rate;
  const s = d.summary, t = s.totals;
  const planOf = Object.fromEntries(((pl && pl.holdings) || []).map(p => [p.code, p]));
  const hcols = [
    COL.code,
    { key: 'action', label: 'Action', html: r => { const p = planOf[r.code]; return p ? `<div title="${esc((p.reasons || []).join('\n'))}">${sigBadge(p.signal)}<div class="small muted" style="width:260px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc((p.reasons || [])[0] || '')}</div></div>` : '—'; } },
    { key: 'trail', label: 'Trailing stop', align: 'r', html: r => { const p = planOf[r.code]; return p && p.holding ? `<span class="down">${fmt(p.holding.trailing_stop)}</span>` : '—'; } },
    { key: 'qty', label: 'Shares', align: 'r', html: r => fmtInt(r.qty) },
    { key: 'avg_cost', label: 'Avg cost', align: 'r', html: r => fmt(r.avg_cost) },
    { key: 'price', label: 'Price', align: 'r', html: r => fmt(r.price) },
    { key: 'change_pct', label: 'Today', align: 'r', html: r => chg(r.change_pct) },
    { key: 'market_value', label: 'Value', align: 'r', html: r => fmt(r.market_value, 0) },
    { key: 'unrealized', label: 'P/L (Tk)', align: 'r', html: r => tkSigned(r.unrealized) },
    { key: 'unrealized_pct', label: 'P/L %', align: 'r', html: r => chg(r.unrealized_pct) },
    { key: 'day_change', label: 'Day P/L', align: 'r', html: r => tkSigned(r.day_change) },
    { key: 'weight', label: 'Weight', align: 'r', html: r => fmt(r.weight, 1) + '%' },
  ];
  const tcols = [
    { key: 'trade_date', label: 'Date', html: r => fmtDate(r.trade_date) },
    { key: 'side', label: 'Side', html: r => `<b class="${r.side === 'BUY' ? 'up' : 'down'}">${r.side}</b>` },
    { key: 'code', label: 'Code', html: r => stockLink(r.code) },
    { key: 'qty', label: 'Qty', align: 'r', html: r => fmtInt(r.qty) },
    { key: 'price', label: 'Price', align: 'r', html: r => fmt(r.price) },
    { key: 'fee', label: 'Fees', align: 'r', html: r => fmt(r.fee) },
    { key: 'total', label: 'Total', align: 'r', val: r => r.qty * r.price, html: r => fmt(r.side === 'BUY' ? r.qty * r.price + r.fee : r.qty * r.price - r.fee) },
    { key: 'note', label: 'Note', html: r => `<span class="small">${esc(r.note || '')}</span>` },
    { key: 'x', label: '', sort: false, html: r => `<button class="x-btn" data-del-tx="${r.id}" title="Delete" aria-label="Delete transaction">✕</button>` },
  ];
  const maxSec = Math.max(1, ...s.sectors.map(x => x.pct));
  ctx.render(`
    <div class="page-h"><div><h1>Portfolio</h1><div class="sub">Average-cost method · commission ${fmt(S.commissionRate)}% per side by default (change in Settings)</div></div>
      <div class="actions"><button class="btn btn-primary" id="p-add">+ Add transaction</button></div></div>
    <div class="tiles">
      <div class="tile"><div class="k">Market value</div><div class="v">${tk(t.market_value, 0)}</div><div class="d">${t.positions} position${t.positions === 1 ? '' : 's'}</div></div>
      <div class="tile"><div class="k">Invested (cost)</div><div class="v">${tk(t.cost, 0)}</div><div class="d">incl. Tk ${fmt(t.fees, 0)} fees paid</div></div>
      <div class="tile"><div class="k">Unrealised P/L</div><div class="v">${tkSigned(t.unrealized)}</div><div class="d">${chg(t.unrealized_pct)}</div></div>
      <div class="tile"><div class="k">Today</div><div class="v">${tkSigned(t.day_change)}</div><div class="d">${chg(t.day_change_pct)}</div></div>
      <div class="tile"><div class="k">Realised P/L</div><div class="v">${tkSigned(t.realized)}</div><div class="d">from closed sales</div></div>
    </div>
    <div class="grid g-main">
      <div class="card"><div class="card-h"><h2>Holdings</h2></div><div class="table-wrap" id="p-hold"></div></div>
      <div class="card"><div class="card-h"><h2>Allocation by sector</h2></div><div class="card-b">
        ${s.sectors.length ? `<div class="hbars">${s.sectors.map(x => `<div class="hbar"><span class="n">${esc(x.sector)}</span><div class="track"><div class="fill mag" style="width:${(x.pct / maxSec) * 100}%"></div></div><span class="val">${fmt(x.pct, 1)}%</span></div>`).join('')}</div>` : '<p class="muted small">No holdings.</p>'}
      </div></div>
    </div>
    ${s.holdings.length ? `<div class="card"><div class="card-h"><h2>AI portfolio review</h2><span class="small muted">local LLM, from your holdings, the tool's signals and health checks</span><button class="btn btn-sm" id="ai-review">${rv && rv.review ? 'Review again' : 'Review my portfolio'}</button></div>
      <div class="card-b ai-note" id="ai-review-out">${rv && rv.review ? mdLite(rv.review.note) + `<p class="small muted">${esc(rv.review.model || '')} · ${fmtTime(rv.review.created_at)}</p>` : '<p class="muted small">Click to get a plain-language review (about 3–5 minutes on your PC).</p>'}</div></div>` : ''}
    <div class="card"><div class="card-h"><h2>Transactions</h2><span class="small muted">${d.transactions.length} recorded</span></div><div class="table-wrap tall" id="p-tx"></div></div>`);
  const rb = $('#ai-review');
  if (rb) rb.addEventListener('click', async () => {
    rb.disabled = true;
    rb.textContent = 'Reviewing…';
    $('#ai-review-out').innerHTML = '<p class="muted small">The local model is reviewing your portfolio (3–5 minutes, keep this page open)…</p>';
    try {
      const r = await api('/ai/portfolio', { method: 'POST' });
      $('#ai-review-out').innerHTML = mdLite(r.review.note) + `<p class="small muted">${esc(r.review.model || '')} · ${fmtTime(r.review.created_at)}</p>`;
      rb.textContent = 'Review again';
    } catch (err) { $('#ai-review-out').innerHTML = `<p class="down small">${esc(err.message)}</p>`; rb.textContent = 'Review my portfolio'; }
    finally { rb.disabled = false; }
  });
  mountTable($('#p-hold'), hcols, s.holdings, { sort: 'market_value', dir: -1 }, { empty: 'No open positions — add a BUY transaction' });
  mountTable($('#p-tx'), tcols, d.transactions.map(r => ({ ...r, code_: r.code })), { sort: 'trade_date', dir: -1 }, { noNav: true, empty: 'No transactions yet' });
  $('#p-add').addEventListener('click', () => openTxModal());
}

/* ---------------- alerts */
async function viewAlerts(el, args, ctx) {
  const d = await api('/alerts');
  if (!ctx.alive()) return;
  S.alertKinds = d.kinds;
  ctx.render(`
    <div class="page-h"><div><h1>Alerts</h1><div class="sub">Price and %-change alerts are checked every few minutes while the dashboard is running and DSE is open. Every alert is checked again by the end-of-day run.</div></div></div>
    <div class="card"><div class="card-h"><h2>New alert</h2></div>
      <form class="card-b modal-b" id="al-form" style="padding:14px 16px" novalidate>${alertFormFields(d.kinds, { kind: 'price_above' })}
        <div class="sp"><div class="form-err" id="al-err"></div><button class="btn btn-primary">Create alert</button></div></form></div>
    <div class="card"><div class="card-h"><h2>Your alerts</h2><span class="small muted">${d.alerts.filter(a => a.active).length} active</span></div><div class="table-wrap" id="al-table"></div></div>
    <div class="card"><div class="card-h"><h2>Triggered</h2><span class="small muted">last 100</span></div><div class="table-wrap tall" id="ev-table"></div></div>`);
  const f = $('#al-form');
  const read = bindAlertForm(f, d.kinds);
  f.addEventListener('submit', async e => {
    e.preventDefault();
    try { await api('/alerts', { method: 'POST', body: read() }); toast('Alert created'); rerender(); }
    catch (err) { $('#al-err').textContent = err.message; }
  });
  mountTable($('#al-table'), [
    { key: 'code', label: 'Code', html: r => `<span class="code">${esc(r.code)}</span><span class="nm">${esc(r.name || '')}</span>` },
    { key: 'description', label: 'Condition', cls: 'wrap' },
    { key: 'price', label: 'Last price', align: 'r', html: r => fmt(r.price) },
    { key: 'active', label: 'Status', html: r => `<button class="btn btn-sm ${r.active ? 'on' : ''}" data-toggle-alert="${r.id}">${r.active ? 'Active' : 'Paused'}</button>` },
    { key: 'repeat_daily', label: 'Repeats', html: r => (r.repeat_daily ? 'Daily' : 'Once') },
    { key: 'last_triggered_on', label: 'Last triggered', html: r => fmtDate(r.last_triggered_on) },
    { key: 'note', label: 'Note', html: r => `<span class="small">${esc(r.note || '')}</span>` },
    { key: 'x', label: '', sort: false, html: r => `<button class="x-btn" data-del-alert="${r.id}" title="Delete" aria-label="Delete alert">✕</button>` },
  ], d.alerts, { sort: 'code', dir: 1 }, { empty: 'No alerts yet' });
  mountTable($('#ev-table'), [
    { key: 'triggered_at', label: 'When', html: r => fmtTime(r.triggered_at) },
    { key: 'code', label: 'Code', html: r => `<span class="code">${esc(r.code)}</span>` },
    { key: 'message', label: 'What happened', cls: 'wrap' },
  ], d.events, { sort: 'triggered_at', dir: -1 }, { empty: 'Nothing has triggered yet' });
}

/* ---------------- reports */
async function viewReports(el, args, ctx) {
  if (args[0]) return viewReport(el, args[0], ctx);
  const d = await api('/reports');
  if (!ctx.alive()) return;
  ctx.render(`
    <div class="page-h"><div><h1>Daily reports</h1><div class="sub">Generated after each trading session by the end-of-day run.</div></div>
      <div class="actions"><button class="btn" data-job="daily">Run end-of-day now</button></div></div>
    <div class="card"><div class="table-wrap" id="r-table"></div></div>`);
  const t = $('#r-table');
  t.innerHTML = tableHtml([
    { key: 'date', label: 'Session', html: r => `<a href="#/reports/${esc(r.date)}">${fmtDate(r.date, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' })}</a>` },
    { key: 'dsex', label: 'DSEX', align: 'r', html: r => fmt(r.dsex) },
    { key: 'dsex_pct', label: 'Change', align: 'r', html: r => chg(r.dsex_pct) },
    { key: 'turnover_mn', label: 'Turnover', align: 'r', html: r => 'Tk ' + crore(r.turnover_mn) },
    { key: 'alerts', label: 'Alerts', align: 'r' },
  ], d.reports, null, 'No reports yet. They are created after the first end-of-day run.');
  t.addEventListener('click', e => { const tr = e.target.closest('tr'); const a = tr && $('a', tr); if (a && !e.target.closest('a')) location.hash = a.getAttribute('href'); });
}
async function viewReport(el, date, ctx) {
  const [d, list] = await Promise.all([api('/reports/' + encodeURIComponent(date)), api('/reports')]);
  if (!ctx.alive()) return;
  Object.assign(S.flagLabels, d.flag_labels || {});
  const r = d.report, m = r.market || {}, pf = r.portfolio || {};
  const dates = list.reports.map(x => String(x.date));
  const i = dates.indexOf(date);
  const newer = i > 0 ? dates[i - 1] : null, older = i >= 0 && i < dates.length - 1 ? dates[i + 1] : null;
  const small = rows => tableHtml([
    { key: 'code', label: 'Code', html: x => `<span class="code">${esc(x.code)}</span>` },
    { key: 'close', label: 'Close', align: 'r', html: x => fmt(x.close) },
    { key: 'change_pct', label: 'Change', align: 'r', html: x => chg(x.change_pct) },
    { key: 'value_mn', label: 'Value', align: 'r', html: x => crore(x.value_mn) },
  ], rows || [], null);
  const idxTile = k => { const x = (r.indices || {})[k]; return `<div class="tile idx"><div class="k"><span>${k}</span></div><div class="v">${x ? fmt(x.value) : '—'}</div><div class="d">${x ? `<span class="${dir(x.change)}">${arrow(x.change)}${fmtSigned(x.change)} (${fmtPct(x.pct)})</span>` : '&nbsp;'}</div></div>`; };
  const hl = Object.entries(r.highlights || {});
  ctx.render(`
    <div class="page-h"><div><h1>Daily report</h1><div class="sub">${fmtDate(r.date, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })} · generated ${fmtTime(r.generated_at)}</div></div>
      <div class="actions no-print">${older ? `<a class="btn" href="#/reports/${older}">← Older</a>` : ''}${newer ? `<a class="btn" href="#/reports/${newer}">Newer →</a>` : ''}<button class="btn" onclick="window.print()">Print</button><a class="btn" href="#/reports">All reports</a></div></div>
    <div class="tiles">${['DSEX', 'DS30', 'DSES'].map(idxTile).join('')}
      <div class="tile"><div class="k">Turnover</div><div class="v">Tk ${crore(m.turnover_mn)}</div><div class="d">${isNum(m.turnover_change_pct) ? chg(m.turnover_change_pct, 1) + ' vs previous' : '&nbsp;'}</div></div>
      <div class="tile"><div class="k">Breadth</div><div class="v" style="font-size:18px"><span class="up">▲${m.advanced ?? '—'}</span> <span class="down">▼${m.declined ?? '—'}</span> <span class="flat">=${m.unchanged ?? '—'}</span></div><div class="d">${m.traded} instruments traded</div></div>
    </div>
    ${r.ai_brief ? `<div class="card"><div class="card-h"><h2>AI market brief</h2><span class="small muted">local LLM, written only from the figures below</span></div><div class="card-b ai-note">${mdLite(r.ai_brief)}</div></div>` : ''}
    ${pf.positions ? `<div class="card"><div class="card-h"><h2>Your portfolio</h2></div><div class="card-b"><div class="kv">
      <div><span>Market value</span><span>${tk(pf.market_value, 0)}</span></div><div><span>Today</span><span>${tkSigned(pf.day_change)} (${chg(pf.day_change_pct)})</span></div>
      <div><span>Unrealised P/L</span><span>${tkSigned(pf.unrealized)} (${chg(pf.unrealized_pct)})</span></div><div><span>Realised P/L</span><span>${tkSigned(pf.realized)}</span></div></div></div></div>` : ''}
    ${(r.trade_signals || {}).counts ? `<div class="card"><div class="card-h"><h2>Buy / sell signals</h2><span class="small muted">${Object.entries(r.trade_signals.counts).map(([k, v]) => `${esc(k)} ${v}`).join(' · ')}</span><a class="small" href="#/predict">Prediction page</a></div>
      ${(r.trade_signals.holdings || []).length ? `<div class="card-b"><h3>Your holdings</h3></div><div class="table-wrap rn">${signalTable(r.trade_signals.holdings.map(h => ({ ...h, holding: { trailing_stop: h.trailing_stop } })), '')}</div>` : ''}
      <div class="card-b"><h3>BUY signals</h3></div><div class="table-wrap rn">${signalTable(r.trade_signals.buy || [], 'No BUY signals this session')}</div></div>` : ''}
    ${(r.forecast_check || []).length ? `<div class="card"><div class="card-h"><h2>Forecast check</h2><span class="small muted">earlier forecasts vs today's real close</span><a class="small" href="#/accuracy">Details</a></div>
      <div class="table-wrap"><table class="t"><thead><tr><th>Horizon</th><th>Type</th><th>Made on</th><th class="r">Stocks</th><th class="r">In 50% range</th><th class="r">In 68% range</th><th class="r">In 90% range</th><th class="r">Direction right</th><th class="r">Avg miss</th></tr></thead><tbody>
      ${r.forecast_check.map(c => `<tr><td>${esc({ 1: 'Next session', 5: '1 week', 20: '1 month', 60: '3 months' }[c.horizon] || c.horizon)}</td><td>${esc(SRC_LABEL[c.source] || c.source)}</td><td>${fmtDate(c.made_on, { day: 'numeric', month: 'short' })}</td><td class="r">${fmtInt(c.n)}</td><td class="r">${isNum(c.in50) ? `${fmt(c.in50, 0)}% <span class="muted small">/ 50</span>` : '—'}</td><td class="r">${fmt(c.in68, 0)}% <span class="muted small">/ 68</span></td><td class="r">${fmt(c.in90, 0)}% <span class="muted small">/ 90</span></td><td class="r">${fmt(c.direction, 0)}%</td><td class="r">${fmt(c.mae_pct, 2)}%</td></tr>`).join('')}
      </tbody></table></div></div>` : ''}
    ${(r.news_mine || []).length ? `<div class="card"><div class="card-h"><h2>News for your stocks</h2><span class="small muted">watchlist &amp; holdings · ${fmtInt(r.news_count)} announcements market-wide today</span></div><div class="card-b">${newsListHtml(r.news_mine.map(n => ({ ...n, news_date: r.date })), {}, true)}</div></div>` : ''}
    ${(r.alerts || []).length ? `<div class="card"><div class="card-h"><h2>Alerts triggered</h2></div><div class="card-b"><ul class="list-plain">${r.alerts.map(e => `<li>${stockLink(e.code)} ${esc(e.message)} <span class="muted small">${fmtTime(e.triggered_at)}</span></li>`).join('')}</ul></div></div>` : ''}
    ${(r.watchlist || []).length ? `<div class="card"><div class="card-h"><h2>Watchlist</h2></div><div class="table-wrap" id="rw"></div></div>` : ''}
    <div class="grid g3">
      <div class="card"><div class="card-h"><h2>Top gainers</h2></div><div class="table-wrap rn">${small(r.top_gainers)}</div></div>
      <div class="card"><div class="card-h"><h2>Top losers</h2></div><div class="table-wrap rn">${small(r.top_losers)}</div></div>
      <div class="card"><div class="card-h"><h2>Most traded</h2></div><div class="table-wrap rn">${small(r.top_turnover)}</div></div>
    </div>
    <div class="grid g2">
      <div class="card"><div class="card-h"><h2>Sector performance</h2><span class="small muted">average % change</span></div><div class="card-b">${sectorBars(r.sectors)}</div></div>
      <div class="card"><div class="card-h"><h2>Signal highlights</h2><span class="small muted">sorted by traded value</span></div><div class="card-b">
        ${signalBreadth(r.signal_breadth || {})}
        ${hl.length ? hl.map(([flag, rows]) => `<h3 class="mt">${esc(S.flagLabels[flag] || flag)} · ${rows.length}</h3><div class="chips mt">${rows.map(x => `<a class="chip" href="#/stock/${encodeURIComponent(x.code)}" title="${esc(x.name || '')}">${esc(x.code)} <span class="${dir(x.change_pct)}">${fmtPct(x.change_pct, 1)}</span></a>`).join('')}</div>`).join('') : '<p class="muted small">No highlights.</p>'}
      </div></div>
    </div>
    ${(r.picks_short || []).length || (r.picks_long || []).length ? `<div class="grid g2">
      <div class="card"><div class="card-h"><h2>Short-term setups</h2><a class="small" href="#/predict">Prediction page</a></div><div class="card-b">
        ${(r.picks_short || []).length ? `<div class="chips">${r.picks_short.map(x => `<a class="chip" href="#/predict/${encodeURIComponent(x.code)}" title="Stop ${fmt(x.stop)} · target ${fmt(x.target)}">${esc(x.code)} ${fmt(x.close)}</a>`).join('')}</div>` : '<p class="muted small">No share matched the setup this session.</p>'}</div></div>
      <div class="card"><div class="card-h"><h2>Long-term (regular buying)</h2></div><div class="card-b">
        <div class="chips">${(r.picks_long || []).map(x => `<a class="chip" href="#/predict/${encodeURIComponent(x.code)}" title="Score ${fmt(x.score, 0)} · P/E ${fmt(x.pe, 1)} · yield ${fmt(x.dividend_yield, 1)}%">${esc(x.code)} · ${fmt(x.score, 0)}</a>`).join('')}</div></div></div>
    </div>` : ''}
    <p class="disclaimer">Signals are rule-based summaries of chart indicators for personal research, not investment advice.</p>`);
  $$('.rn').forEach(mountRowNav);
  const rw = $('#rw');
  if (rw) mountTable(rw, [COL.code, { key: 'close', label: 'Close', align: 'r', html: x => fmt(x.close) }, COL.chg, COL.rsi, COL.signal, COL.flags], r.watchlist.map(x => ({ ...x, volume: x.volume ?? 1 })), { sort: 'code', dir: 1 });
}

/* ---------------- settings */
async function viewSettings(el, args, ctx) {
  const [cfg, st, ai] = await Promise.all([api('/settings'), api('/status'), api('/ai/status').catch(() => ({}))]);
  if (!ctx.alive()) return;
  const s = cfg.settings, h = st.history || {};
  const runs = st.runs || [];
  ctx.render(`
    <div class="page-h"><div><h1>Settings</h1><div class="sub">Tracker v${esc(st.version)} · MySQL ${esc(cfg.mysql.user)}@${esc(cfg.mysql.host)}:${esc(cfg.mysql.port)} / <b>${esc(cfg.mysql.database)}</b></div></div></div>
    <div class="grid g2">
      <div class="card"><div class="card-h"><h2>Data</h2></div><div class="card-b">
        <div class="kv" style="grid-template-columns:1fr">
          <div><span>Instruments</span><span>${fmtInt(st.instruments)}</span></div>
          <div><span>Price rows stored</span><span>${fmtInt(h.n_rows)}</span></div>
          <div><span>Trading days</span><span>${fmtInt(h.days)} (${fmtDate(h.first_date)} – ${fmtDate(h.last_date)})</span></div>
          <div><span>Signals computed for</span><span>${fmtDate(st.signal_date)}</span></div>
          <div><span>Latest daily report</span><span>${fmtDate(st.last_report)}</span></div>
        </div>
        <div class="toolbar mt">
          <button class="btn" data-job="refresh-now">Fetch prices now</button>
          <button class="btn" data-job="daily" data-force="1">Run end-of-day now</button>
          <button class="btn" data-job="analyze">Re-run analysis</button>
          <button class="btn" data-job="backfill">Re-download 1-year history</button>
          <button class="btn" data-job="news">Fetch news now</button>
          <button class="btn" data-job="predict">Retrain forecasts</button>
          <button class="btn" data-job="simulate">Rebuild forecast history</button>
        </div>
        <p class="small muted">End-of-day normally runs by itself after the session closes (2:10 PM + 10 min). History download fetches each company page from DSE with a short pause between requests.</p>
      </div></div>
      <div class="card"><div class="card-h"><h2>Daily monitoring</h2></div><div class="card-b small">
        <p>While this dashboard is running it refreshes prices every few minutes during market hours, checks your alerts, and runs the end-of-day analysis after the close.</p>
        <p>To monitor <b>every trading day even when the dashboard is closed</b>, register the Windows scheduled task once (PowerShell, in the project folder):</p>
        <p><code>powershell -ExecutionPolicy Bypass -File scripts\\install_tasks.ps1</code></p>
        <p>It runs <code>python run.py daily</code> at 2:30 PM Sunday–Thursday (and catches up after the PC was off). Add <code>-StartServerAtLogon</code> to also start this dashboard when you sign in.</p>
      </div></div>
    </div>
    <div class="grid g2">
      <form class="card" id="set-form" autocomplete="off"><div class="card-h"><h2>Preferences</h2></div><div class="card-b modal-b" style="padding:14px 16px">
        <div class="row">
          <label class="field"><span>Broker commission per side (%)</span><input type="number" step="0.01" min="0" name="commission_rate" value="${esc(s.commission_rate)}"></label>
          <label class="field"><span>Circuit-limit warning at (± %)</span><input type="number" step="0.1" min="1" name="circuit_pct" value="${esc(s.circuit_pct)}"></label>
        </div>
        <label class="check"><input type="checkbox" name="intraday_alerts" ${s.intraday_alerts === '1' ? 'checked' : ''}> Check price alerts during market hours</label>
        <h3>Position sizing (trade plans)</h3>
        <div class="row">
          <label class="field"><span>Trading capital (Tk)</span><input type="number" step="1000" min="0" name="capital" value="${esc(s.capital)}"></label>
          <label class="field"><span>Max loss per trade (% of capital)</span><input type="number" step="0.1" min="0.1" name="risk_pct" value="${esc(s.risk_pct)}"></label>
        </div>
        <div class="row">
          <label class="field"><span>Max in one share (% of capital)</span><input type="number" step="1" min="1" name="max_position_pct" value="${esc(s.max_position_pct)}"></label>
          <label class="field"><span>AI news scoring per day (minutes)</span><input type="number" step="1" min="0" name="ai_daily_minutes" value="${esc(s.ai_daily_minutes)}"></label>
        </div>
        <label class="field"><span>AI company health checks per day (minutes)</span><input type="number" step="1" min="0" name="ai_profile_minutes" value="${esc(s.ai_profile_minutes)}"></label>
        <h3>Political records in the forecast</h3>
        <label class="check"><input type="checkbox" name="use_political_records" ${s.use_political_records === '1' ? 'checked' : ''}> Add my recorded political links (party, MP, minister, relative …) to the combined prediction</label>
        <p class="small muted">Off by default: you choose these companies knowing what happened to them, so their measured effect looks stronger than it will be. They always show in the trade plan. Takes effect at the next forecast update.</p>
        <h3>Notifications</h3>
        <label class="check"><input type="checkbox" name="notify_toast" ${s.notify_toast === '1' ? 'checked' : ''}> Windows desktop notifications</label>
        <label class="check"><input type="checkbox" name="notify_telegram" ${s.notify_telegram === '1' ? 'checked' : ''}> Telegram messages (to your phone)</label>
        <div class="row">
          <label class="field"><span>Telegram bot token</span><input type="password" name="telegram_token" value="${esc(s.telegram_token)}" placeholder="from @BotFather"></label>
          <label class="field"><span>Telegram chat ID</span><input name="telegram_chat_id" value="${esc(s.telegram_chat_id)}" placeholder="your chat ID"></label>
        </div>
        <label class="field"><span>Dashboard link used in notifications</span><input name="dashboard_url" value="${esc(s.dashboard_url)}"></label>
        <div class="sp"><button type="button" class="btn" id="test-notify">Send test notification</button><button class="btn btn-primary">Save preferences</button></div>
      </div></form>
      <div class="grid" style="align-content:start">
        <div class="card"><div class="card-h"><h2>Local AI (Ollama)</h2>${ai.available ? '<span class="label b">Connected</span>' : '<span class="label be">Not available</span>'}</div><div class="card-b small">
          <div class="kv" style="grid-template-columns:1fr">
            <div><span>Server · model</span><span>${esc(ai.host || '')} · ${esc(ai.model || '')}</span></div>
            <div><span>Announcements rated by AI</span><span>${fmtInt(ai.scored)}</span></div>
            <div><span>Waiting to be rated (last 120 days)</span><span>${isNum(ai.pending_news) ? fmtInt(ai.pending_news) : '—'}</span></div>
            <div><span>Analysis model (health checks, briefs, reviews)</span><span>${esc(ai.analysis_model || '')}</span></div>
            <div><span>Company health checks done · due</span><span>${fmtInt(ai.profiles)} · ${isNum(ai.profiles_pending) ? fmtInt(ai.profiles_pending) : '—'}</span></div>
          </div>
          <p class="muted">Used to rate each non-routine announcement (−2 … +2, fed to the model as "AI news score") and to write analyst notes. Runs on this PC; nothing is sent online. Change the model in <code>config.ini</code> [ollama].</p>
          <div class="toolbar"><button class="btn" data-job="ai_news" ${ai.available ? '' : 'disabled'}>Rate pending news now</button>
            <button class="btn" data-job="ai_profiles" ${ai.available ? '' : 'disabled'}>Run health checks now</button></div>
          <p class="muted">On this PC the model runs on the processor (no graphics card), about 2–3 minutes per health check and 3–5 minutes for a portfolio review. The daily run spends up to the minutes set in Preferences, holdings and watchlist first.</p>
        </div></div>
        <div class="card"><div class="card-h"><h2>Recent jobs</h2></div><div class="table-wrap" id="runs"></div></div>
      </div>
    </div>
    <p class="disclaimer">Data comes from the public Dhaka Stock Exchange website. This tool is for personal research. Technical signals are not investment advice; always check disclosures and fundamentals.</p>`);
  $('#runs').innerHTML = tableHtml([
    { key: 'started_at', label: 'Started', html: r => fmtTime(r.started_at) },
    { key: 'job', label: 'Job' },
    { key: 'status', label: 'Status', html: r => `<span class="${r.status === 'ok' ? 'up' : r.status === 'error' ? 'down' : 'muted'}">${esc(r.status)}</span>` },
    { key: 'message', label: 'Result', cls: 'wrap', html: r => `<span class="small">${esc(r.message || '')}</span>` },
  ], runs, null, 'No jobs have run yet');
  const f = $('#set-form');
  f.addEventListener('submit', async e => {
    e.preventDefault();
    const body = {};
    for (const el2 of f.elements) {
      if (!el2.name) continue;
      body[el2.name] = el2.type === 'checkbox' ? (el2.checked ? '1' : '0') : el2.value;
    }
    try { await api('/settings', { method: 'POST', body }); toast('Preferences saved'); S.commissionRate = Number(body.commission_rate) || S.commissionRate; }
    catch (err) { toast(err.message, 'err'); }
  });
  $('#test-notify').addEventListener('click', async () => {
    try {
      const r = await api('/notify/test', { method: 'POST' });
      const sent = Object.entries(r.sent).filter(([, v]) => v).map(([k]) => k);
      toast(sent.length ? `Sent via ${sent.join(' and ')}` : 'Nothing sent. Enable a channel and save first.', sent.length ? '' : 'err');
    } catch (err) { toast(err.message, 'err'); }
  });
}

/* ------------------------------------------------------------------ global events & init */
document.addEventListener('click', async e => {
  const star = e.target.closest('[data-star]');
  if (star) { e.preventDefault(); toggleWatch(star.dataset.star, star); return; }
  const job = e.target.closest('[data-job]');
  if (job) {
    e.preventDefault();
    if (job.dataset.job === 'refresh-now') { refreshNow(); return; }
    startJob(job.dataset.job, job.dataset.force ? { force: true } : {});
    return;
  }
  const un = e.target.closest('[data-unwatch]');
  if (un) {
    try { await api('/watchlist/' + encodeURIComponent(un.dataset.unwatch), { method: 'DELETE' }); S.quotes = null; rerender(); }
    catch (err) { toast(err.message, 'err'); }
    return;
  }
  const dtx = e.target.closest('[data-del-tx]');
  if (dtx) {
    if (!confirm('Delete this transaction?')) return;
    try { await api('/transactions/' + dtx.dataset.delTx, { method: 'DELETE' }); toast('Transaction deleted'); rerender(); }
    catch (err) { toast(err.message, 'err'); }
    return;
  }
  const dal = e.target.closest('[data-del-alert]');
  if (dal) {
    if (!confirm('Delete this alert?')) return;
    try { await api('/alerts/' + dal.dataset.delAlert, { method: 'DELETE' }); toast('Alert deleted'); rerender(); }
    catch (err) { toast(err.message, 'err'); }
    return;
  }
  const dlk = e.target.closest('[data-del-link]');
  if (dlk) {
    if (!confirm('Delete this political exposure record?')) return;
    try { await api('/bd/links/' + dlk.dataset.delLink, { method: 'DELETE' }); toast('Record deleted'); setTimeout(rerender, 2500); }
    catch (err) { toast(err.message, 'err'); }
    return;
  }
  const dev = e.target.closest('[data-del-event]');
  if (dev) {
    if (!confirm('Delete this event?')) return;
    try { await api('/bd/events/' + dev.dataset.delEvent, { method: 'DELETE' }); toast('Event deleted'); setTimeout(rerender, 2500); }
    catch (err) { toast(err.message, 'err'); }
    return;
  }
  const tal = e.target.closest('[data-toggle-alert]');
  if (tal) {
    try { await api(`/alerts/${tal.dataset.toggleAlert}/toggle`, { method: 'POST' }); rerender(); }
    catch (err) { toast(err.message, 'err'); }
  }
});

function goSearch(raw) {
  const v = (raw || '').trim();
  if (!v) return;
  const up = v.toUpperCase();
  const rows = (S.quotes && S.quotes.rows) || [];
  if (rows.some(r => r.code === up)) { location.hash = '#/stock/' + encodeURIComponent(up); $('#search').value = ''; return; }
  const hits = rows.filter(r => (r.name || '').toLowerCase().includes(v.toLowerCase()) || r.code.includes(up));
  if (hits.length === 1) { location.hash = '#/stock/' + encodeURIComponent(hits[0].code); $('#search').value = ''; return; }
  S.market.q = v;
  S.market.traded = false;
  location.hash = '#/market';
  if (S.route === 'market') router();
}

function init() {
  $('#search-form').addEventListener('submit', e => { e.preventDefault(); goSearch($('#search').value); });
  $('#search').addEventListener('input', e => {
    // picking an entry from the datalist inserts the whole code at once
    if (!e.inputType || e.inputType === 'insertReplacementText') {
      const v = e.target.value.trim().toUpperCase();
      if (S.quotes && S.quotes.rows.some(r => r.code === v)) goSearch(v);
    }
  });
  $('#search').addEventListener('focus', () => getQuotes().catch(() => {}), { once: true });
  $('#refresh-btn').addEventListener('click', refreshNow);
  $('#theme-btn').addEventListener('click', () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    root.dataset.theme = dark ? 'light' : 'dark';
    try { localStorage.setItem('bds-theme', root.dataset.theme); } catch (e) { /* private mode */ }
    rerender();
  });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (!document.documentElement.dataset.theme) rerender(); });
  $('#modal').addEventListener('click', e => { if (e.target.id === 'modal') e.target.close(); });
  window.addEventListener('hashchange', () => router());
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { clearTimeout(S.statusTimer); statusLoop(); } });
  router();
  statusLoop();
  getQuotes().catch(() => {});
}
init();
