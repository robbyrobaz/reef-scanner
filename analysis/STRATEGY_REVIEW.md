# Reef Copy-Trading Strategy Review — Apr 18 2026

**Purpose:** package all data collected over the Apr 9–18 window so an outside reviewer can assess whether this is a real, scalable strategy or a paper-only illusion.

## What the system does

- Subscribes to WS (Helius → publicnode fallback + PumpPortal) for **100 source wallets** on Solana mainnet.
- When a source wallet buys or sells a pump.fun token, we **copy** the trade in one of two modes:
  - `copy_mode="live"` → real Jupiter swap at 0.01 SOL scaled size (~$2.50/trade).
  - `copy_mode="watch"` → paper trade, **but at real execution lag**: we wait 4s after the source signal, fetch a live Jupiter quote at that moment, and use that quote as our "fill." This is not bar-replay paper — it's real on-chain pricing at real latency.
- Currently all 100 wallets are in `watch` mode for data collection. 0 SOL at risk.

## The three data regimes

| Regime | Window | Sells | Net PnL | WR | PF | Best | Worst |
|---|---|---|---|---|---|---|---|
| PAPER (historical, source-price fills) | Apr 9–17, ~9 days | 3,810 | +2.524 SOL | 68% | 3.20 | +0.225 | −0.010 |
| WATCH (Jupiter-quote fills at 4s lag) | Apr 18, ~7 hours | 554 | +0.392 SOL | 58% | 3.05 | +0.093 | −0.010 |
| LIVE (real Jupiter swaps) | Apr 17, ~102 trades | 100 | **−0.096 SOL** | **13%** | **0.15** | +0.005 | −0.010 |

**The headline problem:** LIVE does not resemble PAPER or WATCH.
- Paper says PF 3.2, live delivered PF 0.15.
- Paper WR 68%, live WR 13%.
- Paper best trade was +22× capital; live best was +0.48× capital.

## Live slip reality (Apr 17 session)

Contrary to an earlier assumption that slip was the main killer:

```
Live slip: n=205  median=+0.4%  p25=-0.5%  p75=+2.4%  avg=+3.1%
```

Slip on entry is actually **small and symmetric** — about 3% average, sometimes favorable. This matches the watch-sim slip we're now measuring across 100 wallets (most land within ±5%, medians near zero).

**So slip alone does not explain the live PnL collapse.** The paper-to-live gap is somewhere else.

## Where the live gap likely comes from (hypothesis)

Paper PnL is dominated by tail events: **top 20 of 3,810 sells = 73% of total PnL.** Top 5 = 30%.

```
top   5 trades: +0.773 SOL (30.6% of total)
top  10 trades: +1.248 SOL (49.4%)
top  20 trades: +1.840 SOL (72.9%)
top  50 trades: +2.348 SOL (93.0%)
top 100 trades: +2.667 SOL (105.6%, tail starts giving back)
```

**Live caught zero of those tail events.** Best live trade was +0.005 SOL vs paper's +0.225 SOL best. That's not a slip problem — that's either:
1. Selection: the wallets we put live (picked by paper rank) don't generate the actual moonshots; the paper whales came from different wallets we didn't promote.
2. Exit timing: we entered pumps that ran, but sold too fast / at wrong price because our SELL signal triggers off the source wallet exit, and our fill lands later at a different point on the curve.
3. Paper price lies: the `source_price_sol` we used to compute paper PnL over-counted fills for pump-amm tokens with parse artifacts. Paper is partly fiction.

All three are plausible. The fact that WATCH (at real Jupiter lag, but still paper) shows PF 3.05 suggests #3 is real but not the whole story. 6 hours of WATCH is also not enough to have caught a true moonshot.

## Wallet persistence — are these real operators or burners?

Of 100 watched wallets, **18 were active (≥3 sells) in ≥3 separate time buckets across the 9-day window.** The persistent list:

```
wallet                  buckets  sells  pnl_mSOL
84NXvzQMJUhzzv...          4     920   +1550.1  ← known herding anomaly, discount
8LWbSBjGKQgR6y...          5     445    +416.8
yGC3PdX4qu36ow...          3     122    +102.3
BPo2TTuQWyrK8T...          3     139    +102.0
9ncksfZ6UAWDMV...          4     181     +63.2
36HCCCqkbKktVz...          4     138     +45.4
58SuHLvYNe3dg1...          4     184     +42.2
5MYVpHEiLHkddG...          4     171     +41.2
5BvYbDUDuar3Sb...          4     148     +27.8
9g41Gguy92qRZa...          4     118      +9.6
Knv6uhGpGUeRYV...          4     106      +9.1
8xSMaoWC5fXxSj...          4      98      +9.1
5Yy4Ms54P3wL6f...          4     105      +8.9
9nkVWNqs18HmtD...          3      19      +8.5
AXiPFg4VTXUU8b...          4     102      +8.1
BwiyaXiU4FdwbqSg...        4      89      +7.7
8UvtYuW8txUfyW...          4      58      +5.5
9EdcipnA5hxpc6...          5      96      -2.9
```

One wallet (84NXvzQM) produced 61% of paper profit — but is flagged in a prior memory as one wallet herding into its own trades. Discounted.

**62 wallets were active in the last 24h, but only 1 was also active >7 days ago.** Heavy churn at the tail; persistent winners at the head.

## 6hr watch slip + PnL — live execution preview

Data from the last 6 hours (Apr 18), 33 wallets with ≥5 slip samples:

```
wallet          n_slip  avg%   med%   sells  pnl_mSOL  WR%
GTm6JWdmjn...     35   +3.6   +3.6    15    +92.64   73.3   ← hot streak (flipped later, see below)
9nB5wLMwdH...     52   +0.8    0.0    25    +36.22   80.0
8LWbSBjGKQ...     41   +1.1   +3.3    20    +29.22   75.0   ← also paper top, persistent
HVCCKWVTHh...     96  -15.8   -1.4    34    +24.76   42.4   ← big tail, avg misleading
6i4njdS6nG...     20   +0.3   +0.7     9    +17.23  100.0
7wVLCWiAhE...     39   -0.7   -0.7    17    +13.19   70.6
...
bottom:
3hABxPaFS8...     38    0.0    0.0    24     -2.24    4.2   ← 4% WR, steady loser
9EdcipnA5h...      5   +0.2   +0.2     3     -9.33   33.3
21Bn8qWuQ2...      5   +0.7    0.0     3     -9.92   33.3
```

**Half-window consistency check (early 3hr vs late 3hr):**
- 9nB5wLMwdH: +15.7 early / +20.9 late — consistent.
- 8LWbSBjGKQ: +12.8 / +16.5 — consistent.
- GTm6JWdmjn: +97.7 / −9.4 — **hot-streak flipper, exposed by the second window.**

Small-n ranking is actively dangerous — a 3hr window would have put GTm6 on a live list.

## The scaling thesis (user's framing)

User proposes: put $1000 bankroll into this system, NOT to size up single trades (liquidity cap is ~0.02–0.05 SOL per position), but to have enough capital to stay deployed across many parallel positions simultaneously and catch the rare moonshots.

Reasoning:
- Per-trade size stays at ~0.01–0.03 SOL (liquidity-bound, won't change)
- $1000 ≈ 4 SOL = enough gas to run 50–100 concurrent positions without starving
- At current 0.021 SOL wallet we literally skip signals because we're deployed
- Paper concentration (top 20 = 73% of PnL) means success depends on being present for the rare pumps
- More parallel slots = more lottery tickets at same ticket price

Expected outcome per user: ~10–25% net daily average, with occasional +50–100% moonshot days and −5–15% drawdown days.

## Known risks / open questions for the reviewer

1. **Paper-to-live gap is still unexplained.** Watch shows PF 3.0, live shows PF 0.15. Live slip is small. What accounts for the gap? Is it selection, exit timing, or paper price lies?
2. **Hot-streak wallets contaminate any short-window ranking.** GTm6 was #1 earner in first 3 hours, flipped to loser in next 3. 24–72hr minimum ranking window needed — is that actually long enough?
3. **Tail dependency.** 73% of paper PnL came from 20 trades. If those trades were partly artifacts of paper-price parsing, actual live-capturable tail is much smaller and the strategy is break-even at best.
4. **Wallet churn.** Only ~15–18 of 100 wallets persist for more than a few days. Others come and go. How sustainable is a "curated list" when the curation window is narrow?
5. **Execution drift from quote to fill.** Jupiter quote at T+4s is a preview; actual landing is T+6–10s. Real fills may drift another 1–3% from quoted. Is that already visible in the live median +3.1% slip, or is there more to it?
6. **Auto-promote/demote is not yet implemented.** Currently the list is human-curated. The scaling plan assumes an automated pipeline.
7. **Moonshot scaling.** At 0.03 SOL per position, a 100× win = 3 SOL = $720. For that to compound to "10–25% daily on $1000", you need multiple such events per week, not per month. Is the base rate actually there?

## Data files
- Raw per-run data: `analysis/raw_data_20260418.txt`
- Live + paper + watch trades: `data/copy_trades.csv` (~12k rows)
- Code: `copy_engine.py` (orchestrator), `swap_executor.py` (Jupiter), `dashboard.py` (stats)

## The question for review

**Is this a real strategy that can be scaled to a $1000 bankroll with expected positive return, or is the paper/watch signal too contaminated / the live gap too structural / the moonshot base rate too low to actually produce profit at live scale?**

What would you want to see before risking real capital?
