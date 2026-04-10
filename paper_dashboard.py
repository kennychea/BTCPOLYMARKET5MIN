#!/usr/bin/env python3
"""
Live CLI dashboard for CVD paper trading results.
Reads data/paper_trades.csv and displays accuracy stats in real-time.

Updated 2026-04-11 for the stink-paper-validation branch:
- Reads the triple-truth schema (signal_correct_binance_perp/_spot/_polymarket)
  with polymarket as the primary source used by paper_gate.
- Adds a PAPER GATE panel showing current status, n, winrate, p-value,
  distance to MIN_N_FOR_DECISION (120), and a progress bar.

Usage: python paper_dashboard.py
"""
import json
import os
import sys
import time
from datetime import datetime

import pandas as pd
from termcolor import colored

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
PAPER_LOG_FILE = os.path.join(DATA_DIR, "paper_trades.csv")
MM_LOG_FILE = os.path.join(DATA_DIR, "mm_paper_trades.csv")
GATE_SENTINEL_FILE = os.path.join(DATA_DIR, "paper_gate_status.json")
REFRESH_INTERVAL = 5

# Primary truth column used by paper_gate. Bot now writes three parallel
# *_binance_perp / *_binance_spot / *_polymarket columns; polymarket is
# the one the gate actually votes on, so it's what the dashboard displays.
TRUTH_COL = "signal_correct_polymarket"
PERP_COL = "signal_correct_binance_perp"
SPOT_COL = "signal_correct_binance_spot"

# Kept in sync with paper_gate.MIN_N_FOR_DECISION for progress-bar display.
GATE_MIN_N = 120


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def progress_bar(pct: float, width: int = 20) -> str:
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def acc_color(accuracy: float) -> str:
    if accuracy >= 55:
        return "green"
    if accuracy >= 45:
        return "yellow"
    return "red"


def render_dashboard():
    clear_screen()

    # Header
    print(colored(
        "╔══════════════════════════════════════════════════════════╗\n"
        "║  📝 CVD Paper Trading Dashboard  (Live)                  ║\n"
        "╚══════════════════════════════════════════════════════════╝",
        "cyan", attrs=["bold"],
    ))

    now = datetime.now().strftime("%H:%M:%S")
    print(colored(f"  Last refresh: {now}  |  Auto-refresh every {REFRESH_INTERVAL}s\n", "white"))

    # Check CSV exists
    if not os.path.exists(PAPER_LOG_FILE):
        print(colored("  ⏳ Waiting for data/paper_trades.csv...", "yellow"))
        print(colored("  Run the bot with PAPER_MODE=true to start collecting data.\n", "white"))
        return

    try:
        df = pd.read_csv(PAPER_LOG_FILE)
    except Exception as e:
        print(colored(f"  ⚠️ Error reading CSV: {e}", "red"))
        return

    if len(df) == 0:
        print(colored("  ⏳ CSV is empty, waiting for signals...\n", "yellow"))
        return

    if TRUTH_COL not in df.columns:
        print(colored(
            f"  ⚠️ CSV missing '{TRUTH_COL}' column. "
            "Are you pointing at a legacy paper_trades.csv?",
            "red",
        ))
        return

    resolved = df[df[TRUTH_COL].isin(["YES", "NO"])]
    pending = df[~df[TRUTH_COL].isin(["YES", "NO"])]
    total = len(resolved)
    correct = len(resolved[resolved[TRUTH_COL] == "YES"]) if total > 0 else 0
    wrong = total - correct
    accuracy = (correct / total * 100) if total > 0 else 0.0

    render_gate_panel(total, accuracy)
    render_truth_disagreement_panel(df)

    # ── Overall Accuracy ──
    print(colored("  ── OVERALL ACCURACY ─────────────────────────────────", "cyan", attrs=["bold"]))
    if total > 0:
        bar = progress_bar(accuracy)
        print(colored(f"  Signals: {total}  |  Correct: {correct}  |  Wrong: {wrong}", "white"))
        print(
            colored("  Accuracy: ", "white")
            + colored(f"{accuracy:.1f}% {bar}", acc_color(accuracy), attrs=["bold"])
        )
    else:
        print(colored("  No resolved signals yet.", "yellow"))
    print(colored(f"  Pending: {len(pending)}", "yellow"))
    print()

    # ── By Signal Type ──
    if total > 0:
        print(colored("  ── BY SIGNAL TYPE ───────────────────────────────────", "cyan", attrs=["bold"]))
        for sig_type in sorted(resolved["signal_type"].unique()):
            subset = resolved[resolved["signal_type"] == sig_type]
            sub_c = len(subset[subset[TRUTH_COL] == "YES"])
            sub_t = len(subset)
            sub_acc = (sub_c / sub_t * 100) if sub_t > 0 else 0.0
            bar = progress_bar(sub_acc, width=15)
            print(
                colored(f"  {sig_type:<14}: ", "white")
                + colored(f"{sub_c}/{sub_t} ({sub_acc:5.1f}%) {bar}", acc_color(sub_acc))
            )
        print()

    # ── Recent Signals ──
    print(colored("  ── RECENT SIGNALS ───────────────────────────────────", "cyan", attrs=["bold"]))
    recent = df.tail(10).iloc[::-1]
    for _, row in recent.iterrows():
        ts = str(row["timestamp"])[11:19]
        sig = str(row["signal_type"])[:14]
        direction = str(row["direction"])
        btc_at = float(row["btc_price_at_signal"])
        btc_close = float(row["btc_price_at_close"])
        pct = float(row["price_change_pct"])
        status = str(row[TRUTH_COL])

        if status == "YES":
            emoji, clr = "✅", "green"
        elif status == "NO":
            emoji, clr = "❌", "red"
        else:
            emoji, clr = "⏳", "yellow"

        if btc_close > 0:
            print(colored(
                f"  {ts}  {sig:<14} {direction:<5} "
                f"${btc_at:>10,.1f} → ${btc_close:>10,.1f}  {pct:+.3f}%  {emoji}",
                clr,
            ))
        else:
            print(colored(
                f"  {ts}  {sig:<14} {direction:<5} "
                f"${btc_at:>10,.1f}   (pending)                {emoji}",
                clr,
            ))
    print()

    # ── Price Movement Stats ──
    if total > 0:
        print(colored("  ── PRICE MOVEMENT STATS ─────────────────────────────", "cyan", attrs=["bold"]))
        correct_df = resolved[resolved[TRUTH_COL] == "YES"]
        wrong_df = resolved[resolved[TRUTH_COL] == "NO"]
        if len(correct_df) > 0:
            avg_c = correct_df["price_change_pct"].abs().mean()
            print(colored(f"  Avg move (correct): {avg_c:.4f}%", "green"))
        if len(wrong_df) > 0:
            avg_w = wrong_df["price_change_pct"].abs().mean()
            print(colored(f"  Avg move (wrong):   {avg_w:.4f}%", "red"))
        total_signals = len(df)
        mtime = os.path.getmtime(PAPER_LOG_FILE)
        hours_elapsed = max(0.001, (time.time() - mtime) / 3600)
        if hours_elapsed < 24:
            signals_per_hour = total_signals / hours_elapsed if hours_elapsed > 0.1 else 0
            if signals_per_hour < 1000:
                print(colored(f"  Signal rate: ~{signals_per_hour:.1f}/hour", "white"))
        print()

    render_mm_panel()

    print(colored("  Press Ctrl+C to exit.", "yellow"))


def render_gate_panel(n: int, live_accuracy_pct: float):
    """Render paper_gate status at the top of the dashboard.

    Reads data/paper_gate_status.json which the bot writes at the end of
    every market cycle. Displays current gate verdict, n vs MIN_N threshold,
    and a progress bar toward the decision point.
    """
    print(colored("  ── PAPER GATE STATUS ────────────────────────────────", "cyan", attrs=["bold"]))

    if not os.path.exists(GATE_SENTINEL_FILE):
        print(colored(
            "  ⏳ No sentinel yet — bot hasn't completed a cycle.",
            "yellow",
        ))
        print()
        return

    try:
        with open(GATE_SENTINEL_FILE) as f:
            sentinel = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(colored(f"  ⚠️ Sentinel unreadable: {e}", "red"))
        print()
        return

    status = sentinel.get("status", "?")
    reason = sentinel.get("reason", "")
    gate_n = int(sentinel.get("n", 0))
    winrate = float(sentinel.get("winrate", 0.0)) * 100
    p_value = float(sentinel.get("p_value", 1.0))
    ev = float(sentinel.get("ev_per_trade", 0.0))
    evaluated_at = str(sentinel.get("evaluated_at", ""))[11:19]

    status_colors = {
        "PASS": ("green", "✅"),
        "KILL": ("red", "🛑"),
        "INCONCLUSIVE": ("yellow", "🟡"),
        "CONTINUE": ("white", "⏳"),
    }
    clr, emoji = status_colors.get(status, ("white", "•"))

    print(colored(
        f"  {emoji}  STATUS: {status}",
        clr, attrs=["bold"],
    ))
    print(colored(f"  Reason: {reason}", clr))

    # Progress bar toward the n threshold.
    pct = min(100.0, gate_n / GATE_MIN_N * 100)
    bar = progress_bar(pct, width=30)
    remaining = max(0, GATE_MIN_N - gate_n)
    if remaining > 0:
        progress_line = (
            f"  Progress: {gate_n}/{GATE_MIN_N} settled  "
            f"({remaining} more for decision)"
        )
    else:
        progress_line = f"  Progress: {gate_n}/{GATE_MIN_N} settled (threshold reached)"
    print(colored(progress_line, "white"))
    print(colored(f"  {bar} {pct:5.1f}%", "cyan"))

    # Stats line — what the gate is judging on.
    ev_color = "green" if ev > 0 else "red"
    print(
        colored(f"  Winrate: {winrate:5.1f}%  ", "white")
        + colored(f"p-value: {p_value:.4f}  ", "white")
        + colored(f"EV/trade: ${ev:+.3f}", ev_color)
    )
    if evaluated_at:
        print(colored(f"  Evaluated at: {evaluated_at}  |  CSV shows n={n}", "white"))
    print()


def render_truth_disagreement_panel(df: pd.DataFrame):
    """Show how often the three truth sources disagree.

    Divergence between binance perp/spot and polymarket is a red flag: it
    means the market resolved on a different print than the reference
    exchanges, or one of the feeds is lagging. High divergence invalidates
    the paper_gate verdict because the gate only votes on the polymarket
    column.
    """
    needed_cols = {PERP_COL, SPOT_COL, TRUTH_COL}
    if not needed_cols.issubset(df.columns):
        return

    # Only rows where all three have a resolved verdict.
    both = df[
        df[PERP_COL].isin(["YES", "NO"])
        & df[SPOT_COL].isin(["YES", "NO"])
        & df[TRUTH_COL].isin(["YES", "NO"])
    ]
    if len(both) == 0:
        return

    n = len(both)
    perp_vs_poly = int((both[PERP_COL] != both[TRUTH_COL]).sum())
    spot_vs_poly = int((both[SPOT_COL] != both[TRUTH_COL]).sum())
    perp_vs_spot = int((both[PERP_COL] != both[SPOT_COL]).sum())

    def pct_color(k: int, total: int) -> str:
        p = k / total if total > 0 else 0
        if p >= 0.10:
            return "red"
        if p >= 0.03:
            return "yellow"
        return "green"

    print(colored("  ── TRUTH SOURCE DISAGREEMENT ────────────────────────", "cyan", attrs=["bold"]))
    print(colored(f"  Cross-checked rows: {n}", "white"))
    print(
        colored("  Binance perp  ≠  Polymarket : ", "white")
        + colored(f"{perp_vs_poly}/{n} ({perp_vs_poly/n*100:.1f}%)",
                  pct_color(perp_vs_poly, n))
    )
    print(
        colored("  Binance spot  ≠  Polymarket : ", "white")
        + colored(f"{spot_vs_poly}/{n} ({spot_vs_poly/n*100:.1f}%)",
                  pct_color(spot_vs_poly, n))
    )
    print(
        colored("  Binance perp  ≠  Binance spot: ", "white")
        + colored(f"{perp_vs_spot}/{n} ({perp_vs_spot/n*100:.1f}%)",
                  pct_color(perp_vs_spot, n))
    )
    print()


def render_mm_panel():
    """Render market maker stats panel (only if MM data exists)."""
    if not os.path.exists(MM_LOG_FILE):
        return

    try:
        df = pd.read_csv(MM_LOG_FILE)
    except Exception:
        return

    if len(df) == 0:
        return

    print(colored("  ── MARKET MAKER STATS ────────────────────────────────", "magenta", attrs=["bold"]))

    buys = df[df["side"] == "BUY"]
    sells = df[df["side"] == "SELL"]

    total_bought = (buys["price"] * buys["size"]).sum() if len(buys) > 0 else 0
    total_sold = (sells["price"] * sells["size"]).sum() if len(sells) > 0 else 0
    realized_pnl = total_sold - total_bought

    print(colored(f"  Fills: {len(df)} ({len(buys)} buys, {len(sells)} sells)", "white"))
    pnl_color = "green" if realized_pnl >= 0 else "red"
    print(colored(f"  Realized P&L: ${realized_pnl:.4f}", pnl_color, attrs=["bold"]))

    if len(buys) > 0:
        print(colored(f"  Avg buy:  ${buys['price'].mean():.4f}", "white"))
    if len(sells) > 0:
        print(colored(f"  Avg sell: ${sells['price'].mean():.4f}", "white"))
    if len(buys) > 0 and len(sells) > 0:
        avg_spread = sells["price"].mean() - buys["price"].mean()
        print(colored(f"  Avg spread captured: ${avg_spread:.4f}", "cyan"))

    # Last 5 fills
    print(colored("\n  Recent fills:", "white"))
    recent = df.tail(5).iloc[::-1]
    for _, row in recent.iterrows():
        ts = str(row["timestamp"])[11:19]
        side = row["side"]
        emoji = "🟢" if side == "BUY" else "🔴"
        clr = "green" if side == "BUY" else "red"
        print(colored(
            f"  {ts}  {emoji} {side:<4} {int(row['size'])} @ ${row['price']:.4f} "
            f"| inv: {int(row['inventory_after'])} | skew: {row['cvd_skew']:+.4f}",
            clr,
        ))
    print()


def main():
    print(colored("Starting Paper Trading Dashboard...", "cyan"))
    while True:
        try:
            render_dashboard()
            time.sleep(REFRESH_INTERVAL)
        except KeyboardInterrupt:
            print(colored("\n\n  Dashboard stopped.", "yellow"))
            break
        except Exception as e:
            print(colored(f"\n  ⚠️ Dashboard error: {e}", "red"))
            time.sleep(REFRESH_INTERVAL)


if __name__ == "__main__":
    main()
