/**
 * Discovery tab — wallets table + recent swaps with pagination
 */
import { api, scheduleRefresh, state } from './app.js';

export const discovery = {
  walletsPage: 1,
  swapsPage: 1,
  swapsPerPage: 50,
  swapTokenFilter: '',

  async addToCopy(addr) {
    try {
      await api('/api/copy/wallet', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ address: addr, alloc_sol: 0.01 }),
      });
      const btns = document.querySelectorAll(`[onclick*="discovery.addToCopy('${addr}')"]`);
      btns.forEach(b => { b.textContent = '✓ Added'; b.disabled = true; b.className = 'btn btn-ghost btn-small'; });
    } catch(e) {
      alert('Failed to add wallet: ' + e.message);
    }
  },
};

// ── Top wallet banner ─────────────────────────────────────────────────────────
export async function loadTopWallet() {
  try {
    const stats = await api('/api/stats');
    const top = stats.top_wallets && stats.top_wallets[0];
    const banner = document.getElementById('top-wallet-banner');
    if (!banner || !top) return;

    const scoreColor = top.score >= 0.9 ? 'var(--green)' : top.score >= 0.8 ? 'var(--orange)' : 'var(--muted)';
    banner.innerHTML = `
      <div class="label">🔥 #1 WALLET</div>
      <a class="address" href="${top.solscan_link}" target="_blank">${truncate(top.address, 16)}</a>
      <div class="metrics">
        <div class="metric">Score: <span style="color:${scoreColor}">${Number(top.score).toFixed(3)}</span></div>
        <div class="metric">Win: <span>${(Number(top.win_rate)*100).toFixed(1)}%</span></div>
        <div class="metric">PF: <span>${top.profit_factor === '999.0' ? '∞' : Number(top.profit_factor).toFixed(2)}</span></div>
        <div class="metric">ROI: <span style="color:${Number(top.avg_roi)>0?'var(--green)':'var(--red)'}">${fmtROI(top.avg_roi)}%</span></div>
        <div class="metric">Token: <span>${top.favorite_token || '—'}</span></div>
      </div>
      <div style="margin-top:8px;display:flex;gap:8px;align-items:center;">
        <a class="solscan-link" href="${top.solscan_link}" target="_blank">View on Solscan →</a>
        <button class="btn btn-primary btn-small" onclick="discovery.addToCopy('${top.address}')" id="banner-copy-btn">📋 Copy Trade</button>
      </div>
    `;
  } catch(e) {
    console.error('loadTopWallet failed', e);
  }
}

// ── Top Wallets Table (always top 50 from API) ─────────────────────────────────
export async function loadTopWallets() {
  try {
    const res = await api('/api/stats');
    const wallets = res.top_wallets || [];
    const body = document.getElementById('top-wallets-body');
    if (!body) return;

    if (!wallets.length) {
      body.innerHTML = `<tr><td colspan="9" class="empty">No wallets yet</td></tr>`;
      return;
    }

    body.innerHTML = wallets.map(w => {
      const score = Number(w.score);
      const wr = Number(w.win_rate);
      const pf = w.profit_factor === '999.0' ? '∞' : Number(w.profit_factor).toFixed(2);
      const roi = fmtROI(w.avg_roi);
      const sc = score >= 0.9 ? 'var(--green)' : score >= 0.8 ? 'var(--orange)' : 'var(--muted)';
      const wrColor = wr >= 0.8 ? 'var(--green)' : wr >= 0.5 ? 'var(--orange)' : 'var(--red)';
      return `<tr>
        <td class="mono"><a href="${w.solscan_link}" target="_blank">${truncate(w.address, 14)}</a></td>
        <td><span style="color:${sc}">${score.toFixed(3)}</span></td>
        <td>${w.total_trades}</td>
        <td><span style="color:${wrColor}">${(wr*100).toFixed(1)}%</span></td>
        <td>${pf}</td>
        <td class="${Number(w.avg_roi)>=0?'pos':'neg'}">${roi}%</td>
        <td class="sm">${w.favorite_token || '—'}</td>
        <td class="sm">${w.last_active ? timeAgo(new Date(w.last_active).getTime()) : '—'}</td>
        <td><button class="btn btn-primary btn-small" onclick="discovery.addToCopy('${w.address}')">📋 Copy</button></td>
      </tr>`;
    }).join('');
  } catch(e) {
    document.getElementById('top-wallets-body').innerHTML =
      `<tr><td colspan="9" class="empty">Failed to load wallets</td></tr>`;
}

// ── Recent Swaps ───────────────────────────────────────────────────────────────
export async function loadSwaps() {
  try {
    const stats = await api('/api/stats');
    const swaps = (stats.recent_swaps || []).slice(0, 50); // cap at 50
    const body = document.getElementById('swaps-body');
    if (!body) return;

    if (!swaps.length) {
      body.innerHTML = `<tr><td colspan="7" class="empty">No swaps yet</td></tr>`;
      return;
    }

    body.innerHTML = swaps.map(s => {
      return `<tr>
        <td class="sm">${fmtTime(s.block_time)}</td>
        <td class="${s.action==='BUY'?'buy':'sell'}">${s.action}</td>
        <td class="mono sm">${truncate(s.token_mint || s.token, 10)}</td>
        <td>${fmtAmt(s.amount)}</td>
        <td>${(Number(s.amount_sol) / 1e9).toFixed(4)} SOL</td>
        <td class="sm">${s.dex || '?'}</td>
        <td class="mono sm"><a href="${s.solscan_sig || '#'}" target="_blank">${truncate(s.signature || s.sig, 12)}</a></td>
      </tr>`;
    }).join('');

    // Token filter dropdown
    const uniqueTokens = [...new Set(swaps.map(s => s.mint || s.token))].slice(0, 100);
    const filter = document.getElementById('swap-token-filter');
    if (filter) {
      const cur = filter.value;
      filter.innerHTML = `<option value="">All tokens</option>` +
        uniqueTokens.map(t => `<option value="${t}">${t}</option>`).join('');
      filter.value = cur;
    }
  } catch(e) {
    document.getElementById('swaps-body').innerHTML =
      `<tr><td colspan="7" class="empty">Failed to load swaps</td></tr>`;
  }
}

// ── Helpers ────────────────────────────────────────────────────────────────────
export function truncate(str, len) {
  if (!str) return '—';
  return str.length <= len ? str : str.slice(0, len) + '…';
}

export function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
}

export function fmtAmt(raw) {
  if (!raw) return '—';
  const n = Number(raw);
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(2) + 'K';
  return n.toFixed(2);
}

export function fmtROI(val) {
  const n = Number(val);
  if (isNaN(n)) return '—';
  if (!isFinite(n)) return '∞';
  return n.toFixed(2);
}

export function timeAgo(ts) {
  if (!ts) return '—';
  const diff = Date.now() - new Date(ts).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

// ── Init ──────────────────────────────────────────────────────────────────────
export async function refresh() {
  await Promise.all([loadTopWallet(), loadTopWallets(), loadSwaps()]);
}

// schedule refresh every 30s when discovery tab is active
let _interval = null;
export function startRefresh() {
  if (_interval) return;
  _interval = setInterval(() => {
    if (state.activeTab === 'discovery') {
      loadTopWallet().catch(() => {});
      loadSwaps().catch(() => {});
    }
  }, 30000);
}

// ── Expose to global scope for onclick handlers ───────────────────────────────
window.discovery = discovery;
