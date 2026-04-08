/**
 * Reef Scanner Dashboard — Vanilla JS + ES Modules
 * All API calls and path references use BASE from import.meta.url
 */

// ── State ────────────────────────────────────────────────────────────────────
export const state = {
  activeTab: localStorage.getItem('reef_tab') || 'discovery',
  uptimeStart: Date.now(),
  stats: null,
};
export async function api(path, opts = {}) {
  const script = document.querySelector('script[type="module"][src]') || document.querySelector('script[type="module"]');
  const src = script?.src || '';
  const idx = src.indexOf('/static/');
  const base = idx >= 0 ? src.slice(0, idx) : '';
  const url = base + path;
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${url}`);
  return res.json();
}

// ── Tab switching ─────────────────────────────────────────────────────────────
export function switchTab(name) {
  state.activeTab = name;
  localStorage.setItem('reef_tab', name);

  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === `tab-${name}`));

  if (name === 'discovery') discoveryRefresh();
  if (name === 'copy') copyTrading.refresh();
}

// ── Uptime ────────────────────────────────────────────────────────────────────
export function startUptime() {
  setInterval(() => {
    const elapsed = Math.floor((Date.now() - state.uptimeStart) / 1000);
    const h = Math.floor(elapsed / 3600);
    const m = Math.floor((elapsed % 3600) / 60);
    const s = elapsed % 60;
    const el = document.getElementById('uptime');
    if (el) el.textContent = h > 0 ? `${h}h ${m}m` : `${m}m ${s}s`;
  }, 1000);
}

// ── Global refresh ─────────────────────────────────────────────────────────────
let refreshTimer = null;
export function scheduleRefresh(fn, interval = 10000) {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(fn, interval);
  setTimeout(fn, 2000); // run once after 2s
}

// ── Init ──────────────────────────────────────────────────────────────────────
import { refresh as discoveryRefresh } from './discovery.js';
import * as _discovery from './discovery.js';
import copyTrading from './copy-trading.js';

// Expose to window for onclick handlers
window.copyTrading = copyTrading;
window.discovery = _discovery;

export async function init() {
  startUptime();

  // Initial stats load (shared header data)
  try {
    state.stats = await api('/api/stats');
    renderHeader(state.stats);
  } catch(e) {
    console.error('Failed to load stats', e);
  }

  // Restore saved tab
  switchTab(state.activeTab);
}

function renderHeader(stats) {
  if (!stats) return;
  const fmt = (n) => n != null ? Number(n).toLocaleString() : '—';

  document.getElementById('total-swaps').textContent = fmt(stats.total_swaps);
  document.getElementById('total-wallets').textContent = fmt(stats.total_wallets);
  document.getElementById('last-scan').textContent = stats.last_scan
    ? new Date(Number(stats.last_scan) * 1000).toLocaleString()
    : '—';

  // DEX breakdown
  const dexEl = document.getElementById('dex-breakdown');
  if (dexEl && stats.dex_counts) {
    const total = Object.values(stats.dex_counts).reduce((a, b) => a + b, 0);
    dexEl.innerHTML = Object.entries(stats.dex_counts).map(([dex, count]) => {
      const pct = total > 0 ? ((count / total) * 100).toFixed(1) : 0;
      return `<div class="dex-row"><span class="dex-name">${dex}</span><span class="dex-pct">${count.toLocaleString()} (${pct}%)</span></div>`;
    }).join('');
  }

  // Stats grid
  const buys = stats.buys || 0, sells = stats.sells || 0;
  const total = buys + sells;
  const buyPct = total > 0 ? ((buys / total) * 100).toFixed(1) : '0.0';

  document.getElementById('stat-swaps').textContent = fmt(stats.total_swaps);
  document.getElementById('stat-wallets').textContent = fmt(stats.total_wallets);
  document.getElementById('stat-qualified').textContent = fmt(stats.qualified_wallets);
  document.getElementById('stat-buy-pct').textContent = buyPct + '%';
}

// Mount for global access
window.app = { switchTab, init };
