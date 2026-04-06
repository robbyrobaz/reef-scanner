// ── State ─────────────────────────────────────────────────────────────
var activeTab = "discovery";
var uptimeStart = Date.now();
var lastStatsTs = 0;
var pendingChanges = {};  // addr -> {enabled: bool, alloc: float}
var pendingChallenge = "";  // challenge message for wallet verify

// ── Tab Switching ──────────────────────────────────────────────────────
function switchTab(name) {
  activeTab = name;
  document.querySelectorAll(".tab").forEach(function(t){ t.classList.remove("active"); });
  document.querySelectorAll(".tab-content").forEach(function(c){ c.classList.remove("active"); });
  document.getElementById("tab-" + name).classList.add("active");
  document.getElementById("content-" + name).classList.add("active");
}

// ── Phantom Wallet Connect ─────────────────────────────────────────────
async function connectPhantomWallet() {
  var input = document.getElementById('new-wallet-input');
  var addr = input ? input.value.trim() : '';
  
  // If Phantom is available, try to connect directly
  var phantom = window.phantom && window.phantom.solana;
  if (phantom && phantom.isConnected && phantom.publicKey) {
    addr = phantom.publicKey.toString();
  }
  
  if (!addr) {
    alert('Please enter your Solana wallet address');
    return;
  }
  
  // Generate a challenge
  var challenge = 'Reef Scanner copy trading auth: ' + Date.now() + '|' + Math.random().toString(36).slice(2);
  pendingChallenge = challenge;
  
  // Try to sign with Phantom if available
  if (phantom && phantom.isPhantom) {
    try {
      var messageBytes = new TextEncoder().encode(challenge);
      var sig = await phantom.signMessage(messageBytes, 'utf8');
      if (sig && sig.signature) {
        var sigB64 = btoa(String.fromCharCode.apply(null, Array.from(sig.signature)));
        var res = await api('/api/wallet/verify', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ address: addr, message: challenge, signature: sigB64 })
        });
        if (res && res.ok) {
          location.reload();
          return;
        }
      }
    } catch (e) {
      console.log('Phantom sign failed, trying address-only verify:', e);
    }
  }
  
  // Fallback: just set the wallet address directly (trust the user)
  var res = await api('/api/copy/wallet', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ address: addr })
  });
  if (res && res.ok) {
    location.reload();
  } else {
    alert('Failed to set wallet: ' + (res && res.error || 'unknown error'));
  }
}

// ── REST ───────────────────────────────────────────────────────────────
function api(url, opts) {
  return fetch(url, opts).then(function(r){ return r.ok ? r.json() : null; }).catch(function(){ return null; });
}

// ── Partial DOM updates ────────────────────────────────────────────────
function rebuildSwaps(swaps) {
  var tbody = document.getElementById("swap-table-body");
  if (!swaps || !swaps.length) { tbody.innerHTML = '<tr><td colspan="7" class="neutral">No swaps yet</td></tr>'; return; }
  var html = swaps.slice().reverse().map(function(s){
    var sig = s.signature || "";
    return "<tr><td class=\"neutral\">" + fmtTs(s.block_time) + "</td>" +
      "<td><span class=\"action " + s.action + "\">" + s.action + "</span></td>" +
      "<td class=\"mono\">" + (s.token_mint||"").slice(0,12) + "</td>" +
      "<td>" + (Number(s.amount)||0).toFixed(2) + "</td>" +
      "<td>" + (Number(s.amount_sol)||0).toFixed(4) + " SOL</td>" +
      "<td><span class=\"dex-badge\">" + (s.dex||"") + "</span></td>" +
      "<td class=\"addr\"><a href=\"https://solscan.io/tx/" + sig + "\" target=\"_blank\" style=\"color:#58a6ff\">" + shorten(sig,6) + "</a></td></tr>";
  }).join("");
  tbody.innerHTML = html;
}

function rebuildWallets(wallets) {
  var tbody = document.getElementById("wallet-table-body");
  if (!wallets || !wallets.length) { tbody.innerHTML = '<tr><td colspan="7" class="neutral">No wallets yet</td></tr>'; return; }
  var html = wallets.map(function(w){
    var score = Number(w.score||0), roi = Number(w.avg_roi||0)*100;
    var addr = w.address||"";
    var scoreColor = score > 0.8 ? "#3fb950" : score > 0.5 ? "#58a6ff" : "#7d8590";
    var roiColor = roi > 0 ? "#3fb950" : roi < 0 ? "#f85149" : "#7d8590";
    return "<tr><td class=\"addr\"><a href=\"https://solscan.io/account/" + addr + "\" target=\"_blank\" style=\"color:#58a6ff;text-decoration:none\">" + shorten(addr,8) + "</a></td>" +
      "<td style=\"color:" + scoreColor + ";font-weight:600\">" + score.toFixed(3) + "</td>" +
      "<td>" + (w.total_trades||"0") + "</td>" +
      "<td>" + (w.win_rate||"N/A") + "</td>" +
      "<td style=\"color:" + roiColor + "\">" + roi.toFixed(0) + "%</td>" +
      "<td class=\"neutral\">" + ((w.favorite_token||"")||"").slice(0,12) + "</td>" +
      "<td class=\"neutral\">" + fmtAge(w.last_active||"N/A") + "</td></tr>";
  }).join("");
  tbody.innerHTML = html;
}

function rebuildDex(dexCounts, totalSwaps) {
  var table = document.getElementById("dex-table");
  var rows = Object.entries(dexCounts||{}).sort(function(a,b){ return b[1]-a[1]; })
    .map(function(e){ var pct = totalSwaps ? (e[1]/totalSwaps*100).toFixed(1) : "0.0"; return "<tr><td><span class=\"dex-badge\">" + e[0] + "</span></td><td>" + e[1] + "</td><td class=\"neutral\">" + pct + "%</td></tr>"; })
    .join("");
  table.innerHTML = "<tr><th>DEX</th><th>Swaps</th><th>Share</th></tr>" + rows;
}

// ── Refresh functions ─────────────────────────────────────────────────
async function refreshStats() {
  var data = await api("/api/stats");
  if (!data) return;
  if (data.computed_at === lastStatsTs) return;
  lastStatsTs = data.computed_at;
  document.getElementById("stat-swaps").textContent = Number(data.total_swaps).toLocaleString();
  document.getElementById("stat-swaps2").textContent = Number(data.total_swaps).toLocaleString();
  document.getElementById("stat-wallets").textContent = Number(data.total_wallets).toLocaleString();
  document.getElementById("stat-wallets2").textContent = Number(data.total_wallets).toLocaleString();
  document.getElementById("stat-buys").textContent = Number(data.buys).toLocaleString();
  document.getElementById("stat-sells").textContent = Number(data.sells).toLocaleString();
  document.getElementById("stat-qualified").textContent = data.qualified_wallets;
  if (data.last_scan) {
    var diff = (Date.now()/1000 - new Date(data.last_scan).getTime()/1000);
    var ago = diff < 60 ? Math.floor(diff) + "s ago" : diff < 3600 ? Math.floor(diff/60) + "m ago" : data.last_scan.split("T")[1].slice(0,5);
    document.getElementById("last-scan").textContent = ago;
  }
  if (activeTab === "discovery") {
    rebuildSwaps(data.recent_swaps);
    rebuildWallets(data.top_wallets);
  }
  rebuildDex(data.dex_counts, data.total_swaps);
  updateCopyStatus();
}

async function refreshWalletStats() {
  var data = await api("/api/wallet/stats");
  if (!data) return;
  
  var pnlEl = document.getElementById("wstat-pnl");
  var wrEl = document.getElementById("wstat-winrate");
  var pfEl = document.getElementById("wstat-profit-factor");
  var ttEl = document.getElementById("wstat-total-trades");
  var tbEl = document.getElementById("wstat-total-buy");
  var tsEl = document.getElementById("wstat-total-sell");
  var awEl = document.getElementById("wstat-avg-win");
  var alEl = document.getElementById("wstat-avg-loss");
  
  if (pnlEl) {
    pnlEl.textContent = data.pnl_sol > 0 ? "+" + data.pnl_sol.toFixed(4) : data.pnl_sol.toFixed(4);
    pnlEl.style.color = data.pnl_sol >= 0 ? "#3fb950" : "#f85149";
  }
  if (wrEl) wrEl.textContent = data.win_rate.toFixed(1) + "%";
  if (pfEl) {
    pfEl.textContent = data.profit_factor.toFixed(3);
    pfEl.style.color = data.profit_factor >= 1 ? "#3fb950" : "#f85149";
  }
  if (ttEl) ttEl.textContent = data.total_trades;
  if (tbEl) tbEl.textContent = data.total_buys;
  if (tsEl) tsEl.textContent = data.total_sells;
  if (awEl) awEl.textContent = data.avg_win > 0 ? "+" + data.avg_win.toFixed(4) : data.avg_win.toFixed(4);
  if (alEl) alEl.textContent = data.avg_loss > 0 ? "-" + data.avg_loss.toFixed(4) : data.avg_loss.toFixed(4);
}

async function refreshCopy() {
  if (activeTab !== "copy") return;
  var config = await api("/api/copy/config");
  if (!config) return;
  var totalAlloc = Object.values(config.copies||{}).filter(function(e){ return e.enabled; }).reduce(function(s,e){ return s+(e.alloc_sol||0); }, 0);
  var enabledCount = Object.values(config.copies||{}).filter(function(e){ return e.enabled; }).length;
  document.getElementById("total-allocated").textContent = totalAlloc.toFixed(3) + " SOL";
  document.getElementById("total-allocated2").textContent = totalAlloc.toFixed(3) + " SOL";
  document.getElementById("copying-count").textContent = enabledCount + " wallets";
  var btn = document.getElementById("global-toggle-btn");
  if (btn) { btn.className = "global-toggle " + (config.global_enabled ? "on" : "off"); btn.style.background = config.global_enabled ? "#da3633" : "#238636"; btn.textContent = config.global_enabled ? "⏹ STOP ALL" : "▶ START ALL"; }
  var badge = document.getElementById("copy-status-badge");
  if (badge) { badge.textContent = config.global_enabled ? "COPY ACTIVE" : "COPY OFF"; badge.className = "tag " + (config.global_enabled ? "live" : "warning"); }
  Object.entries(config.copies||{}).forEach(function(e){
    var addr = e[0], info = e[1];
    var row = document.querySelector('[data-copy-addr="' + addr + '"]');
    if (!row) return;
    var b = row.querySelector(".toggle-btn");
    if (b) { b.className = "toggle-btn " + (info.enabled ? "on" : "off"); b.textContent = info.enabled ? "ON" : "OFF"; }
    var inp = row.querySelector(".alloc-input");
    if (inp) inp.value = (info.alloc_sol||0.01).toFixed(3);
    row.style.background = info.enabled ? "#1c2d1a" : "";
  });
  var trades = await api("/api/copy/trades");
  if (trades && trades.length > 0) {
    var tbody = document.querySelector("#copy-history-section table tbody");
    if (tbody) {
      tbody.innerHTML = trades.map(function(t){
        return "<tr class=\"copy-trade-row\"><td class=\"neutral\">" + fmtTs(t.timestamp) + "</td>" +
          "<td><span class=\"action " + t.action + "\">" + t.action + "</span></td>" +
          "<td class=\"addr\" style=\"font-size:11px\">" + shorten(t.source_wallet||"",6) + "</td>" +
          "<td>" + (t.token_mint||"").slice(0,12) + "</td>" +
          "<td>" + Number(t.amount_sol||0).toFixed(4) + " \u2192 " + Number(t.scaled_amount_sol||0).toFixed(4) + "</td>" +
          "<td class=\"copy-status-" + (t.status||"pending") + "\">" + (t.status||"pending").toUpperCase() + "</td>" +
          "<td class=\"addr\" style=\"font-size:11px\"><a href=\"https://solscan.io/tx/" + (t.source_sig||"") + "\" style=\"color:#58a6ff\">" + shorten(t.source_sig||"",6) + "</a></td></tr>";
      }).join("");
    }
    document.getElementById("copy-history-count").textContent = "(" + trades.length + " trades)";
  }
}

function updateCopyStatus() {
  api("/api/copy/config").then(function(config){
    if (!config) return;
    var badge = document.getElementById("copy-status-badge");
    if (badge) { badge.textContent = config.global_enabled ? "COPY ACTIVE" : "COPY OFF"; badge.className = "tag " + (config.global_enabled ? "live" : "warning"); }
  });
}

// ── Actions ────────────────────────────────────────────────────────────
async function setWallet() {
  var addr = document.getElementById("new-wallet-input").value.trim();
  if (!addr) return;
  var res = await api("/api/copy/wallet", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({address:addr})});
  if (res && res.ok) location.reload();
}

async function toggleGlobal() {
  await api("/api/copy/global-toggle", {method:"POST"});
  updateCopyStatus();
  refreshCopy();
}

// Event delegation for toggle buttons
document.addEventListener("click", function(e){
  var btn = e.target.closest(".toggle-btn");
  if (!btn) return;
  e.stopPropagation();
  var addr = btn.dataset.addr;
  api("/api/copy/wallet/" + addr + "/toggle", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({alloc:0.01})})
    .then(function(res){
      if (!res) return;
      btn.className = "toggle-btn " + (res.enabled ? "on" : "off");
      btn.textContent = res.enabled ? "ON" : "OFF";
      var row = btn.closest("tr");
      if (row) row.style.background = res.enabled ? "#1c2d1a" : "";
      refreshCopy();
    });
});

// Alloc input — track pending change on blur
document.addEventListener("blur", function(e){
  if (!e.target.classList.contains("alloc-input")) return;
  var addr = e.target.dataset.addr;
  var alloc = parseFloat(e.target.value);
  if (isNaN(alloc) || alloc <= 0) return;
  var row = e.target.closest("tr");
  var checkbox = row ? row.querySelector(".wallet-select") : null;
  var enabled = checkbox ? checkbox.checked : false;
  markPending(addr, enabled, alloc);
}, true);

// Wallet checkbox — track pending, don't save immediately
document.addEventListener("change", function(e){
  if (!e.target.classList.contains("wallet-select")) return;
  var addr = e.target.dataset.addr;
  var row = e.target.closest("tr");
  var allocInput = row ? row.querySelector(".alloc-input") : null;
  var alloc = allocInput ? parseFloat(allocInput.value) || 0.01 : 0.01;
  var currentlyEnabled = e.target.checked;
  
  // Update the toggle button to match
  var toggleBtn = row ? row.querySelector(".toggle-btn") : null;
  if (toggleBtn) {
    toggleBtn.className = "toggle-btn " + (currentlyEnabled ? "on" : "off");
    toggleBtn.textContent = currentlyEnabled ? "ON" : "OFF";
  }
  row.style.background = currentlyEnabled ? "#1c2d1a" : "";
  
  markPending(addr, currentlyEnabled, alloc);
});

// ── Utils ──────────────────────────────────────────────────────────────
function shorten(s, n) { if (!s || s.length < n*2) return s||""; return s.slice(0,n) + "..." + s.slice(-4); }
function fmtTs(ts) { if (!ts) return "?"; var d = new Date(ts * 1000); return d.toISOString().slice(11,19); }
function fmtAge(s) {
  if (!s || s === "N/A") return "N/A";
  try { var diff = (Date.now() - new Date(s.replace("Z","+00:00")).getTime()) / 1000;
    if (diff < 60) return Math.floor(diff) + "s ago";
    if (diff < 3600) return Math.floor(diff/60) + "m ago";
    if (diff < 86400) return Math.floor(diff/3600) + "h ago";
    return Math.floor(diff/86400) + "d ago";
  } catch(e) { return s.slice(0,16); }
}

// ── Uptime ─────────────────────────────────────────────────────────────
function updateUptime() {
  var s = Math.floor((Date.now() - uptimeStart) / 1000);
  var m = Math.floor(s / 60), h = Math.floor(m / 60);
  document.getElementById("uptime").textContent = h > 0 ? h + "h " + (m%60) + "m" : m + "m " + (s%60) + "s";
}
setInterval(updateUptime, 1000);

// ── Pending changes tracking ───────────────────────────────────────────
function markPending(addr, enabled, alloc) {
  pendingChanges[addr] = { enabled: enabled, alloc: alloc };
  var indicator = document.getElementById("pending-indicator");
  if (indicator) indicator.style.display = "inline-block";
  var btn = document.getElementById("save-changes-btn");
  if (btn) btn.disabled = false;
  var status = document.getElementById("save-status");
  if (status) status.textContent = Object.keys(pendingChanges).length + " unsaved change(s)";
}

async function savePendingChanges() {
  var btn = document.getElementById("save-changes-btn");
  var status = document.getElementById("save-status");
  if (btn) btn.disabled = true;
  if (status) status.textContent = "Saving...";
  
  var promises = Object.entries(pendingChanges).map(function(e) {
    var addr = e[0], info = e[1];
    return api("/api/copy/wallet/" + addr + "/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ alloc: info.alloc })
    });
  });
  
  await Promise.all(promises);
  pendingChanges = {};
  
  var indicator = document.getElementById("pending-indicator");
  if (indicator) indicator.style.display = "none";
  if (btn) btn.disabled = false;
  if (status) status.textContent = "Saved!";
  setTimeout(function() { if (status) status.textContent = ""; }, 2000);
  
  refreshCopy();
}

// ── Position display ─────────────────────────────────────────────────
async function refreshPositions() {
  if (activeTab !== "copy") return;
  var data = await api("/api/positions");
  if (!data || !Array.isArray(data)) return;
  
  var grid = document.getElementById("positions-grid");
  var totalEl = document.getElementById("positions-total");
  if (!grid) return;
  
  if (data.length === 0) {
    grid.innerHTML = '<div class="position-card"><div class="position-token" style="text-align:center;color:#7d8590">No open positions</div></div>';
    if (totalEl) totalEl.textContent = "";
    return;
  }
  
  var totalValue = 0, totalPnl = 0;
  var html = data.map(function(p) {
    totalValue += Number(p.current_value_sol || 0);
    totalPnl += Number(p.pnl_sol || 0);
    var isProfit = Number(p.pnl_sol || 0) >= 0;
    var cardClass = isProfit ? "position-card profit" : "position-card loss";
    var pnlClass = isProfit ? "position-pnl positive" : "position-pnl negative";
    var pnlSign = isProfit ? "+" : "";
    var shortMint = (p.token_mint || "").slice(0, 8) + "..." + (p.token_mint || "").slice(-4);
    return '<div class="' + cardClass + '">' +
      '<div class="position-token" title="' + (p.token_mint || "") + '">' + shortMint + '</div>' +
      '<div class="position-amount">' + Number(p.amount || 0).toFixed(4) + '</div>' +
      '<div class="position-value">~' + Number(p.current_value_sol || 0).toFixed(4) + ' SOL</div>' +
      '<div class="' + pnlClass + '">' + pnlSign + Number(p.pnl_sol || 0).toFixed(4) + ' SOL (' + pnlSign + Number(p.pnl_pct || 0).toFixed(1) + '%)</div>' +
      '<div class="position-meta">Avg: ' + Number(p.avg_price_sol || 0).toFixed(9) + ' SOL</div>' +
      '</div>';
  }).join("");
  
  grid.innerHTML = html;
  if (totalEl) {
    var totalClass = totalPnl >= 0 ? "positive" : "negative";
    totalEl.innerHTML = 'Total: <span class="' + totalClass + '" style="font-weight:600">' + (totalPnl >= 0 ? "+" : "") + totalPnl.toFixed(4) + ' SOL</span> in ' + data.length + ' position(s)';
  }
}

// ── Background polling — no full page reload ────────────────────────────
function backgroundRefresh() {
  if (activeTab === "discovery") refreshStats();
  else if (activeTab === "copy") { refreshCopy(); refreshWalletStats(); refreshPositions(); }
}
setInterval(backgroundRefresh, 10000);
setTimeout(backgroundRefresh, 3000);