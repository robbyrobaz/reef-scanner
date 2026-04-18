# Paper Moonshot Audit — Apr 18 2026

**Question (from advisor review):** "For each of the top 20 paper moonshot trades, verify whether the source wallet actually made 10×+ on-chain, or whether the paper PnL was a parse/pairing artifact."

**Answer: 19 of 20 are artifacts. The paper edge is ~60% fictional.**

## Method

The audit turned out to be decidable from the CSV alone, without RPC calls. Every paper SELL in `data/copy_trades.csv` is supposed to pair against a prior BUY row for the same `(source_wallet, token_mint)` composite key. If the prior BUY is present in the log, we can compute the actual price ratio and check if it's plausible. If the prior BUY is **missing**, the SELL was paired against stale state in `paper_positions.json` — and the entry price used for PnL is untrustworthy.

## Results

Top 20 paper SELL trades by `realized_pnl_sol`:

| # | Wallet | Claimed PnL | Prior BUY in CSV? | Claimed ratio | Verdict |
|---|---|---|---|---|---|
| 1–16, 18–20 | **84NXvzQM** (19 rows) | +1.542 SOL | **NO** (all) | n/a | **STALE ENTRY** |
| 17 | 8LWbSBjGKQ | +0.049 SOL | YES (7:38 before SELL) | 5.93× | **LEGIT** |

All 19 artifact rows belong to a single wallet (84NXvzQM) which was already flagged in prior memory as a herding anomaly, not an alpha source.

## Why 84NXvzQM looks like a whale on paper but isn't

- 1,128 BUYs and 917 SELLs logged for this wallet
- Sell-price distribution spans 5 orders of magnitude (1.4e-8 to 1.37e-3 SOL/token) — consistent with Jupiter quote parse artifacts on pump-amm tokens around graduation events, not real price action
- Multiple SELL rows pair against `paper_positions.json` entries that predate our current logging window, meaning the "entry price" was whatever happened to be in the state file, not a verified BUY
- Running total: this one wallet generated **61.4% of all paper PnL** (+1.549 of +2.524 SOL)

## What the real paper edge looks like after removing the artifact

Backing out 84NXvzQM entirely:

| Metric | All paper (as logged) | Excluding 84NXvzQM |
|---|---|---|
| Net PnL | +2.524 SOL | +0.975 SOL |
| Sells | 3,810 | 2,893 |
| PnL / sell | +0.66 mSOL (0.66% of 0.1 SOL basis) | +0.34 mSOL (0.34%) |

The residual edge is ~0.3% per sell — and live execution costs (3% median slip + 0.1–0.3% Jupiter fee + 0.002 SOL ATA rent per new mint) **exceed the edge**. This matches what the live session showed: PF 0.15, net −0.096 SOL over 100 sells at 0.01 SOL scaling.

## What about the watch-mode data?

Watch mode computes PnL against **real Jupiter quotes at T+4s**, not source prices, so it doesn't suffer the same artifact. Current watch stats (PF 3.05, WR 58% over 554 sells in 7 hours) is a different number than paper and should be trusted more — but:

- 7 hours of watch is small-sample; 0 real moonshots captured in that window
- Watch does NOT yet subtract execution costs (priority fee, ATA rent, Jupiter protocol fee); subtract ~0.5% per round trip and the PF drops meaningfully
- Watch is still paper in the sense that actual landing drifts 1–3% from quote; real live PF on the same signals will be lower

## Recommendation

1. **Do NOT put $1000 in based on paper data.** Paper was 60% artifact.
2. **Do NOT conclude the strategy is dead** — the legit paper winners (8LWbSBjG type) are real, and watch mode is producing genuine Jupiter-quote data. But the edge is much thinner than paper claimed.
3. **Let watch run 72h+** on the 100 wallets. Rank wallets by (a) consistent positive PnL across non-overlapping sub-windows, (b) ≥15 completed round-trips, (c) slip tolerable (<5% median).
4. **Rebuild an honest live expectation** from watch data alone, *after* subtracting realistic execution costs: Jupiter 0.15% + priority ~0.003 SOL + ATA rent 0.002 SOL per new mint. If watch-net-of-costs shows PF > 1.3 on a curated subset, there's a real edge to risk capital on — at $200–$300 scale first, not $1000.
5. **Start with slip-gate enforced** (skip execution if T+4s Jupiter quote >5% adverse vs source price). Already have the plumbing from watch mode.

## What this audit does NOT settle

- The one legit moonshot (#17, 8LWbSBjG 5.93×) was not verified on-chain; should pull tx receipts for full confirmation. But it doesn't change the conclusion: even if real, one legitimate tail in 3,810 sells is not enough to support "10–25% daily."
- Whether 84NXvzQM has any real alpha underneath the herding/artifact layer; unlikely to matter since it's not in the live copy list.
