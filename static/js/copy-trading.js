/**
 * Copy Trading tab — wallet config, enabled copies, trade log
 */
import { api, state } from './api.js';

export const copyTrading = {
  config: null,
  wallets: [],   // list of {address, enabled, alloc_sol, last_sig, last_copy_ts}
};

// ── Full refresh ──────────────────────────────────────────────────────────────
export async function refresh() {
  await Promise.all([loadConfig(), loadTrades()]);
}

// ── Load config + render ───────────────────────────────────────────────────────
export async function loadConfig() {
  try {
    copyTrading.config = await api('/api/copy/config');
    renderWalletStatus();
    renderGlobalToggle();
    renderEnabledCopies();
  } catch(e) {
    console.error('loadConfig failed', e);
  }
}

// ── Wallet Status ──────────────────────────────────────────────────────────────
function renderWalletStatus() {
  const cfg = copyTrading.config;
  const walletEl = document.getElementById('wallet-status');
  if (!walletEl) return;

  if (!cfg || !cfg.user_wallet) {
    walletEl.innerHTML = `
      <div class="wallet-empty">
        <p>No wallet connected. Generate a keypair or import your existing private key.</p>
        <div class="form-row">
          <button class="btn btn-primary" onclick="copyTrading.generateWallet()">🔑 Generate New Keypair</button>
        </div>
        <div style="margin: 10px 0; color: var(--muted); font-size: 12px;">— or —</div>
        <textarea id="privkey-input" class="seed-input" rows="2" placeholder="Enter base58 private key (SOLD...)"></textarea>
        <div class="form-row" style="margin-top:8px">
          <button class="btn btn-secondary" onclick="copyTrading.importWallet()">📥 Import Private Key</button>
        </div>
        <div id="wallet-msg" class="msg" style="margin-top:8px"></div>
      </div>`;
  } else {
    walletEl.innerHTML = `
      <div class="wallet-info">
        <span class="wallet-addr">${truncate(cfg.user_wallet, 16)}</span>
        <span class="sm" style="color:var(--muted)">${cfg.trade_mode || 'paper'} mode</span>
        <button class="btn btn-ghost btn-small" onclick="copyTrading.removeWallet()">✕ Remove</button>
      </div>`;
  }
}

// ── Generate keypair ──────────────────────────────────────────────────────────
export async function generateWallet() {
  const msg = document.getElementById('wallet-msg');
  try {
    const res = await api('/api/wallet/generate', { method: 'POST' });
    if (!copyTrading.config) copyTrading.config = {};
    copyTrading.config.user_wallet = res.address;
    copyTrading.config.keypair_path = res.keypair_path || '';
    renderWalletStatus();
    if (msg) { msg.textContent = '✅ Keypair generated! Address: ' + res.address + ' — send SOL to this address to fund it.'; msg.className = 'msg ok'; }
  } catch(e) {
    if (msg) { msg.textContent = 'Failed: ' + e.message; msg.className = 'msg err'; }
  }
}

// ── Import private key ─────────────────────────────────────────────────────────
export async function importWallet() {
  const privkey = document.getElementById('privkey-input')?.value.trim();
  const msg = document.getElementById('wallet-msg');
  if (!privkey) { msg.textContent = 'Please enter a private key'; msg.className = 'msg err'; return; }
  try {
    const res = await api('/api/wallet/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ privkey }),
    });
    if (!copyTrading.config) copyTrading.config = {};
    copyTrading.config.user_wallet = res.address;
    renderWalletStatus();
    if (msg) { msg.textContent = '✅ Wallet imported! Address: ' + res.address; msg.className = 'msg ok'; }
  } catch(e) {
    if (msg) { msg.textContent = 'Failed: ' + e.message; msg.className = 'msg err'; }
  }
}

// ── Seed phrase save (legacy — kept for backwards compat) ─────────────────────
export async function saveWallet() {
  // Deprecated — redirect to importWallet
  await importWallet();
}

// ── Remove wallet ──────────────────────────────────────────────────────────────
export async function removeWallet() {
  if (!confirm('Remove your wallet from copy trading config?')) return;
  await api('/api/wallet/disconnect', { method: 'POST' });
  if (copyTrading.config) copyTrading.config.user_wallet = '';
  renderWalletStatus();
}

// ── Global toggle ─────────────────────────────────────────────────────────────
function renderGlobalToggle() {
  const cfg = copyTrading.config;
  const toggle = document.getElementById('global-toggle');
  const liveToggle = document.getElementById('live-toggle');
  if (toggle) toggle.checked = cfg?.global_enabled ?? false;
  if (liveToggle) liveToggle.checked = cfg?.trade_mode === 'live';
}

export async function toggleGlobal() {
  const enabled = document.getElementById('global-toggle')?.checked;
  await api('/api/copy/global-toggle', { method: 'POST' });
}

export async function toggleMode() {
  const live = document.getElementById('live-toggle')?.checked;
  await api('/api/trade/mode', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: live ? 'live' : 'paper' }),
  });
}

export async function setDefaultAlloc() {
  const val = parseFloat(document.getElementById('default-alloc')?.value);
  if (isNaN(val) || val <= 0) return;
  if (copyTrading.config) copyTrading.config.default_alloc = val;
}

// ── Enabled Copies Table ──────────────────────────────────────────────────────
async function renderEnabledCopies() {
  try {
    const cfg = copyTrading.config || {};
    const copies = Object.entries(cfg.copies || {});
    document.getElementById('copy-count').textContent = `(${copies.length})`;

    const body = document.getElementById('copy-wallets-body');
    if (!copies.length) {
      body.innerHTML = `<tr><td colspan="5" class="empty">No copy trades enabled. Add wallets from the Discovery tab.</td></tr>`;
      return;
    }

    body.innerHTML = copies.map(([addr, info]) => `
      <tr data-addr="${addr}">
        <td class="mono address-cell"><a href="https://solscan.io/account/${addr}" target="_blank">${truncate(addr, 16)}</a></td>
        <td>
          <input type="number" class="alloc-input" value="${info.alloc_sol ?? 0.01}" step="0.001" min="0.001"
            onchange="copyTrading.setAlloc('${addr}', this.value)">
        </td>
        <td class="mono sm">${truncate(info.last_sig || '—', 14)}</td>
        <td class="sm">${info.last_copy_ts ? timeAgo(info.last_copy_ts * 1000) : 'never'}</td>
        <td>
          <button class="btn btn-ghost btn-small" onclick="copyTrading.toggleWallet('${addr}')">
            ${info.enabled ? 'Disable' : 'Enable'}
          </button>
          <button class="btn btn-danger btn-small" onclick="copyTrading.removeCopy('${addr}')">✕</button>
        </td>
      </tr>`).join('');
  } catch(e) {
    console.error('renderEnabledCopies failed', e);
  }
}

export async function toggleWallet(addr) {
  try {
    const result = await api(`/api/copy/wallet/${addr}/toggle`, { method: 'POST' });
    // Update local state and re-render immediately
    const copies = result.copies || {};
    for (const [a, info] of Object.entries(copies)) {
      const existing = copyTrading.config?.copies?.[a] || {};
      if (!copyTrading.config) copyTrading.config = {};
      if (!copyTrading.config.copies) copyTrading.config.copies = {};
      copyTrading.config.copies[a] = { ...existing, ...info };
    }
    renderEnabledCopies();
  } catch(e) {
    console.error('toggleWallet failed', e);
    alert('Toggle failed: ' + e.message);
  }
}

export async function setAlloc(addr, val) {
  await api(`/api/copy/wallet/${addr}/alloc`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ alloc: parseFloat(val) }),
  });
}

export async function removeCopy(addr) {
  if (!confirm(`Stop copying ${truncate(addr, 12)}?`)) return;
  await api(`/api/copy/wallet/${addr}/toggle`, { method: 'POST' });
  await loadConfig();
}

// ── Copy Trade Log ──────────────────────────────────────────────────────────────
export async function loadTrades() {
  try {
    const trades = await api('/api/copy/trades?limit=50');
    const body = document.getElementById('copy-trades-body');
    const countEl = document.getElementById('trade-log-count');
    if (!Array.isArray(trades)) { body.innerHTML = `<tr><td colspan="7" class="empty">No copy trades yet</td></tr>`; return; }

    if (countEl) countEl.textContent = `(${trades.length})`;
    if (!trades.length) {
      body.innerHTML = `<tr><td colspan="7" class="empty">No copy trades yet</td></tr>`;
      return;
    }

    body.innerHTML = trades.map(t => `
      <tr>
        <td class="sm">${fmtTime(t.timestamp || t.block_time)}</td>
        <td class="${(t.action||'').toUpperCase()==='BUY'?'buy':'sell'}">${t.action || '?'}</td>
        <td class="mono sm">${truncate(t.source_wallet || t.source, 12)}</td>
        <td class="mono sm">${truncate(t.token, 10)}</td>
        <td class="sm">${fmtAmt(t.orig_amount)} → ${fmtAmt(t.copy_amount)}</td>
        <td class="${statusClass(t.status)}">${t.status || '?'}</td>
        <td class="mono sm"><a href="${t.solscan_sig || '#'}" target="_blank">${truncate(t.sig || t.signature, 12)}</a></td>
      </tr>`).join('');
  } catch(e) {
    document.getElementById('copy-trades-body').innerHTML =
      `<tr><td colspan="7" class="empty">Failed to load trades</td></tr>`;
  }
}

function statusClass(s) {
  if (!s) return 'sm';
  if (s === 'FILLED' || s === 'LIVE') return 'pos';
  if (s === 'FAILED') return 'neg';
  return 'sm';
}

// ── Helpers ────────────────────────────────────────────────────────────────────
export function truncate(str, len) {
  if (!str) return '—';
  return str.length <= len ? str : str.slice(0, len) + '…';
}

export function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function fmtAmt(raw) {
  if (!raw) return '—';
  const n = Number(raw);
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(2) + 'K';
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
