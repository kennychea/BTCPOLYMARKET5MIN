# Ex-Post Experiment Results — 2026-04-10

**Baseline CSV:** `data/mm_paper_trades_baseline_2026-04-10.csv`
**Session:** 2026-04-10, ~3h45 paper trading, bot stopped mid-81st cycle
**Rows:** 822 (382 BUY, 376 SELL, 64 SETTLE)
**Cycles:** 81 total, 64 settled, 17 flat (16 net-zero mid-session + 1 orphan open short at end)
**Harness commit:** `9696fcb` (after `5307f14` running-net refactor)

## Clean baseline totals (from `backtest.experiment_baseline`)

- **`total_pnl`** (sum of cycle_cash across all 81 cycles) = **−$269.4825**
  - Matches `paper_dashboard.compute_portfolio()["cash"]` to floating-point noise (delta ~3×10⁻¹³)
  - NOT `settled_pnl` (−$276.9965) — dashboard's `settled_pnl` accumulator leaks flat-cycle cash into adjacent settled cycles; see comment block in `tests/unit/test_backtest.py::TestCycleCash` and `paper_dashboard.py:88-103` for the latent bug
- **Including open-position mark-to-market** (−10 short × 0.77): `dash["total_pnl"]` = −$277.1825
- **Per-cycle mean (all 81):** −$3.3269
- **Clean alignment (inventory sign vs outcome):** **17/64 = 26.56%**
  - One-sided binomial p(X≤17 | n=64, p=0.5) = **1.13×10⁻⁴**
  - Strongly anti-correlated, not noise — signal is effectively inverted

## Comparison table

| Experiment | cycles_kept | total_pnl | per_cycle_mean | alignment | p-value | Score |
|---|---:|---:|---:|---:|---:|---|
| **baseline** | 81 | −$269.48 | −$3.33 | 17/64 = 26.6% | 1.1×10⁻⁴ | reference (Archive) |
| **neutral_only** | 21 | **+$2.63** | **+$0.13** | **10/16 = 62.5%** | **0.227** | **Small edge** |
| **forced_flatten_0.50** | 81 | −$19.48 | −$0.24 | N/A | — | Break-even-ish (spread only) |
| **half_cap_10** | 81 | −$180.71 | −$2.23 | 10/45 = 22.2% | 1.2×10⁻⁴ | Archive |

## Per-experiment analysis

### `experiment_baseline` — sanity / reproduction
Reproduces `dash["cash"]` exactly. The baseline strategy is statistically catastrophic: 26.6% alignment on 64 settled cycles is ~1 chance in 10 000 under the null hypothesis of random signal. The CVD skew as computed in `cvd_5min_bot.py:1863-1868` is **anti-correlated** with 5-minute BTC outcomes on this session — the inventory sign is consistently on the wrong side of settlement. Per-cycle mean of −$3.33 across all cycles is the reference loss rate.

### `experiment_neutral_only` — drop non-NEUTRAL dominant-signal cycles
The only experiment that flips the session positive. Drops 60 of 81 cycles (74% of session), keeps 21. Retained cycles: total +$2.63, per_cycle +$0.13, alignment 10/16 = 62.5%.

**What this actually tells us:**
- **Raw headline:** the filter works on the in-sample data.
- **Caveat 1 (statistical):** n=16 settled cycles. p(X≥10 | n=16, p=0.5) = 0.227. NOT statistically significant. Could easily be noise.
- **Caveat 2 (mechanism):** the 60 dropped cycles were the ones losing catastrophically. The "edge" is mostly **not-trading-in-bad-regimes**, not **trading-profitably-in-good-regimes**. In the 21 kept cycles, the bot realized +$0.13/cycle ≈ break-even.
- **Caveat 3 (yield):** +$2.63 realized in ~3h45 = $0.70/hour. Below any realistic operational cost.
- **Caveat 4 (out-of-sample):** we do not know if `NEUTRAL regime` is a stable predictor of `CVD alignment flip`, or if it was correlated with range-bound BTC price action on this specific afternoon.

### `experiment_forced_flatten_0.50` — strip settlement leg, mark open to $0.50
**This is the most informative experiment despite not being the "winner".** Stripping the settlement leg entirely and marking residual inventory to the binary midpoint yields total −$19.48, per_cycle −$0.24. Interpretation:

- Pure MM spread P&L (rebates + fill spread, no settlement exposure) is essentially **zero** across the whole session — slightly negative (~$0.24/cycle drag) but within noise.
- 93% of the baseline loss (−$250 of −$269) comes from the settlement leg alone.
- **There is no market-making alpha to recover**: the rebates + captured spread barely cover the opening/closing cost of the inventory. The bot is not a profitable market maker; it is a directional bet with neutral dressing.
- The real question is not "how do we improve the MM spread?" (there's nothing to improve) but "how do we avoid taking directional exposure into settlement when the signal is inverted?".

### `experiment_half_cap_10` — simulate MM_MAX_INVENTORY=10
Directional simulation only (rejects fills that would exceed |net|=10 under the tighter cap; does not simulate the bot's subsequent decisions under a real cap). Total −$180.71, per_cycle −$2.23, alignment 10/45 = 22.2% (p ≈ 1.2×10⁻⁴). The cap reduces the loss magnitude by ~33% because it limits position size, but **does not fix the signal inversion**. Still strongly adverse alignment. **Archive.**

## Phase 3 Gate — Winner selection

Per plan Task 3.2 rules:

1. **Any Clear/Small edge?** — `neutral_only` scores Small edge (per_cycle +$0.13 > $0 ✓ AND alignment 62.5% ≥ 55% ✓). **WINNER by Rule 1.**
2. Rules 2-4 not triggered.

**Proceeding to Phase 4.1a: `neutral_only` gating implementation.**

## Honest caveats before Phase 4.1a

The gate decision follows the plan's rules, but the plan's thresholds are sample-size-agnostic. The statistical reality:

- **Base strategy is confirmed dead** (p<10⁻⁴). Do not deploy unfiltered under any circumstance.
- **neutral_only is a hint, not a confirmed fix.** 16 settled cycles is not enough to reject the null at standard significance.
- **forced_flatten confirms there is no MM alpha** to recover. Any fix that still takes settlement exposure with the current CVD signal is exposed to the same −$4/cycle loss rate in non-NEUTRAL regimes.
- **Phase 5 at 60 minutes is insufficient.** ~12 cycles × 25% NEUTRAL ratio ≈ 3 NEUTRAL cycles in a 60-minute window — too few to validate a 62.5% alignment hypothesis. A statistically meaningful Phase 5 would need ~6-8 hours of paper data to accumulate 80-100 NEUTRAL cycles.

## Recommendations (orthogonal to the Phase 4 execution)

1. **Execute Phase 4.1a + Phase 5** as the plan specifies, treating Phase 5 as a sanity check that the gating logic *compiles and runs*, not as a statistical confirmation.
2. **After Phase 6, if the user wants true validation:** extend paper validation to ≥6 hours before declaring `neutral_only` a keeper.
3. **Open a separate issue** to fix `paper_dashboard.py:88-103` leak-forward bug in `cycle_cash` accumulator. Not in scope for this plan, but it is a latent footgun for any future P&L analysis.
4. **Consider the forced_flatten path as a parallel track.** It addresses the root cause (settlement exposure) directly, and its pure-MM P&L of −$0.24/cycle is in the same neighborhood as neutral_only's +$0.13/cycle, without dropping 74% of cycles. If neutral_only's Phase 5 is inconclusive, forced_flatten is the natural next hypothesis.
5. **Consider archive** if the user's tolerance for statistical uncertainty is low. The honest summary of this diagnostic is: "the CVD signal as wired does not work; market-making alone has no alpha; the one experiment that looked positive was underpowered."
