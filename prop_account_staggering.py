"""Test prop-account staggering from trade sweeps.

The default run combines every hourly RR=1.00 sweep in ``1_sweeps/RR``. It
models each account independently, applies a supplied per-trade commission,
and checks the drawdown rule at the trade's MAE (not only at end of day).

Example:
    python prop_account_staggering.py --start-policy time --interval-days 30 \
        --commission-per-trade 0.75
    python prop_account_staggering.py --start-policy any --interval-days 14 \
        --profit-trigger 1000 --dd-trigger 400
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
DEFAULT_SWEEP_ROOT = ROOT / "1_sweeps" / "RR"
RESULTS_DIR = ROOT / "results"


@dataclass
class Account:
    number: int
    started_at: pd.Timestamp
    start_reason: str
    balance: float = 0.0  # Net trading P&L; account notional is intentionally excluded.
    peak: float = 0.0
    drawdown: float = 0.0
    frozen: bool = False
    alive: bool = True
    failed_at: pd.Timestamp | None = None
    failure_reason: str | None = None
    gross_pnl: float = 0.0
    commissions: float = 0.0
    trades: int = 0
    reached_freeze: bool = False
    history: list[tuple[pd.Timestamp, float]] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--input-csv", type=Path, help="One MAE-style TSV/CSV trade export.")
    source.add_argument("--sweep-root", type=Path, default=DEFAULT_SWEEP_ROOT,
                        help=f"RR sweep root (default: {DEFAULT_SWEEP_ROOT}).")
    parser.add_argument("--rr", type=float, default=1.00, help="RR selected from every window.")
    parser.add_argument("--windows", default="all",
                        help="Comma-separated windows, e.g. 9-10,10-11; default: all.")
    parser.add_argument("--start-policy", choices=("time", "profit", "dd", "any"), default="time",
                        help="How to start the next account. 'any' combines enabled triggers.")
    parser.add_argument("--max-accounts", type=int, default=20, help="Maximum accounts ever started.")
    parser.add_argument("--interval-days", type=int, default=30, help="Calendar spacing for time starts.")
    parser.add_argument("--profit-trigger", type=float, default=1000.0,
                        help="Net P&L required on the newest live account for a profit start.")
    parser.add_argument("--dd-trigger", type=float, default=400.0,
                        help="Closed drawdown that triggers a DD start (positive dollar amount).")
    parser.add_argument("--dd-recovery", type=float, default=0.0,
                        help="Required recovery of the triggering account before another DD start.")
    parser.add_argument("--min-days-between-starts", type=int, default=1)
    parser.add_argument("--dd-limit", type=float, default=1500.0,
                        help="Trailing drawdown before the account is frozen.")
    parser.add_argument("--freeze-at-profit", type=float, default=1600.0,
                        help="Unrealized P&L peak (the Safety Net) at which trailing DD becomes fixed.")
    parser.add_argument("--frozen-dd-floor", type=float, default=100.0,
                        help="Fixed account P&L floor after trailing DD freezes.")
    parser.add_argument("--account-fee", type=float, default=0.0,
                        help="One-off fee per account; reported separately from trading P&L.")
    parser.add_argument("--commission-per-trade", type=float, default=0.0,
                        help="Round-turn commission to subtract from every trade. Use 0 if P&L is already net.")
    parser.add_argument("--intratrade-path", choices=("mfe-first", "mae-first"), default="mfe-first",
                        help="Unknown MAE/MFE ordering within a trade. mfe-first is the conservative prop-DD case.")
    parser.add_argument("--start-date", type=str, help="Inclusive YYYY-MM-DD filter.")
    parser.add_argument("--end-date", type=str, help="Inclusive YYYY-MM-DD filter.")
    parser.add_argument("--no-plot", action="store_true", help="Write CSV results only.")
    return parser.parse_args()


def sweep_files(root: Path, rr: float, windows: str) -> list[Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"Sweep root does not exist: {root}")
    wanted = None if windows.lower() == "all" else {item.strip() for item in windows.split(",")}
    files: list[Path] = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        if wanted is not None and directory.name not in wanted:
            continue
        candidate = directory / f"{directory.name}_{rr:.2f}.csv"
        if candidate.exists():
            files.append(candidate)
        else:
            print(f"WARNING: no RR={rr:.2f} file for window {directory.name}: {candidate}")
    if not files:
        raise FileNotFoundError("No sweep files matched the selected RR/windows.")
    return files


def read_trade_file(path: Path) -> pd.DataFrame:
    """Read both headerless MT5 sweep exports and the original MAE export."""
    # MT5 exports in this folder are not consistently encoded: most are UTF-8
    # but some were saved as UTF-16 LE.  The content is otherwise identical.
    raw = None
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8", "utf-16", "cp1252"):
        try:
            raw = pd.read_csv(path, sep="\t", header=None, dtype=str, encoding=encoding)
            break
        except UnicodeDecodeError as error:
            last_error = error
    if raw is None:
        raise last_error or ValueError(f"Could not read {path}")
    if raw.empty:
        return pd.DataFrame(columns=["Ticket", "Entry_time", "Exit_time", "MAE", "MFE", "PNL", "Commission"])

    header = str(raw.iloc[0, 0]).strip().lower() in {"ticket", "#", "id"}
    if header:
        columns = [str(value).strip() for value in raw.iloc[0].tolist()]
        raw = raw.iloc[1:].reset_index(drop=True)
        raw.columns = columns
        aliases = {
            "ticket": "Ticket", "entry_time": "Entry_time", "exit_time": "Exit_time",
            "mae": "MAE", "mfe": "MFE", "pnl": "PNL", "commission": "Commission",
        }
        raw = raw.rename(columns={column: aliases.get(column.lower(), column) for column in raw.columns})
    else:
        if raw.shape[1] < 6:
            raise ValueError(f"{path} has {raw.shape[1]} columns; expected at least 6.")
        # The headerless sweep export has a seventh, undocumented field.  It
        # is not a commission (it is far too large and tracks trade risk), so
        # preserve it as Extra and use an explicit commission CLI setting.
        names = ["Ticket", "Entry_time", "Exit_time", "MAE", "MFE", "PNL", "Extra"]
        raw = raw.iloc[:, :len(names)]
        raw.columns = names[:raw.shape[1]]

    required = ["Entry_time", "Exit_time", "MAE", "MFE", "PNL"]
    missing = [column for column in required if column not in raw.columns]
    if missing:
        raise ValueError(f"{path} is missing required columns: {', '.join(missing)}")
    if "Ticket" not in raw:
        raw["Ticket"] = np.arange(1, len(raw) + 1, dtype=int)
    if "Commission" not in raw:
        raw["Commission"] = 0.0

    for column in ("MAE", "MFE", "PNL", "Commission"):
        raw[column] = pd.to_numeric(
            raw[column].astype(str).str.replace(",", ".", regex=False).str.strip(), errors="coerce"
        ).fillna(0.0)
    for column in ("Entry_time", "Exit_time"):
        raw[column] = pd.to_datetime(raw[column], errors="coerce")
    raw = raw.dropna(subset=["Entry_time", "Exit_time"]).copy()
    return raw[["Ticket", "Entry_time", "Exit_time", "MAE", "MFE", "PNL", "Commission"]]


def load_trades(args: argparse.Namespace) -> tuple[pd.DataFrame, list[Path]]:
    files = [args.input_csv] if args.input_csv else sweep_files(args.sweep_root, args.rr, args.windows)
    trades = pd.concat([read_trade_file(path).assign(Source=str(path)) for path in files], ignore_index=True)
    before = len(trades)
    trades = trades.drop_duplicates(subset=["Ticket", "Entry_time", "Exit_time", "MAE", "MFE", "PNL", "Commission"])
    duplicates = before - len(trades)
    if duplicates:
        print(f"Removed {duplicates:,} identical duplicated trade rows.")
    if args.start_date:
        trades = trades[trades["Entry_time"] >= pd.Timestamp(args.start_date)]
    if args.end_date:
        trades = trades[trades["Entry_time"] < pd.Timestamp(args.end_date) + pd.Timedelta(days=1)]
    if trades.empty:
        raise ValueError("No trades remain after loading/filtering.")
    trades = trades.sort_values(["Entry_time", "Exit_time", "Ticket"]).reset_index(drop=True)
    trades["Commission"] = trades["Commission"] + args.commission_per_trade
    trades["Net_PNL"] = trades["PNL"] - trades["Commission"]
    trades["Worst_PNL"] = trades["MAE"] - trades["Commission"]
    return trades, files


def floor_for(account: Account, args: argparse.Namespace) -> float:
    return args.frozen_dd_floor if account.frozen else account.peak - args.dd_limit


def update_account(account: Account, trade: pd.Series, args: argparse.Namespace) -> None:
    """Apply a trade against the live-equity trailing threshold.

    Legacy Performance Accounts trail the *peak unrealized* balance.  A sweep
    provides MAE and MFE but not their order, so the default uses MFE first and
    then MAE: the adverse, conservative sequence for a trailing threshold.
    """
    if not account.alive or trade.Entry_time < account.started_at:
        return
    account.trades += 1
    account.gross_pnl += float(trade.PNL)
    account.commissions += float(trade.Commission)

    def move_peak_to_floating_high() -> None:
        account.peak = max(account.peak, account.balance + float(trade.MFE))
        if not account.frozen and account.peak >= args.freeze_at_profit:
            account.frozen = True
            account.reached_freeze = True

    # The source has extrema but not their intra-trade order.  Running MFE
    # first is conservative because it can lift the trailing liquidation floor
    # before the trade reaches its MAE.
    if args.intratrade_path == "mfe-first":
        move_peak_to_floating_high()

    floor = floor_for(account, args)
    if account.balance + float(trade.Worst_PNL) <= floor:
        account.balance = floor
        account.drawdown = account.balance - account.peak
        account.alive = False
        account.failed_at = trade.Entry_time
        account.failure_reason = "MAE breached drawdown floor"
        return

    account.balance += float(trade.Net_PNL)
    if args.intratrade_path == "mae-first":
        move_peak_to_floating_high()
    account.peak = max(account.peak, account.balance)
    if not account.frozen and account.peak >= args.freeze_at_profit:
        account.frozen = True
        account.reached_freeze = True
    account.drawdown = account.balance - account.peak
    if account.balance <= floor_for(account, args):
        account.alive = False
        account.failed_at = trade.Exit_time
        account.failure_reason = "close breached drawdown floor"


def should_start(
    accounts: list[Account], date: pd.Timestamp, last_start: pd.Timestamp,
    waiting_for_dd_recovery: bool, args: argparse.Namespace,
) -> tuple[bool, str, bool]:
    """Return (start, reason, waiting_for_dd_recovery)."""
    latest = accounts[-1]
    live = [account for account in accounts if account.alive]
    min_drawdown = min((account.drawdown for account in live), default=0.0)
    days_since_start = (date - last_start).days

    if waiting_for_dd_recovery:
        if min_drawdown < -args.dd_recovery:
            return False, "", True
        waiting_for_dd_recovery = False

    triggers: list[str] = []
    if args.start_policy in {"time", "any"} and days_since_start >= args.interval_days:
        triggers.append("time")
    if args.start_policy in {"profit", "any"} and latest.alive and latest.balance >= args.profit_trigger:
        triggers.append("profit")
    if args.start_policy in {"dd", "any"} and min_drawdown <= -args.dd_trigger:
        triggers.append("dd")
    if not triggers or days_since_start < args.min_days_between_starts:
        return False, "", waiting_for_dd_recovery
    reason = "+".join(triggers)
    return True, reason, waiting_for_dd_recovery or "dd" in triggers


def simulate(trades: pd.DataFrame, args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame]:
    first_start = trades.Entry_time.iloc[0] - pd.Timedelta(microseconds=1)
    accounts = [Account(1, first_start, "initial")]
    last_start = first_start.normalize()
    waiting_for_dd_recovery = False
    daily_rows: list[dict[str, object]] = []

    # A new account is always created after a trading day's exits, so it cannot
    # inherit a trade that was already open before its purchase/start.
    for date, day_trades in trades.groupby(trades["Exit_time"].dt.normalize(), sort=True):
        for _, trade in day_trades.iterrows():
            for account in accounts:
                update_account(account, trade, args)

        for account in accounts:
            if account.started_at <= date + pd.Timedelta(days=1):
                account.history.append((date, account.balance))

        if len(accounts) < args.max_accounts:
            start, reason, waiting_for_dd_recovery = should_start(
                accounts, date, last_start, waiting_for_dd_recovery, args
            )
            if start:
                start_time = date + pd.Timedelta(days=1)
                accounts.append(Account(len(accounts) + 1, start_time, reason))
                last_start = date

        portfolio_pnl = sum(account.balance for account in accounts)
        daily_rows.append({
            "Date": date,
            "Portfolio_PNL": portfolio_pnl,
            "Alive": sum(account.alive for account in accounts),
            "Started": len(accounts),
            "Frozen": sum(account.reached_freeze for account in accounts),
        })

    account_rows = []
    for account in accounts:
        account_rows.append({
            "account": account.number,
            "start_date": account.started_at.date(),
            "start_reason": account.start_reason,
            "status": "alive" if account.alive else "blown",
            "failure_date": account.failed_at,
            "failure_reason": account.failure_reason,
            "final_net_pnl": account.balance,
            "gross_pnl": account.gross_pnl,
            "commissions": account.commissions,
            "peak_net_pnl": account.peak,
            "drawdown_at_end": account.drawdown,
            "dd_floor_at_end": floor_for(account, args),
            "trailing_frozen": account.reached_freeze,
            "trades_taken": account.trades,
        })
    return pd.DataFrame(daily_rows), pd.DataFrame(account_rows)


def warn_for_overlaps(trades: pd.DataFrame) -> None:
    ordered = trades.sort_values("Entry_time")
    overlaps = (ordered["Entry_time"].iloc[1:].to_numpy() < ordered["Exit_time"].iloc[:-1].to_numpy()).sum()
    if overlaps:
        print(
            f"WARNING: {overlaps:,} overlapping trade entries detected. MAE is exact for individual trades, "
            "but cannot represent the combined floating DD of simultaneous positions."
        )


def report(daily: pd.DataFrame, accounts: pd.DataFrame, args: argparse.Namespace, files: Iterable[Path]) -> None:
    final_pnl = float(accounts["final_net_pnl"].sum())
    fees = len(accounts) * args.account_fee
    running_peak = daily["Portfolio_PNL"].cummax()
    portfolio_dd = daily["Portfolio_PNL"] - running_peak
    frozen = int(accounts["trailing_frozen"].sum())
    blown = int((accounts["status"] == "blown").sum())
    print("\n" + "=" * 72)
    print("PROP ACCOUNT STAGGERING RESULTS")
    print("=" * 72)
    print(f"Input files:                    {len(list(files))}")
    print(f"Accounts started / alive:       {len(accounts)} / {len(accounts) - blown}")
    print(f"Accounts blown:                 {blown} ({blown / len(accounts):.1%})")
    print(f"Reached frozen DD floor:        {frozen} ({frozen / len(accounts):.1%})")
    print(f"Final combined trading P&L:     ${final_pnl:,.2f}")
    print(f"Account fees:                   ${fees:,.2f}")
    print(f"P&L after account fees:         ${final_pnl - fees:,.2f}")
    print(f"Portfolio max drawdown:         ${portfolio_dd.min():,.2f}")
    print(f"Total commissions charged:      ${accounts['commissions'].sum():,.2f}")
    print("NOTE: This is trading P&L after commissions, not a payout-cashflow model.")


def save_results(daily: pd.DataFrame, accounts: pd.DataFrame, args: argparse.Namespace) -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    stem = (
        f"rr{args.rr:.2f}_{args.start_policy}_{args.max_accounts}accounts_"
        f"comm{args.commission_per_trade:.2f}_{args.intratrade_path}"
    )
    daily.to_csv(RESULTS_DIR / f"{stem}_daily.csv", index=False)
    accounts.to_csv(RESULTS_DIR / f"{stem}_accounts.csv", index=False)
    if args.no_plot:
        return
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 1, figsize=(14, 9), sharex=True)
    axes[0].plot(daily["Date"], daily["Portfolio_PNL"], color="navy", linewidth=2, label="Combined account P&L")
    axes[0].axhline(0, color="black", linewidth=0.8)
    axes[0].set_ylabel("Net P&L ($)")
    axes[0].set_title("Staggered Prop Accounts — Net of Trade Commissions")
    axes[0].grid(alpha=0.3)
    axes[0].legend()
    axes[1].step(daily["Date"], daily["Alive"], where="post", label="Alive", color="green")
    axes[1].step(daily["Date"], daily["Started"], where="post", label="Started", color="gray")
    axes[1].step(daily["Date"], daily["Frozen"], where="post", label="Reached freeze", color="darkorange")
    axes[1].set_ylabel("Accounts")
    axes[1].set_xlabel("Date")
    axes[1].grid(alpha=0.3)
    axes[1].legend()
    fig.tight_layout()
    fig.savefig(RESULTS_DIR / f"{stem}.png", dpi=160)
    plt.close(fig)


def main() -> None:
    args = parse_args()
    trades, files = load_trades(args)
    print(f"Loaded {len(trades):,} trades from {trades.Entry_time.min():%Y-%m-%d} to {trades.Exit_time.max():%Y-%m-%d}.")
    print(f"Raw P&L: ${trades.PNL.sum():,.2f}; commissions: ${trades.Commission.sum():,.2f}; "
          f"net used: ${trades.Net_PNL.sum():,.2f}")
    warn_for_overlaps(trades)
    daily, accounts = simulate(trades, args)
    report(daily, accounts, args, files)
    save_results(daily, accounts, args)
    print(f"Results written to: {RESULTS_DIR}")


if __name__ == "__main__":
    main()
