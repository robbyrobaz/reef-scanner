# Large-Order-Follow Strategy Backtest — Apr 18 2026

**Hypothesis:** instead of copying every trade from curated wallets, only copy their *large orders* (high conviction). Watch all 67k wallets; filter by trade size, not just wallet identity.

**Also tested:** user's "price = demand" hypothesis — does price actually move up after a large BUY?

## Data

- Scanner DB: 3.7M verified on-chain swaps
- Cleaned: amount_sol 0.001–100k SOL, last 30 days → 2.45M rows
- Paired BUYs → next same-wallet SELL within 24h → 480k BUYs, 147k closed round-trips
- Round-trip ROI computed from actual on-chain price: `(sell_price / buy_price) - 1`
- ROI capped at 0.01–1000× to rule out parse artifacts

## TEST 1 — Round-trip ROI by BUY size

| Size (SOL) | N | WR | Median ROI | Avg ROI | PF |
|---|---|---|---|---|---|
| 0.01–1 | 96,431 | 72.7% | +3.5% | +81% | 17.3 |
| 1–5 | 18,554 | 64.0% | +2.9% | +188% | 31.3 |
| **5–25** | **1,030** | **44.2%** | **−0.7%** | +11% | 2.2 |
| 25–100 | 75 | 42.7% | −3.5% | +1251% | 44.9 |
| **100–500** | **70** | **82.9%** | **+8.2%** | +5356% | **470.9** |
| 500–5000 | 30 | 70.0% | +4.1% | +5.4% | 1.8 |

**Findings:**
- Small BUYs (<5 SOL) have high WR (64–73%) with tiny median gains. This is probably pre-curated winners appearing disproportionately in the small-buy samples (survivor bias in the scanner DB).
- **5–25 SOL is a dead zone**: 44% WR, negative median. Big enough to move price, not big enough to signal conviction.
- **100–500 SOL is the sweet spot**: 83% WR, +8.2% median, PF 471.
- 500+ SOL bucket is noisy (n=30). Some may be whales dumping into their own pool.

## TEST 2 — Post-BUY price movement (any trader)

For each large BUY, compute median price change over all subsequent trades in the same mint by any trader:

| Size threshold | N | Median Δ @15min | Median Δ @60min |
|---|---|---|---|
| ≥1 SOL | 2,960 | +2.42% | +3.71% |
| ≥5 SOL | 2,772 | +1.18% | +1.51% |
| ≥25 SOL | 2,484 | +2.50% | +3.38% |
| ≥100 SOL | 1,538 | +1.98% | +2.79% |
| ≥500 SOL | 764 | 0.00%* | 0.00%* |

*500+ SOL bucket likely biased by illiquid/dead tokens where no trades happen post-buy.

**Finding:** median price moves UP 2–4% in the hour after a large BUY. "Price = demand" confirmed as a measurable effect.

## TEST 3 — Good wallet × Size cross-tab (the key result)

Good wallet = net > 10 SOL over 30d.

| Size | Tier | N | WR | Median ROI |
|---|---|---|---|---|
| 5–25 | good | 319 | 48.9% | −0.16% |
| 5–25 | other | 711 | 42.1% | −0.79% |
| **25–100** | **good** | **31** | **64.5%** | **+8.65%** |
| 25–100 | other | 44 | 27.3% | −19.35% |
| **100–500** | **good** | **36** | **94.4%** | **+15.89%** |
| 100–500 | other | 34 | 70.6% | +5.87% |
| 500–5000 | good | 29 | 72.4% | +5.30% |

**Finding:** size alone is NOT a sufficient filter. At 25–100 SOL:
- Good wallets' large buys → 64% WR, +8.7% median
- Random wallets' large buys → 27% WR, **−19% median**

At 100–500 SOL, good wallets hit **94% WR** vs 71% for random.

Random whales placing large orders are often liquidation, FOMO dumps, or coordinated pumps — NOT alpha.

## Proposed strategy — Wallet × Size compound filter

1. **Maintain curated wallet list** from on-chain-verified 30-day net-SOL ranking (top ~200 wallets, not 100).
2. **Only copy when that wallet's BUY amount_sol ≥ 25 SOL** (skip all scalps). Sweet spot is 100–500 SOL but 25+ is the statistically robust threshold.
3. **Drop copy volume** from ~3000/day (signals) to ~20–50/day (conviction signals).
4. **Expected per-trade WR** jumps from ~55% to ~80–94%, median ROI from ~0% to +8–16%.
5. **Mirror on SELL side**: when a curated wallet SELLs ≥25 SOL of a token we hold, we exit.

## Risks / unknowns

1. Sample size is thin in the 100–500 SOL good-wallet bucket (n=36). 94% WR is probably inflated by luck at small n; real is likely 75–85%.
2. Survivor bias in the "good wallet" group — wallets that net > 10 SOL / 30d already passed a filter for being alive and winning. New entrants won't show up here until they've banked a win.
3. We haven't measured our execution slip at the point when whales place large orders — such trades may already move price before our 4s-lag quote lands. At 100 SOL buy size, the price impact is real and we may chase. Needs live slip-gate.
4. Jupiter quote at T+4s after a 100 SOL source buy may show price already +5% up. We'd need to set slip-gate at maybe 10% to not miss these opportunities — which means tolerating some bad fills.
5. Good-wallet list must update continuously — our survivor-biased snapshot from today may drift.
6. The 500+ SOL bucket underperforming is concerning. Could mean the very biggest trades are coordinated/advertised, or it could just be sample-size noise.

## Implementation sketch

Add to `CopyEntry` / `copy_engine.py`:
- `min_source_sol: float` — per-wallet threshold (default 25)
- Before executing copy, check `trade.amount_sol >= entry.min_source_sol`; else log-only (could still record as watch)
- Keep watch-mode infrastructure for data collection on skipped small trades

Expected impact: dashboard copy rate drops 50–100×, win rate + ROI climbs sharply, real capital required stays the same or smaller.
