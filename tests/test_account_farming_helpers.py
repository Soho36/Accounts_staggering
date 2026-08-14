import argparse
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from account_farming import (
    BookCfg,
    Harvest,
    Rule,
    _filter_part_dates,
    book_terminal_values,
    build_stream,
    death_event_metrics,
    new_account,
    read_trade_file,
    replay_period,
    run_book,
    should_start,
)


class DeathEventMetricsTests(unittest.TestCase):
    def test_partial_large_shock_from_twenty_to_one(self):
        self.assertEqual(
            death_event_metrics(20, 1),
            {
                "deaths": 19,
                "fraction": 0.95,
                "full_wipe": False,
                "large_shock": True,
            },
        )

    def test_six_seat_full_wipe_is_a_large_shock(self):
        self.assertEqual(
            death_event_metrics(6, 0),
            {
                "deaths": 6,
                "fraction": 1.0,
                "full_wipe": True,
                "large_shock": True,
            },
        )

    def test_four_seat_full_wipe_is_below_large_shock_threshold(self):
        self.assertEqual(
            death_event_metrics(4, 0),
            {
                "deaths": 4,
                "fraction": 1.0,
                "full_wipe": True,
                "large_shock": False,
            },
        )


class BookTerminalValuesTests(unittest.TestCase):
    def test_cash_endpoints_exclude_non_cashable_equity(self):
        rule = Rule(dd=2500.0, frozen_floor=100.0)
        cfg = BookCfg(seed=1200.0, split=0.8)
        live = [
            {"eq": 3600.0, "frozen": True},   # $1,000 above Safety Net
            {"eq": 2500.0, "frozen": True},   # below Safety Net: no cash value
            {"eq": 5000.0, "frozen": False},  # continuation value only
        ]

        values = book_terminal_values(
            live, cash=600.0, spent=400.0, cfg=cfg, rule=rule
        )

        self.assertEqual(values["equity"], 8880.0)
        self.assertEqual(values["withdrawable"], 800.0)
        self.assertEqual(values["realized_wealth"], -1000.0)
        self.assertEqual(values["cashout_wealth"], -200.0)
        self.assertEqual(values["wealth"], 7880.0)


class FilterPartDatesTests(unittest.TestCase):
    def test_entry_boundaries_keep_cross_end_trade_for_replay(self):
        part = {
            "en": np.array(
                [
                    "2022-12-31T23:59:00",
                    "2023-01-01T00:00:00",
                    "2023-01-31T23:59:59",
                    "2023-02-01T00:00:00",
                ],
                dtype="datetime64[s]",
            ),
            # The first trade exits inside the evaluation period, but its entry
            # precedes the boundary and must not survive the fresh-start filter.
            "ex": np.array(
                [
                    "2023-01-02T12:00:00",
                    "2023-01-01T01:00:00",
                    "2023-02-01T01:00:00",
                    "2023-02-01T02:00:00",
                ],
                dtype="datetime64[s]",
            ),
            "net": np.array([10.0, 20.0, 30.0, 40.0]),
            "mae": np.array([-1.0, -2.0, -3.0, -4.0]),
            "mfe": np.array([1.0, 2.0, 3.0, 4.0]),
        }

        filtered = _filter_part_dates(
            part, start_date="2023-01-01", end_date="2023-01-31"
        )

        np.testing.assert_array_equal(
            filtered["en"],
            np.array(["2023-01-01T00:00:00", "2023-01-31T23:59:59"],
                     dtype="datetime64[s]"),
        )
        np.testing.assert_array_equal(
            filtered["ex"],
            np.array(["2023-01-01T01:00:00", "2023-02-01T01:00:00"],
                     dtype="datetime64[s]"),
        )
        np.testing.assert_array_equal(filtered["net"], np.array([20.0, 30.0]))
        np.testing.assert_array_equal(filtered["mae"], np.array([-2.0, -3.0]))
        np.testing.assert_array_equal(filtered["mfe"], np.array([2.0, 3.0]))


class InputValidationTests(unittest.TestCase):
    def test_reader_accepts_exact_ea_header_and_comma_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ea_export.csv"
            path.write_text(
                "ticket,entry_time,exit_time,mae_money,mfe_money,trade_profit,candle_range\n"
                "1,2023-01-02 10:00:00,2023-01-02 11:00:00,-25.5,80.0,50.0,100\n",
                encoding="utf-8",
            )
            frame = read_trade_file(path)

        self.assertEqual(list(frame.columns),
                         ["Ticket", "Entry_time", "Exit_time", "MAE", "MFE", "PNL",
                          "Candle_range"])
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0]["MAE"], -25.5)
        self.assertEqual(frame.iloc[0]["MFE"], 80.0)
        self.assertEqual(frame.iloc[0]["PNL"], 50.0)
        self.assertEqual(frame.iloc[0]["Candle_range"], 100.0)

    def test_strict_stream_rejects_tester_truncated_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sweep = root / "RR" / "16-17"
            stats = root / "RR_stats" / "16-17"
            sweep.mkdir(parents=True)
            stats.mkdir(parents=True)
            (sweep / "16-17_2.00.csv").write_text(
                "1\t2023-01-02 10:00:00\t2023-01-02 11:00:00\t-25\t80\t50\n",
                encoding="utf-8",
            )
            (stats / "16-17_2.00_stats.csv").write_text(
                "net_profit\n-4920\n", encoding="utf-8"
            )
            args = argparse.Namespace(
                input_csv=None, sweep_root=root / "RR",
                stats_root=root / "RR_stats", rr=2.0, windows="all",
                start_date=None, end_date=None, allow_incomplete=False,
            )

            with self.assertRaisesRegex(ValueError, "16-17"):
                build_stream(args)


class StartPolicyTests(unittest.TestCase):
    def test_withdrawal_does_not_masquerade_as_trading_drawdown(self):
        account = new_account(0, Rule())
        account.update({
            "eq": 2600.0, "peak": 4000.0,       # payout lowered account equity
            "trade_eq": 4000.0, "trade_peak": 4000.0,
        })
        cfg = BookCfg(policy="dd", dd_trigger=400.0, min_days=1)

        self.assertFalse(should_start(
            [account], pd.Timestamp("2023-02-01"),
            pd.Timestamp("2023-01-01"), cfg,
        ))

        account["trade_eq"] = 3500.0
        self.assertTrue(should_start(
            [account], pd.Timestamp("2023-02-01"),
            pd.Timestamp("2023-01-01"), cfg,
        ))

    def test_profit_trigger_uses_trading_pnl_not_post_payout_equity(self):
        account = new_account(0, Rule())
        account.update({"eq": 200.0, "trade_eq": 1500.0, "trade_peak": 1500.0})
        cfg = BookCfg(policy="profit", profit_trigger=1000.0, min_days=1)
        self.assertTrue(should_start(
            [account], pd.Timestamp("2023-02-01"),
            pd.Timestamp("2023-01-01"), cfg,
        ))

    def test_dd_event_that_kills_last_seat_can_buy_replacement(self):
        ex = np.array(["2023-01-01", "2023-02-01"], dtype="datetime64[s]")
        net = np.array([0.0, -100.0])
        mae = np.array([0.0, -2600.0])
        mfe = np.array([0.0, 0.0])
        cfg = BookCfg(seats=1, seed=400.0, policy="dd", dd_trigger=400.0,
                      min_days=1, max_per_event=1, funding="cash")
        result = run_book(ex, net, mae, mfe, Harvest("none"), Rule(cost=200.0), cfg)

        self.assertEqual(result["deaths"], 1)
        self.assertEqual(result["bought"], 2)
        self.assertEqual(result["starts"], 2)
        self.assertEqual(result["live"], 1)
        self.assertFalse(result["ruined"])

    def test_funded_profit_policy_restarts_after_last_seat_dies(self):
        ex = np.array(["2023-01-01", "2023-01-02", "2023-02-01"],
                      dtype="datetime64[s]")
        net = np.array([0.0, -100.0, 0.0])
        mae = np.array([0.0, -2600.0, 0.0])
        mfe = np.zeros(3)
        cfg = BookCfg(seats=1, seed=400.0, policy="profit",
                      profit_trigger=1000.0, min_days=10,
                      max_per_event=1, funding="cash")
        result = run_book(ex, net, mae, mfe, Harvest("none"), Rule(cost=200.0), cfg)

        self.assertEqual(result["deaths"], 1)
        self.assertEqual(result["bought"], 2)
        self.assertEqual(result["live"], 1)
        self.assertFalse(result["ruined"])


class PeriodReplayTests(unittest.TestCase):
    def test_period_replay_starts_flat_before_resolving_blocking(self):
        part = {
            "en": np.array(["2022-12-31T23:00", "2023-01-01T01:00"],
                           dtype="datetime64[s]"),
            "ex": np.array(["2023-01-01T02:00", "2023-01-01T01:30"],
                           dtype="datetime64[s]"),
            "net": np.array([-10.0, 20.0]),
            "mae": np.array([-5.0, -2.0]),
            "mfe": np.array([1.0, 4.0]),
        }
        period = replay_period([part], pd.Timestamp("2023-01-01"),
                               pd.Timestamp("2023-02-01"))

        np.testing.assert_array_equal(period["net"], np.array([20.0]))

    def test_cross_horizon_position_still_blocks_later_entry(self):
        part = {
            "en": np.array(["2023-01-31T20:00", "2023-01-31T21:00"],
                           dtype="datetime64[s]"),
            "ex": np.array(["2023-02-01T02:00", "2023-01-31T22:00"],
                           dtype="datetime64[s]"),
            "net": np.array([10.0, 20.0]),
            "mae": np.array([-1.0, -2.0]),
            "mfe": np.array([2.0, 4.0]),
        }
        with self.assertRaisesRegex(ValueError, "No trades remain"):
            replay_period([part], pd.Timestamp("2023-01-01"),
                          pd.Timestamp("2023-02-01"))


if __name__ == "__main__":
    unittest.main()
