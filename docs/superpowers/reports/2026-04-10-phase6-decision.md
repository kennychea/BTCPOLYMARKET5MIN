# Phase 6 Decision — CVD Market Maker

**Date:** 2026-04-10
**Status:** ARCHIVED
**Summary:** The CVD market maker strategy is archived. The signal is confirmed inverted, MM spread alpha is confirmed zero, and the neutral_only filter is statistically underpowered. No live deployment path exists with the current signal.

---

## Decision

The CVD market maker strategy (`STRATEGY=mm`, `CVDMarketMaker` in `cvd_5min_bot.py`) is archived pending a fundamental redesign of the directional signal. The `MM_NEUTRAL_ONLY` flag remains in the codebase as an opt-in for future research but defaults to `false`.

---

## Why Archive — Not Fix

**1. Signal inversion is confirmed.**
26.6% alignment on 64 settled cycles, p=1.1×10⁻⁴. One chance in ~9,000 of observing this by luck under a random signal. The `compute_cvd_skew()` derivation (`cvd_5min_bot.py:1848-`) is not noisy — it is systematically pointing the wrong direction. The bot did not have bad luck; the signal is wired backwards relative to Polymarket 5-minute BTC outcomes on this data.

**2. No MM spread alpha exists.**
`experiment_forced_flatten_0.50` strips the settlement leg entirely and marks residual inventory to midpoint ($0.50). The result: −$19.48 total, −$0.24/cycle. Spread capture + maker rebates net to approximately zero across the full session. 93% of the baseline loss (−$250 of −$269) comes from the settlement leg alone. The strategy was never a market maker capturing spread; it was a directional bet in market-maker clothing. There is nothing in the MM execution quality to fix.

**3. neutral_only is underpowered and mechanistic.**
p=0.227 on 16 settled cycles. The filter does not trade better in NEUTRAL regimes; it avoids trading 74% of the time. The +$0.13/cycle yield in retained cycles is break-even at best. +$2.63 realized over 3h45 = $0.70/hour, below any realistic operational cost. Phase 5 was skipped because the math is clear: 60 minutes of paper would produce ~3 NEUTRAL-regime cycles, which cannot validate a 16-cycle hypothesis. The correct minimum sample for statistical confidence (80%+ power) is ~6-8 hours of additional paper data. Running 6 hours to learn nothing new beyond what Phase 2 already showed is not a useful trade of time.

---

## What Is Still in the Codebase

| Component | State | Notes |
|---|---|---|
| `MM_NEUTRAL_ONLY` env flag + gate logic in `_refresh_quotes` | **Kept**, defaults `false` | Available for future signal research; commit `af9807f` |
| `CVDMarketMaker` class and all MM infrastructure | **Kept** | If a future signal redesign reuses the execution scaffolding |
| `backtest.py` diagnostic harness + 4 experiment drivers | **Kept** | Useful for ex-post counterfactuals on any future replay CSV |
| `data/mm_paper_trades_baseline_2026-04-10.csv` | **Kept** | 822-row ground truth for the diagnostic |
| `.env` defaults | `STRATEGY=stink`, `MM_NEUTRAL_ONLY=false` | **Do not run `STRATEGY=mm` live until signal is redesigned** |

---

## Scope Issue in Commit af9807f — Honest Disclosure

Commit `af9807f` ("feat(mm): add MM_NEUTRAL_ONLY gate to disable quotes on non-NEUTRAL signals") contains approximately 240 lines of unrelated in-session work that was bundled via file-level staging rather than hunk-level staging. The bundled material includes:

- `MMInventory` rebate and settle modeling (rebate accounting corrected)
- `check_mm_paper_fills` refactor (improved fill detection logic)
- Per-side fill cooldowns
- `PAPER_BALANCE` constant extraction
- New `TestLogMmSettle` unit tests

This was not reverted for three reasons: (a) the history is local-only — no remote exists that has already read this commit as scoped; (b) the bundled code is legitimately useful and correct — the rebate accounting and fill detection improvements are improvements; (c) the cost of reset + resplit exceeds the cleanliness benefit at this point in the diagnostic.

Future git blame readers: the "feat(mm): add MM_NEUTRAL_ONLY gate" subject line is misleading. The actual diff is a broader MM infrastructure patch. The honest record of what is in that commit lives here.

---

## Latent Bug to Track Separately — paper_dashboard.py:88-103

`paper_dashboard.py` lines 88-103 maintain a running `cycle_cash` accumulator that resets only on SETTLE rows. This causes two separate problems:

1. **Flat cycles leak forward.** If a cycle closes with no SETTLE (net inventory went to zero mid-cycle without a market-resolved SETTLE event), the accumulated fill cash for that cycle carries forward into the next cycle's accumulator. The next SETTLE row then claims that cash as part of its cycle — attributing the wrong P&L to the wrong cycle.

2. **Trailing fills are dropped.** Any fills that occur after the last SETTLE row in the session are never attributed to a cycle and disappear from `settled_pnl`.

Consequence: `dash["settled_pnl"]` diverged from `dash["cash"]` by $7.51 on the 2026-04-10 baseline session. The diagnostic harness works around this by targeting `dash["cash"]` (the cycle-independent sum across all rows) rather than `dash["settled_pnl"]`.

**The correct fix** is to split the accumulator to reset at cycle boundaries (by `cycle_id` group or by net-position-zero crossing), not only at SETTLE events, and to handle trailing-fill cycles explicitly. This is out of scope for the MM diagnostic. It should become its own tracked issue before any future P&L analysis trusts `settled_pnl` directly.

---

## What a Future MM Attempt Would Need

**1. A signal that is not CVD-on-Binance-perp.**
CVD derived from the perpetual funding/volume feed did not transfer to Polymarket 5-minute BTC outcomes in this session. Candidate replacements: spot order flow imbalance, options skew, funding rate divergence, or on-chain flow. An inversion of the current signal (flip the sign) is not acceptable as a fix without a mechanism explanation — that is pure overfitting to a 64-cycle in-sample.

**2. A real spread-capture test.**
`experiment_forced_flatten_0.50` already answers the question "is there spread alpha in Polymarket 5-min binaries?": approximately no. Before building another MM strategy, confirm that the bid-ask spread in the target market exceeds the inventory carry cost times the fill rate. Polymarket 5-minute binaries may structurally not be a market-making opportunity — taker volume is thin, and the settlement binary payoff means carry cost is not smooth.

**3. Position sizing that respects directional exposure.**
`MM_MAX_INVENTORY=20` with ~$0.50 pricing means up to $10 of directional exposure per cycle. At ~12 cycles per hour, that is $120/hour of directional risk on a $500 paper account — 24% drawdown per hour of sustained adversity. The 2026-04-10 session confirmed: sustained adversity is exactly what a broken signal delivers. Any future MM variant must treat the inventory limit as a directional risk cap, not just a share count cap, and must size it relative to account equity and expected cycle duration.

---

## Final Numbers Reference

| Experiment | cycles_kept | total_pnl | per_cycle_mean | alignment | p-value |
|---|---:|---:|---:|---:|---:|
| baseline | 81 | −$269.48 | −$3.33 | 17/64 = 26.6% | 1.1×10⁻⁴ |
| neutral_only | 21 | +$2.63 | +$0.13 | 10/16 = 62.5% | 0.227 |
| forced_flatten_0.50 | 81 | −$19.48 | −$0.24 | N/A | — |
| half_cap_10 | 81 | −$180.71 | −$2.23 | 10/45 = 22.2% | 1.2×10⁻⁴ |

---

## Commit References

| Phase | Commit | Artifact |
|---|---|---|
| Phase 0 — baseline snapshot | `d70d92d` | `data/mm_paper_trades_baseline_2026-04-10.csv` |
| Phase 1 — harness build | `c16eb74`, `5307f14`, `9696fcb` | `backtest.py`, `tests/unit/test_backtest.py` |
| Phase 3 — gate report | `19dcda5` | `docs/superpowers/reports/2026-04-10-experiment-results.md` |
| Phase 4.1a — neutral_only gate | `af9807f` (bundled, see disclosure above), `e1a698f` | `cvd_5min_bot.py`, `tests/unit/test_mm_paper.py` |
| Phase 5 | **SKIPPED** | Statistically insufficient sample in 60 min; see rationale above |
| Phase 6 — this document | *(committed after this file is written)* | `docs/superpowers/reports/2026-04-10-phase6-decision.md` |

---

## Diagnostic Outcome

The plan promised a diagnostic. The diagnostic delivered:

- A confirmed root cause (inverted CVD signal, not execution noise).
- A confirmed absence of MM spread alpha (forced_flatten).
- A mechanistic explanation for 93% of the session loss (settlement leg under an inverted signal).
- A working backtest harness that will serve future sessions.
- A correct negative conclusion: no deployment path exists with this signal.

That is a successful diagnostic. The fact that the conclusion is "archive" is not a failure. The failure would have been deploying this live and learning the same lesson with real USDC.
