# Lessons Learned

## 2026-04-10 — CVD Market Maker Diagnostic (ARCHIVED)

### Lesson: A negative diagnostic is a successful diagnostic
The 6-phase plan promised to find out why the MM strategy was losing, not to make it profitable. Running the diagnostic to completion and discovering that (a) the CVD signal is anti-correlated with p<10⁻⁴, (b) the pure spread P&L is flat (no MM alpha), and (c) the "winner" experiment was underpowered — that is exactly what the plan was supposed to produce. Archiving is a legitimate outcome, not a failure.

### Lesson: `paper_dashboard.py:88-103` has a latent cycle_cash leak-forward bug
The single running `cycle_cash` accumulator in `compute_portfolio` only resets on SETTLE rows. Flat cycles (BUY/SELL with no settlement) never clear it, so their cash leaks forward into the next settled cycle. Trailing fills after the last SETTLE are dropped entirely. On the 2026-04-10 baseline this caused `settled_pnl` to diverge from `cash` by $7.51 (out of −$269 total). The diagnostic harness `backtest.py` works around the bug by targeting `dash["cash"]` instead of `dash["settled_pnl"]`. A proper fix is its own issue.

### Lesson: Don't parallelize subagents for deterministic pandas work
Phase 2 called for 4 parallel subagents to run 4 counterfactual experiments. Pushed back in mentor mode: 4 pure deterministic Python functions running in ~1 second total on the same CSV don't benefit from parallel subagents. The subagent overhead (prompt tokens, dispatch latency, coordination) is larger than the sequential runtime. Parallelism should be reserved for genuinely independent long-running work (e.g., Phase 5 monitoring).

### Lesson: File-level staging bundles unrelated work
Commit `af9807f` (the Phase 4.1a MM_NEUTRAL_ONLY gate) inadvertently included ~240 lines of pre-existing in-session MM improvements (rebate modeling, fill detection refactor, per-side cooldowns, PAPER_BALANCE). The implementer used `git add cvd_5min_bot.py` which stages the entire file. Use `git add -p` or `git stash` + clean apply workflow when the working tree has unrelated dirty state. This doesn't affect correctness but pollutes commit messages. Controller accepted the pollution rather than rewriting history because the repo is local-only.

### Lesson: `forced_flatten` is the most informative counterfactual
Dropping the SETTLE leg and marking residual inventory to $0.50 isolates pure MM spread P&L from directional settlement exposure. On the 2026-04-10 baseline, this showed per_cycle = −$0.24 (essentially flat), meaning 93% of the observed loss came from the settlement leg alone, not from MM execution. This is how to distinguish "bad signal" from "bad execution" in any future MM-like strategy.

### Lesson: Small n means a Phase 5 validation window of 60 minutes is theater
The plan's Phase 5 spec was a 60-minute live paper validation. With ~12 cycles × 25% NEUTRAL ratio ≈ 3 NEUTRAL cycles in that window, it cannot validate a 16-cycle hypothesis. Sample-size-agnostic exit criteria ("alignment ≥ 55%", "per_cycle > $0") need to be paired with a minimum-n check. A statistically meaningful Phase 5 for this hypothesis would need ≥6 hours of paper data. Phase 5 was skipped in favor of archiving directly.
