// Custom M1 HLC3 session VWAP for the MT5 RTL experiment. No Order Flow license.
// Version 2026.10.29. Install this file once in NinjaTrader 8/bin/Custom/Indicators.
// The hosting strategy must also call AddDataSeries(BarsPeriodType.Minute, 1).
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using System.Xml.Serialization;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;

#region RTL VWAP calculation core
// Kept independent of NinjaTrader types so the tests execute this exact code.
namespace NinjaTrader.NinjaScript
{
    internal static class RtlVwapMath
    {
        internal static bool Finite(double value)
        {
            return !double.IsNaN(value) && !double.IsInfinity(value);
        }

        internal static DateTime SessionDate(DateTime sourceTime, TimeZoneInfo sourceZone,
            TimeZoneInfo sessionZone, int shiftMinutes)
        {
            DateTime clock = TimeZoneInfo.ConvertTime(DateTime.SpecifyKind(sourceTime,
                DateTimeKind.Unspecified), sourceZone, sessionZone);
            return clock.AddMinutes(shiftMinutes).Date;
        }

        internal static DateTime Anchor(DateTime signalOpen, TimeZoneInfo sourceZone,
            TimeZoneInfo sessionZone, int shiftMinutes, int anchorMinute)
        {
            DateTime sessionDate = SessionDate(signalOpen, sourceZone, sessionZone, shiftMinutes);
            DateTime unshiftedAnchor = sessionDate.AddMinutes(anchorMinute - shiftMinutes);
            return TimeZoneInfo.ConvertTime(DateTime.SpecifyKind(unshiftedAnchor,
                DateTimeKind.Unspecified), sessionZone, sourceZone);
        }

        internal static bool HasAnchorHistory(DateTime anchor, DateTime firstMinuteEnd,
            DateTime firstSessionBegin)
        {
            // An earlier loaded day proves the requested anchor is in loaded history.
            // If history starts on this date, allow its first minute only when it is
            // the real opening minute of a template session starting at/after anchor.
            // This accepts the CME 17:01 opening minute after the 16:00 clock anchor,
            // but refuses a truncated load beginning at (for example) 10:31 CT.
            return firstMinuteEnd.AddMinutes(-1) <= anchor
                || (firstSessionBegin >= anchor
                    && firstMinuteEnd <= firstSessionBegin.AddMinutes(1));
        }

        internal static int LastAtOrBefore(int count, Func<int, DateTime> time, DateTime cutoff)
        {
            int left = 0, right = count - 1, result = -1;
            while (left <= right)
            {
                int mid = left + (right - left) / 2;
                if (time(mid) <= cutoff) { result = mid; left = mid + 1; }
                else right = mid - 1;
            }
            return result;
        }

        internal static bool TryVwap(int count, Func<int, DateTime> time,
            Func<int, double> high, Func<int, double> low, Func<int, double> close,
            Func<int, double> volume, DateTime anchor, DateTime signalEnd,
            bool hasAnchorHistory, out double vwap)
        {
            vwap = double.NaN;
            if (!hasAnchorHistory || count <= 0 || signalEnd <= anchor) return false;
            int last = LastAtOrBefore(count, time, signalEnd);
            // Do not silently calculate from a stale secondary series. The final
            // minute must exist, even if its OnBarUpdate callback has not run yet.
            if (last < 0 || time(last) != signalEnd) return false;
            int first = LastAtOrBefore(count, time, anchor) + 1;
            double sumPriceVolume = 0.0, sumVolume = 0.0;
            for (int i = first; i <= last; i++)
            {
                double weight = volume(i);
                if (!Finite(weight) || weight < 0) return false;
                if (weight == 0) continue;
                double typical = (high(i) + low(i) + close(i)) / 3.0;
                if (!Finite(typical) || typical <= 0) return false;
                sumPriceVolume += typical * weight;
                sumVolume += weight;
            }
            if (sumVolume <= 0) return false;
            vwap = sumPriceVolume / sumVolume;
            return Finite(vwap) && vwap > 0;
        }

        internal static bool TryA20(int currentBar, Func<int, double> high,
            Func<int, double> low, Func<int, double> close, out double a20)
        {
            a20 = double.NaN;
            if (currentBar < 20) return false;
            double sum = 0.0;
            for (int i = currentBar; i > currentBar - 20; i--)
            {
                double h = high(i), l = low(i), previousClose = close(i - 1);
                if (!Finite(h) || !Finite(l) || !Finite(previousClose) || h < l) return false;
                sum += Math.Max(h - l, Math.Max(Math.Abs(h - previousClose),
                    Math.Abs(l - previousClose)));
            }
            a20 = sum / 20.0;
            return Finite(a20) && a20 > 0;
        }

        internal static bool IsFarBelow(double close, double vwap, double a20, double z)
        {
            return Finite(close) && Finite(vwap) && vwap > 0 && Finite(a20)
                && a20 > 0 && Finite(z) && close < vwap + z * a20;
        }
    }
}
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    public class RTLSessionVWAP : Indicator
    {
        private TimeZoneInfo sourceZone;
        private TimeZoneInfo sessionZone;
        private Series<double> a20Values;
        private bool known;
        private DateTime firstMinuteEnd = DateTime.MinValue;
        private DateTime firstSessionBegin = DateTime.MinValue;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "RTLSessionVWAP";
                Description = "MT5 RTL experiment: M1 HLC3 volume-weighted session VWAP and simple M30 A20 gate. Version 2026.10.29.";
                Calculate = Calculate.OnBarClose;
                IsOverlay = true;
                DisplayInDataBox = true;
                DrawOnPricePanel = true;
                PaintPriceMarkers = true;
                IsSuspendedWhileInactive = false;
                BarsRequiredToPlot = 0;
                FarBelowZ = -1.0;
                AnchorMinute = 0;
                SessionTimeZoneId = "Central Standard Time";
                SessionShiftMinutes = 480;
                AddPlot(Brushes.DodgerBlue, "SessionVWAP");
                AddPlot(Brushes.DarkOrange, "GateLine");
            }
            else if (State == State.Configure)
            {
                if (Calculate != Calculate.OnBarClose)
                    throw new InvalidOperationException("RTLSessionVWAP requires Calculate.OnBarClose, matching the completed-bar MT5 experiment.");
                AddDataSeries(BarsPeriodType.Minute, 1);
            }
            else if (State == State.DataLoaded)
            {
                if (BarsPeriod.BarsPeriodType != BarsPeriodType.Minute || BarsPeriod.Value != 30)
                    throw new InvalidOperationException("RTLSessionVWAP requires a standard 30-minute primary series, matching the MT5 experiment.");
                sourceZone = Core.Globals.GeneralOptions.TimeZoneInfo;
                sessionZone = TimeZoneInfo.FindSystemTimeZoneById(SessionTimeZoneId);
                a20Values = new Series<double>(this, MaximumBarsLookBack.Infinite);
            }
        }

        public DateTime SessionDate(DateTime sourceTime)
        {
            return RtlVwapMath.SessionDate(sourceTime, sourceZone, sessionZone, SessionShiftMinutes);
        }

        protected override void OnBarUpdate()
        {
            if (BarsInProgress != 0 || CurrentBar < 0) return;
            known = false;
            Values[0].Reset();
            Values[1].Reset();
            a20Values[0] = double.NaN;
            Bars minuteBars = BarsArray[1];
            if (minuteBars == null || minuteBars.Count == 0) return;

            if (firstMinuteEnd != minuteBars.GetTime(0))
            {
                firstMinuteEnd = minuteBars.GetTime(0);
                SessionIterator iterator = new SessionIterator(minuteBars);
                iterator.GetNextSession(firstMinuteEnd, true);
                firstSessionBegin = iterator.ActualSessionBegin;
            }

            DateTime signalEnd = Time[0];
            DateTime anchor = RtlVwapMath.Anchor(signalEnd.AddMinutes(-30), sourceZone,
                sessionZone, SessionShiftMinutes, AnchorMinute);
            double vwap;
            // Absolute timestamp-bounded access is intentional. With identical
            // timestamps NT8 can deliver primary OnBarUpdate before the secondary
            // callback. CurrentBars[1]/Closes[1][0] would then omit the final minute.
            // Read only bars ending <= this *completed* primary bar, never future
            // history or the next forming minute. No callback accumulator is used.
            bool hasVwap = RtlVwapMath.TryVwap(minuteBars.Count, minuteBars.GetTime,
                minuteBars.GetHigh, minuteBars.GetLow, minuteBars.GetClose,
                i => (double)minuteBars.GetVolume(i), anchor, signalEnd,
                RtlVwapMath.HasAnchorHistory(anchor, firstMinuteEnd, firstSessionBegin), out vwap);
            if (!hasVwap) return;
            Values[0][0] = vwap;
            double a20;
            if (!RtlVwapMath.TryA20(CurrentBar, Bars.GetHigh, Bars.GetLow, Bars.GetClose, out a20)) return;
            a20Values[0] = a20;
            Values[1][0] = vwap + FarBelowZ * a20;
            known = RtlVwapMath.Finite(Values[1][0]);
        }

        [NinjaScriptProperty]
        [Range(-100.0, 100.0)]
        [Display(Name = "Far below Z", Order = 0, GroupName = "VWAP",
            Description = "Gate line is VWAP + Z times simple A20. MT5 experiment uses -1.")]
        public double FarBelowZ { get; set; }

        [NinjaScriptProperty]
        [Range(0, 1439)]
        [Display(Name = "Anchor minute", Order = 1, GroupName = "VWAP",
            Description = "Minutes after midnight of the shifted session clock. 0 uses the whole session date.")]
        public int AnchorMinute { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Session time zone ID", Order = 2, GroupName = "VWAP",
            Description = "Windows time zone ID. Central Standard Time automatically follows US daylight saving time.")]
        public string SessionTimeZoneId { get; set; }

        [NinjaScriptProperty]
        [Range(-1440, 1440)]
        [Display(Name = "Session clock shift (minutes)", Order = 3, GroupName = "VWAP",
            Description = "Applied after time zone conversion. Chicago +480 reproduces the MT5 CME session date (16:00 Chicago rollover).")]
        public int SessionShiftMinutes { get; set; }

        [Browsable(false)]
        [XmlIgnore]
        public Series<double> SessionVWAP { get { return Values[0]; } }

        [Browsable(false)]
        [XmlIgnore]
        public Series<double> GateLine { get { return Values[1]; } }

        [Browsable(false)]
        [XmlIgnore]
        public Series<double> A20 { get { Update(); return a20Values; } }

        [Browsable(false)]
        [XmlIgnore]
        public bool IsKnown { get { Update(); return known; } }
    }
}

#region NinjaScript generated code. Neither change nor remove.
namespace NinjaTrader.NinjaScript.Indicators
{
    public partial class Indicator : NinjaTrader.Gui.NinjaScript.IndicatorRenderBase
    {
        private RTLSessionVWAP[] cacheRTLSessionVWAP;
        public RTLSessionVWAP RTLSessionVWAP(double farBelowZ, int anchorMinute, string sessionTimeZoneId, int sessionShiftMinutes)
        {
            return RTLSessionVWAP(Input, farBelowZ, anchorMinute, sessionTimeZoneId, sessionShiftMinutes);
        }
        public RTLSessionVWAP RTLSessionVWAP(ISeries<double> input, double farBelowZ, int anchorMinute, string sessionTimeZoneId, int sessionShiftMinutes)
        {
            if (cacheRTLSessionVWAP != null)
                for (int idx = 0; idx < cacheRTLSessionVWAP.Length; idx++)
                    if (cacheRTLSessionVWAP[idx] != null && cacheRTLSessionVWAP[idx].FarBelowZ == farBelowZ
                        && cacheRTLSessionVWAP[idx].AnchorMinute == anchorMinute
                        && cacheRTLSessionVWAP[idx].SessionTimeZoneId == sessionTimeZoneId
                        && cacheRTLSessionVWAP[idx].SessionShiftMinutes == sessionShiftMinutes
                        && cacheRTLSessionVWAP[idx].EqualsInput(input))
                        return cacheRTLSessionVWAP[idx];
            return CacheIndicator<RTLSessionVWAP>(new RTLSessionVWAP { FarBelowZ = farBelowZ,
                AnchorMinute = anchorMinute, SessionTimeZoneId = sessionTimeZoneId,
                SessionShiftMinutes = sessionShiftMinutes }, input, ref cacheRTLSessionVWAP);
        }
    }
}
namespace NinjaTrader.NinjaScript.MarketAnalyzerColumns
{
    public partial class MarketAnalyzerColumn : MarketAnalyzerColumnBase
    {
        public Indicators.RTLSessionVWAP RTLSessionVWAP(double farBelowZ, int anchorMinute, string sessionTimeZoneId, int sessionShiftMinutes)
        {
            return indicator.RTLSessionVWAP(Input, farBelowZ, anchorMinute, sessionTimeZoneId, sessionShiftMinutes);
        }
        public Indicators.RTLSessionVWAP RTLSessionVWAP(ISeries<double> input, double farBelowZ, int anchorMinute, string sessionTimeZoneId, int sessionShiftMinutes)
        {
            return indicator.RTLSessionVWAP(input, farBelowZ, anchorMinute, sessionTimeZoneId, sessionShiftMinutes);
        }
    }
}
namespace NinjaTrader.NinjaScript.Strategies
{
    public partial class Strategy : NinjaTrader.Gui.NinjaScript.StrategyRenderBase
    {
        public Indicators.RTLSessionVWAP RTLSessionVWAP(double farBelowZ, int anchorMinute, string sessionTimeZoneId, int sessionShiftMinutes)
        {
            return indicator.RTLSessionVWAP(Input, farBelowZ, anchorMinute, sessionTimeZoneId, sessionShiftMinutes);
        }
        public Indicators.RTLSessionVWAP RTLSessionVWAP(ISeries<double> input, double farBelowZ, int anchorMinute, string sessionTimeZoneId, int sessionShiftMinutes)
        {
            return indicator.RTLSessionVWAP(input, farBelowZ, anchorMinute, sessionTimeZoneId, sessionShiftMinutes);
        }
    }
}
#endregion
