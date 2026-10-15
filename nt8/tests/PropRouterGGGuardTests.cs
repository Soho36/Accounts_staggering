using System;
using System.Collections.Generic;
using System.IO;
using NinjaTrader.NinjaScript.AddOns;

// Tests that exist only for the GG router: its separate state folder and the
// cross-router account guard. Everything the two routers share is covered by the
// generated PropRouterGGRegressionTests.cs, which runs the whole RTL suite
// against PropRouterGG.
internal static class PropRouterGGGuardTests
{
    private const string Header = "account,start_balance,drawdown,peak,updated_utc";
    private const string Stamp = "2026-10-15T00:00:00Z";
    private const string ConfigKey = "instrument=TEST;bars=Minute:30;windows=canonical";

    private static int passed;
    private static int failed;

    private static int Main(string[] args)
    {
        if (args.Length != 1 || string.IsNullOrWhiteSpace(args[0]))
        {
            Console.Error.WriteLine("Usage: PropRouterGGGuardTests.exe <temporary-user-data-directory>");
            return 2;
        }

        NinjaTrader.Core.Globals.UserDataDir = Path.GetFullPath(args[0]);
        Directory.CreateDirectory(NinjaTrader.Core.Globals.UserDataDir);

        Run("GG seeds come from PropRouterGG, never from the RTL folder", TestSeparateStateFolder);
        Run("an account listed in an RTL peak file is refused", TestRtlSeatIsRefused);
        Run("the RTL match uses router account normalization", TestRtlMatchIsNormalized);
        Run("an account absent from every RTL peak file registers", TestNonRtlAccountRegisters);
        Run("a missing RTL folder is not an error", TestMissingRtlFolder);
        Run("only peaks_*.csv counts, not backups or longer extensions", TestOnlyPeakFilesCount);
        Run("an unreadable RTL peak file fails closed", TestUnreadableRtlFileFailsClosed);

        Console.WriteLine();
        Console.WriteLine("Result: {0} passed, {1} failed", passed, failed);
        return failed == 0 ? 0 : 1;
    }

    private static void Run(string name, Action test)
    {
        try
        {
            ResetRtlFolder();
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

    private static void TestSeparateStateFolder()
    {
        WriteSeed(GgFolder(), "GG_SEEDED", "GGS-1");
        string reason = RegisterExpectingSuccess("GG_SEEDED", "GGS-1");
        ExpectContains(reason, "peak seeded", "GG seed read from PropRouterGG");

        // A book whose peak file exists only in the RTL folder is not seeded by GG.
        WriteSeed(RtlFolder(), "GG_RTL_ONLY", "SOMEONE-ELSE");
        reason = RegisterExpectingSuccess("GG_RTL_ONLY", "GGS-2");
        ExpectContains(reason, "NOT SEEDED", "GG ignored the RTL folder for its own seeds");
    }

    private static void TestRtlSeatIsRefused()
    {
        WriteSeed(RtlFolder(), "LIVE", "PA-APEX-1");
        WriteSeed(GgFolder(), "GG_A", "PA-APEX-1");
        string reason = RegisterExpectingRefusal("GG_A", "PA-APEX-1");
        ExpectContains(reason, "RTL router seat", "refusal reason");
        ExpectContains(reason, "peaks_LIVE.csv", "refusal names the RTL file");
    }

    private static void TestRtlMatchIsNormalized()
    {
        WriteSeed(RtlFolder(), "LIVE", "pa-apex-2");
        WriteSeed(GgFolder(), "GG_B", "PA-APEX-2");
        string reason = RegisterExpectingRefusal("GG_B", "PA-APEX-2!Apex!Apex");
        ExpectContains(reason, "RTL router seat", "normalized refusal reason");
    }

    private static void TestNonRtlAccountRegisters()
    {
        WriteSeed(RtlFolder(), "LIVE", "PA-APEX-3");
        WriteSeed(GgFolder(), "GG_C", "PA-APEX-4");
        string reason = RegisterExpectingSuccess("GG_C", "PA-APEX-4");
        ExpectContains(reason, "peak seeded", "non-RTL account");
    }

    private static void TestMissingRtlFolder()
    {
        Directory.Delete(RtlFolder(), true);
        WriteSeed(GgFolder(), "GG_D", "PA-APEX-5");
        string reason = RegisterExpectingSuccess("GG_D", "PA-APEX-5");
        ExpectContains(reason, "peak seeded", "missing RTL folder");
    }

    private static void TestOnlyPeakFilesCount()
    {
        Directory.CreateDirectory(RtlFolder());
        string body = Header + Environment.NewLine + Row("PA-APEX-6") + Environment.NewLine;
        File.WriteAllText(Path.Combine(RtlFolder(), "peaks_OLD.csv.bak"), body);
        File.WriteAllText(Path.Combine(RtlFolder(), "peaks_OLD.csvx"), body);
        WriteSeed(GgFolder(), "GG_E", "PA-APEX-6");
        string reason = RegisterExpectingSuccess("GG_E", "PA-APEX-6");
        ExpectContains(reason, "peak seeded", "backup files ignored");
    }

    private static void TestUnreadableRtlFileFailsClosed()
    {
        WriteSeed(RtlFolder(), "LOCKED", "SOMEONE-ELSE-7");
        WriteSeed(GgFolder(), "GG_F", "PA-APEX-7");
        string path = Path.Combine(RtlFolder(), "peaks_LOCKED.csv");
        using (new FileStream(path, FileMode.Open, FileAccess.ReadWrite, FileShare.None))
        {
            string reason = RegisterExpectingRefusal("GG_F", "PA-APEX-7");
            ExpectContains(reason, "cannot verify", "unreadable RTL file reason");
        }
    }

    // ------------------------------------------------------------------------

    private static string RegisterExpectingSuccess(string book, string account)
    {
        Guid lease;
        string reason;
        bool ok = PropRouterGG.Register(book, 1, account, 25000, 1500, 100,
            3, 1, ConfigKey, out lease, out reason);
        Expect(ok, "registration of " + account + " in " + book + " was refused: " + reason);
        Expect(lease != Guid.Empty, "a granted registration returned no lease");
        return reason;
    }

    private static string RegisterExpectingRefusal(string book, string account)
    {
        Guid lease;
        string reason;
        bool ok = PropRouterGG.Register(book, 1, account, 25000, 1500, 100,
            3, 1, ConfigKey, out lease, out reason);
        Expect(!ok, "registration of " + account + " in " + book + " was accepted");
        Expect(lease == Guid.Empty, "a refused registration returned a lease");
        return reason;
    }

    private static string RtlFolder()
    {
        return Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "PropRouter");
    }

    private static string GgFolder()
    {
        return Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "PropRouterGG");
    }

    private static void ResetRtlFolder()
    {
        if (Directory.Exists(RtlFolder()))
            Directory.Delete(RtlFolder(), true);
        Directory.CreateDirectory(RtlFolder());
    }

    private static string Row(string account)
    {
        return account + ",25000,1500,26000," + Stamp;
    }

    private static void WriteSeed(string folder, string book, string account)
    {
        Directory.CreateDirectory(folder);
        File.WriteAllText(Path.Combine(folder, "peaks_" + book + ".csv"),
            Header + Environment.NewLine + Row(account) + Environment.NewLine);
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
}
