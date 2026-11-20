import logging
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from weather_bot import PaperBot, active_group, extract_weather, load_rules


class WeatherBotTests(unittest.TestCase):
    def setUp(self):
        self.rules = Path(__file__).parent / "rules.txt"
        self.now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        self.config = {
            "station": "KMCI", "weather_field": "temp", "min_request_interval_seconds": 2,
            "rules_reload_seconds": 30,
            "groups": [{"name": "first", "minutes": [0], "window_seconds": 12, "poll_seconds": 2}],
        }
        self.logger = logging.getLogger("test-paper-bot")

    def make_bot(self, weather=None, quote=None):
        return PaperBot(self.config, self.rules, self.logger,
                        weather or (lambda: [{"icaoId": "KMCI", "temp": 12, "obsTime": 123}]),
                        quote or (lambda: {"price": "0.42", "available_size": "12"}))

    def test_rule_parsing_and_match(self):
        rule = load_rules(self.rules)[0]
        self.assertTrue(rule.matches("2026-11-20", rule.value))
        self.assertFalse(rule.matches("2026-11-21", rule.value))

    def test_invalid_rule_is_rejected(self):
        with patch.object(Path, "read_text", return_value="2026-11-20, >=, NaN, 0.55, 5\n"):
            with self.assertRaises(ValueError):
                load_rules(self.rules)

    def test_matching_observation_produces_only_one_paper_order(self):
        bot = self.make_bot()
        self.assertEqual("paper-order", bot.tick(self.now, 100, force=True))
        self.assertEqual("stale-observation", bot.tick(self.now, 101, force=True))

    def test_quote_above_limit_is_rejected(self):
        bot = self.make_bot(quote=lambda: {"price": "0.62", "available_size": "12"})
        self.assertEqual("quote-rejected", bot.tick(self.now, 100, force=True))

    def test_schedule_and_poll_interval(self):
        bot = self.make_bot()
        self.assertEqual("first", active_group(self.config, self.now))
        self.assertEqual("paper-order", bot.tick(self.now, 100))
        self.assertEqual("group-interval", bot.tick(self.now, 101))

    def test_missing_weather_field_fails_closed(self):
        with self.assertRaises(ValueError):
            extract_weather([{"icaoId": "KMCI", "temp": None}], "KMCI", "temp")


if __name__ == "__main__":
    unittest.main()
