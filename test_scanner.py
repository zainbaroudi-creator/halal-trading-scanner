import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scanner import AccountRules, AlertCooldown, DailyWatchlistState, Filters, TradingViewFloatData, format_daily_watchlist, format_practice_telegram, format_telegram, load_halal_universe, pullback_signal, relative_volume, size_position, us_market_phase


class ScannerTests(unittest.TestCase):
    def test_price_range_is_one_to_twenty_dollars(self):
        self.assertEqual(Filters().min_price, 1.0)
        self.assertEqual(Filters().max_price, 20.0)

    def test_premarket_and_regular_volume_thresholds(self):
        self.assertEqual(Filters().min_premarket_relative_volume, 1.0)
        self.assertEqual(Filters().min_relative_volume, 5.0)

    def test_spread_limit_is_two_percent(self):
        self.assertEqual(Filters().max_spread_pct, 2.0)

    def test_alert_cooldown_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.json"
            first = AlertCooldown(path, hours=2)
            first.mark(["ABC"], now=1_000)
            restarted = AlertCooldown(path, hours=2)
            self.assertFalse(restarted.allowed("ABC", now=1_000 + 60 * 60))
            self.assertTrue(restarted.allowed("ABC", now=1_000 + 2 * 60 * 60))

    def test_daily_watchlist_is_once_per_adelaide_date(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "daily.json"
            before = datetime(2026, 8, 25, 11, 59, tzinfo=timezone.utc)
            after = datetime(2026, 8, 25, 12, 1, tzinfo=timezone.utc)
            state = DailyWatchlistState(path)
            self.assertFalse(state.due(before))
            self.assertTrue(state.due(after))
            state.mark(after)
            self.assertFalse(DailyWatchlistState(path).due(after))

    def test_daily_watchlist_deduplicates_symbols(self):
        row = {
            "symbol": "TEST", "price": 6.2, "change_pct": 32.5,
            "relative_volume": 8.4, "float_millions": 12.5,
        }
        message = format_daily_watchlist([row, row])
        self.assertEqual(message.count("1. TEST"), 1)
        self.assertNotIn("2. TEST", message)
        self.assertIn("not a prediction", message)

    def test_halal_allowlist(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "symbols.csv"
            path.write_text("symbol,halal,company,float_millions\nABC,true,A,12\nBAD,false,B,2\n")
            universe = load_halal_universe(path)
            self.assertEqual(list(universe), ["ABC"])
            self.assertEqual(universe["ABC"]["float_millions"], 12)

    def test_relative_volume(self):
        bars = [{"v": 100}] * 20 + [{"v": 500}]
        self.assertEqual(relative_volume(500, bars), 5)

    def test_two_hundred_dollar_position_sizing(self):
        sizing = size_position(5.0, 4.9, AccountRules())
        self.assertEqual(sizing["shares"], 10)
        self.assertAlmostEqual(sizing["planned_risk"], 1.0)
        self.assertAlmostEqual(sizing["position_value"], 50.0)

    def test_telegram_alert_is_simple_and_requires_zoya(self):
        rows = [{
            "symbol": "TEST", "price": 6.2, "change_pct": 32.5,
            "relative_volume": 8.4, "float_millions": 12.5,
            "setup": {"status": "WATCH", "trigger": 6.28, "stop": 6.05,
                      "target_low": 6.74, "target_high": 6.97, "chase_pct": 0},
        }]
        message = format_telegram(rows)
        self.assertIn("TEST\n", message)
        self.assertIn("Confirm halal status in Zoya", message)
        self.assertNotIn("cash sizing", message)
        self.assertNotIn("daily stop", message)

    def test_telegram_never_sends_unknown_float(self):
        rows = [{
            "symbol": "TEST", "price": 6.2, "change_pct": 32.5,
            "relative_volume": 8.4, "float_millions": None,
            "setup": {"status": "WATCH", "trigger": 6.28, "stop": 6.05,
                      "target_low": 6.74, "target_high": 6.97, "chase_pct": 0},
        }]
        self.assertEqual(format_telegram(rows), "")

    def test_qualified_stock_alert_does_not_require_pullback(self):
        rows = [{
            "symbol": "TEST", "price": 6.2, "change_pct": 32.5,
            "relative_volume": 8.4, "float_millions": 12.5,
            "setup": {"status": "WAIT", "reason": "no clean pullback"},
        }]
        message = format_telegram(rows)
        self.assertIn("QUALIFIED MOMENTUM STOCK", message)
        self.assertIn("No automated entry recommendation yet", message)
        self.assertNotIn("Trigger:", message)

    def test_float_data_is_cached_and_stale_value_survives_outage(self):
        with tempfile.TemporaryDirectory() as folder:
            source = TradingViewFloatData(Path(folder) / "float-cache.json")
            source._fetch = lambda symbols: {"ABC": 8.25}
            self.assertEqual(source.get_float_millions(["ABC"]), {"ABC": 8.25})
            source.cache["ABC"]["fetched_at"] = 0

            def unavailable(symbols):
                raise OSError("temporary outage")

            source._fetch = unavailable
            self.assertEqual(source.get_float_millions(["ABC"]), {"ABC": 8.25})

    def test_us_market_phase(self):
        self.assertEqual(us_market_phase(datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc)), "PREMARKET")
        self.assertEqual(us_market_phase(datetime(2026, 8, 25, 14, 0, tzinfo=timezone.utc)), "REGULAR")
        self.assertEqual(us_market_phase(datetime(2026, 8, 25, 22, 0, tzinfo=timezone.utc)), "CLOSED")

    def test_practice_alert_cannot_be_mistaken_for_entry_signal(self):
        row = {
            "symbol": "TEST", "price": 4.2, "change_pct": 18.0,
            "relative_volume": 0.8, "float_millions": 3.1,
            "setup": {"status": "WAIT", "reason": "no clean pullback"},
        }
        message = format_practice_telegram(row)
        self.assertIn("PRACTICE WATCH — PAPER TRADE ONLY", message)
        self.assertIn("No clean pullback yet", message)
        self.assertNotIn("Trigger:", message)

    def test_pullback_trigger(self):
        bars = [
            {"o": 5.0, "h": 5.3, "l": 4.9, "c": 5.25},
            {"o": 5.25, "h": 5.6, "l": 5.2, "c": 5.55},
            {"o": 5.55, "h": 5.8, "l": 5.5, "c": 5.75},
            {"o": 5.75, "h": 5.76, "l": 5.55, "c": 5.60},
            {"o": 5.60, "h": 5.62, "l": 5.45, "c": 5.50},
            {"o": 5.50, "h": 5.66, "l": 5.48, "c": 5.64},
        ]
        result = pullback_signal(bars, Filters())
        self.assertEqual(result["status"], "TRIGGERED")
        self.assertEqual(result["entry"], 5.62)
        self.assertEqual(result["stop"], 5.45)
        self.assertAlmostEqual(result["target_low"], 5.96)
        self.assertAlmostEqual(result["target_high"], 6.13)


if __name__ == "__main__":
    unittest.main()
