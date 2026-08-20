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
// contracts can exceed R during overlap. Seats with a granted/working entry
// reservation count against the R quota, which limits ambiguous broker exposure.
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
	/// To trade without real orders, use simulation accounts. Testing independent seats
	/// requires a distinct simulation account per seat; six charts on Sim101 are one account.
	/// </summary>
	public enum PropRouterMode
	{
		/// <summary>Router decides which seats submit. An unseeded seat is never selected, so this fails closed.</summary>
		Routed,

		/// <summary>ORDERS STILL SUBMITTED on every enabled window. The router prints a non-mutating preview.</summary>
		UnroutedLogOnly,

		/// <summary>ORDERS STILL SUBMITTED on every enabled window. Router bypassed entirely.</summary>
		Unrouted
	}

	public class PropSeat
	{
		public int			InstanceId;
		public string		AccountName		= string.Empty;
		internal string		NormalizedAccount = string.Empty;
		internal Guid		Lease			= Guid.Empty;
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
		internal DateTime	LeaseHeartbeatAsOf = DateTime.MinValue;

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

	internal class PeakRecord
	{
		public string		AccountName		= string.Empty;
		public string		NormalizedAccount = string.Empty;
		public double		StartBalance;
		public double		DrawdownSize;
		public double		Peak;
		public DateTime		UpdatedUtc;
	}

	internal class RoutingDecision
	{
		public int			Copies;
		public double		RequiredHeadroom;
		public int			TopologyVersion;
		public List<int>	Winners		= new List<int>();
		public string		Detail			= string.Empty;
	}

	internal class PropBook
	{
		public readonly Dictionary<int, PropSeat>		Seats			= new Dictionary<int, PropSeat>();
		public readonly Dictionary<long, RoutingDecision>	Decisions		= new Dictionary<long, RoutingDecision>();
		public readonly Queue<long>						DecisionOrder	= new Queue<long>();
		public readonly Dictionary<string, PeakRecord>	PeakRecords		=
			new Dictionary<string, PeakRecord>(StringComparer.OrdinalIgnoreCase);
		public string									BookKey			= string.Empty;
		public bool									ManifestSet;
		public int									ExpectedSeats;
		public int									Copies;
		public string									ConfigKey		= string.Empty;
		public int									TopologyVersion;
		public bool									PeaksLoadAttempted;
		public bool									PersistenceHealthy = true;
		public string									PersistenceError	= string.Empty;
		public DateTime								LastPersist		= DateTime.MinValue;
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

		// =====================================================================
		// Registration
		// =====================================================================

		/// <summary>
		/// Claims one configured seat and returns a lease which must accompany every
		/// later seat-specific call. A lease is never shared between strategy instances.
		/// A stale seat may be replaced only when it last reported Free.
		/// </summary>
		public static bool Register(string book, int instanceId, string accountName,
			double startBalance, double drawdownSize, double frozenOffset,
			int expectedSeats, int copies, string configKey, out Guid lease, out string reason)
		{
			lease = Guid.Empty;

			lock (sync)
			{
				if (string.IsNullOrWhiteSpace(book) || !IsSafeBookKey(book.Trim()))
				{
					reason = "book must contain only ASCII letters, digits, '-' or '_'";
					return false;
				}

				string normalizedAccount;
				if (!ValidateRegistrationInput(instanceId, accountName, startBalance, drawdownSize,
					frozenOffset, expectedSeats, copies, configKey, out normalizedAccount, out reason))
					return false;

				PropBook b = GetBook(book);
				string normalizedConfig = configKey.Trim();

				if (!b.ManifestSet)
				{
					b.ManifestSet		= true;
					b.ExpectedSeats	= expectedSeats;
					b.Copies			= copies;
					b.ConfigKey		= normalizedConfig;
				}
				else if (b.ExpectedSeats != expectedSeats || b.Copies != copies
					|| !string.Equals(b.ConfigKey, normalizedConfig, StringComparison.Ordinal))
				{
					reason = string.Format(CultureInfo.InvariantCulture,
						"book manifest mismatch: expected seats={0} copies={1} config='{2}', got seats={3} copies={4} config='{5}'",
						b.ExpectedSeats, b.Copies, b.ConfigKey, expectedSeats, copies, normalizedConfig);
					return false;
				}

				if (!EnsurePeaksLoaded(b, out reason))
					return false;

				PropSeat existing;
				if (b.Seats.TryGetValue(instanceId, out existing)
					&& (existing.Status != SeatStatus.Free || IsOwnerFresh(existing)))
				{
					reason = string.Format(CultureInfo.InvariantCulture,
						"InstanceId {0} is owned by account {1}; status={2}, ownerFresh={3}",
						instanceId, existing.AccountName, existing.Status, IsOwnerFresh(existing));
					return false;
				}

				// One account is one drawdown container. It cannot be a seat in another
				// router book at the same time, even if that book is a different signal stream.
				foreach (PropBook otherBook in books.Values)
				{
					foreach (PropSeat other in otherBook.Seats.Values)
					{
						if (ReferenceEquals(otherBook, b) && other.InstanceId == instanceId)
							continue;
						if (string.Equals(other.NormalizedAccount, normalizedAccount,
							StringComparison.OrdinalIgnoreCase))
						{
							reason = string.Format(CultureInfo.InvariantCulture,
								"account '{0}' is already registered in book '{1}' as InstanceId {2}",
								accountName, otherBook.BookKey, other.InstanceId);
							return false;
						}
					}
				}

				PeakRecord record;
				if (b.PeakRecords.TryGetValue(normalizedAccount, out record)
					&& (record.StartBalance != startBalance || record.DrawdownSize != drawdownSize))
				{
					reason = string.Format(CultureInfo.InvariantCulture,
						"peak row config mismatch for '{0}': file start/dd={1:R}/{2:R}, configured={3:R}/{4:R}",
						accountName, record.StartBalance, record.DrawdownSize, startBalance, drawdownSize);
					MarkPersistenceUnhealthy(b, reason);
					return false;
				}

				DateTime now = DateTime.UtcNow;
				PropSeat seat = new PropSeat();
				seat.InstanceId			= instanceId;
				seat.AccountName		= accountName.Trim();
				seat.NormalizedAccount	= normalizedAccount;
				seat.Lease				= Guid.NewGuid();
				seat.StartBalance		= startBalance;
				seat.DrawdownSize		= drawdownSize;
				seat.FrozenOffset		= frozenOffset;
				seat.Status				= SeatStatus.Free;
				seat.Connected			= false;
				// Default Free is not broker evidence. Readiness stays closed until the
				// lease owner publishes an explicit current status.
				seat.StatusAsOf			= DateTime.MinValue;
				seat.LeaseHeartbeatAsOf	= now;

				if (record != null)
				{
					seat.Peak		= record.Peak;
					seat.PeakSeeded	= true;
				}

				b.Seats[instanceId] = seat;
				b.TopologyVersion++;
				ClearDecisions(b);
				lease = seat.Lease;

				reason = seat.PeakSeeded
					? string.Format(CultureInfo.InvariantCulture,
						"lease granted; peak seeded at {0:F2}, floor {1:F2}", seat.Peak, seat.Floor)
					: string.Format(CultureInfo.InvariantCulture,
						"lease granted; NOT SEEDED - no row for '{0}' in {1}",
						seat.AccountName, Path.GetFileName(PeakPath(b.BookKey)));
				return true;
			}
		}

		public static bool Unregister(string book, int instanceId, Guid lease, out string reason)
		{
			lock (sync)
			{
				PropBook b;
				PropSeat seat;
				if (!TryGetSeat(book, instanceId, lease, out b, out seat, out reason))
					return false;

				if (seat.Status != SeatStatus.Free)
				{
					reason = string.Format(CultureInfo.InvariantCulture,
						"unregister refused while seat status is {0}; reservation retained", seat.Status);
					return false;
				}

				b.Seats.Remove(instanceId);
				b.TopologyVersion++;
				ClearDecisions(b);
				reason = "lease released; durable peak row retained";
				return true;
			}
		}

		// =====================================================================
		// State publishing - called from account, order and bar events
		// =====================================================================

		public static bool PublishEquity(string book, int instanceId, Guid lease,
			double equity, bool connected, out string reason)
		{
			lock (sync)
			{
				PropBook b;
				PropSeat seat;
				if (!TryGetSeat(book, instanceId, lease, out b, out seat, out reason))
					return false;

				seat.LeaseHeartbeatAsOf = DateTime.UtcNow;
				seat.Connected = connected;

				if (!IsFinite(equity) || equity <= 0)
				{
					seat.Equity = double.NaN;
					seat.EquityAsOf = DateTime.MinValue;
					seat.Connected = false;
					reason = "invalid equity; seat remains ineligible until a finite positive value is published";
					return false;
				}

				seat.Equity		= equity;
				seat.EquityAsOf	= DateTime.UtcNow;

				return RatchetPeak(b, seat, equity, out reason);
			}
		}

		/// <summary>
		/// Monotonically records an observed account-equity value without treating that
		/// callback value as the latest current equity. This preserves transient highs
		/// while PublishEquity remains responsible for ordered current headroom.
		/// </summary>
		public static bool ObservePeak(string book, int instanceId, Guid lease,
			double observedEquity, out string reason)
		{
			lock (sync)
			{
				PropBook b;
				PropSeat seat;
				if (!TryGetSeat(book, instanceId, lease, out b, out seat, out reason))
					return false;

				seat.LeaseHeartbeatAsOf = DateTime.UtcNow;
				if (!IsFinite(observedEquity) || observedEquity <= 0)
				{
					reason = "invalid observed equity; peak was not changed";
					return false;
				}

				return RatchetPeak(b, seat, observedEquity, out reason);
			}
		}

		public static bool PublishStatus(string book, int instanceId, Guid lease,
			SeatStatus status, bool connected, out string reason)
		{
			lock (sync)
			{
				PropBook b;
				PropSeat seat;
				if (!TryGetSeat(book, instanceId, lease, out b, out seat, out reason))
					return false;

				if (!Enum.IsDefined(typeof(SeatStatus), status))
				{
					seat.StatusAsOf = DateTime.MinValue;
					seat.Connected = false;
					reason = "invalid seat status";
					return false;
				}

				seat.Status		= status;
				seat.Connected	= connected;
				seat.StatusAsOf	= DateTime.UtcNow;
				seat.LeaseHeartbeatAsOf = seat.StatusAsOf;
				reason = string.Empty;
				return true;
			}
		}

		/// <summary>
		/// Manually raises a seat's high-water mark, for example when NT8 was offline through
		/// an intraday peak. This API is monotonic; a firm reset requires an offline,
		/// independently verified seed-file replacement and a full NT8 restart.
		/// </summary>
		public static bool OverridePeak(string book, int instanceId, Guid lease,
			double peak, out string reason)
		{
			lock (sync)
			{
				PropBook b;
				PropSeat seat;
				if (!TryGetSeat(book, instanceId, lease, out b, out seat, out reason))
					return false;

				seat.LeaseHeartbeatAsOf = DateTime.UtcNow;
				if (!b.PersistenceHealthy)
				{
					reason = b.PersistenceError;
					return false;
				}

				if (!IsFinite(peak) || peak <= 0 || peak < seat.StartBalance
					|| (IsFinite(seat.Equity) && peak < seat.Equity)
					|| (seat.PeakSeeded && IsFinite(seat.Peak) && peak < seat.Peak))
				{
					reason = string.Format(CultureInfo.InvariantCulture,
						"invalid peak {0:R}; peak must be finite and >= start/current equity/trusted peak", peak);
					return false;
				}

				seat.Peak		= peak;
				seat.PeakSeeded	= true;

				PeakRecord record;
				if (!b.PeakRecords.TryGetValue(seat.NormalizedAccount, out record))
				{
					record = new PeakRecord();
					record.NormalizedAccount = seat.NormalizedAccount;
					b.PeakRecords[seat.NormalizedAccount] = record;
				}

				record.AccountName		= seat.AccountName;
				record.StartBalance		= seat.StartBalance;
				record.DrawdownSize		= seat.DrawdownSize;
				record.Peak				= peak;
				record.UpdatedUtc		= DateTime.UtcNow;

				if (!PersistPeaks(b, out reason))
					return false;

				ClearDecisions(b);
				reason = "peak override committed durably";
				return true;
			}
		}

		/// <summary>True when this seat's peak came from the seed file or an explicit override.</summary>
		public static bool IsSeeded(string book, int instanceId, Guid lease, out string reason)
		{
			lock (sync)
			{
				PropBook b;
				PropSeat seat;
				if (!TryGetSeat(book, instanceId, lease, out b, out seat, out reason))
					return false;
				seat.LeaseHeartbeatAsOf = DateTime.UtcNow;
				if (!b.PersistenceHealthy)
				{
					reason = b.PersistenceError;
					return false;
				}
				reason = seat.PeakSeeded ? "seeded" : "not seeded";
				return seat.PeakSeeded;
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
		public static bool TryClaim(string book, int instanceId, Guid lease,
			DateTime barTime, int copies, double requiredHeadroom, out string reason)
		{
			lock (sync)
			{
				PropBook b;
				PropSeat caller;
				if (!TryGetSeat(book, instanceId, lease, out b, out caller, out reason))
					return false;

				caller.LeaseHeartbeatAsOf = DateTime.UtcNow;
				if (copies != b.Copies)
				{
					reason = string.Format(CultureInfo.InvariantCulture,
						"copies mismatch: book manifest requires {0}, caller supplied {1}", b.Copies, copies);
					return false;
				}
				if (!IsFinite(requiredHeadroom) || requiredHeadroom < 0)
				{
					reason = "requiredHeadroom must be finite and non-negative";
					return false;
				}
				if (requiredHeadroom == 0)
					requiredHeadroom = 0.0;

				long key = barTime.Ticks;

				RoutingDecision decision;
				if (!b.Decisions.TryGetValue(key, out decision))
				{
					decision = new RoutingDecision();
					decision.Copies = copies;
					decision.RequiredHeadroom = requiredHeadroom;
					decision.TopologyVersion = b.TopologyVersion;
					decision.Winners = Allocate(b, copies, requiredHeadroom, out decision.Detail);

					b.Decisions[key] = decision;
					b.DecisionOrder.Enqueue(key);
					int cacheLimit = Math.Max(1, MaxCachedDecisions);
					while (b.DecisionOrder.Count > cacheLimit)
						b.Decisions.Remove(b.DecisionOrder.Dequeue());

					foreach (int id in decision.Winners)
					{
						PropSeat w;
						if (b.Seats.TryGetValue(id, out w))
							w.TradesTaken++;
					}

					Log(b.BookKey, barTime, copies, decision.Winners, decision.Detail, b);
				}
				else if (decision.Copies != copies
					|| BitConverter.DoubleToInt64Bits(decision.RequiredHeadroom)
						!= BitConverter.DoubleToInt64Bits(requiredHeadroom)
					|| decision.TopologyVersion != b.TopologyVersion)
				{
					reason = string.Format(CultureInfo.InvariantCulture,
						"cached decision input mismatch for key {0}: cached copies/headroom/topology={1}/{2:R}/{3}, caller={4}/{5:R}/{6}",
						key, decision.Copies, decision.RequiredHeadroom, decision.TopologyVersion,
						copies, requiredHeadroom, b.TopologyVersion);
					return false;
				}

				if (decision.Detail.StartsWith("FAIL_CLOSED", StringComparison.Ordinal))
				{
					reason = decision.Detail;
					return false;
				}

				string readiness;
				if (!IsBookReady(b, out readiness))
				{
					reason = "book not ready at claim: " + readiness;
					return false;
				}

				bool granted = decision.Winners.Contains(instanceId);
				if (granted && (caller.Status != SeatStatus.Free
					|| !caller.HasEquity || caller.Headroom <= 0
					|| caller.Headroom < decision.RequiredHeadroom))
				{
					reason = string.Format(CultureInfo.InvariantCulture,
						"selected earlier but no longer eligible at claim (status={0}, headroom={1:R}, required={2:R}); no reroute",
						caller.Status, caller.HasEquity ? caller.Headroom : double.NaN,
						decision.RequiredHeadroom);
					return false;
				}
				if (granted)
				{
					// Reserve this claimant under the same router lock that grants it. This
					// closes the grant-to-PublishStatus window across later signal timestamps.
					// A crash or ambiguous submit intentionally leaves a conservative Pending
					// reservation for broker reconciliation.
					caller.Status = SeatStatus.Pending;
					caller.StatusAsOf = DateTime.UtcNow;
					caller.LeaseHeartbeatAsOf = caller.StatusAsOf;
				}
				string myHeadroom = caller.HasEquity
					? caller.Headroom.ToString("F2", CultureInfo.InvariantCulture) : "n/a";

				reason = granted
					? string.Format(CultureInfo.InvariantCulture,
						"selected; headroom={0}; winners=[{1}]; book {2}",
						myHeadroom, string.Join(",", decision.Winners), RankingSnapshot(b))
					: string.Format(CultureInfo.InvariantCulture,
						"not selected; headroom={0}; winners=[{1}]",
						myHeadroom,
						decision.Winners.Count == 0 ? "none" : string.Join(",", decision.Winners));
				return granted;
			}
		}

		/// <summary>Non-mutating view of what the router would decide. Used by UnroutedLogOnly.</summary>
		public static string Preview(string book, int instanceId, Guid lease,
			int copies, double requiredHeadroom)
		{
			lock (sync)
			{
				PropBook b;
				PropSeat seat;
				string validation;
				if (!TryGetSeat(book, instanceId, lease, out b, out seat, out validation))
					return "preview refused: " + validation;
				if (copies != b.Copies || !IsFinite(requiredHeadroom) || requiredHeadroom < 0)
					return string.Format(CultureInfo.InvariantCulture,
						"preview refused: manifest copies={0}, caller copies={1}, requiredHeadroom={2:R}",
						b.Copies, copies, requiredHeadroom);

				seat.LeaseHeartbeatAsOf = DateTime.UtcNow;
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
				sb.AppendFormat(CultureInfo.InvariantCulture,
					"book={0} manifest={1}/{2}/{3} topology={4} persistence={5}{6} ",
					b.BookKey,
					b.ManifestSet ? b.ExpectedSeats.ToString(CultureInfo.InvariantCulture) : "unset",
					b.ManifestSet ? b.Copies.ToString(CultureInfo.InvariantCulture) : "unset",
					b.ManifestSet ? b.ConfigKey : "unset",
					b.TopologyVersion,
					b.PersistenceHealthy ? "healthy" : "UNHEALTHY",
					b.PersistenceHealthy ? string.Empty : "(" + b.PersistenceError + ")");
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
			// Pending exposure is broker exposure, not a heartbeat. It remains reserved
			// even when the owning chart is stale or disconnected.
			int pending = b.Seats.Values.Count(s => s.Status == SeatStatus.Pending);
			int need    = Math.Max(0, copies - pending);
			string readiness;
			if (!IsBookReady(b, out readiness))
			{
				detail = string.Format(CultureInfo.InvariantCulture,
					"FAIL_CLOSED {0}; R={1} pending={2} need={3}", readiness, copies, pending, need);
				return new List<int>();
			}

			// max_headroom, with signal_router.py's tie-breaks: (-headroom, trades, seat_id)
			List<PropSeat> eligible = b.Seats.Values
				.Where(s => s.Status == SeatStatus.Free
						 && s.Headroom > 0
						 && s.Headroom >= requiredHeadroom)
				.OrderByDescending(s => s.Headroom)
				.ThenBy(s => s.TradesTaken)
				.ThenBy(s => s.InstanceId)
				.ToList();

			List<int> winners = eligible.Take(need).Select(s => s.InstanceId).ToList();

			detail = string.Format(CultureInfo.InvariantCulture,
				"READY R={0} pending={1} need={2} eligible={3}/{4} blocked={5}{6}",
				copies, pending, need, eligible.Count, b.Seats.Count,
				Math.Max(0, need - winners.Count),
				pending > copies ? " OVER_CAP=" + (pending - copies) : string.Empty);

			return winners;
		}

		// Caller must hold the lock.
		private static bool IsBookReady(PropBook b, out string reason)
		{
			if (!b.ManifestSet)
			{
				reason = "manifest is not initialized";
				return false;
			}
			if (!b.PersistenceHealthy)
			{
				reason = "persistence unhealthy: " + b.PersistenceError;
				return false;
			}
			if (b.Seats.Count != b.ExpectedSeats)
			{
				reason = string.Format(CultureInfo.InvariantCulture,
					"registered seats={0}, expected exactly {1}", b.Seats.Count, b.ExpectedSeats);
				return false;
			}

			for (int id = 1; id <= b.ExpectedSeats; id++)
			{
				PropSeat seat;
				if (!b.Seats.TryGetValue(id, out seat))
				{
					reason = string.Format(CultureInfo.InvariantCulture, "missing InstanceId {0}", id);
					return false;
				}
				if (!IsFresh(seat) || !seat.Connected || !seat.PeakSeeded || !seat.HasEquity)
				{
					reason = string.Format(CultureInfo.InvariantCulture,
						"seat {0} not ready (fresh={1}, connected={2}, seeded={3}, equity={4})",
						id, IsFresh(seat), seat.Connected, seat.PeakSeeded, seat.HasEquity);
					return false;
				}
			}

			reason = "ready";
			return true;
		}

		private static bool IsFresh(PropSeat seat)
		{
			if (seat == null || seat.AsOf == DateTime.MinValue)
				return false;
			if (!IsFinite(StaleSeconds) || StaleSeconds <= 0)
				return false;
			double age = (DateTime.UtcNow - seat.AsOf).TotalSeconds;
			return age >= 0 && age <= StaleSeconds;
		}

		private static bool IsOwnerFresh(PropSeat seat)
		{
			if (seat == null || seat.LeaseHeartbeatAsOf == DateTime.MinValue)
				return false;
			if (!IsFinite(StaleSeconds) || StaleSeconds <= 0)
				return true;
			double age = (DateTime.UtcNow - seat.LeaseHeartbeatAsOf).TotalSeconds;
			return age < 0 || age <= StaleSeconds;
		}

		private static PropBook GetBook(string book)
		{
			string key = NormalizeBook(book);
			PropBook b;
			if (!books.TryGetValue(key, out b))
			{
				b = new PropBook();
				b.BookKey = key;
				books[key] = b;
			}
			return b;
		}

		private static string NormalizeBook(string book)
		{
			return string.IsNullOrWhiteSpace(book) ? "DEFAULT" : book.Trim();
		}

		// Caller must hold the lock.
		private static bool TryGetSeat(string book, int instanceId, Guid lease,
			out PropBook b, out PropSeat seat, out string reason)
		{
			seat = null;
			string key = NormalizeBook(book);
			if (!books.TryGetValue(key, out b))
			{
				reason = "book is not registered";
				return false;
			}
			if (lease == Guid.Empty || !b.Seats.TryGetValue(instanceId, out seat)
				|| seat.Lease != lease)
			{
				reason = string.Format(CultureInfo.InvariantCulture,
					"stale or invalid lease ignored for InstanceId {0}", instanceId);
				return false;
			}
			reason = string.Empty;
			return true;
		}

		// Caller must hold the lock.
		private static void ClearDecisions(PropBook b)
		{
			b.Decisions.Clear();
			b.DecisionOrder.Clear();
		}

		private static bool ValidateRegistrationInput(int instanceId, string accountName,
			double startBalance, double drawdownSize, double frozenOffset,
			int expectedSeats, int copies, string configKey,
			out string normalizedAccount, out string reason)
		{
			normalizedAccount = NormalizeAccount(accountName);
			if (expectedSeats <= 0 || copies <= 0 || copies > expectedSeats)
			{
				reason = "expectedSeats must be positive and copies must be in 1..expectedSeats";
				return false;
			}
			if (instanceId < 1 || instanceId > expectedSeats)
			{
				reason = string.Format(CultureInfo.InvariantCulture,
					"InstanceId {0} is outside configured range 1..{1}", instanceId, expectedSeats);
				return false;
			}
			if (string.IsNullOrEmpty(normalizedAccount) || accountName.IndexOf(',') >= 0
				|| accountName.IndexOf('\r') >= 0 || accountName.IndexOf('\n') >= 0)
			{
				reason = "account name is empty or contains a CSV delimiter/newline";
				return false;
			}
			if (!IsFinite(startBalance) || startBalance <= 0
				|| !IsFinite(drawdownSize) || drawdownSize <= 0
				|| !IsFinite(frozenOffset) || frozenOffset < 0)
			{
				reason = "start balance/drawdown must be finite positive values and frozen offset must be finite non-negative";
				return false;
			}
			if (string.IsNullOrWhiteSpace(configKey))
			{
				reason = "configKey is required";
				return false;
			}
			reason = string.Empty;
			return true;
		}

		private static bool IsFinite(double value)
		{
			return !double.IsNaN(value) && !double.IsInfinity(value);
		}

		// Caller must hold the lock.
		private static bool RatchetPeak(PropBook b, PropSeat seat,
			double observedEquity, out string reason)
		{
			// An unseeded seat may maintain a sane in-memory observation, but PeakSeeded
			// stays false and the value can never become a trusted durable seed implicitly.
			bool newHigh = double.IsNaN(seat.Peak) || observedEquity > seat.Peak;
			if (newHigh)
				seat.Peak = observedEquity;

			if (seat.PeakSeeded && newHigh)
			{
				PeakRecord record;
				if (!b.PeakRecords.TryGetValue(seat.NormalizedAccount, out record))
				{
					reason = "seeded seat has no durable peak record";
					MarkPersistenceUnhealthy(b, reason);
					return false;
				}

				record.AccountName	= seat.AccountName;
				record.Peak			= seat.Peak;
				record.UpdatedUtc	= DateTime.UtcNow;
				if (!PersistPeaks(b, out reason))
					return false;
			}

			if (!b.PersistenceHealthy)
			{
				reason = b.PersistenceError;
				return false;
			}

			reason = string.Empty;
			return true;
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

		private static bool IsSafeBookKey(string name)
		{
			if (string.IsNullOrEmpty(name))
				return false;
			foreach (char c in name)
				if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')
					|| (c >= '0' && c <= '9') || c == '-' || c == '_'))
					return false;
			return true;
		}

		private static string PeakPath(string book)
		{
			return Path.Combine(StateDir, "peaks_" + Sanitize(book) + ".csv");
		}

		// Caller must hold the lock.
		private static bool EnsurePeaksLoaded(PropBook b, out string reason)
		{
			if (b.PeaksLoadAttempted)
			{
				reason = b.PersistenceHealthy ? string.Empty : b.PersistenceError;
				return b.PersistenceHealthy;
			}

			b.PeaksLoadAttempted = true;
			string path = string.Empty;
			try
			{
				path = PeakPath(b.BookKey);
				if (!File.Exists(path))
				{
					reason = string.Empty;
					return true;
				}

				string[] lines = File.ReadAllLines(path);
				const string header = "account,start_balance,drawdown,peak,updated_utc";
				if (lines.Length == 0
					|| !string.Equals(lines[0].TrimStart('\uFEFF'), header, StringComparison.Ordinal))
				{
					reason = string.Format(CultureInfo.InvariantCulture,
						"invalid peak CSV header in {0}; expected exactly '{1}'", path, header);
					MarkPersistenceUnhealthy(b, reason);
					return false;
				}

				Dictionary<string, PeakRecord> loaded =
					new Dictionary<string, PeakRecord>(StringComparer.OrdinalIgnoreCase);

				for (int lineNumber = 2; lineNumber <= lines.Length; lineNumber++)
				{
					string line = lines[lineNumber - 1];
					string[] f = line.Split(',');
					if (f.Length != 5)
					{
						reason = string.Format(CultureInfo.InvariantCulture,
							"invalid peak CSV row {0} in {1}: expected exactly 5 comma-separated fields, got {2}",
							lineNumber, path, f.Length);
						MarkPersistenceUnhealthy(b, reason);
						return false;
					}

					string accountName = f[0].Trim();
					string normalized = NormalizeAccount(accountName);
					double start = double.NaN;
					double dd = double.NaN;
					double peak = double.NaN;
					DateTime updatedUtc = DateTime.MinValue;
					bool parsed = !string.IsNullOrEmpty(normalized)
						&& double.TryParse(f[1], NumberStyles.Float, CultureInfo.InvariantCulture, out start)
						&& double.TryParse(f[2], NumberStyles.Float, CultureInfo.InvariantCulture, out dd)
						&& double.TryParse(f[3], NumberStyles.Float, CultureInfo.InvariantCulture, out peak)
						&& DateTime.TryParseExact(f[4].Trim(), "yyyy-MM-ddTHH:mm:ss'Z'",
							CultureInfo.InvariantCulture,
							DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out updatedUtc);

					if (!parsed || !IsFinite(start) || start <= 0
						|| !IsFinite(dd) || dd <= 0 || !IsFinite(peak) || peak <= 0 || peak < start)
					{
						reason = string.Format(CultureInfo.InvariantCulture,
							"invalid peak CSV row {0} in {1}: account/start/dd/peak/timestamp failed strict validation",
							lineNumber, path);
						MarkPersistenceUnhealthy(b, reason);
						return false;
					}

					if (loaded.ContainsKey(normalized))
					{
						reason = string.Format(CultureInfo.InvariantCulture,
							"duplicate normalized account '{0}' in peak CSV {1}", normalized, path);
						MarkPersistenceUnhealthy(b, reason);
						return false;
					}

					PeakRecord record = new PeakRecord();
					record.AccountName		= accountName;
					record.NormalizedAccount	= normalized;
					record.StartBalance		= start;
					record.DrawdownSize		= dd;
					record.Peak				= peak;
					record.UpdatedUtc		= updatedUtc;
					loaded[normalized] = record;
				}

				b.PeakRecords.Clear();
				foreach (KeyValuePair<string, PeakRecord> item in loaded)
					b.PeakRecords[item.Key] = item.Value;

				reason = string.Empty;
				return true;
			}
			catch (Exception ex)
			{
				reason = string.Format(CultureInfo.InvariantCulture,
					"failed to read/validate peak CSV {0}: {1}: {2}",
					string.IsNullOrEmpty(path) ? "<unresolved>" : path, ex.GetType().Name, ex.Message);
				MarkPersistenceUnhealthy(b, reason);
				return false;
			}
		}

		// Caller must hold the lock.
		private static bool PersistPeaks(PropBook b, out string reason)
		{
			if (!b.PersistenceHealthy)
			{
				reason = b.PersistenceError;
				return false;
			}

			string path = string.Empty;
			string backupPath = string.Empty;
			string tempPath = string.Empty;

			try
			{
				path = PeakPath(b.BookKey);
				backupPath = path + ".bak";
				tempPath = path + ".tmp." + Guid.NewGuid().ToString("N");
				Directory.CreateDirectory(StateDir);
				StringBuilder sb = new StringBuilder();
				sb.AppendLine("account,start_balance,drawdown,peak,updated_utc");

				foreach (PeakRecord record in b.PeakRecords.Values.OrderBy(x => x.NormalizedAccount))
				{
					if (string.IsNullOrEmpty(record.NormalizedAccount)
						|| string.IsNullOrEmpty(record.AccountName)
						|| record.AccountName.IndexOf(',') >= 0
						|| record.AccountName.IndexOf('\r') >= 0
						|| record.AccountName.IndexOf('\n') >= 0
						|| !IsFinite(record.StartBalance) || record.StartBalance <= 0
						|| !IsFinite(record.DrawdownSize) || record.DrawdownSize <= 0
						|| !IsFinite(record.Peak) || record.Peak < record.StartBalance)
					{
						reason = string.Format(CultureInfo.InvariantCulture,
							"refusing to persist invalid durable peak record for '{0}'", record.AccountName);
						MarkPersistenceUnhealthy(b, reason);
						return false;
					}

					DateTime stamp = record.UpdatedUtc == DateTime.MinValue
						? DateTime.UtcNow : record.UpdatedUtc.ToUniversalTime();
					sb.AppendFormat(CultureInfo.InvariantCulture, "{0},{1:R},{2:R},{3:R},{4}\n",
						record.AccountName, record.StartBalance, record.DrawdownSize, record.Peak,
						stamp.ToString("yyyy-MM-ddTHH:mm:ss'Z'", CultureInfo.InvariantCulture));
				}

				byte[] bytes = new UTF8Encoding(false).GetBytes(sb.ToString());
				using (FileStream stream = new FileStream(tempPath, FileMode.CreateNew, FileAccess.Write,
					FileShare.None, 4096, FileOptions.WriteThrough))
				{
					stream.Write(bytes, 0, bytes.Length);
					stream.Flush(true);
				}

				if (File.Exists(path))
					File.Replace(tempPath, path, backupPath, true);
				else
				{
					File.Move(tempPath, path);
					File.Copy(path, backupPath, true);
				}

				b.LastPersist = DateTime.UtcNow;
				reason = string.Empty;
				return true;
			}
			catch (Exception ex)
			{
				try
				{
					if (!string.IsNullOrEmpty(tempPath) && File.Exists(tempPath))
						File.Delete(tempPath);
				}
				catch (Exception cleanupEx)
				{
					ex = new IOException(ex.Message + "; temp cleanup failed: " + cleanupEx.Message, ex);
				}

				reason = string.Format(CultureInfo.InvariantCulture,
					"failed to atomically persist peak CSV {0}: {1}: {2}",
					string.IsNullOrEmpty(path) ? "<unresolved>" : path, ex.GetType().Name, ex.Message);
				MarkPersistenceUnhealthy(b, reason);
				return false;
			}
		}

		// Caller must hold the lock.
		private static void MarkPersistenceUnhealthy(PropBook b, string reason)
		{
			b.PersistenceHealthy = false;
			if (string.IsNullOrEmpty(b.PersistenceError))
				b.PersistenceError = reason;
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
						stamp, bar, copies, CsvField(winStr), CsvField(detail),
						s.InstanceId, CsvField(s.AccountName), s.Status,
						s.HasEquity ? s.Equity : 0, s.HasEquity ? s.Peak : 0,
						s.HasEquity ? s.Floor : 0, s.HasEquity ? s.Headroom : 0,
						s.Frozen, s.PeakSeeded, s.TradesTaken);

				File.AppendAllText(path, sb.ToString());
			}
			catch { /* logging must never interrupt trading */ }
		}

		/// <summary>Compact "instance=headroom" ranking, in the order max_headroom would pick.</summary>
		private static string RankingSnapshot(PropBook b)
		{
			return string.Join(" ", b.Seats.Values
				.Where(s => s.HasEquity)
				.OrderByDescending(s => s.Headroom)
				.ThenBy(s => s.TradesTaken)
				.ThenBy(s => s.InstanceId)
				.Select(s => string.Format(CultureInfo.InvariantCulture,
					"{0}:{1:F2}{2}", s.InstanceId, s.Headroom,
					s.Status == SeatStatus.Free ? string.Empty : "(" + s.Status + ")"))
				.ToArray());
		}

		private static string CsvField(string value)
		{
			string text = value ?? string.Empty;
			if (text.IndexOfAny(new[] { ',', '"', '\r', '\n' }) < 0)
				return text;
			return "\"" + text.Replace("\"", "\"\"") + "\"";
		}
	}
}
