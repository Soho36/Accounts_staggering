using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using NinjaTrader.NinjaScript.AddOns;

internal static class PropRouterRegressionTests
{
    private const string Header = "account,start_balance,drawdown,peak,updated_utc";
    private const string Stamp = "2026-08-17T00:00:00Z";
    private const string ConfigKey = "instrument=TEST;bars=Minute:1;windows=canonical";

    private static int passed;
    private static int failed;

    private sealed class Seed
    {
        public string Account;
        public double Start;
        public double Drawdown;
        public double Peak;

        public Seed(string account, double start, double drawdown, double peak)
        {
            Account = account;
            Start = start;
            Drawdown = drawdown;
            Peak = peak;
        }
    }

    private sealed class SeatSnapshot
    {
        public double Equity;
        public double Peak;
        public double Headroom;
        public SeatStatus Status;
    }

    private static int Main(string[] args)
    {
        if (args.Length != 1 || string.IsNullOrWhiteSpace(args[0]))
        {
            Console.Error.WriteLine("Usage: PropRouterRegressionTests.exe <temporary-user-data-directory>");
            return 2;
        }

        NinjaTrader.Core.Globals.UserDataDir = Path.GetFullPath(args[0]);
        Directory.CreateDirectory(NinjaTrader.Core.Globals.UserDataDir);

        Run("six-row seed survives first publish and sequential unregister", TestSeedSurvivesPublishAndUnregister);
        Run("malformed decimal-comma and excess-column seeds fail closed", TestMalformedSeedsFailClosed);
        Run("stored start/drawdown mismatch refuses registration", TestStoredConfigurationMismatch);
        Run("quorum blocks claims before every expected seat registers", TestQuorumBlocksEarlyClaims);
        Run("registration default Free is not status evidence", TestExplicitStatusIsRequiredForQuorum);
        Run("invalid current equity immediately revokes readiness", TestInvalidCurrentEquityRevokesReadiness);
        Run("a not-ready first claim stays denied for that signal", TestNotReadyDecisionCannotGrantLater);
        Run("max-headroom winner is deterministic once the book is ready", TestMaxHeadroomWinner);
        Run("a grant atomically reserves the R=1 pending slot", TestGrantAtomicallyReservesPendingSlot);
        Run("a delayed cached winner is denied without rerouting if it becomes ineligible", TestDelayedWinnerInvalidation);
        Run("transient observed peak does not replace current equity", TestObservedPeakDoesNotReplaceCurrentEquity);
        Run("override peak cannot decrease a trusted high-water mark", TestOverridePeakIsMonotonic);
        Run("unsafe book identifiers are rejected", TestUnsafeBookIdentifier);
        Run("duplicate instance and duplicate account registrations are refused", TestDuplicateRegistration);
        Run("stale lease callbacks are ignored after a safe takeover", TestStaleLeaseCallbacksAreIgnored);
        Run("blocked-order write failure returns no saved path", TestBlockedOrderWriteFailureIsVisible);

        Console.WriteLine();
        Console.WriteLine("Result: {0} passed, {1} failed", passed, failed);
        return failed == 0 ? 0 : 1;
    }

    private static void Run(string name, Action test)
    {
        try
        {
            test();
            passed++;
            Console.WriteLine("[PASS] {0}", name);
        }
        catch (Exception ex)
        {
            failed++;
            Console.Error.WriteLine("[FAIL] {0}", name);
            Console.Error.WriteLine("       {0}: {1}", ex.GetType().Name, ex.Message);
        }
    }

    private static void TestSeedSurvivesPublishAndUnregister()
    {
        const string book = "PERSISTENCE";
        Seed[] seeds = SixSeeds("PERSIST");
        WriteSeed(book, seeds);

        Guid[] leases = new Guid[6];
        string reason;
        Expect(PropRouter.Register(book, 1, seeds[0].Account,
            seeds[0].Start, seeds[0].Drawdown, 100,
            6, 1, ConfigKey, out leases[0], out reason),
            "first seat registration failed: " + reason);

        // Force a durable rewrite while only one seat is active. The other five
        // rows must remain in the durable store.
        Expect(PropRouter.PublishEquity(book, 1, leases[0], 27050, true, out reason),
            "first-seat equity publish failed: " + reason);
        AssertSeedFile(book, seeds.Select(s => s.Account), 6);
        ExpectNearlyEqual(27050, ReadPeak(book, seeds[0].Account),
            "the first seat's new peak was not persisted");
        ExpectNearlyEqual(seeds[5].Peak, ReadPeak(book, seeds[5].Account),
            "an inactive seat's durable peak was changed or dropped");

        for (int id = 2; id <= 6; id++)
        {
            Seed seed = seeds[id - 1];
            Expect(PropRouter.Register(book, id, seed.Account,
                seed.Start, seed.Drawdown, 100,
                6, 1, ConfigKey, out leases[id - 1], out reason),
                "seat " + id + " registration failed: " + reason);
        }

        for (int id = 1; id <= 6; id++)
        {
            Expect(PropRouter.Unregister(book, id, leases[id - 1], out reason),
                "seat " + id + " unregister failed: " + reason);
            AssertSeedFile(book, seeds.Select(s => s.Account), 6);
        }

        ExpectNearlyEqual(27050, ReadPeak(book, seeds[0].Account),
            "sequential unregister lost the updated peak");
    }

    private static void TestMalformedSeedsFailClosed()
    {
        const string decimalBook = "BAD_DECIMAL_COMMA";
        WriteRaw(decimalBook, Header + Environment.NewLine
            + "BAD-DECIMAL,25000,00,1500,00,26600,00," + Stamp + Environment.NewLine);

        Guid lease;
        string reason;
        bool registered = PropRouter.Register(decimalBook, 1, "BAD-DECIMAL",
            25000, 1500, 100, 1, 1, ConfigKey, out lease, out reason);
        Expect(!registered, "decimal-comma row unexpectedly registered");
        Expect(lease == Guid.Empty, "failed decimal-comma registration returned a lease");
        ExpectContains(reason, "expected exactly 5", "decimal-comma failure reason");
        ExpectContains(PropRouter.Describe(decimalBook), "UNHEALTHY", "decimal-comma book state");

        const string extraBook = "BAD_EXTRA_COLUMN";
        WriteRaw(extraBook, Header + Environment.NewLine
            + "BAD-EXTRA,25000,1500,26600," + Stamp + ",unexpected" + Environment.NewLine);

        registered = PropRouter.Register(extraBook, 1, "BAD-EXTRA",
            25000, 1500, 100, 1, 1, ConfigKey, out lease, out reason);
        Expect(!registered, "excess-column row unexpectedly registered");
        Expect(lease == Guid.Empty, "failed excess-column registration returned a lease");
        ExpectContains(reason, "expected exactly 5", "excess-column failure reason");
        ExpectContains(PropRouter.Describe(extraBook), "UNHEALTHY", "excess-column book state");
    }

    private static void TestStoredConfigurationMismatch()
    {
        AssertConfigurationMismatch("BAD_START", 50000, 1500, "start/dd");
        AssertConfigurationMismatch("BAD_DRAWDOWN", 25000, 2500, "start/dd");
    }

    private static void AssertConfigurationMismatch(
        string book, double configuredStart, double configuredDrawdown, string expectedReason)
    {
        Seed seed = new Seed(book + "-ACCOUNT", 25000, 1500, 26600);
        WriteSeed(book, seed);

        Guid lease;
        string reason;
        bool registered = PropRouter.Register(book, 1, seed.Account,
            configuredStart, configuredDrawdown, 100,
            1, 1, ConfigKey, out lease, out reason);

        Expect(!registered, book + " unexpectedly registered");
        Expect(lease == Guid.Empty, book + " returned a lease on failure");
        ExpectContains(reason, expectedReason, book + " failure reason");
        ExpectContains(PropRouter.Describe(book), "UNHEALTHY", book + " book state");
    }

    private static void TestQuorumBlocksEarlyClaims()
    {
        const string book = "QUORUM";
        Seed[] seeds = SixSeeds("QUORUM");
        WriteSeed(book, seeds);

        Guid[] leases = new Guid[5];
        string reason;
        for (int id = 1; id <= 5; id++)
        {
            Seed seed = seeds[id - 1];
            Expect(PropRouter.Register(book, id, seed.Account,
                seed.Start, seed.Drawdown, 100,
                6, 1, ConfigKey, out leases[id - 1], out reason),
                "seat " + id + " registration failed: " + reason);
            PublishReady(book, id, leases[id - 1], 25500);
        }

        bool granted = PropRouter.TryClaim(book, 1, leases[0],
            new DateTime(2026, 8, 17, 1, 0, 0), 1, 0, out reason);
        Expect(!granted, "claim was granted before quorum was present");
        ExpectContains(reason, "registered seats=5, expected exactly 6", "quorum failure reason");
    }

    private static void TestMaxHeadroomWinner()
    {
        const string book = "MAX_HEADROOM";
        Seed[] seeds = SixSeeds("HEADROOM");
        WriteSeed(book, seeds);

        double[] equities = { 25200, 25500, 25900, 25800, 25100, 25700 };
        Guid[] leases = new Guid[6];
        string reason;

        for (int id = 1; id <= 6; id++)
        {
            Seed seed = seeds[id - 1];
            Expect(PropRouter.Register(book, id, seed.Account,
                seed.Start, seed.Drawdown, 100,
                6, 1, ConfigKey, out leases[id - 1], out reason),
                "seat " + id + " registration failed: " + reason);
        }
        for (int id = 1; id <= 6; id++)
            PublishReady(book, id, leases[id - 1], equities[id - 1]);

        DateTime barTime = new DateTime(2026, 8, 17, 2, 0, 0);
        bool callerSixWon = PropRouter.TryClaim(book, 6, leases[5], barTime, 1, 0, out reason);
        Expect(!callerSixWon, "seat 6 won despite seat 3 having greater headroom");
        ExpectContains(reason, "winners=[3]", "first deterministic decision");

        bool callerThreeWon = PropRouter.TryClaim(book, 3, leases[2], barTime, 1, 0, out reason);
        Expect(callerThreeWon, "seat 3 did not receive its cached winning decision: " + reason);
        ExpectContains(reason, "winners=[3]", "cached deterministic decision");

        bool callerOneWon = PropRouter.TryClaim(book, 1, leases[0], barTime, 1, 0, out reason);
        Expect(!callerOneWon, "a second seat was granted the same R=1 decision");
        ExpectContains(reason, "winners=[3]", "loser's cached decision");
    }

    private static void TestExplicitStatusIsRequiredForQuorum()
    {
        const string book = "STATUS_EVIDENCE";
        Seed[] seeds =
        {
            new Seed("STATUS-1", 25000, 1500, 26000),
            new Seed("STATUS-2", 25000, 1500, 26000)
        };
        WriteSeed(book, seeds);

        Guid[] leases = new Guid[2];
        string reason;
        for (int id = 1; id <= 2; id++)
        {
            Seed seed = seeds[id - 1];
            Expect(PropRouter.Register(book, id, seed.Account,
                seed.Start, seed.Drawdown, 100,
                2, 1, ConfigKey, out leases[id - 1], out reason),
                "seat " + id + " registration failed: " + reason);
            Expect(PropRouter.PublishEquity(book, id, leases[id - 1], 25500, true, out reason),
                "seat " + id + " valid equity publish failed: " + reason);
            // Deliberately do not call PublishStatus. Registration's default Free
            // value is initialization, not observed broker/order state.
        }

        bool granted = PropRouter.TryClaim(book, 1, leases[0],
            new DateTime(2026, 8, 17, 1, 10, 0), 1, 0, out reason);
        Expect(!granted, "claim was granted without any explicit status observation");
        ExpectContains(reason, "FAIL_CLOSED", "missing-status quorum decision");
        ExpectContains(reason, "fresh=False", "missing-status readiness evidence");
    }

    private static void TestInvalidCurrentEquityRevokesReadiness()
    {
        const string book = "INVALID_CURRENT";
        Seed seed = new Seed("INVALID-CURRENT-ACCOUNT", 50, 20, 120);
        WriteSeed(book, seed);

        Guid lease;
        string reason;
        Expect(PropRouter.Register(book, 1, seed.Account,
            seed.Start, seed.Drawdown, 100,
            1, 1, ConfigKey, out lease, out reason),
            "registration failed: " + reason);
        PublishReady(book, 1, lease, 110);

        bool granted = PropRouter.TryClaim(book, 1, lease,
            new DateTime(2026, 8, 17, 1, 20, 0), 1, 0, out reason);
        Expect(granted, "book was not ready before the invalid callback: " + reason);

        bool accepted = PropRouter.PublishEquity(book, 1, lease, double.NaN, true, out reason);
        Expect(!accepted, "invalid current equity was accepted");
        ExpectContains(reason, "invalid equity", "invalid current-equity rejection");
        Expect(double.IsNaN(ReadSeatSnapshot(book, 1).Equity),
            "invalid callback left the previous current equity available");

        granted = PropRouter.TryClaim(book, 1, lease,
            new DateTime(2026, 8, 17, 1, 21, 0), 1, 0, out reason);
        Expect(!granted, "new signal reused stale good equity after an invalid callback");
        ExpectContains(reason, "FAIL_CLOSED", "post-invalid claim decision");
        ExpectContains(reason, "equity=False", "post-invalid readiness evidence");
        ExpectNearlyEqual(120, ReadPeak(book, seed.Account),
            "invalid current-equity callback changed the durable peak");
    }

    private static void TestNotReadyDecisionCannotGrantLater()
    {
        const string book = "NOT_READY_CACHE";
        Seed[] seeds =
        {
            new Seed("NOT-READY-1", 25000, 1500, 26000),
            new Seed("NOT-READY-2", 25000, 1500, 26000)
        };
        WriteSeed(book, seeds);

        Guid[] leases = new Guid[2];
        string reason;
        for (int id = 1; id <= 2; id++)
        {
            Seed seed = seeds[id - 1];
            Expect(PropRouter.Register(book, id, seed.Account,
                seed.Start, seed.Drawdown, 100,
                2, 1, ConfigKey, out leases[id - 1], out reason),
                "seat " + id + " registration failed: " + reason);
        }

        PublishReady(book, 1, leases[0], 25500);
        DateTime failedSignal = new DateTime(2026, 8, 17, 1, 30, 0);
        bool granted = PropRouter.TryClaim(book, 1, leases[0], failedSignal, 1, 0, out reason);
        Expect(!granted, "the first claim was granted while seat 2 was not ready");
        ExpectContains(reason, "FAIL_CLOSED", "initial not-ready decision");

        // Becoming ready later must not retroactively turn an already failed signal
        // into an allocation. The empty decision remains cached for this bar key.
        PublishReady(book, 2, leases[1], 25400);
        granted = PropRouter.TryClaim(book, 1, leases[0], failedSignal, 1, 0, out reason);
        Expect(!granted, "seat 1 was retroactively granted the failed signal");
        ExpectContains(reason, "FAIL_CLOSED", "seat 1 cached failed decision");

        granted = PropRouter.TryClaim(book, 2, leases[1], failedSignal, 1, 0, out reason);
        Expect(!granted, "seat 2 was retroactively granted the failed signal");
        ExpectContains(reason, "FAIL_CLOSED", "seat 2 cached failed decision");

        // A genuinely new signal key may allocate normally once the book is ready.
        DateTime nextSignal = failedSignal.AddMinutes(1);
        granted = PropRouter.TryClaim(book, 1, leases[0], nextSignal, 1, 0, out reason);
        Expect(granted, "a new signal did not recover after the book became ready: " + reason);
        ExpectContains(reason, "winners=[1]", "new ready decision");
    }

    private static void TestGrantAtomicallyReservesPendingSlot()
    {
        const string book = "ATOMIC_PENDING";
        Seed[] seeds =
        {
            new Seed("ATOMIC-1", 50, 20, 120),
            new Seed("ATOMIC-2", 50, 20, 120)
        };
        WriteSeed(book, seeds);

        Guid[] leases = new Guid[2];
        string reason;
        for (int id = 1; id <= 2; id++)
        {
            Seed seed = seeds[id - 1];
            Expect(PropRouter.Register(book, id, seed.Account,
                seed.Start, seed.Drawdown, 100,
                2, 1, ConfigKey, out leases[id - 1], out reason),
                "seat " + id + " registration failed: " + reason);
        }

        // floor=100; seat 1 has headroom 20 and deterministically wins over
        // seat 2's headroom 15.
        PublishReady(book, 1, leases[0], 120);
        PublishReady(book, 2, leases[1], 115);

        DateTime firstSignal = new DateTime(2026, 8, 17, 2, 10, 0);
        bool granted = PropRouter.TryClaim(book, 1, leases[0], firstSignal, 1, 0, out reason);
        Expect(granted, "seat 1 did not receive the initial R=1 grant: " + reason);
        Expect(ReadSeatSnapshot(book, 1).Status == SeatStatus.Pending,
            "TryClaim did not atomically reserve the granted seat as Pending");

        // No PublishStatus call occurs here. A later signal timestamp must see
        // the atomic reservation and have need=0 rather than select seat 2.
        DateTime nextSignal = firstSignal.AddMinutes(1);
        granted = PropRouter.TryClaim(book, 2, leases[1], nextSignal, 1, 0, out reason);
        Expect(!granted, "a second winner was allocated before order-status publication");
        ExpectContains(reason, "winners=[none]", "post-grant R=1 reservation decision");
        Expect(ReadSeatSnapshot(book, 1).Status == SeatStatus.Pending,
            "the pending reservation did not persist across the later claim");

        granted = PropRouter.TryClaim(book, 1, leases[0], nextSignal, 1, 0, out reason);
        Expect(!granted, "the already-reserved caller received another grant");
        ExpectContains(reason, "winners=[none]", "reserved caller's later decision");
    }

    private static void TestDelayedWinnerInvalidation()
    {
        const string book = "DELAYED_WINNER";
        Seed[] seeds =
        {
            new Seed("DELAYED-1", 50, 20, 120),
            new Seed("DELAYED-2", 50, 20, 120)
        };
        WriteSeed(book, seeds);

        Guid[] leases = new Guid[2];
        string reason;
        for (int id = 1; id <= 2; id++)
        {
            Seed seed = seeds[id - 1];
            Expect(PropRouter.Register(book, id, seed.Account,
                seed.Start, seed.Drawdown, 100,
                2, 1, ConfigKey, out leases[id - 1], out reason),
                "seat " + id + " registration failed: " + reason);
        }

        // Both seats cover the $10 requirement, but seat 2 has greater headroom:
        // floor=100, seat 1 headroom=15, seat 2 headroom=20.
        PublishReady(book, 1, leases[0], 115);
        PublishReady(book, 2, leases[1], 120);

        DateTime positionSignal = new DateTime(2026, 8, 17, 2, 30, 0);
        bool granted = PropRouter.TryClaim(book, 1, leases[0], positionSignal, 1, 10, out reason);
        Expect(!granted, "the lower-headroom first caller unexpectedly won");
        ExpectContains(reason, "winners=[2]", "cached delayed winner by status");

        Expect(PropRouter.PublishStatus(book, 2, leases[1], SeatStatus.InPosition, true, out reason),
            "could not make the delayed winner busy: " + reason);
        granted = PropRouter.TryClaim(book, 2, leases[1], positionSignal, 1, 10, out reason);
        Expect(!granted, "cached winner was granted after becoming InPosition");
        ExpectContains(reason, "no longer eligible", "busy cached-winner rejection");
        ExpectContains(reason, "no reroute", "busy cached-winner policy");

        granted = PropRouter.TryClaim(book, 1, leases[0], positionSignal, 1, 10, out reason);
        Expect(!granted, "signal was rerouted to seat 1 after cached winner became busy");
        ExpectContains(reason, "winners=[2]", "busy decision remained pinned");

        // Repeat on a new signal with a headroom invalidation. Seat 1 remains
        // eligible, so denying it proves the router did not silently reroute.
        Expect(PropRouter.PublishStatus(book, 2, leases[1], SeatStatus.Free, true, out reason),
            "could not return seat 2 to Free: " + reason);
        DateTime headroomSignal = positionSignal.AddMinutes(1);
        granted = PropRouter.TryClaim(book, 1, leases[0], headroomSignal, 1, 10, out reason);
        Expect(!granted, "the lower-headroom first caller unexpectedly won the second signal");
        ExpectContains(reason, "winners=[2]", "cached delayed winner by headroom");

        Expect(PropRouter.PublishEquity(book, 2, leases[1], 105, true, out reason),
            "could not reduce the delayed winner's current headroom: " + reason);
        granted = PropRouter.TryClaim(book, 2, leases[1], headroomSignal, 1, 10, out reason);
        Expect(!granted, "cached winner was granted below required headroom");
        ExpectContains(reason, "headroom=5", "thin cached-winner rejection");
        ExpectContains(reason, "no reroute", "thin cached-winner policy");

        granted = PropRouter.TryClaim(book, 1, leases[0], headroomSignal, 1, 10, out reason);
        Expect(!granted, "signal was rerouted to still-eligible seat 1 after winner became thin");
        ExpectContains(reason, "winners=[2]", "thin decision remained pinned");
    }

    private static void TestDuplicateRegistration()
    {
        const string book = "DUPLICATES";
        Seed[] seeds =
        {
            new Seed("DUP-A", 25000, 1500, 26000),
            new Seed("DUP-B", 25000, 1500, 26000)
        };
        WriteSeed(book, seeds);

        Guid firstLease;
        string reason;
        Expect(PropRouter.Register(book, 1, "DUP-A", 25000, 1500, 100,
            2, 1, ConfigKey, out firstLease, out reason),
            "initial registration failed: " + reason);

        Guid rejectedLease;
        bool registered = PropRouter.Register(book, 1, "DUP-A", 25000, 1500, 100,
            2, 1, ConfigKey, out rejectedLease, out reason);
        Expect(!registered, "duplicate instance/same account was accepted");
        Expect(rejectedLease == Guid.Empty, "duplicate instance returned a lease");
        ExpectContains(reason, "InstanceId 1 is owned", "duplicate instance reason");

        registered = PropRouter.Register(book, 1, "DUP-B", 25000, 1500, 100,
            2, 1, ConfigKey, out rejectedLease, out reason);
        Expect(!registered, "duplicate instance/different account was accepted");
        ExpectContains(reason, "InstanceId 1 is owned", "duplicate owner reason");

        registered = PropRouter.Register(book, 2, "DUP-A!Provider!Connection", 25000, 1500, 100,
            2, 1, ConfigKey, out rejectedLease, out reason);
        Expect(!registered, "duplicate normalized account was accepted on another instance");
        ExpectContains(reason, "already registered in book 'DUPLICATES' as InstanceId 1",
            "duplicate account reason");

        const string firstBook = "GLOBAL_DUP_A";
        const string secondBook = "GLOBAL_DUP_B";
        Seed globalSeed = new Seed("GLOBAL-DUP", 25000, 1500, 26000);
        WriteSeed(firstBook, globalSeed);
        WriteSeed(secondBook, globalSeed);

        Guid globalLease;
        Expect(PropRouter.Register(firstBook, 1, globalSeed.Account,
            globalSeed.Start, globalSeed.Drawdown, 100,
            1, 1, ConfigKey, out globalLease, out reason),
            "first global account registration failed: " + reason);

        registered = PropRouter.Register(secondBook, 1,
            "GLOBAL-DUP!Provider!Connection",
            globalSeed.Start, globalSeed.Drawdown, 100,
            1, 1, ConfigKey, out rejectedLease, out reason);
        Expect(!registered, "same normalized account was accepted in a second book");
        Expect(rejectedLease == Guid.Empty, "cross-book duplicate registration returned a lease");
        ExpectContains(reason, "already registered in book 'GLOBAL_DUP_A'",
            "cross-book duplicate account reason");
    }

    private static void TestObservedPeakDoesNotReplaceCurrentEquity()
    {
        const string book = "OBSERVED_PEAK";
        Seed seed = new Seed("OBSERVED-ACCOUNT", 50, 20, 100);
        WriteSeed(book, seed);

        Guid lease;
        string reason;
        Expect(PropRouter.Register(book, 1, seed.Account,
            seed.Start, seed.Drawdown, 100,
            1, 1, ConfigKey, out lease, out reason),
            "registration failed: " + reason);

        Expect(PropRouter.PublishEquity(book, 1, lease, 100, true, out reason),
            "initial current-equity publish failed: " + reason);
        Expect(PropRouter.PublishStatus(book, 1, lease, SeatStatus.Free, true, out reason),
            "status publish failed: " + reason);
        Expect(PropRouter.ObservePeak(book, 1, lease, 120, out reason),
            "transient high observation failed: " + reason);

        SeatSnapshot afterObservation = ReadSeatSnapshot(book, 1);
        ExpectNearlyEqual(100, afterObservation.Equity,
            "ObservePeak incorrectly replaced current equity");
        ExpectNearlyEqual(120, afterObservation.Peak,
            "ObservePeak did not ratchet the in-memory high-water mark");
        ExpectNearlyEqual(0, afterObservation.Headroom,
            "headroom after observation was not based on current equity 100");
        ExpectNearlyEqual(120, ReadPeak(book, seed.Account),
            "observed transient high was not persisted durably");

        Expect(PropRouter.PublishEquity(book, 1, lease, 90, true, out reason),
            "later lower current-equity publish failed: " + reason);

        SeatSnapshot afterLowerCurrent = ReadSeatSnapshot(book, 1);
        ExpectNearlyEqual(90, afterLowerCurrent.Equity,
            "later current equity was not recorded as 90");
        ExpectNearlyEqual(120, afterLowerCurrent.Peak,
            "later lower current equity reduced the high-water mark");
        ExpectNearlyEqual(-10, afterLowerCurrent.Headroom,
            "headroom did not reflect current 90 against the peak-derived floor 100");
        ExpectNearlyEqual(120, ReadPeak(book, seed.Account),
            "later lower current equity reduced the durable peak");
        ExpectContains(PropRouter.Describe(book), "hr=-10", "described current headroom");
    }

    private static void TestOverridePeakIsMonotonic()
    {
        const string book = "MONOTONIC_OVERRIDE";
        Seed seed = new Seed("MONOTONIC-ACCOUNT", 50, 20, 120);
        WriteSeed(book, seed);

        Guid lease;
        string reason;
        Expect(PropRouter.Register(book, 1, seed.Account,
            seed.Start, seed.Drawdown, 100,
            1, 1, ConfigKey, out lease, out reason),
            "registration failed: " + reason);

        bool overridden = PropRouter.OverridePeak(book, 1, lease, 119, out reason);
        Expect(!overridden, "OverridePeak accepted a decrease from 120 to 119");
        ExpectContains(reason, "trusted peak", "monotonic override rejection reason");
        ExpectNearlyEqual(120, ReadPeak(book, seed.Account),
            "rejected decrease changed the durable peak");
        ExpectNearlyEqual(120, ReadSeatSnapshot(book, 1).Peak,
            "rejected decrease changed the in-memory peak");
    }

    private static void TestUnsafeBookIdentifier()
    {
        Guid lease;
        string reason;
        bool registered = PropRouter.Register("UNSAFE/../BOOK", 1, "UNSAFE-ACCOUNT",
            25000, 1500, 100,
            1, 1, ConfigKey, out lease, out reason);

        Expect(!registered, "unsafe path-like book identifier was accepted");
        Expect(lease == Guid.Empty, "unsafe book registration returned a lease");
        ExpectContains(reason, "only ASCII letters", "unsafe book rejection reason");
    }

    private static void TestStaleLeaseCallbacksAreIgnored()
    {
        const string book = "LEASE_TAKEOVER";
        Seed seed = new Seed("LEASE-ACCOUNT", 25000, 1500, 26000);
        WriteSeed(book, seed);

        double originalStaleSeconds = PropRouter.StaleSeconds;
        try
        {
            PropRouter.StaleSeconds = 0.02;

            Guid oldLease;
            string reason;
            Expect(PropRouter.Register(book, 1, seed.Account,
                seed.Start, seed.Drawdown, 100,
                1, 1, ConfigKey, out oldLease, out reason),
                "initial lease registration failed: " + reason);

            Thread.Sleep(120);

            Guid newLease;
            Expect(PropRouter.Register(book, 1, seed.Account,
                seed.Start, seed.Drawdown, 100,
                1, 1, ConfigKey, out newLease, out reason),
                "stale Free-seat takeover was not permitted: " + reason);
            Expect(newLease != Guid.Empty && newLease != oldLease,
                "takeover did not issue a distinct new lease");

            bool accepted = PropRouter.PublishEquity(book, 1, oldLease, 99999, true, out reason);
            Expect(!accepted, "old lease equity callback was accepted");
            ExpectContains(reason, "stale or invalid lease ignored", "old equity callback reason");

            accepted = PropRouter.PublishStatus(book, 1, oldLease, SeatStatus.Pending, true, out reason);
            Expect(!accepted, "old lease status callback was accepted");
            ExpectContains(reason, "stale or invalid lease ignored", "old status callback reason");

            accepted = PropRouter.Unregister(book, 1, oldLease, out reason);
            Expect(!accepted, "old lease unregistered the replacement owner");
            ExpectContains(reason, "stale or invalid lease ignored", "old unregister reason");

            // The rejected old callback must not ratchet or persist a bogus peak.
            ExpectNearlyEqual(seed.Peak, ReadPeak(book, seed.Account),
                "old lease callback mutated the durable peak");

            PropRouter.StaleSeconds = originalStaleSeconds;
            PublishReady(book, 1, newLease, 25500);
            bool granted = PropRouter.TryClaim(book, 1, newLease,
                new DateTime(2026, 8, 17, 3, 0, 0), 1, 0, out reason);
            Expect(granted, "new lease could not claim after rejecting old callbacks: " + reason);
        }
        finally
        {
            PropRouter.StaleSeconds = originalStaleSeconds;
        }
    }

    private static void TestBlockedOrderWriteFailureIsVisible()
    {
        string originalUserDataDir = NinjaTrader.Core.Globals.UserDataDir;
        string blockingFile = Path.Combine(originalUserDataDir, "not-a-user-data-directory");
        File.WriteAllText(blockingFile, "This file deliberately prevents creation of PropRouter beneath it.");

        try
        {
            NinjaTrader.Core.Globals.UserDataDir = blockingFile;
            string savedPath = PropRouter.RecordBlockedOrder(
                "WRITE_FAILURE", 1, "ACCOUNT-1", "Long1_1", "ORDER-123", "Unknown",
                1, 0, new DateTime(2026, 8, 28, 12, 0, 0));

            Expect(string.IsNullOrEmpty(savedPath),
                "a failed blocked-order write incorrectly returned a saved path: " + savedPath);
        }
        finally
        {
            NinjaTrader.Core.Globals.UserDataDir = originalUserDataDir;
        }
    }

    private static void PublishReady(string book, int id, Guid lease, double equity)
    {
        string reason;
        Expect(PropRouter.PublishEquity(book, id, lease, equity, true, out reason),
            "seat " + id + " equity publish failed: " + reason);
        Expect(PropRouter.PublishStatus(book, id, lease, SeatStatus.Free, true, out reason),
            "seat " + id + " status publish failed: " + reason);
    }

    private static Seed[] SixSeeds(string prefix)
    {
        return Enumerable.Range(1, 6)
            .Select(id => new Seed(prefix + "-" + id, 25000, 1500, 26000))
            .ToArray();
    }

    private static void WriteSeed(string book, params Seed[] seeds)
    {
        List<string> lines = new List<string> { Header };
        lines.AddRange(seeds.Select(seed => string.Format(CultureInfo.InvariantCulture,
            "{0},{1:R},{2:R},{3:R},{4}",
            seed.Account, seed.Start, seed.Drawdown, seed.Peak, Stamp)));
        WriteRaw(book, string.Join(Environment.NewLine, lines) + Environment.NewLine);
    }

    private static void WriteRaw(string book, string contents)
    {
        string path = PeakPath(book);
        Directory.CreateDirectory(Path.GetDirectoryName(path));
        File.WriteAllText(path, contents);
    }

    private static void AssertSeedFile(string book, IEnumerable<string> expectedAccounts, int expectedRows)
    {
        string[] lines = File.ReadAllLines(PeakPath(book));
        Expect(lines.Length == expectedRows + 1,
            "expected " + expectedRows + " seed rows for " + book + ", found " + (lines.Length - 1));
        Expect(lines[0].TrimStart('\uFEFF') == Header, "seed header changed for " + book);

        HashSet<string> actual = new HashSet<string>(
            lines.Skip(1).Select(line => line.Split(',')[0]),
            StringComparer.OrdinalIgnoreCase);
        HashSet<string> expected = new HashSet<string>(expectedAccounts, StringComparer.OrdinalIgnoreCase);
        Expect(actual.SetEquals(expected),
            "durable accounts differ for " + book + ": actual=[" + string.Join(",", actual) + "]");
    }

    private static double ReadPeak(string book, string account)
    {
        foreach (string line in File.ReadAllLines(PeakPath(book)).Skip(1))
        {
            string[] fields = line.Split(',');
            if (fields.Length == 5 && string.Equals(fields[0], account, StringComparison.OrdinalIgnoreCase))
                return double.Parse(fields[3], NumberStyles.Float, CultureInfo.InvariantCulture);
        }
        throw new InvalidOperationException("No durable peak row for " + account + " in " + book);
    }

    private static SeatSnapshot ReadSeatSnapshot(string book, int instanceId)
    {
        FieldInfo booksField = typeof(PropRouter).GetField(
            "books", BindingFlags.NonPublic | BindingFlags.Static);
        Expect(booksField != null, "could not reflect PropRouter.books");

        IDictionary books = booksField.GetValue(null) as IDictionary;
        Expect(books != null && books.Contains(book), "reflected book was not found: " + book);

        object reflectedBook = books[book];
        FieldInfo seatsField = reflectedBook.GetType().GetField(
            "Seats", BindingFlags.Public | BindingFlags.Instance);
        Expect(seatsField != null, "could not reflect PropBook.Seats");

        IDictionary seats = seatsField.GetValue(reflectedBook) as IDictionary;
        Expect(seats != null && seats.Contains(instanceId),
            "reflected seat was not found: " + instanceId);

        PropSeat seat = seats[instanceId] as PropSeat;
        Expect(seat != null, "reflected seat had an unexpected type");
        return new SeatSnapshot
        {
            Equity = seat.Equity,
            Peak = seat.Peak,
            Headroom = seat.Headroom,
            Status = seat.Status
        };
    }

    private static string PeakPath(string book)
    {
        return Path.Combine(NinjaTrader.Core.Globals.UserDataDir,
            "PropRouter", "peaks_" + book + ".csv");
    }

    private static void Expect(bool condition, string message)
    {
        if (!condition)
            throw new InvalidOperationException(message);
    }

    private static void ExpectContains(string actual, string expected, string label)
    {
        if (actual == null || actual.IndexOf(expected, StringComparison.OrdinalIgnoreCase) < 0)
            throw new InvalidOperationException(label + " did not contain '" + expected + "'; actual='" + actual + "'");
    }

    private static void ExpectNearlyEqual(double expected, double actual, string message)
    {
        if (Math.Abs(expected - actual) > 0.0000001)
            throw new InvalidOperationException(message + "; expected="
                + expected.ToString("R", CultureInfo.InvariantCulture) + ", actual="
                + actual.ToString("R", CultureInfo.InvariantCulture));
    }
}
