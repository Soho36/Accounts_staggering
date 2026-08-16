#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using NinjaTrader.Cbi;
#endregion

// -----------------------------------------------------------------------------
// PropRouter - max_headroom signal routing across independent prop-account
// drawdown containers, for NinjaTrader 8.
//
// Mirrors addiotional_helpers/signal_router.py and account_farming.py:
//
//     floor    = min(peak - dd, start + frozen_floor)      // peak is UNREALIZED
//     headroom = equity - floor
//     select   = sorted(free, key=(-headroom, trades_taken, seat_id))[:need]
//
// R (copies per signal) and K (seats on the book) are separate: seats already
// holding a POSITION are invisible to the router, which is why total open
// contracts can exceed R during overlap. Seats holding an unfilled entry order
// count against the R quota, which is what keeps exposure at R copies/signal.
//
// Deployment: Documents\NinjaTrader 8\bin\Custom\AddOns\PropRouter.cs
// -----------------------------------------------------------------------------

namespace NinjaTrader.NinjaScript.AddOns
{
	public enum SeatStatus
	{
		Free,			// flat, no working entry order - competes for new signals
		Pending,		// entry order working - already carrying, counts against R
		InPosition		// filled - busy with an earlier signal, invisible to router
	}

	/// <summary>
	/// NONE of these modes is a dry run. Every mode submits real orders to the account the
	/// strategy is running on. The mode controls only whether the router may VETO an entry.
	/// To trade without real orders, run the chart on a Sim101 account.
	/// </summary>
	public enum PropRouterMode
	{
		/// <summary>Router decides which seats submit. An unseeded seat is never selected, so this fails closed.</summary>
		Routed,

		/// <summary>ORDERS STILL SUBMITTED on every enabled window. The router only writes what it would have chosen.</summary>
		UnroutedLogOnly,

		/// <summary>ORDERS STILL SUBMITTED on every enabled window. Router bypassed entirely.</summary>
		Unrouted
	}

	public class PropSeat
	{
		public int			InstanceId;
		public string		AccountName		= string.Empty;
		public double		StartBalance;
		public double		DrawdownSize;
		public double		FrozenOffset;
		public double		Equity			= double.NaN;
		public double		Peak			= double.NaN;

		/// <summary>
		/// True only when the peak came from the stored seed file or an explicit OverridePeak.
		/// A peak bootstrapped from whatever equity happened to be present at startup is NOT
		/// seeded: it would put the floor at equity - dd and make a damaged seat look pristine.
		/// Unseeded seats are never selected.
		/// </summary>
		public bool			PeakSeeded;

		public SeatStatus	Status			= SeatStatus.Free;
		public bool			Connected;
		public int			TradesTaken;
		public DateTime		EquityAsOf		= DateTime.MinValue;
		public DateTime		StatusAsOf		= DateTime.MinValue;

		public bool HasEquity
		{
			get { return !double.IsNaN(Equity) && !double.IsNaN(Peak); }
		}

		/// <summary>Trailing floor: rises with the equity peak, then locks at start + FrozenOffset.</summary>
		public double Floor
		{
			get { return Math.Min(Peak - DrawdownSize, StartBalance + FrozenOffset); }
		}

		public double Headroom
		{
			get { return HasEquity ? Equity - Floor : double.NaN; }
		}

		/// <summary>True once the trailing floor has stopped rising - the seat is payout-capable and permanently safer.</summary>
		public bool Frozen
		{
			get { return HasEquity && (Peak - DrawdownSize) >= (StartBalance + FrozenOffset); }
		}

		public DateTime AsOf
		{
			get { return EquityAsOf > StatusAsOf ? StatusAsOf : EquityAsOf; }
		}
	}

	internal class PropBook
	{
		public readonly Dictionary<int, PropSeat>		Seats			= new Dictionary<int, PropSeat>();
		public readonly Dictionary<long, List<int>>		Decisions		= new Dictionary<long, List<int>>();
		public readonly Queue<long>						DecisionOrder	= new Queue<long>();
		public DateTime									LastPersist		= DateTime.MinValue;
	}

	public static class PropRouter
	{
		private static readonly object sync = new object();
		private static readonly Dictionary<string, PropBook> books =
			new Dictionary<string, PropBook>(StringComparer.OrdinalIgnoreCase);

		/// <summary>A seat whose data is older than this is excluded from selection.</summary>
		public static double StaleSeconds			= 20.0;

		/// <summary>Decisions retained for late-arriving instances on the same bar.</summary>
		public static int MaxCachedDecisions		= 128;

		/// <summary>Peaks are rewritten to disk at most this often (and always on a new high).</summary>
		public static double PersistThrottleSeconds	= 5.0;

		// =====================================================================
		// Registration
		// =====================================================================

		/// <summary>
		/// Claims a seat id within a book. Returns false if the id is already held by a
		/// different, live account - the caller must then refuse to trade.
		/// </summary>
		public static bool Register(string book, int instanceId, string accountName,
			double startBalance, double drawdownSize, double frozenOffset, out string reason)
		{
			lock (sync)
			{
				PropBook b = GetBook(book);
				PropSeat existing;

				if (b.Seats.TryGetValue(instanceId, out existing)
					&& !string.Equals(existing.AccountName, accountName, StringComparison.OrdinalIgnoreCase)
					&& IsFresh(existing))
				{
					reason = string.Format(CultureInfo.InvariantCulture,
						"InstanceId {0} is already registered to account {1}", instanceId, existing.AccountName);
					return false;
				}

				PropSeat seat = existing ?? new PropSeat();
				seat.InstanceId		= instanceId;
				seat.AccountName	= accountName ?? string.Empty;
				seat.StartBalance	= startBalance;
				seat.DrawdownSize	= drawdownSize;
				seat.FrozenOffset	= frozenOffset;
				seat.Status			= SeatStatus.Free;
				seat.StatusAsOf		= DateTime.UtcNow;
				b.Seats[instanceId]	= seat;

				LoadPeak(book, seat);

				reason = seat.PeakSeeded
					? string.Format(CultureInfo.InvariantCulture,
						"peak seeded at {0:F2}, floor {1:F2}", seat.Peak, seat.Floor)
					: string.Format(CultureInfo.InvariantCulture,
						"NOT SEEDED - no row for '{0}' in {1}; this seat will never be selected until one exists",
						seat.AccountName, Path.GetFileName(PeakPath(book)));
				return true;
			}
		}

		public static void Unregister(string book, int instanceId)
		{
			lock (sync)
			{
				PropBook b = GetBook(book);
				if (b.Seats.Remove(instanceId))
					PersistPeaks(book, b, true);
			}
		}

		// =====================================================================
		// State publishing - called from account, order and bar events
		// =====================================================================

		public static void PublishEquity(string book, int instanceId, double equity, bool connected)
		{
			if (double.IsNaN(equity) || equity <= 0)
				return;

			lock (sync)
			{
				PropBook b = GetBook(book);
				PropSeat seat;
				if (!b.Seats.TryGetValue(instanceId, out seat))
					return;

				seat.Equity		= equity;
				seat.Connected	= connected;
				seat.EquityAsOf	= DateTime.UtcNow;

				// The peak still ratchets on an unseeded seat so the value is sane once it is
				// seeded, but PeakSeeded stays false and Allocate keeps the seat out of selection.
				bool newHigh = double.IsNaN(seat.Peak) || equity > seat.Peak;
				if (newHigh)
					seat.Peak = equity;

				if (seat.PeakSeeded
					&& (newHigh || (DateTime.UtcNow - b.LastPersist).TotalSeconds >= PersistThrottleSeconds))
					PersistPeaks(book, b, false);
			}
		}

		public static void PublishStatus(string book, int instanceId, SeatStatus status, bool connected)
		{
			lock (sync)
			{
				PropBook b = GetBook(book);
				PropSeat seat;
				if (!b.Seats.TryGetValue(instanceId, out seat))
					return;

				seat.Status		= status;
				seat.Connected	= connected;
				seat.StatusAsOf	= DateTime.UtcNow;
			}
		}

		/// <summary>
		/// Manually overrides a seat's high-water mark. Use when NT8 was offline through an
		/// intraday peak, or after the prop firm resets the account - never casually, since
		/// a peak that is too low makes a damaged seat look pristine.
		/// </summary>
		public static void OverridePeak(string book, int instanceId, double peak)
		{
			lock (sync)
			{
				PropBook b = GetBook(book);
				PropSeat seat;
				if (!b.Seats.TryGetValue(instanceId, out seat))
					return;

				seat.Peak		= peak;
				seat.PeakSeeded	= true;
				PersistPeaks(book, b, true);
			}
		}

		/// <summary>True when this seat's peak came from the seed file or an explicit override.</summary>
		public static bool IsSeeded(string book, int instanceId)
		{
			lock (sync)
			{
				PropSeat seat;
				return GetBook(book).Seats.TryGetValue(instanceId, out seat) && seat.PeakSeeded;
			}
		}

		// =====================================================================
		// Allocation
		// =====================================================================

		/// <summary>
		/// Asks whether this instance may carry a copy of the signal on this bar.
		/// The first caller for a given bar computes the allocation; every later caller
		/// reads the same cached answer, so all instances agree regardless of thread order.
		/// </summary>
		public static bool TryClaim(string book, int instanceId, DateTime barTime, int copies,
			double requiredHeadroom, out string reason)
		{
			lock (sync)
			{
				PropBook b = GetBook(book);
				long key = barTime.Ticks;

				List<int> winners;
				if (!b.Decisions.TryGetValue(key, out winners))
				{
					string detail;
					winners = Allocate(b, copies, requiredHeadroom, out detail);

					b.Decisions[key] = winners;
					b.DecisionOrder.Enqueue(key);
					while (b.DecisionOrder.Count > MaxCachedDecisions)
						b.Decisions.Remove(b.DecisionOrder.Dequeue());

					foreach (int id in winners)
					{
						PropSeat w;
						if (b.Seats.TryGetValue(id, out w))
							w.TradesTaken++;
					}

					Log(book, barTime, copies, winners, detail, b);
				}

				bool granted = winners.Contains(instanceId);
				reason = granted
					? string.Format(CultureInfo.InvariantCulture, "selected; winners=[{0}]", string.Join(",", winners))
					: string.Format(CultureInfo.InvariantCulture, "not selected; winners=[{0}]",
						winners.Count == 0 ? "none" : string.Join(",", winners));
				return granted;
			}
		}

		/// <summary>Non-mutating view of what the router would decide. Used by UnroutedLogOnly.</summary>
		public static string Preview(string book, int instanceId, int copies, double requiredHeadroom)
		{
			lock (sync)
			{
				PropBook b = GetBook(book);
				string detail;
				List<int> winners = Allocate(b, copies, requiredHeadroom, out detail);
				return string.Format(CultureInfo.InvariantCulture, "would route to [{0}] ({1}); this seat {2}",
					winners.Count == 0 ? "none" : string.Join(",", winners),
					detail,
					winners.Contains(instanceId) ? "WOULD trade" : "would stand down");
			}
		}

		public static string Describe(string book)
		{
			lock (sync)
			{
				PropBook b = GetBook(book);
				StringBuilder sb = new StringBuilder();
				foreach (PropSeat s in b.Seats.Values.OrderBy(x => x.InstanceId))
					sb.AppendFormat(CultureInfo.InvariantCulture, "{0}#{1}[{2} hr={3} {4}{5}{6}] ",
						sb.Length == 0 ? "" : "| ",
						s.InstanceId, s.AccountName,
						s.HasEquity ? s.Headroom.ToString("F0", CultureInfo.InvariantCulture) : "n/a",
						s.Status,
						s.Frozen ? " FROZEN" : "",
						s.PeakSeeded ? "" : " UNSEEDED");
				return sb.ToString();
			}
		}

		// Caller must hold the lock.
		private static List<int> Allocate(PropBook b, int copies, double requiredHeadroom, out string detail)
		{
			List<PropSeat> live = b.Seats.Values.Where(IsFresh).ToList();

			int pending = live.Count(s => s.Status == SeatStatus.Pending);
			int need    = Math.Max(0, copies - pending);

			// max_headroom, with signal_router.py's tie-breaks: (-headroom, trades, seat_id)
			List<PropSeat> eligible = live
				.Where(s => s.Status == SeatStatus.Free
						 && s.Connected
						 && s.HasEquity
						 && s.PeakSeeded			// an unverified peak is worse than no router
						 && s.Headroom > 0
						 && s.Headroom >= requiredHeadroom)
				.OrderByDescending(s => s.Headroom)
				.ThenBy(s => s.TradesTaken)
				.ThenBy(s => s.InstanceId)
				.ToList();

			List<int> winners = eligible.Take(need).Select(s => s.InstanceId).ToList();

			int unseeded = b.Seats.Values.Count(s => !s.PeakSeeded);

			detail = string.Format(CultureInfo.InvariantCulture,
				"R={0} pending={1} need={2} eligible={3}/{4} blocked={5}{6}",
				copies, pending, need, eligible.Count, b.Seats.Count,
				Math.Max(0, need - winners.Count),
				unseeded > 0 ? " UNSEEDED=" + unseeded : string.Empty);

			return winners;
		}

		private static bool IsFresh(PropSeat seat)
		{
			if (seat == null || seat.AsOf == DateTime.MinValue)
				return false;
			return (DateTime.UtcNow - seat.AsOf).TotalSeconds <= StaleSeconds;
		}

		private static PropBook GetBook(string book)
		{
			string key = string.IsNullOrEmpty(book) ? "DEFAULT" : book;
			PropBook b;
			if (!books.TryGetValue(key, out b))
			{
				b = new PropBook();
				books[key] = b;
			}
			return b;
		}

		// =====================================================================
		// Persistence - the peak MUST survive a restart
		// =====================================================================

		private static string StateDir
		{
			get { return Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "PropRouter"); }
		}

		/// <summary>
		/// NT8 shows accounts as "NAME!Provider!Connection" in some UI contexts but Account.Name
		/// is usually the bare name. Comparing on the part before the first '!' matches either
		/// form, so a hand-written seed row works whichever string was copied.
		/// </summary>
		private static string NormalizeAccount(string name)
		{
			if (string.IsNullOrEmpty(name))
				return string.Empty;
			int cut = name.IndexOf('!');
			return (cut >= 0 ? name.Substring(0, cut) : name).Trim();
		}

		private static string Sanitize(string name)
		{
			StringBuilder sb = new StringBuilder();
			foreach (char c in (name ?? "DEFAULT"))
				sb.Append(char.IsLetterOrDigit(c) || c == '-' || c == '_' ? c : '_');
			return sb.ToString();
		}

		private static string PeakPath(string book)
		{
			return Path.Combine(StateDir, "peaks_" + Sanitize(book) + ".csv");
		}

		// Caller must hold the lock.
		private static void LoadPeak(string book, PropSeat seat)
		{
			try
			{
				string path = PeakPath(book);
				if (!File.Exists(path))
					return;

				foreach (string line in File.ReadAllLines(path).Skip(1))
				{
					string[] f = line.Split(',');
					if (f.Length < 4)
						continue;
					if (!string.Equals(NormalizeAccount(f[0]), NormalizeAccount(seat.AccountName),
							StringComparison.OrdinalIgnoreCase))
						continue;

					double start, dd, peak;
					if (double.TryParse(f[1], NumberStyles.Any, CultureInfo.InvariantCulture, out start)
						&& double.TryParse(f[2], NumberStyles.Any, CultureInfo.InvariantCulture, out dd)
						&& double.TryParse(f[3], NumberStyles.Any, CultureInfo.InvariantCulture, out peak))
					{
						seat.Peak		= peak;
						seat.PeakSeeded	= true;
						// Configured start/dd win over the stored copy; the file only carries the peak.
					}
					return;
				}
			}
			catch { /* a missing or unreadable peak leaves the seat ineligible, which is the safe direction */ }
		}

		// Caller must hold the lock.
		private static void PersistPeaks(string book, PropBook b, bool force)
		{
			try
			{
				if (!force && (DateTime.UtcNow - b.LastPersist).TotalSeconds < 1.0)
					return;

				Directory.CreateDirectory(StateDir);
				StringBuilder sb = new StringBuilder();
				sb.AppendLine("account,start_balance,drawdown,peak,updated_utc");
				// Only seeded seats are written back. Persisting a bootstrapped peak would
				// launder it into a "seeded" value on the next restart.
				foreach (PropSeat s in b.Seats.Values.Where(x => x.HasEquity && x.PeakSeeded).OrderBy(x => x.AccountName))
					sb.AppendFormat(CultureInfo.InvariantCulture, "{0},{1:F2},{2:F2},{3:F2},{4:yyyy-MM-ddTHH:mm:ssZ}\n",
						s.AccountName, s.StartBalance, s.DrawdownSize, s.Peak, DateTime.UtcNow);

				File.WriteAllText(PeakPath(book), sb.ToString());
				b.LastPersist = DateTime.UtcNow;
			}
			catch { /* never let a disk problem interrupt trading */ }
		}

		// Caller must hold the lock.
		private static void Log(string book, DateTime barTime, int copies, List<int> winners, string detail, PropBook b)
		{
			try
			{
				Directory.CreateDirectory(StateDir);
				string path = Path.Combine(StateDir,
					string.Format(CultureInfo.InvariantCulture, "routing_{0}_{1:yyyyMMdd}.csv", Sanitize(book), barTime));

				bool fresh = !File.Exists(path);
				StringBuilder sb = new StringBuilder();

				if (fresh)
					sb.AppendLine("utc,bar_time,copies,winners,detail,seat,account,status,equity,peak,floor,headroom,frozen,seeded,trades");

				string stamp   = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ", CultureInfo.InvariantCulture);
				string bar     = barTime.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);
				string winStr  = winners.Count == 0 ? "none" : string.Join(" ", winners);

				foreach (PropSeat s in b.Seats.Values.OrderBy(x => x.InstanceId))
					sb.AppendFormat(CultureInfo.InvariantCulture,
						"{0},{1},{2},{3},{4},{5},{6},{7},{8:F2},{9:F2},{10:F2},{11:F2},{12},{13},{14}\n",
						stamp, bar, copies, winStr, detail,
						s.InstanceId, s.AccountName, s.Status,
						s.HasEquity ? s.Equity : 0, s.HasEquity ? s.Peak : 0,
						s.HasEquity ? s.Floor : 0, s.HasEquity ? s.Headroom : 0,
						s.Frozen, s.PeakSeeded, s.TradesTaken);

				File.AppendAllText(path, sb.ToString());
			}
			catch { /* logging must never interrupt trading */ }
		}
	}
}
