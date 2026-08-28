#region Using declarations
using System;
using NinjaTrader.Cbi;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.AddOns;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
#endregion

// -----------------------------------------------------------------------------
// Routed variant of RRLongTimeWinStopLimitTPlimitGAPWindowRROffsetsSafeExits.
//
// The original strategy plus routing, lifecycle reconciliation and diagnostics.
// Fill behaviour must match what signal_router.py measured, so nothing may
// suppress an entry the original would have taken:
//
//   * RealtimeErrorHandling stays IgnoreAllErrors, as in the original.
//   * A new red candle re-prices the working entry IN PLACE (managed API), so an
//     entry order is continuously live - no cancel/resubmit gap.
//   * The only entry gates are the original's own (window, R:R, gap) plus the
//     router claim, a one-time startup preflight, and the book quorum.
//   * End-of-session flattening is NinjaTrader's built-in handling only.
//
// Core order flow is preserved: latest-red-candle stop-limit entry, zero-band
// candle-low stop-limit protection, bar-close take-profit limit. Deliberate
// divergences are documented in project_memory/DECISIONS.md and README.md,
// notably the gap-skipped-candle setup fix and emergency invalid-setup exits.
// The zero-band protective stop-limit remains a live-release blocker.
//
// Requires: bin\Custom\AddOns\PropRouter.cs
// -----------------------------------------------------------------------------

namespace NinjaTrader.NinjaScript.Strategies
{
    public class RRLongTimeWinStopLimitTPlimitGAPWindowRROffsetsSafeExitsRouted : Strategy
    {
        private Order longOrder;
        private double pendingStopPrice;
        private double entryPrice;
        private double riskPerTrade;
        private double pendingRiskReward;
        private double positionRiskReward;
        private bool takeProfitSubmitted;
        private sealed class EntrySetup
        {
            public readonly DateTime SignalTime;
            public readonly double EntryPrice;
            public readonly double StopPrice;
            public readonly double Risk;
            public readonly double RiskReward;

            public EntrySetup(DateTime signalTime, double entryPrice, double stopPrice,
                double risk, double riskReward)
            {
                SignalTime = signalTime;
                EntryPrice = entryPrice;
                StopPrice = stopPrice;
                Risk = risk;
                RiskReward = riskReward;
            }
        }

        private readonly object entrySetupsSync = new object();
        private readonly Dictionary<Order, EntrySetup> entrySetupsByOrder =
            new Dictionary<Order, EntrySetup>();
        private readonly Dictionary<string, EntrySetup> entrySetupsById =
            new Dictionary<string, EntrySetup>(StringComparer.Ordinal);
        private EntrySetup submittingEntrySetup;
        private readonly List<EntrySetup> recentEntrySetups = new List<EntrySetup>();
        private DateTime pendingWithoutOrderSince = Core.Globals.MinDate;
        private readonly object exitOrdersSync = new object();
        private readonly List<Order> stopOrders = new List<Order>();
        private readonly List<Order> takeProfitOrders = new List<Order>();
        private bool lastWindowState = false;

        // Router plumbing
        private volatile bool routerRegistered;
        private Guid routerLease = Guid.Empty;
        // Set ONLY by the one-time preflight in RegisterSeat. No in-session condition
        // sets this, so a transient order error can never disarm a running seat.
        private volatile bool startupInterlocked;
        private DateTime lastRouterHeartbeatUtc = Core.Globals.MinDate;
        private string lastRouterFailure = string.Empty;
        private DateTime lastRouterFailureUtc = Core.Globals.MinDate;
        private readonly object routerStatusSync = new object();
        private readonly object routerEquitySync = new object();
        private SeatStatus routerSeatStatus = SeatStatus.Free;
        private bool strategyPositionFlat = true;

        // Derived signal names — all unique per instance to prevent cross-instance interference
        private string EntrySignalName    => $"Long1_{InstanceId}";
        private string StopLossSignalName => $"StopLimit_{InstanceId}";
        private string TakeProfitName     => $"RR_Limit_{InstanceId}";
        private string EmergencyExitName  => $"InvalidStopExit_{InstanceId}";
        private int EntryQuantity         => UseCustomQuantity ? CustomQuantity : DefaultQuantity;

		// ===== INSTANCE ID =====
		[NinjaScriptProperty]
		[Range(1, int.MaxValue)]
		[Display(Name = "Instance ID", Order = 0, GroupName = "Risk Management",
		         Description = "Unique ID per chart instance — prevents cross-instance order interference when running multiple copies")]
		public int InstanceId { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Use Custom Quantity", Order = 2, GroupName = "Risk Management")]
        public bool UseCustomQuantity { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Custom Quantity", Order = 3, GroupName = "Risk Management")]
        public int CustomQuantity { get; set; }

		// ===== OFFSETS =====
        [NinjaScriptProperty]
        [Range(0, int.MaxValue)]
        [Display(Name = "Exit Offset (ticks)", Order = 5, GroupName = "Risk Management")]
        public int ExitOffset { get; set; }

        // ===== SIGNAL ROUTING =====
        [NinjaScriptProperty]
        [Display(Name = "Routing Mode", Order = 0, GroupName = "Signal Routing",
                 Description = "NONE of these is a dry run — all three submit real orders. " +
                               "Routed = only signals the router sends here (an unseeded seat trades nothing). " +
                               "UnroutedLogOnly = trades EVERY enabled window, router only logs. " +
                               "Unrouted = trades EVERY enabled window, no router. " +
                               "To trade without real orders, use simulation accounts; independent seats need distinct accounts.")]
        public PropRouterMode RoutingMode { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Book ID", Order = 1, GroupName = "Signal Routing",
                 Description = "Seats only compete against seats in the same book. Keep Sim and Live on separate book IDs.")]
        public string BookId { get; set; }

        [NinjaScriptProperty]
        [Range(1, 20)]
        [Display(Name = "R — copies per signal", Order = 2, GroupName = "Signal Routing",
                 Description = "Requested market exposure. Must be the SAME on every instance in the book. Capacity needs K >= 5R.")]
        public int GlobalCopies { get; set; }

        [NinjaScriptProperty]
        [Range(1, 100)]
        [Display(Name = "Expected seats in book", Order = 3, GroupName = "Signal Routing",
                 Description = "Fail-closed quorum. Routing stays disarmed until exactly this many matching, seeded, connected seats are fresh.")]
        public int ExpectedSeats { get; set; }

        [NinjaScriptProperty]
        [Range(0.01, double.MaxValue)]
        [Display(Name = "Seat starting balance", Order = 4, GroupName = "Signal Routing",
                 Description = "This account's starting balance. The drawdown floor is measured against it.")]
        public double SeatStartBalance { get; set; }

        [NinjaScriptProperty]
        [Range(0.01, double.MaxValue)]
        [Display(Name = "Seat trailing drawdown", Order = 5, GroupName = "Signal Routing",
                 Description = "This account's max trailing drawdown in dollars — 1500 on a 25K seat, 2500 on a 50K seat.")]
        public double SeatDrawdown { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name = "Frozen floor offset", Order = 6, GroupName = "Signal Routing",
                 Description = "Profit above the starting balance at which the trailing floor stops rising. Apex: 100.")]
        public double SeatFrozenOffset { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Track peak on unrealized", Order = 7, GroupName = "Signal Routing",
                 Description = "True = NetLiquidation, for an intraday trailing drawdown. False = CashValue, for an end-of-day rule.")]
        public bool UseUnrealizedEquity { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Require headroom covers stop", Order = 8, GroupName = "Signal Routing",
                 Description = "Optional safety: skip any seat whose headroom is smaller than this trade's initial stop risk. Off matches the study exactly.")]
        public bool RequireHeadroomCoversStop { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Acknowledged orphan order IDs", Order = 9, GroupName = "Signal Routing",
                 Description = "Normally empty. To clear a startup block caused by an order stuck in state " +
                               "Unknown, confirm at the broker that it is not live, then paste the id the " +
                               "interlock message prints. Separate several with ';'. This names ONE dead " +
                               "order, so a future orphan still blocks correctly - leave it set rather than " +
                               "clearing it after the seat starts.")]
        public string AcknowledgeOrphanOrderIds { get; set; }

        // ===== RISK/REWARD BY TIME WINDOW =====
        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="00:00-01:00", Order=0, GroupName="Window Risk/Reward")]
        public double RR00 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="01:00-02:00", Order=1, GroupName="Window Risk/Reward")]
        public double RR01 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="02:00-03:00", Order=2, GroupName="Window Risk/Reward")]
        public double RR02 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="03:00-04:00", Order=3, GroupName="Window Risk/Reward")]
        public double RR03 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="04:00-05:00", Order=4, GroupName="Window Risk/Reward")]
        public double RR04 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="05:00-06:00", Order=5, GroupName="Window Risk/Reward")]
        public double RR05 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="06:00-07:00", Order=6, GroupName="Window Risk/Reward")]
        public double RR06 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="07:00-08:00", Order=7, GroupName="Window Risk/Reward")]
        public double RR07 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="08:00-09:00", Order=8, GroupName="Window Risk/Reward")]
        public double RR08 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="09:00-10:00", Order=9, GroupName="Window Risk/Reward")]
        public double RR09 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="10:00-11:00", Order=10, GroupName="Window Risk/Reward")]
        public double RR10 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="11:00-12:00", Order=11, GroupName="Window Risk/Reward")]
        public double RR11 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="12:00-13:00", Order=12, GroupName="Window Risk/Reward")]
        public double RR12 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="13:00-14:00", Order=13, GroupName="Window Risk/Reward")]
        public double RR13 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="14:00-15:00", Order=14, GroupName="Window Risk/Reward")]
        public double RR14 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="15:00-16:00", Order=15, GroupName="Window Risk/Reward")]
        public double RR15 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="16:00-17:00", Order=16, GroupName="Window Risk/Reward")]
        public double RR16 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="17:00-18:00", Order=17, GroupName="Window Risk/Reward")]
        public double RR17 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="18:00-19:00", Order=18, GroupName="Window Risk/Reward")]
        public double RR18 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="19:00-20:00", Order=19, GroupName="Window Risk/Reward")]
        public double RR19 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="20:00-21:00", Order=20, GroupName="Window Risk/Reward")]
        public double RR20 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="21:00-22:00", Order=21, GroupName="Window Risk/Reward")]
        public double RR21 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="22:00-23:00", Order=22, GroupName="Window Risk/Reward")]
        public double RR22 { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name="23:00-00:00", Order=23, GroupName="Window Risk/Reward")]
        public double RR23 { get; set; }

        // ===== TIME WINDOW INPUTS =====
		[NinjaScriptProperty]
		[Display(Name = "Use Trade Window", Order = 0, GroupName = "Trade Windows")]
		[Browsable(false)]
		public bool UseTradeWindow { get; set; }

        [NinjaScriptProperty]
        [Display(Name="00:00–01:00", Order=1, GroupName="Trade Windows")]
        [Browsable(false)]
        public bool W00 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="01:00–02:00", Order=2, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W01 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="02:00–03:00", Order=3, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W02 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="03:00–04:00", Order=4, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W03 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="04:00–05:00", Order=5, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W04 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="05:00–06:00", Order=6, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W05 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="06:00–07:00", Order=7, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W06 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="07:00–08:00", Order=8, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W07 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="08:00–09:00", Order=9, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W08 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="09:00–10:00", Order=10, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W09 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="10:00–11:00", Order=11, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W10 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="11:00–12:00", Order=12, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W11 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="12:00–13:00", Order=13, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W12 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="13:00–14:00", Order=14, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W13 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="14:00–15:00", Order=15, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W14 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="15:00–16:00", Order=16, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W15 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="16:00–17:00", Order=17, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W16 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="17:00–18:00", Order=18, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W17 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="18:00–19:00", Order=19, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W18 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="19:00–20:00", Order=20, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W19 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="20:00–21:00", Order=21, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W20 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="21:00–22:00", Order=22, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W21 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="22:00–23:00", Order=23, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W22 { get; set; }

		[NinjaScriptProperty]
		[Display(Name="23:00–00:00", Order=24, GroupName="Trade Windows")]
		[Browsable(false)]
		public bool W23 { get; set; }

        private bool[] tradeWindows;
        private double[] windowRiskRewards;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "RRLongTimeWinStopLimitTPlimitGAPWindowRROffsetsSafeExitsRouted";
                Calculate = Calculate.OnBarClose;
                EntriesPerDirection = 1;
                EntryHandling = EntryHandling.UniqueEntries;
                BarsRequiredToTrade = 5;
                IsExitOnSessionCloseStrategy = true;
                ExitOnSessionCloseSeconds = 120;
                StartBehavior = StartBehavior.ImmediatelySubmit;
                IsUnmanaged = false;
                // Fidelity setting from the original strategy. There is no complete custom
                // rejection state machine: errors are logged, but NinjaTrader does not
                // automatically cancel/flatten/stop this strategy. StopCancelClose would change
                // outcomes by flattening at market on a transient rejection.
                RealtimeErrorHandling = RealtimeErrorHandling.IgnoreAllErrors;
                InstanceId = 1;
                UseCustomQuantity = false;
                CustomQuantity = 1;
                ExitOffset = 0;
                UseTradeWindow = true;

                // Default is the fail-closed option: with no seeded peak in peaks_<book>.csv,
                // Routed selects nothing and the strategy trades nothing. The unrouted modes
                // submit on every enabled window and are the dangerous default, not the safe one.
                RoutingMode = PropRouterMode.Routed;
                BookId = "PLAYBACK_ONLY";
                GlobalCopies = 1;
                ExpectedSeats = 6;
                SeatStartBalance = 50000;
                SeatDrawdown = 2500;
                SeatFrozenOffset = 100;
                UseUnrealizedEquity = true;
                RequireHeadroomCoversStop = false;
                AcknowledgeOrphanOrderIds = string.Empty;

                RR00 = RR01 = RR02 = RR03 = RR04 = RR05 = 0.0;
                RR06 = RR07 = RR08 = RR09 = RR10 = RR11 = 0.0;
                RR12 = RR13 = RR14 = RR15 = RR16 = RR17 = 0.0;
                RR18 = RR19 = RR20 = RR21 = RR22 = RR23 = 0.0;
            }
            else if (State == State.DataLoaded)
            {
                W00 = RR00 > 0; W01 = RR01 > 0; W02 = RR02 > 0; W03 = RR03 > 0;
                W04 = RR04 > 0; W05 = RR05 > 0; W06 = RR06 > 0; W07 = RR07 > 0;
                W08 = RR08 > 0; W09 = RR09 > 0; W10 = RR10 > 0; W11 = RR11 > 0;
                W12 = RR12 > 0; W13 = RR13 > 0; W14 = RR14 > 0; W15 = RR15 > 0;
                W16 = RR16 > 0; W17 = RR17 > 0; W18 = RR18 > 0; W19 = RR19 > 0;
                W20 = RR20 > 0; W21 = RR21 > 0; W22 = RR22 > 0; W23 = RR23 > 0;
                UseTradeWindow = true;

                tradeWindows = new bool[]
                {
                    W00, W01, W02, W03, W04, W05,
                    W06, W07, W08, W09, W10, W11,
                    W12, W13, W14, W15, W16, W17,
                    W18, W19, W20, W21, W22, W23
                };
                windowRiskRewards = new double[]
                {
                    RR00, RR01, RR02, RR03, RR04, RR05, RR06, RR07,
                    RR08, RR09, RR10, RR11, RR12, RR13, RR14, RR15,
                    RR16, RR17, RR18, RR19, RR20, RR21, RR22, RR23
                };
            }
            else if (State == State.Realtime)
            {
                longOrder = null;
                ClearEntrySetupBindings();
                pendingStopPrice = 0;
                entryPrice = 0;
                riskPerTrade = 0;
                pendingRiskReward = 0;
                positionRiskReward = 0;
                ResetExitTracking();
                routerRegistered = false;
                routerLease = Guid.Empty;
                startupInterlocked = false;
                lastRouterHeartbeatUtc = Core.Globals.MinDate;
                lastRouterFailure = string.Empty;
                lastRouterFailureUtc = Core.Globals.MinDate;
                lock (routerStatusSync)
                {
                    routerSeatStatus = SeatStatus.Free;
                    strategyPositionFlat = true;
                }
                Print($"=== Strategy entering REALTIME mode (Instance {InstanceId}, signal={EntrySignalName}) ===");

                RegisterSeat();
            }
            else if (State == State.Terminated)
            {
                ReleaseSeat();
            }
        }

        // =====================================================================
        // Router plumbing
        // =====================================================================

        /// <summary>
        /// Both unrouted modes let EVERY enabled window trade on this account. Neither is a dry run.
        /// If all six charts share the same all-window config, an unrouted mode means six copies of
        /// every signal - R=6 on a book sized for R=1.
        ///
        /// Note that a per-chart window split is NOT a workaround: the 24 window R:R values are part
        /// of the book manifest fingerprint, so charts with differing windows cannot register into
        /// the same book and no preview is produced. There is no configuration in which an unrouted
        /// mode yields a meaningful routing preview on the live book. Use Playback with simulation
        /// accounts instead.
        /// </summary>
        private void WarnIfUnrouted()
        {
            if (RoutingMode == PropRouterMode.Routed)
                return;

            int enabled = 0;
            if (windowRiskRewards != null)
                foreach (double rr in windowRiskRewards)
                    if (rr > 0) enabled++;

            Print($"[{EntrySignalName}] ⚠️ Routing Mode = {RoutingMode}: this is NOT a dry run. Real orders " +
                  $"will be submitted on account {(Account == null ? "?" : Account.Name)} for all " +
                  $"{enabled} of its enabled windows, unrouted.");
            Print($"[{EntrySignalName}] ⚠️ If every chart in book '{BookId}' has the same windows enabled, " +
                  $"the book is running {enabled}-window copies on all seats, not R={GlobalCopies}. " +
                  $"Giving each chart different windows does NOT help: the window R:R values are part " +
                  $"of the book manifest, so mismatched charts cannot register and produce no preview.");
        }

        private void RegisterSeat()
        {
            WarnIfUnrouted();

            string preflightReason;
            if (!ValidateStartup(out preflightReason))
            {
                startupInterlocked = true;
                Print($"[{EntrySignalName}] ⛔ STARTUP INTERLOCK — {preflightReason}");
                Print($"[{EntrySignalName}] ⛔ No new entries are permitted in ANY routing mode on this chart.");
                if (RoutingMode == PropRouterMode.Routed)
                    Print($"[{EntrySignalName}] ⛔ This seat will NOT register, so book '{BookId}' cannot reach " +
                          $"its quorum of {ExpectedSeats} seats. Every other seat will register and seed " +
                          "normally but NO seat in the book will trade until this one is resolved. " +
                          "Re-seeding the peak file does not help - this is an order-state problem.");
                return;
            }

            if (RoutingMode == PropRouterMode.Unrouted)
            {
                Print($"[{EntrySignalName}] Routing Mode = Unrouted — no seat registered, no router involvement");
                return;
            }

            string reason;
            routerRegistered = PropRouter.Register(BookId, InstanceId, Account.Name,
                SeatStartBalance, SeatDrawdown, SeatFrozenOffset,
                ExpectedSeats, GlobalCopies, BuildRouterConfigKey(),
                out routerLease, out reason);

            if (!routerRegistered)
            {
                routerLease = Guid.Empty;
                Print($"[{EntrySignalName}] ⛔ SEAT REGISTRATION REFUSED — {reason}.");
                Print(RoutingMode == PropRouterMode.Routed
                    ? $"[{EntrySignalName}] ⛔ Routed mode fails closed: this instance will not trade."
                    : $"[{EntrySignalName}] ⚠️ UnroutedLogOnly still submits entries, but router previews are unavailable.");
                return;
            }

            PublishEquity();
            PublishStatus();

            Print($"[{EntrySignalName}] 🔗 seat registered — book={BookId} account={Account.Name} " +
                   $"start={SeatStartBalance:F0} dd={SeatDrawdown:F0} freeze=+{SeatFrozenOffset:F0} " +
                   $"mode={RoutingMode} seats={ExpectedSeats} R={GlobalCopies}");

            string seedReason;
            if (PropRouter.IsSeeded(BookId, InstanceId, routerLease, out seedReason))
            {
                Print($"[{EntrySignalName}]    {seedReason}");
            }
            else
            {
                Print($"[{EntrySignalName}] ⛔ PEAK NOT SEEDED — {seedReason}");
                Print($"[{EntrySignalName}] ⛔ This seat will publish state but will NEVER be selected. " +
                      $"Add or correct its row in peaks_{BookId}.csv (the account part before '!' must match " +
                      $"'{Account.Name}'), " +
                      $"then fully restart NinjaTrader so the static book reloads the file.");
            }
        }

        private void ReleaseSeat()
        {
            if (routerRegistered)
            {
                PublishStatus();

                // Account-level Auto Close can flatten externally and disable this strategy
                // before its final strategy-position callback arrives.  At termination the
                // account position plus the account order collection are the authoritative
                // facts: if both are clear, publish Free once more so a stale strategy-side
                // position cannot strand the static reservation in this NT process.
                bool accountFlat = IsAccountFlatNow();
                bool liveStrategyOrder = HasLiveOrderOnAccount(accountFlat);
                bool submissionInFlight;
                lock (entrySetupsSync)
                    submissionInFlight = submittingEntrySetup != null;

                if (accountFlat && !liveStrategyOrder && !submissionInFlight)
                    SetRouterStatus(SeatStatus.Free);

                bool holdsExposure;
                lock (routerStatusSync)
                    holdsExposure = routerSeatStatus != SeatStatus.Free;

                string reason;
                if (PropRouter.Unregister(BookId, InstanceId, routerLease, out reason))
                {
                    Print($"[{EntrySignalName}] router lease released — {reason}");
                }
                else if (holdsExposure)
                {
                    Print($"[{EntrySignalName}] ⚠️ seat retained on shutdown — {reason}.");
                    Print($"[{EntrySignalName}] ⚠️ At termination the account/order snapshots were not " +
                          $"yet proven clear (accountFlat={accountFlat}, liveStrategyOrder={liveStrategyOrder}, " +
                          $"submissionInFlight={submissionInFlight}). " +
                          "NinjaTrader account Auto Close may still be in flight. Verify the broker account is " +
                          "flat with no working strategy orders, then fully restart NinjaTrader before enabling " +
                          "the book again.");
                }
                else
                {
                    ReportRouterFailure("unregister", reason);
                }
                routerRegistered = false;
                routerLease = Guid.Empty;
            }
        }

        protected override void OnAccountItemUpdate(Account account, AccountItem accountItem, double value)
        {
            if (!routerRegistered || account == null || Account == null
                || !string.Equals(account.Name, Account.Name, StringComparison.OrdinalIgnoreCase))
                return;

            AccountItem watched = UseUnrealizedEquity ? AccountItem.NetLiquidation : AccountItem.CashValue;
            if (accountItem != watched)
                return;

            // Preserve every callback high monotonically, but do not use a possibly delayed
            // callback as current equity. Re-read current account state under a separate
            // serialization lock for the headroom snapshot.
            string reason;
            if (!PropRouter.ObservePeak(BookId, InstanceId, routerLease, value, out reason))
                ReportRouterFailure("observe peak", reason);
            PublishEquity();
        }

        protected override void OnMarketData(MarketDataEventArgs marketDataUpdate)
        {
            if (State != State.Realtime || !routerRegistered)
                return;

            DateTime now = DateTime.UtcNow;
            if ((now - lastRouterHeartbeatUtc).TotalSeconds < 5.0)
                return;

            lastRouterHeartbeatUtc = now;
            PublishEquity();
            PublishStatus();
        }

        private bool ValidateStartup(out string reason)
        {
            if (Account == null)
            {
                reason = "no account is assigned";
                return false;
            }
            if (string.IsNullOrWhiteSpace(BookId))
            {
                reason = "Book ID is empty";
                return false;
            }
            if (!Enum.IsDefined(typeof(PropRouterMode), RoutingMode))
            {
                reason = $"Routing Mode value {(int)RoutingMode} is invalid";
                return false;
            }
            if (GlobalCopies <= 0 || ExpectedSeats <= 0 || ExpectedSeats < GlobalCopies)
            {
                reason = $"Expected seats ({ExpectedSeats}) is smaller than R ({GlobalCopies})";
                return false;
            }
            if (InstanceId < 1 || InstanceId > ExpectedSeats)
            {
                reason = $"Instance ID {InstanceId} is outside the required 1..{ExpectedSeats} range";
                return false;
            }
            if (SeatStartBalance <= 0 || double.IsNaN(SeatStartBalance) || double.IsInfinity(SeatStartBalance)
                || SeatDrawdown <= 0 || double.IsNaN(SeatDrawdown) || double.IsInfinity(SeatDrawdown)
                || SeatFrozenOffset < 0 || double.IsNaN(SeatFrozenOffset) || double.IsInfinity(SeatFrozenOffset)
                || ExitOffset < 0)
            {
                reason = "seat start balance/drawdown/frozen offset or exit offset is invalid";
                return false;
            }
            if (windowRiskRewards == null || windowRiskRewards.Length != 24)
            {
                reason = "the 24-value window R:R configuration is unavailable";
                return false;
            }
            for (int i = 0; i < windowRiskRewards.Length; i++)
            {
                double rr = windowRiskRewards[i];
                if (rr < 0 || double.IsNaN(rr) || double.IsInfinity(rr))
                {
                    reason = $"window R:R at hour {i:00} is invalid";
                    return false;
                }
            }
            if (Instrument == null || Instrument.MasterInstrument == null
                || BarsPeriod == null || Bars == null || Bars.TradingHours == null
                || TickSize <= 0 || double.IsNaN(TickSize) || double.IsInfinity(TickSize)
                || Instrument.MasterInstrument.PointValue <= 0
                || double.IsNaN(Instrument.MasterInstrument.PointValue)
                || double.IsInfinity(Instrument.MasterInstrument.PointValue))
            {
                reason = "instrument, bars, Trading Hours, tick size or point value is unavailable/invalid";
                return false;
            }
            if (EntryQuantity != 1)
            {
                reason = $"entry quantity is {EntryQuantity}; this version is interlocked to one contract " +
                         "until multi-contract partial-fill handling is implemented and tested";
                return false;
            }
            if (PositionAccount != null && PositionAccount.MarketPosition != MarketPosition.Flat)
            {
                reason = $"account position is {PositionAccount.MarketPosition}; this strategy cannot adopt " +
                         "or safely reconstruct an existing position";
                return false;
            }

            try
            {
                List<string> usedAcks = new List<string>();

                lock (Account.Orders)
                {
                    foreach (Order order in Account.Orders)
                    {
                        if (order == null || order.Instrument == null || Instrument == null)
                            continue;
                        if (!string.Equals(order.Instrument.FullName, Instrument.FullName,
                                StringComparison.OrdinalIgnoreCase))
                            continue;
                        if (!IsOurSignalName(order.Name) || !IsActiveOrder(order))
                            continue;

                        bool ambiguous = IsAmbiguousOrderState(order.OrderState);
                        bool flat = PositionAccount == null
                            || PositionAccount.MarketPosition == MarketPosition.Flat;

                        if (ambiguous && flat && IsOrphanAcknowledged(order))
                        {
                            usedAcks.Add(OrphanKey(order));
                            Print($"[{EntrySignalName}] ACKNOWLEDGED ORPHAN - ignoring '{order.Name}' " +
                                  $"id={OrphanKey(order)} state={order.OrderState} qty={order.Quantity} " +
                                  $"filled={order.Filled} from {order.Time}. You have asserted this specific " +
                                  "order is not live at the broker.");
                            continue;
                        }

                        // Persist the id BEFORE returning. The Output window is volatile, and
                        // without the id the only remaining option would be deleting
                        // NinjaTrader's database - which destroys all order and trade history.
                        string recordPath = PropRouter.RecordBlockedOrder(
                            BookId, InstanceId, Account.Name, order.Name, OrphanKey(order),
                            order.OrderState.ToString(), order.Quantity, order.Filled, order.Time);

                        reason = $"existing non-terminal order '{order.Name}' ({order.OrderState}) " +
                                 $"id={OrphanKey(order)} qty {order.Quantity} filled {order.Filled} " +
                                 $"from {order.Time}; restart/adoption is not implemented";

                        if (!string.IsNullOrEmpty(recordPath))
                            reason += $". This id is saved in {recordPath}, so it survives the " +
                                      "Output window";
                        else
                            reason += ". WARNING: the order id could not be saved to the PropRouter audit " +
                                      "file. Copy the id from this message before closing the Output window";

                        if (ambiguous)
                            reason += ". State Unknown usually means NinjaTrader could not reconcile a record " +
                                      "left by a crashed session; it cannot be cancelled because the order no " +
                                      "longer exists, and it does NOT clear on restart. If the broker confirms " +
                                      $"nothing is live, paste id={OrphanKey(order)} into 'Acknowledged orphan " +
                                      "order IDs' on THIS chart and LEAVE IT THERE - it names this one dead " +
                                      "order, so a future orphan still blocks";
                        else if (!flat)
                            reason += ". The account also holds a position, so this is real exposure";

                        return false;
                    }
                }

                ReportStaleAcknowledgements(usedAcks);
            }
            catch (Exception ex)
            {
                reason = $"could not verify existing account orders: {ex.Message}";
                return false;
            }

            reason = "ok";
            return true;
        }

        /// <summary>Authoritative broker check: does this account hold any live order of ours?</summary>
        private bool HasLiveOrderOnAccount()
        {
            return HasLiveOrderOnAccount(IsFlatEverywhere());
        }

        /// <summary>
        /// The acknowledged-orphan exception is deliberately identical to startup: only an
        /// exact-id match in ambiguous Unknown state, and only when the supplied authoritative
        /// position view is flat. Every other active-looking order keeps the seat reserved.
        /// </summary>
        private bool HasLiveOrderOnAccount(bool flatForOrphanAcknowledgement)
        {
            try
            {
                if (Account == null || Instrument == null)
                    return true;    // cannot prove absence - assume yes and stay reserved

                lock (Account.Orders)
                {
                    foreach (Order order in Account.Orders)
                    {
                        if (order == null || order.Instrument == null)
                            continue;
                        if (!string.Equals(order.Instrument.FullName, Instrument.FullName,
                                StringComparison.OrdinalIgnoreCase))
                            continue;
                        if (!IsOurSignalName(order.Name) || !IsActiveOrder(order))
                            continue;

                        if (flatForOrphanAcknowledgement
                            && IsAmbiguousOrderState(order.OrderState)
                            && IsOrphanAcknowledged(order))
                            continue;

                        return true;
                    }
                }
                return false;
            }
            catch { return true; }
        }

        /// <summary>
        /// A Pending reservation that no broker order backs would otherwise hold a slot
        /// forever: the router subtracts every Pending seat from R, so at R=1 one stuck
        /// seat silently halts the WHOLE book. A null or throwing submission leaves exactly
        /// that state. Release it only once the broker proves nothing exists - flat account,
        /// no live order of ours, no submission in flight - and only after a grace period.
        /// </summary>
        private void TryReconcileStuckPending()
        {
            if (!routerRegistered)
                return;

            bool inFlight;
            lock (entrySetupsSync)
                inFlight = submittingEntrySetup != null;

            SeatStatus status;
            lock (routerStatusSync)
                status = routerSeatStatus;

            bool positionOpen = (PositionAccount != null
                    && PositionAccount.MarketPosition != MarketPosition.Flat)
                || (Position != null && Position.MarketPosition != MarketPosition.Flat);

            if (inFlight || status == SeatStatus.Free || positionOpen
                || IsActiveOrder(longOrder) || HasActiveExitOrders()
                || HasLiveOrderOnAccount())
            {
                pendingWithoutOrderSince = Core.Globals.MinDate;
                return;
            }

            if (pendingWithoutOrderSince == Core.Globals.MinDate)
            {
                pendingWithoutOrderSince = DateTime.UtcNow;
                Print($"[{EntrySignalName}] ⚠️ seat is {status} but the account and strategy are both " +
                      "flat with no live order of ours; will release the reservation if this persists");
                return;
            }

            if ((DateTime.UtcNow - pendingWithoutOrderSince).TotalSeconds < 30.0)
                return;

            Print($"[{EntrySignalName}] ⚠️ releasing an unbacked {status} reservation to Free. " +
                  "Broker state says flat with no live order, so the seat is available again.");
            lock (routerStatusSync)
                strategyPositionFlat = true;    // repair the cached flag the latch depends on
            SetRouterStatus(SeatStatus.Free);
            pendingWithoutOrderSince = Core.Globals.MinDate;
        }

        private bool IsOurSignalName(string name)
        {
            // Block adoption under a changed InstanceId too: an old broker-held order
            // from any instance of this strategy family must be resolved first.
            if (string.IsNullOrEmpty(name))
                return false;
            return name.StartsWith("Long1_", StringComparison.Ordinal)
                || name.StartsWith("StopLimit_", StringComparison.Ordinal)
                || name.StartsWith("RR_Limit_", StringComparison.Ordinal)
                || name.StartsWith("DailyFlatten_", StringComparison.Ordinal)
                || name.StartsWith("InvalidStopExit_", StringComparison.Ordinal);
        }

        private string BuildRouterConfigKey()
        {
            StringBuilder sb = new StringBuilder();
            sb.Append("router-v2");
            sb.Append("|instrument=").Append(Instrument == null ? "?" : Instrument.FullName);
            sb.Append("|bars=").Append(BarsPeriod == null ? "?" : BarsPeriod.ToString());
            sb.Append("|hours=").Append(Bars == null || Bars.TradingHours == null ? "?" : Bars.TradingHours.Name);
            sb.Append("|calculate=").Append(Calculate);
            sb.Append("|routing_mode=").Append(RoutingMode);
            sb.Append("|quantity=").Append(EntryQuantity);
            sb.Append("|exit_offset=").Append(ExitOffset);
            sb.Append("|frozen_offset=").Append(SeatFrozenOffset.ToString("R", CultureInfo.InvariantCulture));
            sb.Append("|unrealized_equity=").Append(UseUnrealizedEquity);
            sb.Append("|require_stop_cover=").Append(RequireHeadroomCoversStop);
            sb.Append("|rr=");
            if (windowRiskRewards != null)
            {
                for (int i = 0; i < windowRiskRewards.Length; i++)
                {
                    if (i > 0) sb.Append(';');
                    sb.Append(windowRiskRewards[i].ToString("R", CultureInfo.InvariantCulture));
                }
            }
            return sb.ToString();
        }

        private bool IsAccountConnected()
        {
            try
            {
                return Account != null
                    && Account.Connection != null
                    && Account.Connection.Status == ConnectionStatus.Connected;
            }
            catch { return false; }
        }

        private bool IsRouterConnectedAndHealthy()
        {
            return !startupInterlocked && IsAccountConnected();
        }

        private void PublishEquity()
        {
            if (!routerRegistered || Account == null)
                return;

            lock (routerEquitySync)
            {
                if (!routerRegistered || Account == null)
                    return;

                try
                {
                    double equity = UseUnrealizedEquity
                        ? Account.Get(AccountItem.NetLiquidation, Currency.UsDollar)
                        : Account.Get(AccountItem.CashValue, Currency.UsDollar);

                    string reason;
                    if (!PropRouter.PublishEquity(BookId, InstanceId, routerLease,
                            equity, IsRouterConnectedAndHealthy(), out reason))
                        ReportRouterFailure("publish equity", reason);
                }
                catch (Exception ex)
                {
                    Print($"[{EntrySignalName}] ⛔ failed to read account equity: {ex.Message}");
                }
            }
        }

        /// <summary>
        /// Reads flatness from BOTH position views live, rather than trusting the cached
        /// strategyPositionFlat flag. That flag is only ever set by OnPositionUpdate, so if
        /// that callback does not land as expected the seat latches InPosition forever and
        /// silently removes itself from allocation. Observed repeatedly after a take-profit
        /// exit, where the protective stop is mid-cancel when the fill callback arrives.
        /// </summary>
        private bool IsFlatEverywhere()
        {
            try
            {
                bool strategyFlat = Position == null
                    || Position.MarketPosition == MarketPosition.Flat;
                bool accountFlat = PositionAccount == null
                    || PositionAccount.MarketPosition == MarketPosition.Flat;
                return strategyFlat && accountFlat;
            }
            catch { return false; }     // cannot prove flat - stay reserved
        }

        /// <summary>
        /// Account-only flatness is used only during termination reconciliation.  An external
        /// account flatten can make the strategy Position stale, but it cannot leave the
        /// account PositionAccount non-flat once the execution has completed.
        /// </summary>
        private bool IsAccountFlatNow()
        {
            try
            {
                return PositionAccount == null
                    || PositionAccount.MarketPosition == MarketPosition.Flat;
            }
            catch { return false; }     // cannot prove flat - retain the reservation
        }

        private void PublishStatus()
        {
            if (!routerRegistered)
                return;

            lock (routerStatusSync)
            {
                // Account truth may conservatively upgrade a seat, including after a
                // manual/external fill. Heartbeats never infer Free and therefore cannot
                // erase a Pending/InPosition reservation with a stale snapshot.
                if (!IsFlatEverywhere())
                    routerSeatStatus = SeatStatus.InPosition;
                else if (routerSeatStatus == SeatStatus.InPosition && !HasActiveExitOrders())
                    routerSeatStatus = SeatStatus.Free;
                PublishStatusLocked();
            }
        }

        private void SetRouterStatus(SeatStatus status)
        {
            lock (routerStatusSync)
            {
                routerSeatStatus = status;
                PublishStatusLocked();
            }
        }

        private void PublishStatusLocked()
        {
            if (!routerRegistered)
                return;

            string reason;
            if (!PropRouter.PublishStatus(BookId, InstanceId, routerLease,
                    routerSeatStatus, IsRouterConnectedAndHealthy(), out reason))
                ReportRouterFailure("publish status", reason);
        }

        private void ApplyEntryOrderStatus(OrderState state)
        {
            lock (routerStatusSync)
            {
                // Never let a delayed order callback downgrade a confirmed fill.
                if (routerSeatStatus != SeatStatus.InPosition)
                {
                    if (state == OrderState.Cancelled || state == OrderState.Rejected)
                        routerSeatStatus = PositionAccount != null
                            && PositionAccount.MarketPosition != MarketPosition.Flat
                            ? SeatStatus.InPosition : SeatStatus.Free;
                    else if (state == OrderState.Filled || IsActiveOrderState(state))
                        routerSeatStatus = SeatStatus.Pending;
                }
                PublishStatusLocked();
            }
        }

        private void RefreshFlatRouterStatus()
        {
            lock (routerStatusSync)
            {
                if (!IsFlatEverywhere())
                    routerSeatStatus = SeatStatus.InPosition;
                else if (routerSeatStatus != SeatStatus.Pending)
                    routerSeatStatus = HasActiveExitOrders()
                        ? SeatStatus.InPosition : SeatStatus.Free;

                PublishStatusLocked();
            }
        }

        private void ReportRouterFailure(string operation, string reason)
        {
            string message = operation + ": " + (string.IsNullOrWhiteSpace(reason) ? "unknown failure" : reason);
            DateTime now = DateTime.UtcNow;
            if (!string.Equals(message, lastRouterFailure, StringComparison.Ordinal)
                || (now - lastRouterFailureUtc).TotalSeconds >= 30.0)
            {
                Print($"[{EntrySignalName}] ⛔ ROUTER FAILURE — {message}");
                lastRouterFailure = message;
                lastRouterFailureUtc = now;
            }
        }

        /// <summary>
        /// The routing gate. Returns true if this instance may submit the entry.
        /// Must be called only AFTER every local disqualifier (window, R:R, gap) has passed —
        /// a seat that would refuse the trade anyway must never consume an allocation slot.
        /// </summary>
        private bool MayEnter(double candidateRisk)
        {
            if (startupInterlocked)
                return false;

            if (RoutingMode == PropRouterMode.Unrouted)
                return true;

            PublishEquity();
            PublishStatus();

            double requiredHeadroom = RequireHeadroomCoversStop
                ? candidateRisk * Instrument.MasterInstrument.PointValue * EntryQuantity
                : 0.0;

            if (RoutingMode == PropRouterMode.UnroutedLogOnly)
            {
                if (!routerRegistered)
                {
                    Print($"[{Time[0]}] [{EntrySignalName}] ⚠️ log-only router preview unavailable; " +
                          "ENTRY STILL SUBMITTED because this mode is unrouted");
                    return true;
                }
                Print($"[{Time[0]}] [{EntrySignalName}] 👁 log-only, ENTRY STILL SUBMITTED — " +
                      PropRouter.Preview(BookId, InstanceId, routerLease, GlobalCopies, requiredHeadroom));
                return true;
            }

            // Routed. Fail closed: an unregistered seat never trades.
            if (!routerRegistered)
            {
                Print($"[{Time[0]}] [{EntrySignalName}] ⛔ router not registered → standing down");
                return false;
            }

            string reason;
            bool granted;
            lock (routerStatusSync)
            {
                granted = PropRouter.TryClaim(BookId, InstanceId, routerLease,
                    Time[0], GlobalCopies, requiredHeadroom, out reason);
                if (granted)
                {
                    // TryClaim atomically marks the router seat Pending. Mirror that
                    // state before releasing the local lock so no heartbeat can
                    // publish an older Free state into the reservation window.
                    routerSeatStatus = SeatStatus.Pending;
                    PublishStatusLocked();
                }
            }

            Print($"[{Time[0]}] [{EntrySignalName}] {(granted ? "✅ routed here" : "↪ not routed here")} — {reason}");
            return granted;
        }

        private bool IsTradeWindow(DateTime time)
        {
            return GetWindowRiskReward(time) > 0;
        }

        private double GetWindowRiskReward(DateTime time)
        {
            return windowRiskRewards[time.Hour];
        }

        private bool IsActiveOrder(Order order)
        {
            return order != null && IsActiveOrderState(order.OrderState);
        }

        /// <summary>
        /// NinjaTrader reports Unknown for an order it could not reconcile - typically a
        /// record left behind by a crashed session whose real order no longer exists at
        /// the broker. It is not evidence of live exposure, but it is not evidence of
        /// absence either, so it still blocks unless the operator acknowledges it.
        /// </summary>
        /// <summary>Stable identity for one broker order, used to acknowledge it specifically.</summary>
        private string OrphanKey(Order order)
        {
            if (order == null)
                return string.Empty;
            string id = order.OrderId;
            if (string.IsNullOrWhiteSpace(id))
                id = string.Format(CultureInfo.InvariantCulture, "{0}@{1:yyyyMMddHHmmss}x{2}",
                                   order.Name, order.Time, order.Quantity);
            return id.Trim();
        }

        /// <summary>
        /// Tells the operator when an acknowledged id no longer matches anything on the
        /// account. NinjaTrader drops these records once the session rolls or the broker
        /// reports a terminal state, so the setting is meant to be temporary - but nothing
        /// else would ever say so, and a list that is never pruned eventually hides which
        /// entries still matter.
        /// </summary>
        private void ReportStaleAcknowledgements(List<string> used)
        {
            if (string.IsNullOrWhiteSpace(AcknowledgeOrphanOrderIds))
                return;

            List<string> stale = new List<string>();
            foreach (string entry in AcknowledgeOrphanOrderIds.Split(';', ','))
            {
                string key = entry.Trim();
                if (key.Length == 0)
                    continue;
                bool matched = false;
                foreach (string u in used)
                {
                    if (string.Equals(u, key, StringComparison.OrdinalIgnoreCase))
                    {
                        matched = true;
                        break;
                    }
                }
                if (!matched)
                    stale.Add(key);
            }

            if (stale.Count == 0)
                return;

            Print($"[{EntrySignalName}] ✅ Acknowledged orphan id(s) no longer match any order on " +
                  $"{Account.Name}: {string.Join(", ", stale.ToArray())}. The record has cleared, so you " +
                  "can remove them from 'Acknowledged orphan order IDs'. Leaving them is harmless but " +
                  "they no longer do anything.");
        }

        private bool IsOrphanAcknowledged(Order order)
        {
            if (string.IsNullOrWhiteSpace(AcknowledgeOrphanOrderIds))
                return false;

            string key = OrphanKey(order);
            if (string.IsNullOrEmpty(key))
                return false;

            foreach (string entry in AcknowledgeOrphanOrderIds.Split(';', ','))
            {
                if (string.Equals(entry.Trim(), key, StringComparison.OrdinalIgnoreCase))
                    return true;
            }
            return false;
        }

        private bool IsAmbiguousOrderState(OrderState state)
        {
            return state == OrderState.Unknown;
        }

        private bool IsActiveOrderState(OrderState state)
        {
            return state == OrderState.Initialized ||
                   state == OrderState.TriggerPending ||
                   state == OrderState.Submitted ||
                   state == OrderState.Accepted ||
                   state == OrderState.AcceptedByRisk ||
                   state == OrderState.Working ||
                   state == OrderState.Suspended ||
                   state == OrderState.PartFilled ||
                   state == OrderState.ChangePending ||
                   state == OrderState.ChangeSubmitted ||
                   state == OrderState.CancelPending ||
                   state == OrderState.CancelSubmitted ||
                   state == OrderState.Unknown;
        }

        private void ClearEntrySetupBindings()
        {
            lock (entrySetupsSync)
            {
                entrySetupsByOrder.Clear();
                entrySetupsById.Clear();
                submittingEntrySetup = null;
            }
        }

        private void BeginEntrySetupSubmission(EntrySetup setup)
        {
            lock (entrySetupsSync)
            {
                submittingEntrySetup = setup;
                if (setup != null)
                {
                    recentEntrySetups.Add(setup);
                    while (recentEntrySetups.Count > 8)
                        recentEntrySetups.RemoveAt(0);
                }
            }
        }

        /// <summary>
        /// Picks the setup that actually belongs to this fill, identified primarily by the
        /// execution ORDER's reported price rather than by binding order or fill price.
        ///
        /// A buy stop-limit can fill at its limit or better, so the fill price itself may not
        /// equal the candle high. The order's reported limit/stop price identifies the setup
        /// while surviving the two races the binding cannot: a synchronous fill during
        /// submission (the binding has not been rewritten yet) and a re-price the broker
        /// never applied (the binding has been rewritten too early).
        ///
        /// If no candle matches the order price, the trade is NOT reconstructed from an
        /// unrelated candle - that would create a position with a stop and R:R target
        /// belonging to neither the original strategy nor the exported data. The caller
        /// falls through to the emergency market exit instead.
        /// </summary>
        private EntrySetup ResolveSetupForFill(Execution execution, string orderId, double fillPrice)
        {
            double halfTick = TickSize / 2.0;

            // Match on the ORDER's own price, not the fill price. A buy stop-limit fills
            // at its limit or BETTER, so a price-improved fill is several ticks below the
            // order price and would never match the candle that placed it. The working
            // order carries the latest re-priced value, which identifies the candle exactly.
            double orderPrice = 0.0;
            if (execution != null && execution.Order != null)
            {
                orderPrice = execution.Order.LimitPrice > 0
                    ? execution.Order.LimitPrice : execution.Order.StopPrice;
            }
            if (orderPrice <= 0)
                orderPrice = fillPrice;

            lock (entrySetupsSync)
            {
                // A submission in flight is the newest candle and may not be bound yet.
                if (submittingEntrySetup != null
                    && submittingEntrySetup.StopPrice < fillPrice
                    && Math.Abs(submittingEntrySetup.EntryPrice - orderPrice) < halfTick)
                    return submittingEntrySetup;

                for (int i = recentEntrySetups.Count - 1; i >= 0; i--)
                {
                    EntrySetup s = recentEntrySetups[i];
                    if (s.StopPrice < fillPrice && Math.Abs(s.EntryPrice - orderPrice) < halfTick)
                        return s;
                }
            }

            // No price match. Accept the bound setup only if it is self-consistent.
            EntrySetup bound = ResolveExecutionSetup(execution, orderId);
            if (bound != null && bound.StopPrice < fillPrice)
            {
                Print($"[{EntrySignalName}] ⚠️ no candle matches order price {orderPrice} " +
                      $"(fill {fillPrice}); using the bound {bound.SignalTime} setup " +
                      $"(stop {bound.StopPrice})");
                return bound;
            }

            Print($"[{EntrySignalName}] ⛔ cannot identify the candle behind order price {orderPrice} " +
                  $"(fill {fillPrice}); " +
                  "refusing to reconstruct the trade from an unrelated setup");
            return null;
        }

        private void EndEntrySetupSubmission(EntrySetup setup)
        {
            lock (entrySetupsSync)
            {
                if (ReferenceEquals(submittingEntrySetup, setup))
                    submittingEntrySetup = null;
            }
        }

        private EntrySetup BindEntryOrderSetup(Order order, EntrySetup explicitSetup)
        {
            if (order == null)
                return explicitSetup;

            lock (entrySetupsSync)
            {
                // An explicit setup ALWAYS wins. The managed API re-prices the same order
                // in place, so a later red candle replaces the entry price - and its stop
                // and R:R must replace the old ones with it. Keeping the first binding
                // (correct under the old cancel-and-resubmit design) would protect a fill
                // at the NEW price with the OLD candle's stop.
                EntrySetup setup = explicitSetup;

                if (setup == null
                    && !entrySetupsByOrder.TryGetValue(order, out setup)
                    && !string.IsNullOrWhiteSpace(order.OrderId))
                    entrySetupsById.TryGetValue(order.OrderId, out setup);

                if (setup == null)
                    setup = submittingEntrySetup;

                if (setup != null)
                {
                    entrySetupsByOrder[order] = setup;
                    if (!string.IsNullOrWhiteSpace(order.OrderId))
                        entrySetupsById[order.OrderId] = setup;
                }
                return setup;
            }
        }

        private EntrySetup ResolveExecutionSetup(Execution execution, string orderId)
        {
            lock (entrySetupsSync)
            {
                EntrySetup setup;
                if (execution != null && execution.Order != null
                    && entrySetupsByOrder.TryGetValue(execution.Order, out setup))
                    return setup;

                string id = !string.IsNullOrWhiteSpace(orderId) ? orderId
                    : execution == null ? null : execution.OrderId;
                if (!string.IsNullOrWhiteSpace(id) && entrySetupsById.TryGetValue(id, out setup))
                    return setup;

                return submittingEntrySetup;
            }
        }

        private void SubmitEntrySetup(EntrySetup setup, string context)
        {
            entryPrice = setup.EntryPrice;
            pendingStopPrice = setup.StopPrice;
            riskPerTrade = setup.Risk;
            pendingRiskReward = setup.RiskReward;
            ResetExitTracking();

            // Reserve exposure before crossing into the managed-order API. Callbacks can
            // be synchronous; null/ambiguous submission therefore remains quarantined.
            SetRouterStatus(SeatStatus.Pending);
            BeginEntrySetupSubmission(setup);
            try
            {
                Order submitted = EnterLongStopLimit(0, true, EntryQuantity,
                    entryPrice, entryPrice, EntrySignalName);
                if (submitted == null)
                {
                    PublishStatus();
                    Print($"[{setup.SignalTime}] [{EntrySignalName}] ⛔ {context} entry API returned no Order object");
                }
                else
                {
                    BindEntryOrderSetup(submitted, setup);
                    // A synchronous fill/rejection callback may already have made the order
                    // terminal and cleared longOrder. Do not resurrect a terminal reference.
                    longOrder = IsActiveOrder(submitted) ? submitted : null;
                    Print($"[{setup.SignalTime}] [{EntrySignalName}] Submitted BUY STOP-LIMIT @ {entryPrice} ({context})");
                }
            }
            catch (Exception ex)
            {
                PublishStatus();
                Print($"[{setup.SignalTime}] [{EntrySignalName}] ⛔ {context} entry submission threw " +
                      $"{ex.GetType().Name}: {ex.Message}");
            }
            finally
            {
                EndEntrySetupSubmission(setup);
            }
        }

        private void TrackOrder(List<Order> orders, Order order)
        {
            if (order == null)
                return;

            lock (exitOrdersSync)
            {
                for (int i = 0; i < orders.Count; i++)
                {
                    if (orders[i] == order || orders[i].OrderId == order.OrderId)
                    {
                        orders[i] = order;
                        return;
                    }
                }

                orders.Add(order);
            }
        }

        private void CancelActiveOrders(List<Order> orders)
        {
            Order[] snapshot;
            lock (exitOrdersSync)
                snapshot = orders.ToArray();

            foreach (Order order in snapshot)
            {
                if (IsActiveOrder(order))
                    CancelOrder(order);
            }
        }

        private bool HasActiveExitOrders()
        {
            lock (exitOrdersSync)
            {
                foreach (Order order in stopOrders)
                    if (IsActiveOrder(order))
                        return true;
                foreach (Order order in takeProfitOrders)
                    if (IsActiveOrder(order))
                        return true;
                return false;
            }
        }

        private bool HasTrackedExitState()
        {
            lock (exitOrdersSync)
                return takeProfitSubmitted || stopOrders.Count > 0 || takeProfitOrders.Count > 0;
        }

        private bool TryMarkTakeProfitSubmitted()
        {
            lock (exitOrdersSync)
            {
                if (takeProfitSubmitted)
                    return false;
                takeProfitSubmitted = true;
                return true;
            }
        }

        private void ClearTakeProfitSubmitted()
        {
            lock (exitOrdersSync)
                takeProfitSubmitted = false;
        }

        private void ResetExitTracking()
        {
            lock (exitOrdersSync)
            {
                takeProfitSubmitted = false;
                stopOrders.Clear();
                takeProfitOrders.Clear();
            }
        }

        protected override void OnBarUpdate()
        {
			if (State != State.Realtime)
			    return;

            // Keep the registry current even on bars that produce no signal.
            PublishEquity();
            TryReconcileStuckPending();
            PublishStatus();

            // End-of-session flattening is delegated entirely to NinjaTrader's built-in
            // handling (IsExitOnSessionCloseStrategy / ExitOnSessionCloseSeconds). The
            // strategy-level 23:57 cutoff has been removed.

            if (CurrentBar < BarsRequiredToTrade) return;
            if (State != State.Realtime) return;

			/// EXIT BLOCK
            // R:R target uses the value captured from the entry signal's time window
			if (Position.MarketPosition == MarketPosition.Long)
            {
                double targetPrice = Instrument.MasterInstrument.RoundToTickSize(entryPrice + (riskPerTrade * positionRiskReward));

				if (positionRiskReward > 0 && Close[0] >= targetPrice
                    && TryMarkTakeProfitSubmitted())
				{
					double exitLimitPrice = Instrument.MasterInstrument.RoundToTickSize(Close[0] + (ExitOffset * TickSize));

					Print($"[{Time[0]}] [{EntrySignalName}] {positionRiskReward}R reached: Bar Close={Close[0]}, Target={targetPrice}");
					Order targetOrder = ExitLongLimit(0, true, Position.Quantity,
                        exitLimitPrice, TakeProfitName, EntrySignalName);
                    if (targetOrder == null)
                    {
                        ClearTakeProfitSubmitted();
                        PublishStatus();
                        Print($"[{Time[0]}] [{EntrySignalName}] ⛔ take-profit API returned no Order object; will retry next bar");
                    }
                    else
                    {
                        TrackOrder(takeProfitOrders, targetOrder);
					    Print($"[{Time[0]}] [{EntrySignalName}] Limit order submitted @ {exitLimitPrice} (target={targetPrice}, offset={ExitOffset} ticks, signal={TakeProfitName})");
                    }
				}
				return;
			}

            if (Position.MarketPosition != MarketPosition.Flat)
                return;

            // 🔹 TRADE WINDOW (ENTRY ONLY)
            if (HasTrackedExitState())
                ResetExitTracking();

            bool inWindow = IsTradeWindow(Time[0]);

            if (inWindow != lastWindowState)
            {
                Print($"[{Time[0]}] [{EntrySignalName}] Trade window state changed -> {(inWindow ? "INSIDE" : "OUTSIDE")}");
                lastWindowState = inWindow;
            }

            if (!inWindow)
            {
                if (IsActiveOrder(longOrder))
                {
                    Print($"[{Time[0]}] [{EntrySignalName}] ⏱ Outside window → cancelling pending order @ {longOrder.StopPrice}");
                    CancelOrder(longOrder);
                }
                return;
            }

            // 🔹 Red candle logic
			/// ENTRY BLOCK
            if (Close[0] < Open[0])
			{
				double candidateEntryPrice = High[0];
				double candidateStopPrice = Low[0];
				double candidateRisk = candidateEntryPrice - candidateStopPrice;
				double candidateRiskReward = GetWindowRiskReward(Time[0]);
				Print($"[{Time[0]}] [{EntrySignalName}] Window R:R={candidateRiskReward}");

                if (candidateRiskReward <= 0 || candidateRisk <= 0)
                {
                    Print($"[{Time[0]}] [{EntrySignalName}] Invalid R:R or candle risk - skipping entry");
                    return;
                }

				Print($"[{Time[0]}] [{EntrySignalName}] 🔴 Red candle detected -> evaluating entry");
				Print($"[{Time[0]}] [{EntrySignalName}] Entry={candidateEntryPrice} SL={candidateStopPrice} Risk={candidateRisk}");

				double ask = GetCurrentAsk();

				if (ask >= candidateEntryPrice)
				{
					Print($"[{Time[0]}] [{EntrySignalName}] ⚠️ Gap above entry → skipping stop placement");
					return;
				}

                EntrySetup candidate = new EntrySetup(Time[0], candidateEntryPrice,
                    candidateStopPrice, candidateRisk, candidateRiskReward);

                // Original behaviour: a new red candle re-prices the working entry IN PLACE via
                // the managed API, so an entry order is continuously live. This seat is already
                // carrying the signal, so re-pricing is a continuation, not a new copy, and does
                // not consume a router slot.
                if (IsActiveOrder(longOrder))
                {
                    SubmitEntrySetup(candidate, "re-priced working entry");
                    return;
                }

                // === ROUTING GATE — every local disqualifier has now passed ===
                if (!MayEnter(candidateRisk))
                    return;

                SubmitEntrySetup(candidate, "new routed signal");
			}
        }

		/// STOP-LOSS ORDERS BLOCK
        protected override void OnExecutionUpdate(Execution execution, string executionId,
            double price, int quantity, MarketPosition marketPosition, string orderId, DateTime time)
        {
            if (execution != null && execution.Name == EntrySignalName && quantity > 0)
            {
                // The candle behind this fill is identified by price. A null result means it
                // could not be identified at all; do NOT fall back to the shared pending
                // fields, which hold the newest candle and would attach a stop and R:R target
                // belonging to a different trade.
                EntrySetup fillSetup = ResolveSetupForFill(execution, orderId, price);

                lock (routerStatusSync)
                    strategyPositionFlat = false;
                SetRouterStatus(SeatStatus.InPosition);
                ClearTakeProfitSubmitted();

                if (fillSetup == null)
                {
                    Print($"[{time}] [{EntrySignalName}] ⛔ filled at {price} with no identifiable " +
                          "setup; submitting emergency market exit");
                    ExitLong(EmergencyExitName, EntrySignalName);
                    longOrder = null;
                    return;
                }

                double fillStopPrice = fillSetup.StopPrice;
                double fillRiskReward = fillSetup.RiskReward;
                pendingStopPrice = fillStopPrice;
                pendingRiskReward = fillRiskReward;

                if (quantity > 0 && fillRiskReward > 0)
                    positionRiskReward = fillRiskReward;

                if (quantity > 0 && fillStopPrice > 0)
                {
                    entryPrice = price;

                    if (fillStopPrice > 0)
                    {
                        riskPerTrade = entryPrice - fillStopPrice;

                        if (riskPerTrade <= 0)
                        {
                            Print($"[{time}] [{EntrySignalName}] ⛔ filled at {entryPrice} with invalid stop " +
                                  $"{fillStopPrice}; submitting emergency market exit");
                            ExitLong(EmergencyExitName, EntrySignalName);
                            longOrder = null;
                            return;
                        }

                        // Study-compatible zero-band stop-limit. This is NOT guaranteed to fill
                        // through a gap; see README known limitations before live deployment.
                        double limitPrice = fillStopPrice;

                        Print($"[{time}] [{EntrySignalName}] 🚀 Entry FILLED at {entryPrice} - Submitting STOP-LIMIT immediately");
                        Print($"[{time}] [{EntrySignalName}]    Stop={fillStopPrice}, Limit={limitPrice}, Risk={riskPerTrade}");

						Order protectiveOrder = ExitLongStopLimit(0, true, quantity,
                            limitPrice, fillStopPrice, StopLossSignalName, EntrySignalName);
                        if (protectiveOrder == null)
                        {
                            PublishStatus();
                            Print($"[{time}] [{EntrySignalName}] ⛔ protective-stop API returned no Order object; " +
                                  "submitting emergency market exit");
                            ExitLong(EmergencyExitName, EntrySignalName);
                            longOrder = null;
                            return;
                        }
                        TrackOrder(stopOrders, protectiveOrder);
                    }
                }

                // Startup validation currently enforces quantity=1, so any execution completes
                // the entry. Multi-contract partial-fill handling is intentionally interlocked.
                longOrder = null;
            }
            else
            {
                PublishStatus();
            }
        }

        protected override void OnPositionUpdate(Position position, double averagePrice,
            int quantity, MarketPosition marketPosition)
        {
            lock (routerStatusSync)
            {
                strategyPositionFlat = marketPosition == MarketPosition.Flat;

                if (!strategyPositionFlat || !IsFlatEverywhere())
                {
                    routerSeatStatus = SeatStatus.InPosition;
                }
                else if (routerSeatStatus != SeatStatus.Pending)
                {
                    // Account state may lag this strategy callback for some providers.
                    // Keep InPosition until account truth also reports flat; a later
                    // heartbeat then performs the conservative downgrade to Free.
                    routerSeatStatus = HasActiveExitOrders()
                        || (PositionAccount != null
                            && PositionAccount.MarketPosition != MarketPosition.Flat)
                        ? SeatStatus.InPosition : SeatStatus.Free;
                }

                PublishStatusLocked();
            }
        }

        protected override void OnOrderUpdate(Order order, double limitPrice, double stopPrice,
            int quantity, int filled, double averageFillPrice, OrderState orderState,
            DateTime time, ErrorCode error, string nativeError)
        {
            if (order == null)
                return;

            if (error != ErrorCode.NoError || orderState == OrderState.Rejected)
            {
                // Logged only. RealtimeErrorHandling.IgnoreAllErrors matches the original
                // strategy, and no in-session error disarms this seat.
                Print($"[{time}] [{EntrySignalName}] ⛔ ORDER ERROR name={order.Name} state={orderState} " +
                      $"error={error} broker='{nativeError}'");
            }

            // Only track orders that belong to this instance
            if (order.Name == EntrySignalName)
            {
                BindEntryOrderSetup(order, null);
                longOrder = order;
                ApplyEntryOrderStatus(orderState);
            }
            else if (order.Name == StopLossSignalName)
            {
                TrackOrder(stopOrders, order);

                if (orderState == OrderState.Filled)
                    CancelActiveOrders(takeProfitOrders);
            }
            else if (order.Name == TakeProfitName)
            {
                TrackOrder(takeProfitOrders, order);

                if (orderState == OrderState.Cancelled || orderState == OrderState.Rejected)
                    ClearTakeProfitSubmitted();

                if (orderState == OrderState.Filled)
                    CancelActiveOrders(stopOrders);
            }

            if (order.Name == StopLossSignalName || order.Name == TakeProfitName)
                RefreshFlatRouterStatus();
            else if (order.Name != EntrySignalName)
                PublishStatus();
        }
    }
}
