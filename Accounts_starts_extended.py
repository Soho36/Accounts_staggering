import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import matplotlib.dates as mdates

# ========================================================================================
#  CONFIG
# ========================================================================================
pd.set_option('display.min_rows', 1000)  # Show min 1000 rows when printing
pd.set_option('display.max_rows', 2000)  # Show max 100 rows when printing
pd.set_option('display.max_columns', 10)  # Show max 50 columns when printing

CSV_PATH = "MAE/databento_premarket_picks.csv"  # path to your CSV file
START_CAPITAL = 1500

# --- Drawdown settings ---
TRAILING_DD = 1500  # account is closed if DD exceeds this value
DD_FREEZE_TRIGGER = START_CAPITAL + TRAILING_DD + 100
FROZEN_DD_FLOOR = START_CAPITAL + 100
# --- DD stabilization ---
DD_LOOKBACK = 10  # days to check for new lows
REQUIRE_DD_STABLE = False  # require DD to not make new lows in lookback period before starting new account

# --- Date range filter (set to None to disable) ---
START_DATE = None
END_DATE = None

# ==================================================================
# --- New account start triggers ---
# ==================================================================
MAX_ACCOUNTS = 20  # maximum number of accounts to start (set to high number to disable)

# --- Time-based trigger ---
USE_TIME_TRIGGER = True
TIME_TRIGGER_DAYS = 30  # start new account every N days

# --- Profit triggers ---
USE_PROFIT_TRIGGER = False
START_IF_PROFIT_THRESHOLD = 1000  # Profit trigger to start next account (set too high to disable)

# --- Drawdown triggers ---
USE_DD_TRIGGER = False
START_IF_DD_THRESHOLD = 400  # DD trigger to start next account

# ==================================================================

RECOVERY_LEVEL = 0  # require DD to recover above this value before new account can start
MIN_DAYS_BETWEEN_STARTS = 1  # minimum days between starting new accounts

SHOW_PORTFOLIO_TOTAL_EQUITY = True  # if True, show total equity of all accounts combined
SHOW_DD_PLOT = True
USE_PROP_STYLE_DD = True  # Set to True to use floating/prop-style DD, False for closed equity DD


# ======================
#  FUNCTIONS
# ======================

def calculate_max_drawdown(equity):
    """Calculate maximum drawdown of an equity series."""
    rolling_max = equity.cummax()
    dd = equity - rolling_max
    return dd.min()


def compute_prop_style_drawdown(df):
    """
    Compute prop firm style drawdown using MAE/MFE data.
    This creates a floating equity curve that includes intra-trade drawdowns.
    """
    rows = []
    equity = 0.0

    for _, r in df.sort_values("Entry_time").iterrows():
        # entry
        rows.append({
            "time": r["Entry_time"],
            "equity": equity,
            "event": "entry"
        })

        # worst excursion (MAE)
        rows.append({
            "time": r["Entry_time"],
            "equity": equity + r["MAE"],
            "event": "mae"
        })

        # best excursion (MFE)
        rows.append({
            "time": r["Entry_time"],
            "equity": equity + r["MFE"],
            "event": "mfe"
        })

        # exit
        equity += r["PNL"]
        rows.append({
            "time": r["Exit_time"],
            "equity": equity,
            "event": "exit"
        })

    equity_curve = pd.DataFrame(rows).sort_values("time")

    # Convert time to datetime if not already
    equity_curve["time"] = pd.to_datetime(equity_curve["time"])

    # PROP-FIRM TRAILING DD calculation
    equity_close = 0.0  # cumulative closed PNL
    equity_peak = 0.0  # highest floating equity ever
    worst_dd = 0.0  # worst trailing DD observed

    dd_rows = []

    for _, r in equity_curve.iterrows():
        equity_peak = max(equity_peak, r["equity"])
        dd = r["equity"] - equity_peak
        worst_dd = min(worst_dd, dd)

        dd_rows.append({
            "time": r["time"],
            "equity": r["equity"],
            "equity_peak": equity_peak,
            "trailing_dd": dd,
            "worst_dd_so_far": worst_dd,
            "event": r["event"]
        })

    dd_curve = pd.DataFrame(dd_rows)

    # Prepare daily data for simulation
    daily_data = (
        dd_curve
        .assign(Date=pd.to_datetime(dd_curve["time"]).dt.date)
        .groupby("Date")
        .agg(
            Equity=("equity", "last"),
            Equity_Peak=("equity_peak", "max"),
            Equity_Low=("equity", "min"),
            DD_Floating=("trailing_dd", "min")
        )
        .reset_index()
    )

    daily_data["Date"] = pd.to_datetime(daily_data["Date"])

    # Also calculate closed DD for comparison
    daily_data["Closed_Peak"] = daily_data["Equity"].cummax()
    daily_data["DD_Closed"] = daily_data["Equity"] - daily_data["Closed_Peak"]

    return daily_data, dd_curve


def build_equity_from_pl(pl_series, offset, start_capital):
    """Start the equity curve from <offset> days later using correct P&L values."""
    shifted_pl = pl_series.iloc[offset:]
    equity = start_capital + shifted_pl.cumsum()
    return equity


def compute_drawdown_series(equity):
    """Return drawdown series indexed by date."""
    rolling_max = equity.cummax()
    dd_series = equity - rolling_max
    return dd_series


def print_config():
    print("=== Configuration ===")
    print(f"CSV_PATH: {CSV_PATH}")
    print(f"START_CAPITAL: {START_CAPITAL}")
    print(f"TRAILING_DD: {TRAILING_DD}")
    print(f"DD_FREEZE_TRIGGER: {DD_FREEZE_TRIGGER}")
    print(f"FROZEN_DD_FLOOR: {FROZEN_DD_FLOOR}")
    print(f"USE_PROP_STYLE_DD: {USE_PROP_STYLE_DD}")

    if START_DATE is None:
        print("START_DATE: None (no start date filter)")
    else:
        print(f"START_DATE: {START_DATE}")
    if END_DATE is None:
        print("END_DATE: None (no end date filter)")
    else:
        print(f"END_DATE: {END_DATE}")

    print(f"MAX_ACCOUNTS: {MAX_ACCOUNTS}")
    if USE_DD_TRIGGER:
        print(f"START_IF_DD_THRESHOLD: {START_IF_DD_THRESHOLD}")
    else:
        print("START_IF_DD_THRESHOLD: disabled")

    if USE_PROFIT_TRIGGER:
        print(f"START_IF_PROFIT_THRESHOLD: {START_IF_PROFIT_THRESHOLD}")
    else:
        print("START_IF_PROFIT_THRESHOLD: disabled")
    print(f"RECOVERY_LEVEL: {RECOVERY_LEVEL} (must recover above this value before new account start)")
    print(f"MIN_DAYS_BETWEEN_STARTS: {MIN_DAYS_BETWEEN_STARTS}")
    print(f"SHOW_PORTFOLIO_TOTAL_EQUITY: {SHOW_PORTFOLIO_TOTAL_EQUITY}")
    print("=====================")
    if REQUIRE_DD_STABLE:
        print(f"\nREQUIRE_DD_STABLE: {REQUIRE_DD_STABLE}")
        print(f"DD_LOOKBACK: {DD_LOOKBACK} days\n")
    else:
        print(f"\nREQUIRE_DD_STABLE: {REQUIRE_DD_STABLE} (disabled)")
        print(f"DD_LOOKBACK: {DD_LOOKBACK} days (disabled)\n")


print_config()

# ======================
#  LOAD & CLEAN DATA
# ======================
try:
    df = pd.read_csv(CSV_PATH, sep="\t")
except Exception as e:
    print("Error loading CSV file:".upper(), e)
    exit(1)

# Convert columns to numeric, handling different separators
numeric_columns = ["PNL", "MAE", "MFE"]
for col in numeric_columns:
    if col in df.columns:
        df[col] = df[col].astype(str).str.replace(",", ".").str.strip()
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

# Convert time columns to datetime
df["Entry_time"] = pd.to_datetime(df["Entry_time"])
df["Exit_time"] = pd.to_datetime(df["Exit_time"])

# Filter by date range if specified
if START_DATE:
    df = df[df["Exit_time"] >= pd.to_datetime(START_DATE)]
if END_DATE:
    df = df[df["Exit_time"] <= pd.to_datetime(END_DATE)]

print("Total trades:", len(df))
print("Date range:", df["Exit_time"].min(), "→", df["Exit_time"].max())

if USE_PROP_STYLE_DD:
    print("\nUsing Prop Firm Style (floating) Drawdown Calculation...")
    # Compute prop-style drawdown curve
    daily_data, full_dd_curve = compute_prop_style_drawdown(df)

    # Add start capital to all equity values
    daily_data["Equity"] = START_CAPITAL + daily_data["Equity"]
    daily_data["Equity_Peak"] = START_CAPITAL + daily_data["Equity_Peak"]
    daily_data["Equity_Low"] = START_CAPITAL + daily_data["Equity_Low"]
    daily_data["DD_Floating"] = daily_data["Equity_Low"] - daily_data["Equity_Peak"]

    # Prepare data for simulation
    daily_df = daily_data[["Date", "Equity"]].copy()
    daily_df.rename(columns={"Equity": "PNL_Daily"}, inplace=True)

    # Calculate daily P&L from the equity curve
    daily_df["PNL_Daily"] = daily_df["PNL_Daily"].diff().fillna(daily_df["PNL_Daily"].iloc[0] - START_CAPITAL)

    pl = daily_df.set_index("Date")["PNL_Daily"]
    dd_series = daily_data.set_index("Date")["DD_Floating"]

    print("Max floating DD:", daily_data["DD_Floating"].min())
    print("Max closed DD:", daily_data["DD_Closed"].min())
    print("Final PNL:", daily_data["Equity"].iloc[-1] - START_CAPITAL)

else:
    print("\nUsing Closed Equity Drawdown Calculation...")
    # Group by date to get daily P&L (traditional method)
    df["Date"] = df["Exit_time"].dt.date
    daily_pnl = df.groupby("Date")["PNL"].sum()

    # Convert back to dataframe with Date as column
    daily_df = daily_pnl.reset_index()
    daily_df["Date"] = pd.to_datetime(daily_df["Date"])

    # Filter by date range
    if START_DATE:
        daily_df = daily_df[daily_df["Date"] >= pd.to_datetime(START_DATE)]
    if END_DATE:
        daily_df = daily_df[daily_df["Date"] <= pd.to_datetime(END_DATE)]

    daily_df = daily_df.sort_values("Date").reset_index(drop=True)

    print("Total trading days:", len(daily_df))
    print("Average trades per day:", round(len(df) / len(daily_df), 2))

    # P.L series (daily aggregated)
    pl = daily_df.set_index("Date")["PNL"]

    # Original equity curve
    equity_original = START_CAPITAL + pl.cumsum()
    dd_series = compute_drawdown_series(equity_original)

dd_rolling_min = dd_series.rolling(DD_LOOKBACK, min_periods=1).min()


def simulate_staggered_accounts(pl_series, start_capital, max_accounts, use_prop_style=False, dd_series=None):
    dates = pl_series.index
    accounts = []

    # --- START FIRST ACCOUNT RIGHT AWAY ---
    accounts.append({
        'start_idx': 0,
        'equity': start_capital,
        'rolling_max': start_capital,
        'drawdown': 0.0,
        'alive': True
    })

    last_start_day = 0
    waiting_for_recovery = False

    portfolio_equity = []
    num_alive = []
    account_equities_over_time = []

    for i_date, date in enumerate(dates):

        # ----- UPDATE EXISTING ACCOUNTS -----
        today_equities = []
        for acc in accounts:
            if acc['alive'] and i_date >= acc['start_idx']:
                acc['equity'] += pl_series.iloc[i_date]
                # Update rolling peak
                acc['rolling_max'] = max(acc['rolling_max'], acc['equity'])
                # Compute drawdown
                acc['drawdown'] = acc['equity'] - acc['rolling_max']
                # --- Trailing DD shifts to fixed floor once threshold reached ---
                if acc['rolling_max'] < DD_FREEZE_TRIGGER:
                    # Trailing mode (normal)
                    dd_floor = acc['rolling_max'] - TRAILING_DD
                else:
                    # Fixed mode (freeze)
                    dd_floor = FROZEN_DD_FLOOR

                # Check if account violates DD rule
                if acc['equity'] <= dd_floor:
                    acc['alive'] = False

                # Account cannot go below zero
                if acc['equity'] <= 0:
                    acc['alive'] = False

            today_equities.append(acc['equity'] if acc['start_idx'] <= i_date else np.nan)

        # ----- RECORDING -----
        total_equity = sum(a['equity'] for a in accounts if a['alive'])  # only sum alive accounts
        portfolio_equity.append(total_equity)

        row = [np.nan] * max_accounts
        for k, acc in enumerate(accounts):
            row[k] = acc['equity'] if acc['start_idx'] <= i_date else np.nan
        account_equities_over_time.append(row)

        num_alive.append(sum(a['alive'] for a in accounts))

        # =====================================================
        #   NEW ACCOUNT START LOGIC WITH RECOVERY REQUIREMENT
        # =====================================================

        if len(accounts) < max_accounts:

            # Build drawdown list from alive accounts
            active_dds = [
                acc['drawdown'] for acc in accounts
                if acc['alive'] and acc['start_idx'] <= i_date
            ]
            current_dd = min(active_dds) if active_dds else 0

            # Use prop-style DD for global DD if enabled
            if use_prop_style and dd_series is not None:
                global_dd_now = dd_series.iloc[i_date]
                global_dd_recent_min = dd_rolling_min.iloc[i_date - 1] if i_date > 0 else 0
            else:
                # For closed equity DD
                global_dd_now = current_dd
                global_dd_recent_min = global_dd_now

            dd_not_making_new_lows = global_dd_now >= global_dd_recent_min

            can_start = False
            started_due_to_dd = False

            # =============================
            #  RECOVERY GATE (DD only)
            # =============================
            if waiting_for_recovery and USE_DD_TRIGGER:
                if current_dd >= RECOVERY_LEVEL:
                    waiting_for_recovery = False
                else:
                    can_start = False

            # =============================
            #  TRIGGER EVALUATION
            # =============================
            if not waiting_for_recovery:

                trigger_dd = False
                trigger_profit = False
                trigger_time = False

                # --- DD TRIGGER ---
                if USE_DD_TRIGGER and START_IF_DD_THRESHOLD is not None:
                    if current_dd <= -START_IF_DD_THRESHOLD:
                        trigger_dd = True
                        started_due_to_dd = True

                # --- PROFIT TRIGGER ---
                if USE_PROFIT_TRIGGER and START_IF_PROFIT_THRESHOLD is not None:
                    alive_accounts = [acc for acc in accounts if acc['alive']]
                    if alive_accounts:
                        last_alive = alive_accounts[-1]
                        if last_alive['equity'] - start_capital >= START_IF_PROFIT_THRESHOLD:
                            trigger_profit = True
                    else:
                        trigger_profit = True

                # --- TIME TRIGGER ---
                if USE_TIME_TRIGGER:
                    if (dates[i_date] - dates[last_start_day]).days >= TIME_TRIGGER_DAYS:
                        trigger_time = True

                # --- Combined logic ---
                if trigger_dd or trigger_profit or trigger_time:
                    if REQUIRE_DD_STABLE:
                        can_start = dd_not_making_new_lows
                    else:
                        can_start = True

            # =============================
            #  ACCOUNT START
            # =============================
            if can_start and (i_date - last_start_day) >= MIN_DAYS_BETWEEN_STARTS:
                accounts.append({
                    'start_idx': i_date,
                    'start_date': dates[i_date],
                    'equity': start_capital,
                    'rolling_max': start_capital,
                    'drawdown': 0.0,
                    'alive': True
                })
                last_start_day = i_date

                # Recovery applies ONLY if started due to DD
                waiting_for_recovery = USE_DD_TRIGGER and started_due_to_dd

    # Convert to pandas
    portfolio_eq_series = pd.Series(portfolio_equity, index=dates)
    acc_eq_df = pd.DataFrame(account_equities_over_time, index=dates,
                             columns=[f"acc_{i + 1}" for i in range(max_accounts)])
    num_alive_series = pd.Series(num_alive, index=dates)
    return portfolio_eq_series, acc_eq_df, num_alive_series


portfolio_eq, acc_eq_df, num_alive = simulate_staggered_accounts(
    pl, START_CAPITAL, MAX_ACCOUNTS, USE_PROP_STYLE_DD, dd_series
)

# ============================================================
# UNIFIED EQUITY + DD PLOTS (Same Style as Other Script)
# ============================================================

if USE_PROP_STYLE_DD and 'daily_data' in locals():
    plot_df = daily_data.copy()
else:
    # Closed-only fallback
    plot_df = pd.DataFrame({
        "Date": dd_series.index,
        "Equity": equity_original.values,
        "Equity_Peak": equity_original.cummax().values,
        "Equity_Low": equity_original.values,
        "DD_Closed": dd_series.values,
        "DD_Floating": dd_series.values
    })

fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)

# ============================================
# 1) Equity Curve
# ============================================
axes[0].plot(plot_df["Date"], plot_df["Equity"], linewidth=2, label="Equity")
axes[0].plot(plot_df["Date"], plot_df["Equity_Peak"], linewidth=1, label="Equity_Peak")
axes[0].plot(plot_df["Date"], plot_df["Equity_Low"], linewidth=1, label="Equity_Low")
axes[0].set_title("Equity Curve")
axes[0].set_ylabel("PNL")
axes[0].grid(True)
axes[0].legend()

# ============================================
# 2) Closed DD
# ============================================
axes[1].plot(plot_df["Date"], plot_df["DD_Closed"], linewidth=2, label="Closed DD")
axes[1].axhline(0, linewidth=0.8)
axes[1].set_title("Closed Equity Drawdown")
axes[1].set_ylabel("PNL")
axes[1].grid(True)
axes[1].legend()

# ============================================
# 3) Floating DD
# ============================================
axes[2].plot(plot_df["Date"], plot_df["DD_Floating"], linewidth=2, label="Floating DD")
axes[2].axhline(0, linewidth=0.8)
axes[2].set_title("Floating Drawdown (Prop Style)")
axes[2].set_xlabel("Date")
axes[2].set_ylabel("PNL")
axes[2].grid(True)
axes[2].legend()

plt.tight_layout()

# ======================
# PORTFOLIO EQUITY PLOT
# ======================

if SHOW_PORTFOLIO_TOTAL_EQUITY:
    fig_portfolio, ax_portfolio = plt.subplots(figsize=(14, 6))

    ax_portfolio.plot(
        portfolio_eq.index,
        portfolio_eq.values,
        linewidth=3,
        label="Portfolio Total Equity"
    )

    ax_portfolio.set_title("Portfolio Total Equity")
    ax_portfolio.set_ylabel("Equity ($)")
    ax_portfolio.set_xlabel("Date")
    ax_portfolio.grid(True, alpha=0.3)
    ax_portfolio.legend()

    plt.setp(ax_portfolio.xaxis.get_majorticklabels(), rotation=45)
    plt.tight_layout()

# ======================
# INDIVIDUAL ACCOUNTS PLOT
# ======================

fig_accounts, ax_accounts = plt.subplots(figsize=(14, 6))

number_accounts_started = acc_eq_df.notna().any().sum()

for i, c in enumerate(acc_eq_df.columns[:number_accounts_started]):
    ax_accounts.plot(
        acc_eq_df.index,
        acc_eq_df[c],
        alpha=0.7,
        linewidth=1.5,
        label=f"Account {i + 1}"
    )

ax_accounts.set_title("Individual Accounts Equity")
ax_accounts.set_ylabel("Equity ($)")
ax_accounts.set_xlabel("Date")
ax_accounts.grid(True, alpha=0.3)
# ax_accounts.legend(loc="upper left", fontsize="small")

plt.setp(ax_accounts.xaxis.get_majorticklabels(), rotation=45)
plt.tight_layout()

# ======================
#  ADDITIONAL PLOT: ACCOUNT STATUS OVER TIME
# ======================

fig3, ax5 = plt.subplots(1, 1, figsize=(14, 6))

# Plot number of alive accounts over time
ax5.plot(num_alive.index, num_alive.values,
         color="purple", linewidth=3, label="Number of Alive Accounts")
ax5.fill_between(num_alive.index, num_alive.values, 0,
                 color="purple", alpha=0.2)
ax5.set_title("Number of Active Accounts Over Time")
ax5.set_ylabel("Number of Accounts")
ax5.set_xlabel("Date")
ax5.grid(True, alpha=0.3)
ax5.legend()

# Add horizontal lines for reference
ax5.axhline(y=number_accounts_started, color='gray', linestyle='--',
            alpha=0.5, label=f"Total Started: {number_accounts_started}")
ax5.axhline(y=num_alive.iloc[-1], color='green', linestyle='--',
            alpha=0.5, label=f"Final Alive: {num_alive.iloc[-1]}")

ax5.legend(loc='upper left')

# Rotate x-axis labels
plt.setp(ax5.xaxis.get_majorticklabels(), rotation=45)
plt.tight_layout()

# ============================================================
# CHART 1: MONTHLY P&L (Single Account Strategy)
# ============================================================

# Create a copy of the P&L data with proper date index
if USE_PROP_STYLE_DD and 'daily_data' in locals():
    pnl_data = daily_df.copy()
    pnl_data.set_index('Date', inplace=True)
else:
    pnl_data = pl.to_frame(name='PNL_Daily')

# Group by month and sum P&L
monthly_pnl = pnl_data.resample('M')['PNL_Daily'].sum()

# Create monthly P&L bar chart
fig_monthly, ax_monthly = plt.subplots(figsize=(14, 6))

# Define colors: green for positive, red for negative
colors = ['green' if x >= 0 else 'red' for x in monthly_pnl.values]

# Create bar chart
bars = ax_monthly.bar(monthly_pnl.index, monthly_pnl.values,
                      color=colors, alpha=0.7, edgecolor='black', linewidth=0.5)

# Add a horizontal line at y=0
ax_monthly.axhline(y=0, color='black', linewidth=0.8, alpha=0.7)

# Format x-axis to show months nicely
ax_monthly.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
ax_monthly.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
plt.setp(ax_monthly.xaxis.get_majorticklabels(), rotation=45)

# Add value labels on top of bars
for bar in bars:
    height = bar.get_height()
    if height >= 0:
        label_position = height + (monthly_pnl.max() * 0.01)
    else:
        label_position = height - (monthly_pnl.max() * 0.01)

    ax_monthly.text(bar.get_x() + bar.get_width() / 2., label_position,
                    f'${height:,.0f}',
                    ha='center', va='bottom' if height >= 0 else 'top',
                    fontsize=8, rotation=45)

# Set titles and labels
ax_monthly.set_title("Single Account Strategy - Monthly P&L", fontsize=14, fontweight='bold')
ax_monthly.set_ylabel("P&L ($)")
ax_monthly.set_xlabel("Date")
ax_monthly.grid(True, alpha=0.3, axis='y')

# Add summary statistics
total_pnl = monthly_pnl.sum()
positive_months = (monthly_pnl > 0).sum()
negative_months = (monthly_pnl < 0).sum()
win_rate = positive_months / len(monthly_pnl) * 100

textstr = f'Total: ${total_pnl:,.0f} | Win Rate: {win_rate:.1f}% ({positive_months}/{len(monthly_pnl)})'
ax_monthly.text(0.02, 0.98, textstr, transform=ax_monthly.transAxes,
                fontsize=10, verticalalignment='top',
                bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))

plt.tight_layout()

# ============================================================
# CHART 2: YEARLY P&L (Single Account Strategy)
# ============================================================

# Group by year and sum P&L
yearly_pnl = pnl_data.resample('Y')['PNL_Daily'].sum()

# Create yearly P&L bar chart
fig_yearly, ax_yearly = plt.subplots(figsize=(12, 6))

# Define colors: green for positive, red for negative
colors = ['green' if x >= 0 else 'red' for x in yearly_pnl.values]

# Create bar chart
bars = ax_yearly.bar(yearly_pnl.index.year, yearly_pnl.values,
                     color=colors, alpha=0.7, edgecolor='black', linewidth=0.8, width=0.6)

# Add a horizontal line at y=0
ax_yearly.axhline(y=0, color='black', linewidth=0.8, alpha=0.7)

# Add value labels on top of bars
for bar in bars:
    height = bar.get_height()
    if height >= 0:
        label_position = height + (yearly_pnl.max() * 0.02)
    else:
        label_position = height - (yearly_pnl.max() * 0.02)

    ax_yearly.text(bar.get_x() + bar.get_width() / 2., label_position,
                   f'${height:,.0f}',
                   ha='center', va='bottom' if height >= 0 else 'top',
                   fontsize=10, fontweight='bold')

# Set titles and labels
ax_yearly.set_title("Single Account Strategy - Yearly P&L", fontsize=14, fontweight='bold')
ax_yearly.set_ylabel("P&L ($)")
ax_yearly.set_xlabel("Year")
ax_yearly.grid(True, alpha=0.3, axis='y')

# Add summary statistics
total_pnl_yearly = yearly_pnl.sum()
avg_yearly = yearly_pnl.mean()
positive_years = (yearly_pnl > 0).sum()
negative_years = (yearly_pnl < 0).sum()
win_rate_yearly = positive_years / len(yearly_pnl) * 100 if len(yearly_pnl) > 0 else 0

textstr = f'Total: ${total_pnl_yearly:,.0f} | Avg: ${avg_yearly:,.0f} | Win Rate: {win_rate_yearly:.1f}% ({positive_years}/{len(yearly_pnl)})'
ax_yearly.text(0.02, 0.98, textstr, transform=ax_yearly.transAxes,
               fontsize=10, verticalalignment='top',
               bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))

plt.tight_layout()

# ============================================================
# NEW CHART 3: PORTFOLIO MONTHLY P&L
# ============================================================

# Calculate daily portfolio P&L from portfolio equity curve
portfolio_daily_pnl = portfolio_eq.diff().fillna(portfolio_eq.iloc[0] - START_CAPITAL)
portfolio_daily_pnl.name = 'Portfolio_PNL_Daily'

# Group by month and sum portfolio P&L
portfolio_monthly_pnl = portfolio_daily_pnl.resample('M').sum()

# Create portfolio monthly P&L bar chart
fig_portfolio_monthly, ax_portfolio_monthly = plt.subplots(figsize=(14, 6))

# Define colors: green for positive, red for negative
colors = ['green' if x >= 0 else 'red' for x in portfolio_monthly_pnl.values]

# Create bar chart
bars = ax_portfolio_monthly.bar(portfolio_monthly_pnl.index, portfolio_monthly_pnl.values,
                                color=colors, alpha=0.7, edgecolor='black', linewidth=0.5)

# Add a horizontal line at y=0
ax_portfolio_monthly.axhline(y=0, color='black', linewidth=0.8, alpha=0.7)

# Format x-axis to show months nicely
ax_portfolio_monthly.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
ax_portfolio_monthly.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
plt.setp(ax_portfolio_monthly.xaxis.get_majorticklabels(), rotation=45)

# Add value labels on top of bars
for bar in bars:
    height = bar.get_height()
    if height >= 0:
        label_position = height + (portfolio_monthly_pnl.max() * 0.01)
    else:
        label_position = height - (portfolio_monthly_pnl.max() * 0.01)

    ax_portfolio_monthly.text(bar.get_x() + bar.get_width() / 2., label_position,
                              f'${height:,.0f}',
                              ha='center', va='bottom' if height >= 0 else 'top',
                              fontsize=8, rotation=45)

# Set titles and labels
ax_portfolio_monthly.set_title("Portfolio (All Accounts) - Monthly P&L", fontsize=14, fontweight='bold')
ax_portfolio_monthly.set_ylabel("P&L ($)")
ax_portfolio_monthly.set_xlabel("Date")
ax_portfolio_monthly.grid(True, alpha=0.3, axis='y')

# Add summary statistics
portfolio_total_pnl = portfolio_monthly_pnl.sum()
portfolio_positive_months = (portfolio_monthly_pnl > 0).sum()
portfolio_negative_months = (portfolio_monthly_pnl < 0).sum()
portfolio_win_rate = portfolio_positive_months / len(portfolio_monthly_pnl) * 100

textstr = f'Total: ${portfolio_total_pnl:,.0f} | Win Rate: {portfolio_win_rate:.1f}% ({portfolio_positive_months}/{len(portfolio_monthly_pnl)})'
ax_portfolio_monthly.text(0.02, 0.98, textstr, transform=ax_portfolio_monthly.transAxes,
                          fontsize=10, verticalalignment='top',
                          bbox=dict(boxstyle='round', facecolor='lightblue', alpha=0.5))

plt.tight_layout()

# ============================================================
# NEW CHART 4: PORTFOLIO YEARLY P&L
# ============================================================

# Group by year and sum portfolio P&L
portfolio_yearly_pnl = portfolio_daily_pnl.resample('Y').sum()

# Create portfolio yearly P&L bar chart
fig_portfolio_yearly, ax_portfolio_yearly = plt.subplots(figsize=(12, 6))

# Define colors: green for positive, red for negative
colors = ['green' if x >= 0 else 'red' for x in portfolio_yearly_pnl.values]

# Create bar chart
bars = ax_portfolio_yearly.bar(portfolio_yearly_pnl.index.year, portfolio_yearly_pnl.values,
                               color=colors, alpha=0.7, edgecolor='black', linewidth=0.8, width=0.6)

# Add a horizontal line at y=0
ax_portfolio_yearly.axhline(y=0, color='black', linewidth=0.8, alpha=0.7)

# Add value labels on top of bars
for bar in bars:
    height = bar.get_height()
    if height >= 0:
        label_position = height + (portfolio_yearly_pnl.max() * 0.02)
    else:
        label_position = height - (portfolio_yearly_pnl.max() * 0.02)

    ax_portfolio_yearly.text(bar.get_x() + bar.get_width() / 2., label_position,
                             f'${height:,.0f}',
                             ha='center', va='bottom' if height >= 0 else 'top',
                             fontsize=10, fontweight='bold')

# Set titles and labels
ax_portfolio_yearly.set_title("Portfolio (All Accounts) - Yearly P&L", fontsize=14, fontweight='bold')
ax_portfolio_yearly.set_ylabel("P&L ($)")
ax_portfolio_yearly.set_xlabel("Year")
ax_portfolio_yearly.grid(True, alpha=0.3, axis='y')

# Add summary statistics
portfolio_total_pnl_yearly = portfolio_yearly_pnl.sum()
portfolio_avg_yearly = portfolio_yearly_pnl.mean()
portfolio_positive_years = (portfolio_yearly_pnl > 0).sum()
portfolio_negative_years = (portfolio_yearly_pnl < 0).sum()
portfolio_win_rate_yearly = portfolio_positive_years / len(portfolio_yearly_pnl) * 100 if len(
    portfolio_yearly_pnl) > 0 else 0

textstr = f'Total: ${portfolio_total_pnl_yearly:,.0f} | Avg: ${portfolio_avg_yearly:,.0f} | Win Rate: {portfolio_win_rate_yearly:.1f}% ({portfolio_positive_years}/{len(portfolio_yearly_pnl)})'
ax_portfolio_yearly.text(0.02, 0.98, textstr, transform=ax_portfolio_yearly.transAxes,
                         fontsize=10, verticalalignment='top',
                         bbox=dict(boxstyle='round', facecolor='lightblue', alpha=0.5))

plt.tight_layout()


# quick stats
number_accounts_started = acc_eq_df.notna().any().sum()
number_accounts_alive = num_alive.iloc[-1]
final_portfolio_equity = portfolio_eq.iloc[-1]
final_pnl = final_portfolio_equity - (START_CAPITAL * max(1, number_accounts_alive))

print("\n" + "=" * 60)
print("SIMULATION RESULTS")
print("=" * 60)
print(f"{'DD Calculation Style:':<30} {'Prop Firm (Floating)' if USE_PROP_STYLE_DD else 'Closed Equity'}")
print(f"{'Final Portfolio Equity:':<30} ${final_portfolio_equity:,.2f}")
print(f"{'Final P&L:':<30} ${final_pnl:,.2f}")
print(
    f"{'Total Return:':<30} {((final_portfolio_equity / (START_CAPITAL * max(1, number_accounts_started))) - 1) * 100:.1f}%")
print(f"{'Num Accounts Started:':<30} {number_accounts_started}")
print(f"{'Accounts Still Alive:':<30} {number_accounts_alive}")
print(f"{'Accounts Blown:':<30} {number_accounts_started - number_accounts_alive}")
print(f"{'Success Rate:':<30} {(number_accounts_alive / number_accounts_started * 100):.1f}%")

# Add monthly/yearly P&L summary to console output
print("\n" + "-" * 60)
print("SINGLE ACCOUNT STRATEGY PERFORMANCE")
print("-" * 60)
print(f"{'Monthly P&L Total:':<30} ${monthly_pnl.sum():,.2f}")
print(
    f"{'Monthly Win Rate:':<30} {(monthly_pnl > 0).sum() / len(monthly_pnl) * 100:.1f}% ({monthly_pnl[monthly_pnl > 0].count()}/{len(monthly_pnl)})")
print(f"{'Best Month:':<30} ${monthly_pnl.max():,.2f}")
print(f"{'Average Month:':<30} ${monthly_pnl.mean():,.2f}")
print(f"{'Worst Month:':<30} ${monthly_pnl.min():,.2f}")
print(f"{'Yearly P&L Total:':<30} ${yearly_pnl.sum():,.2f}")
print(
    f"{'Yearly Win Rate:':<30} {(yearly_pnl > 0).sum() / len(yearly_pnl) * 100:.1f}% ({yearly_pnl[yearly_pnl > 0].count()}/{len(yearly_pnl)})")

print("\n" + "-" * 60)
print("PORTFOLIO (ALL ACCOUNTS) PERFORMANCE")
print("-" * 60)
print(f"{'Portfolio Monthly P&L Total:':<30} ${portfolio_monthly_pnl.sum():,.2f}")
print(
    f"{'Portfolio Monthly Win Rate:':<30} {(portfolio_monthly_pnl > 0).sum() / len(portfolio_monthly_pnl) * 100:.1f}% ({portfolio_monthly_pnl[portfolio_monthly_pnl > 0].count()}/{len(portfolio_monthly_pnl)})")
print(f"{'Portfolio Best Month:':<30} ${portfolio_monthly_pnl.max():,.2f}")
print(f"{'Portfolio Average Month:':<30} ${portfolio_monthly_pnl.mean():,.2f}")
print(f"{'Portfolio Worst Month:':<30} ${portfolio_monthly_pnl.min():,.2f}")
print(f"{'Portfolio Yearly P&L Total:':<30} ${portfolio_yearly_pnl.sum():,.2f}")
print(
    f"{'Portfolio Yearly Win Rate:':<30} {(portfolio_yearly_pnl > 0).sum() / len(portfolio_yearly_pnl) * 100:.1f}% ({portfolio_yearly_pnl[portfolio_yearly_pnl > 0].count()}/{len(portfolio_yearly_pnl)})")

# Account performance summary - SIMPLER FIX
print("\n" + "-" * 60)
print("ACCOUNT PERFORMANCE SUMMARY")
print("-" * 60)
print(
    f"{'Account':<10} {'Date':<10} {'Status':<10} {'Start Equity':<15} {'End Equity':<15} {'P&L':<15} {'Return %':<10}")
print("-" * 60)

for i in range(number_accounts_started):
    acc_col = f"acc_{i + 1}"
    if i < len(acc_eq_df.columns):
        start_val = START_CAPITAL
        start_date = acc_eq_df[acc_col].first_valid_index()
        start_date_str = str(start_date.date()) if start_date else "N/A"
        end_val = acc_eq_df[acc_col].dropna().iloc[-1]
        pnl = end_val - start_val
        return_pct = (pnl / start_val) * 100

        # FIXED: Check if account was blown by checking equity against blowout levels
        account_data = acc_eq_df[acc_col].dropna()

        # Determine blowout thresholds dynamically
        # For each day, calculate what the trailing DD floor would be
        blown = False
        rolling_max = account_data.cummax()

        for idx, (date_val, equity_val) in enumerate(account_data.items()):
            current_max = rolling_max.iloc[idx]

            if current_max < DD_FREEZE_TRIGGER:
                # Trailing mode (normal)
                dd_floor = current_max - TRAILING_DD
            else:
                # Fixed mode (freeze)
                dd_floor = FROZEN_DD_FLOOR

            # Check if equity fell below the DD floor
            if equity_val <= dd_floor:
                blown = True
                break

        # Determine status
        if blown:
            status = "BLOWN"
        elif end_val <= 0:
            status = "BLOWN"
        elif end_val <= start_val * 0.8:  # More than 20% drawdown
            status = "DRAWDOWN"
        elif end_val >= start_val * 1.2:  # More than 20% profit
            status = "PROFIT"
        else:
            status = "NEUTRAL"

        print(
            f"{i + 1:<10} {start_date_str:<12} {status:<10} ${start_val:<14,.0f} ${end_val:<14,.0f} ${pnl:<14,.0f} {return_pct:<9.1f}%")

print("=" * 60)

try:
    plt.show()
except KeyboardInterrupt:
    print("\nScript stopped by user.")
