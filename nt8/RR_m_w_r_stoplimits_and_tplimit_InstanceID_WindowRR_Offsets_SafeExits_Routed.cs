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
#endregion

// -----------------------------------------------------------------------------
// Routed variant of RRLongTimeWinStopLimitTPlimitGAPWindowRROffsetsSafeExits.
//
// The trading logic below is UNCHANGED. The only addition is a routing gate on
// the entry: when Routing Mode is Live, an instance may only submit an entry if
// PropRouter selects it as one of the R seats carrying that signal.
//
// Everything downstream of the entry - stop-limit placement, the R:R limit exit,
// safe-exit bookkeeping and the 23:57 flatten - is untouched, so any difference
// in outcome is attributable to the allocation decision and nothing else.
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
        private readonly List<Order> stopOrders = new List<Order>();
        private readonly List<Order> takeProfitOrders = new List<Order>();
        private DateTime lastFlattenDate = Core.Globals.MinDate;
        private bool lastWindowState = false;

        // Router plumbing
        private bool routerRegistered;
        private bool accountSubscribed;

        // Derived signal names — all unique per instance to prevent cross-instance interference
        private string EntrySignalName    => $"Long1_{InstanceId}";
        private string StopLossSignalName => $"StopLimit_{InstanceId}";
        private string TakeProfitName     => $"RR_Limit_{InstanceId}";
        private string FlattenName        => $"DailyFlatten_{InstanceId}";
        private int EntryQuantity         => UseCustomQuantity ? CustomQuantity : DefaultQuantity;

		// ===== INSTANCE ID =====
		[NinjaScriptProperty]
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
                 Description = "Off = behave exactly as before. Shadow = observe and log only, never blocks. Live = the router decides which seats submit.")]
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
        [Range(0.0, double.MaxValue)]
        [Display(Name = "Seat starting balance", Order = 3, GroupName = "Signal Routing",
                 Description = "This account's starting balance. The drawdown floor is measured against it.")]
        public double SeatStartBalance { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name = "Seat trailing drawdown", Order = 4, GroupName = "Signal Routing",
                 Description = "This account's max trailing drawdown in dollars — 1500 on a 25K seat, 2500 on a 50K seat.")]
        public double SeatDrawdown { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, double.MaxValue)]
        [Display(Name = "Frozen floor offset", Order = 5, GroupName = "Signal Routing",
                 Description = "Profit above the starting balance at which the trailing floor stops rising. Apex: 100.")]
        public double SeatFrozenOffset { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Track peak on unrealized", Order = 6, GroupName = "Signal Routing",
                 Description = "True = NetLiquidation, for an intraday trailing drawdown. False = CashValue, for an end-of-day rule.")]
        public bool UseUnrealizedEquity { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Require headroom covers stop", Order = 7, GroupName = "Signal Routing",
                 Description = "Optional safety: skip any seat whose headroom is smaller than this trade's initial stop risk. Off matches the study exactly.")]
        public bool RequireHeadroomCoversStop { get; set; }

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
                ExitOnSessionCloseSeconds = 30;
                StartBehavior = StartBehavior.ImmediatelySubmit;
                IsUnmanaged = false;
                RealtimeErrorHandling = RealtimeErrorHandling.IgnoreAllErrors;
                InstanceId = 1;
                UseCustomQuantity = false;
                CustomQuantity = 1;
                ExitOffset = 0;
                UseTradeWindow = true;

                // Routing defaults deliberately start in Shadow: the first deployment
                // must not change live behaviour.
                RoutingMode = PropRouterMode.Shadow;
                BookId = "LIVE";
                GlobalCopies = 1;
                SeatStartBalance = 50000;
                SeatDrawdown = 2500;
                SeatFrozenOffset = 100;
                UseUnrealizedEquity = true;
                RequireHeadroomCoversStop = false;

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
                pendingStopPrice = 0;
                entryPrice = 0;
                riskPerTrade = 0;
                pendingRiskReward = 0;
                positionRiskReward = 0;
                takeProfitSubmitted = false;
                stopOrders.Clear();
                takeProfitOrders.Clear();
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

        private void RegisterSeat()
        {
            if (RoutingMode == PropRouterMode.Off)
            {
                Print($"[{EntrySignalName}] Routing Mode = Off — running as an unrouted single account");
                return;
            }

            if (Account == null)
            {
                Print($"[{EntrySignalName}] ⛔ no Account on this strategy — seat NOT registered");
                return;
            }

            string reason;
            routerRegistered = PropRouter.Register(BookId, InstanceId, Account.Name,
                SeatStartBalance, SeatDrawdown, SeatFrozenOffset, out reason);

            if (!routerRegistered)
            {
                Print($"[{EntrySignalName}] ⛔ SEAT REGISTRATION REFUSED — {reason}. This instance will not trade.");
                return;
            }

            if (!accountSubscribed)
            {
                Account.AccountItemUpdate += OnAccountItemUpdate;
                accountSubscribed = true;
            }

            PublishEquity();
            PublishStatus();

            Print($"[{EntrySignalName}] 🔗 seat registered — book={BookId} account={Account.Name} " +
                  $"start={SeatStartBalance:F0} dd={SeatDrawdown:F0} freeze=+{SeatFrozenOffset:F0} " +
                  $"mode={RoutingMode} R={GlobalCopies} ({reason})");
        }

        private void ReleaseSeat()
        {
            if (accountSubscribed && Account != null)
            {
                Account.AccountItemUpdate -= OnAccountItemUpdate;
                accountSubscribed = false;
            }

            if (routerRegistered)
            {
                PropRouter.Unregister(BookId, InstanceId);
                routerRegistered = false;
            }
        }

        private void OnAccountItemUpdate(object sender, AccountItemEventArgs e)
        {
            if (e == null || !routerRegistered)
                return;

            AccountItem watched = UseUnrealizedEquity ? AccountItem.NetLiquidation : AccountItem.CashValue;
            if (e.AccountItem != watched)
                return;

            PropRouter.PublishEquity(BookId, InstanceId, e.Value, IsAccountConnected());
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

        private void PublishEquity()
        {
            if (!routerRegistered || Account == null)
                return;

            try
            {
                double equity = UseUnrealizedEquity
                    ? Account.Get(AccountItem.NetLiquidation, Currency.UsDollar)
                    : Account.Get(AccountItem.CashValue, Currency.UsDollar);

                PropRouter.PublishEquity(BookId, InstanceId, equity, IsAccountConnected());
            }
            catch { /* a failed read simply leaves the seat's last value in place */ }
        }

        private void PublishStatus()
        {
            if (!routerRegistered)
                return;

            SeatStatus status;

            if (PositionAccount != null && PositionAccount.MarketPosition != MarketPosition.Flat)
                status = SeatStatus.InPosition;
            else if (IsActiveOrder(longOrder))
                status = SeatStatus.Pending;
            else
                status = SeatStatus.Free;

            PropRouter.PublishStatus(BookId, InstanceId, status, IsAccountConnected());
        }

        /// <summary>
        /// The routing gate. Returns true if this instance may submit the entry.
        /// Must be called only AFTER every local disqualifier (window, R:R, gap) has passed —
        /// a seat that would refuse the trade anyway must never consume an allocation slot.
        /// </summary>
        private bool MayEnter()
        {
            if (RoutingMode == PropRouterMode.Off)
                return true;

            // Already carrying this signal: re-pricing a working entry is a continuation,
            // not a new copy, so it does not go back through the router.
            if (IsActiveOrder(longOrder))
                return true;

            PublishEquity();
            PublishStatus();

            double requiredHeadroom = RequireHeadroomCoversStop
                ? riskPerTrade * Instrument.MasterInstrument.PointValue * EntryQuantity
                : 0.0;

            if (RoutingMode == PropRouterMode.Shadow)
            {
                Print($"[{Time[0]}] [{EntrySignalName}] 👁 shadow — " +
                      PropRouter.Preview(BookId, InstanceId, GlobalCopies, requiredHeadroom));
                return true;
            }

            // Live. Fail closed: an unregistered seat never trades.
            if (!routerRegistered)
            {
                Print($"[{Time[0]}] [{EntrySignalName}] ⛔ router not registered → standing down");
                return false;
            }

            string reason;
            bool granted = PropRouter.TryClaim(BookId, InstanceId, Time[0], GlobalCopies, requiredHeadroom, out reason);

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
            return order != null &&
                (order.OrderState == OrderState.Submitted ||
                 order.OrderState == OrderState.Accepted ||
                 order.OrderState == OrderState.Working ||
                 order.OrderState == OrderState.PartFilled ||
                 order.OrderState == OrderState.ChangeSubmitted ||
                 order.OrderState == OrderState.CancelSubmitted);
        }

        private void TrackOrder(List<Order> orders, Order order)
        {
            if (order == null)
                return;

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

        private void CancelActiveOrders(List<Order> orders)
        {
            foreach (Order order in orders.ToArray())
            {
                if (IsActiveOrder(order))
                    CancelOrder(order);
            }
        }

        private void ResetExitTracking()
        {
            takeProfitSubmitted = false;
            stopOrders.Clear();
            takeProfitOrders.Clear();
        }

        protected override void OnBarUpdate()
        {
			if (State != State.Realtime)
			    return;

            // Keep the registry current even on bars that produce no signal.
            PublishEquity();
            PublishStatus();

            // === End of session FLATTEN LOGIC ===
            if (ToTime(Time[0]) >= 235700 && ToTime(Time[0]) < 235800)
			{
				if (lastFlattenDate.Date != Time[0].Date)
				{
					lastFlattenDate = Time[0];
					Print($"[{Time[0]}] [{EntrySignalName}] ❌ End of session FLATTEN → all positions & orders cleared");

					if (Position.MarketPosition == MarketPosition.Long)
						ExitLong(FlattenName, EntrySignalName);

					if (longOrder != null &&
						(longOrder.OrderState == OrderState.Working ||
						 longOrder.OrderState == OrderState.Accepted))
						CancelOrder(longOrder);
				}
				return;
			}

            if (CurrentBar < BarsRequiredToTrade) return;
            if (State != State.Realtime) return;

			/// EXIT BLOCK
            // R:R target uses the value captured from the entry signal's time window
			if (Position.MarketPosition == MarketPosition.Long)
            {
                double targetPrice = Instrument.MasterInstrument.RoundToTickSize(entryPrice + (riskPerTrade * positionRiskReward));

				if (!takeProfitSubmitted && positionRiskReward > 0 && Close[0] >= targetPrice)
				{
					double exitLimitPrice = Instrument.MasterInstrument.RoundToTickSize(Close[0] + (ExitOffset * TickSize));

					Print($"[{Time[0]}] [{EntrySignalName}] {positionRiskReward}R reached: Bar Close={Close[0]}, Target={targetPrice}");
					ExitLongLimit(0, true, Position.Quantity, exitLimitPrice, TakeProfitName, EntrySignalName);
                    takeProfitSubmitted = true;
					Print($"[{Time[0]}] [{EntrySignalName}] Limit order submitted @ {exitLimitPrice} (target={targetPrice}, offset={ExitOffset} ticks, signal={TakeProfitName})");
				}
				return;
			}

            if (Position.MarketPosition != MarketPosition.Flat)
                return;

            // 🔹 TRADE WINDOW (ENTRY ONLY)
            if (takeProfitSubmitted || stopOrders.Count > 0 || takeProfitOrders.Count > 0)
                ResetExitTracking();

            bool inWindow = IsTradeWindow(Time[0]);

            if (inWindow != lastWindowState)
            {
                Print($"[{Time[0]}] [{EntrySignalName}] Trade window state changed -> {(inWindow ? "INSIDE" : "OUTSIDE")}");
                lastWindowState = inWindow;
            }

            if (!inWindow)
            {
                if (longOrder != null &&
                    (longOrder.OrderState == OrderState.Working || longOrder.OrderState == OrderState.Accepted))
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
				entryPrice = High[0];
				pendingStopPrice = Low[0];
				riskPerTrade = entryPrice - pendingStopPrice;
				pendingRiskReward = GetWindowRiskReward(Time[0]);
				Print($"[{Time[0]}] [{EntrySignalName}] Window R:R={pendingRiskReward}");

                if (pendingRiskReward <= 0)
                {
                    Print($"[{Time[0]}] [{EntrySignalName}] Window R:R is 0 - skipping entry");
                    return;
                }

				Print($"[{Time[0]}] [{EntrySignalName}] 🔴 Red candle detected -> evaluating entry");
				Print($"[{Time[0]}] [{EntrySignalName}] Entry={entryPrice} SL={pendingStopPrice} Risk={riskPerTrade}");

				double ask = GetCurrentAsk();

				if (ask >= entryPrice)
				{
					Print($"[{Time[0]}] [{EntrySignalName}] ⚠️ Gap above entry → skipping stop placement");
					return;
				}

                // === ROUTING GATE — every local disqualifier has now passed ===
                if (!MayEnter())
                    return;

				ResetExitTracking();
				longOrder = EnterLongStopLimit(0, true, EntryQuantity, entryPrice, entryPrice, EntrySignalName);
				Print($"[{Time[0]}] [{EntrySignalName}] Submitted BUY STOP-LIMIT @ {entryPrice}");

                PublishStatus();
			}
        }

		/// STOP-LOSS ORDERS BLOCK
        protected override void OnExecutionUpdate(Execution execution, string executionId,
            double price, int quantity, MarketPosition marketPosition, string orderId, DateTime time)
        {
            if (execution.Order != null && execution.Order.Name == EntrySignalName)
            {
                takeProfitSubmitted = false;

                if (quantity > 0 && pendingRiskReward > 0)
                    positionRiskReward = pendingRiskReward;

                if (quantity > 0 && pendingStopPrice > 0)
                {
                    entryPrice = execution.Order.AverageFillPrice;

                    if (pendingStopPrice > 0)
                    {
                        riskPerTrade = entryPrice - pendingStopPrice;
                        double limitPrice = pendingStopPrice - TickSize;	// Can be used in ExitLongStopLimit (currently 2*pendingStopPrice instead)

                        Print($"[{time}] [{EntrySignalName}] 🚀 Entry FILLED at {entryPrice} - Submitting STOP-LIMIT immediately");
                        Print($"[{time}] [{EntrySignalName}]    Stop={pendingStopPrice}, Limit={limitPrice}, Risk={riskPerTrade}");

						ExitLongStopLimit(0, true, execution.Order.Filled, pendingStopPrice, pendingStopPrice, StopLossSignalName, EntrySignalName);
                    }
                }

                // Clear local reference once our own entry order is filled
                if (execution.Order.OrderState == OrderState.Filled)
                {
                    longOrder = null;
                }
            }

            PublishStatus();
        }

        protected override void OnOrderUpdate(Order order, double limitPrice, double stopPrice,
            int quantity, int filled, double averageFillPrice, OrderState orderState,
            DateTime time, ErrorCode error, string nativeError)
        {
            // Only track orders that belong to this instance
            if (order.Name == EntrySignalName)
            {
                longOrder = order;
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
                    takeProfitSubmitted = false;

                if (orderState == OrderState.Filled)
                    CancelActiveOrders(stopOrders);
            }

            PublishStatus();
        }
    }
}
