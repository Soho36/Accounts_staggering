import importlib.util
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "nt8" / "make_peak_file.py"
SPEC = importlib.util.spec_from_file_location("make_peak_file", MODULE_PATH)
mpf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mpf)


def seed(peak, account="SIM101", start="100000", dd="1500"):
    start_value = Decimal(start)
    dd_value = Decimal(dd)
    peak_value = Decimal(peak)
    return {
        "account": account,
        "instance": 1,
        "start": start_value,
        "dd": dd_value,
        "kind": "intraday",
        "peak": peak_value,
        "balance": start_value,
        "floor": mpf.model_floor(peak_value, dd_value, start_value),
        "broker_floor": start_value - dd_value,
        "headroom": dd_value,
        "frozen": False,
        "freeze_at": start_value + dd_value + mpf.FROZEN_OFFSET,
        "note": "test",
    }


class PeakFileSafetyTests(unittest.TestCase):
    def test_write_is_atomic_with_previous_primary_backup_and_no_peak_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "peaks_SIM.csv"
            mpf.write_peak_file([seed("100000")], str(path), "2026-08-20T00:00:00Z")
            first = path.read_bytes()

            mpf.write_peak_file([seed("100250")], str(path), "2026-08-20T01:00:00Z")
            self.assertEqual(first, Path(str(path) + ".bak").read_bytes())

            with self.assertRaises(mpf.SeedError):
                mpf.write_peak_file(
                    [seed("100100")], str(path), "2026-08-20T02:00:00Z")
            self.assertIn(b",100250.00,", path.read_bytes())

    def test_explicit_reset_override_can_lower_peak(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "peaks_SIM.csv"
            mpf.write_peak_file([seed("100250")], str(path), "2026-08-20T00:00:00Z")
            mpf.write_peak_file(
                [seed("100000")], str(path), "2026-08-20T01:00:00Z",
                allow_peak_reset=True)
            self.assertIn(b",100000.00,", path.read_bytes())

    def test_check_rejects_bad_timestamp_and_normalized_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "peaks_SIM.csv"
            path.write_text(
                mpf.HEADER + "\nSIM101,100000.00,1500.00,100000.00,not-utc\n",
                encoding="utf-8")
            with self.assertRaises(mpf.SeedError):
                mpf.check_existing([seed("100000")], str(path))

            path.write_text(
                mpf.HEADER + "\n"
                "SIM101,100000.00,1500.00,100000.00,2026-08-20T00:00:00Z\n"
                "SIM101!Playback,100000.00,1500.00,100000.00,2026-08-20T00:00:00Z\n",
                encoding="utf-8")
            with self.assertRaises(mpf.SeedError):
                mpf.check_existing([seed("100000")], str(path))

    def test_book_id_uses_router_safe_grammar(self):
        for valid in ("SIM", "PLAYBACK_ROUTER_V2", "live-1"):
            mpf.validate_book_id(valid)
        for invalid in ("", "LIVE 1", "../LIVE", "LIVE.csv"):
            with self.assertRaises(mpf.SeedError):
                mpf.validate_book_id(invalid)

    def test_config_rejects_accounts_that_collide_after_normalization(self):
        config = {
            "SIM101": {"instance": 1, "start": "100000", "dd": "1500", "kind": "intraday"},
            "SIM101!Playback": {"instance": 2, "start": "100000", "dd": "1500", "kind": "intraday"},
        }
        with self.assertRaises(mpf.SeedError):
            mpf.validate_seat_config(config, "test")


if __name__ == "__main__":
    unittest.main()
