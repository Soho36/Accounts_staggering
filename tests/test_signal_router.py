import unittest

import pandas as pd

from account_farming import Harvest, Rule
from addiotional_helpers.signal_router import (
    POLICIES,
    Period,
    RouteCfg,
    capacity_measure,
    simulate_route,
)


def offer_frame(rows):
    values = []
    for offer_id, (entry, exit_time, net, mae, mfe, risk) in enumerate(rows):
        values.append({
            "offer_id": offer_id,
            "Entry_time": pd.Timestamp(entry),
            "Exit_time": pd.Timestamp(exit_time),
            "net": float(net),
            "MAE": float(mae),
            "MFE": float(mfe),
            "stop_risk": float(risk),
        })
    return pd.DataFrame(values)


class CapacityTests(unittest.TestCase):
    def test_r_copies_require_r_times_interval_depth(self):
        offers = offer_frame([
            ("2023-01-01 10:00", "2023-01-01 11:00", 10, -1, 2, 20),
            ("2023-01-01 10:10", "2023-01-01 11:10", 20, -1, 2, 30),
            ("2023-01-01 10:20", "2023-01-01 11:20", 30, -1, 2, 40),
        ])
        period = Period("test", pd.Timestamp("2023-01-01"),
                        pd.Timestamp("2023-01-02"), "test")

        full = capacity_measure(offers, period, k=6, copies=2)
        short = capacity_measure(offers, period, k=5, copies=2)

        self.assertEqual(full["raw_peak_overlap"], 3)
        self.assertEqual(full["filled_copies"], 6)
        self.assertTrue(full["mechanical_full_capacity"])
        self.assertEqual(short["filled_copies"], 5)
        self.assertFalse(short["mechanical_full_capacity"])


class RoutedEconomicsTests(unittest.TestCase):
    def setUp(self):
        self.period = Period("test", pd.Timestamp("2023-01-01"),
                             pd.Timestamp("2023-01-03"), "test")

    def test_fixed_r_has_identical_exposure_across_causal_policies_when_full(self):
        offers = offer_frame([
            ("2023-01-01 10:00", "2023-01-01 11:00", 10, -1, 2, 20),
            ("2023-01-01 10:10", "2023-01-01 10:40", -5, -6, 1, 10),
        ])
        rule = Rule(dd=1_000, frozen_floor=0, cost=20)
        harvest = Harvest("none")
        outcomes = []
        for policy in POLICIES:
            outcomes.append(simulate_route(
                offers, self.period,
                RouteCfg(k=4, copies=2, policy=policy,
                         replacement_reserve=100, split=1.0),
                harvest, rule,
            ))

        self.assertEqual({row["filled_copies"] for row in outcomes}, {4})
        self.assertEqual({row["completed_fills"] for row in outcomes}, {4})
        self.assertEqual({row["delivered_raw_net"] for row in outcomes}, {10.0})
        self.assertEqual({row["initial_stop_risk"] for row in outcomes}, {60.0})
        self.assertEqual({row["contract_hours"] for row in outcomes}, {3.0})

    def test_dead_seat_is_replaced_from_reserved_cash_and_can_trade_later(self):
        offers = offer_frame([
            ("2023-01-01 10:00", "2023-01-01 10:30", -5, -11, 0, 10),
            ("2023-01-01 11:00", "2023-01-01 11:30", 5, -1, 5, 10),
        ])
        rule = Rule(dd=10, frozen_floor=0, cost=2)
        result = simulate_route(
            offers, self.period,
            RouteCfg(k=1, copies=1, policy="round_robin",
                     replacement_reserve=2, split=1.0),
            Harvest("none"), rule,
        )

        self.assertEqual(result["filled_copies"], 2)
        self.assertEqual(result["deaths"], 1)
        self.assertEqual(result["replacements"], 1)
        self.assertEqual(result["terminal_live_seats"], 1)
        self.assertEqual(result["total_seat_spend"], 4)
        self.assertEqual(result["initial_capital"], 4)

    def test_cross_horizon_trade_consumes_capacity_but_outcome_is_unscored(self):
        offers = offer_frame([
            ("2023-01-02 20:00", "2023-01-03 02:00", 100, -1, 100, 20),
            ("2023-01-02 21:00", "2023-01-02 22:00", 50, -1, 50, 20),
        ])
        result = simulate_route(
            offers, self.period,
            RouteCfg(k=1, copies=1, policy="round_robin",
                     replacement_reserve=0, split=1.0),
            Harvest("none"), Rule(dd=1_000, frozen_floor=0, cost=20),
        )

        self.assertEqual(result["filled_copies"], 1)
        self.assertEqual(result["completed_fills"], 0)
        self.assertEqual(result["delivered_raw_net"], 0)
        self.assertEqual(result["contract_hours"], 4.0)

    def test_same_timestamp_deaths_form_one_cluster_and_one_rebuy_event(self):
        offers = offer_frame([
            ("2023-01-01 10:00", "2023-01-01 11:00", -5, -11, 0, 10),
            ("2023-01-01 10:01", "2023-01-01 11:00", -5, -11, 0, 10),
        ])
        result = simulate_route(
            offers, self.period,
            RouteCfg(k=6, copies=3, policy="round_robin",
                     replacement_reserve=20, split=1.0),
            Harvest("none"), Rule(dd=10, frozen_floor=0, cost=2),
        )

        self.assertEqual(result["deaths"], 6)
        self.assertEqual(result["worst_death_cluster"], 6)
        self.assertEqual(result["large_shock_events"], 1)
        self.assertEqual(result["zero_seat_events"], 1)
        self.assertEqual(result["min_live_seats"], 0)
        self.assertEqual(result["replacements"], 1)

    def test_unsorted_offer_frame_routes_chronologically(self):
        offers = offer_frame([
            ("2023-01-01 11:00", "2023-01-01 12:00", 20, -1, 20, 10),
            ("2023-01-01 10:00", "2023-01-01 10:30", 10, -1, 10, 10),
        ])
        result = simulate_route(
            offers, self.period,
            RouteCfg(k=1, copies=1, policy="round_robin",
                     replacement_reserve=0, split=1.0),
            Harvest("none"), Rule(dd=1_000, frozen_floor=0, cost=20),
        )
        self.assertEqual(result["filled_copies"], 2)
        self.assertEqual(result["delivered_raw_net"], 30.0)


if __name__ == "__main__":
    unittest.main()
