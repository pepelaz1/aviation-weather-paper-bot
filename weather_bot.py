"""Weather-triggered PAPER order prototype. LIVE is deliberately unavailable."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


API_URL = "https://aviationweather.gov/api/data/metar"


@dataclass(frozen=True)
class Rule:
    date: str
    operator: str
    value: Decimal
    quote_ceiling: Decimal
    max_size: Decimal

    def matches(self, day: str, observation: Decimal) -> bool:
        if self.date != day:
            return False
        return {
            ">=": lambda: observation >= self.value,
            ">": lambda: observation > self.value,
            "<=": lambda: observation <= self.value,
            "<": lambda: observation < self.value,
            "==": lambda: observation == self.value,
        }[self.operator]()


def load_rules(path: Path) -> list[Rule]:
    result: list[Rule] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = raw.split("#", 1)[0].strip()
        if not text:
            continue
        cells = [part.strip() for part in text.split(",")]
        if len(cells) != 5:
            raise ValueError(f"{path}:{line_number}: expected 5 comma-separated fields")
        date, op, value, ceiling, size = cells
        datetime.strptime(date, "%Y-%m-%d")
        if op not in {">=", ">", "<=", "<", "=="}:
            raise ValueError(f"{path}:{line_number}: unsupported operator {op!r}")
        try:
            rule = Rule(date, op, Decimal(value), Decimal(ceiling), Decimal(size))
        except InvalidOperation as exc:
            raise ValueError(f"{path}:{line_number}: invalid number") from exc
        if not all(x.is_finite() for x in (rule.value, rule.quote_ceiling, rule.max_size)):
            raise ValueError(f"{path}:{line_number}: non-finite number")
        if rule.quote_ceiling <= 0 or rule.max_size <= 0:
            raise ValueError(f"{path}:{line_number}: ceiling and size must be positive")
        result.append(rule)
    return result


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            if "=" in raw and not raw.lstrip().startswith("#"):
                key, value = raw.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def extract_weather(data: object, station: str, field: str) -> tuple[Decimal, str]:
    if not isinstance(data, list):
        raise ValueError("weather API did not return a list")
    for row in data:
        if isinstance(row, dict) and row.get("icaoId") == station:
            raw = row.get(field)
            if raw is None or isinstance(raw, bool):
                raise ValueError(f"weather field {field!r} missing")
            try:
                value = Decimal(str(raw))
            except InvalidOperation as exc:
                raise ValueError(f"weather field {field!r} is not numeric") from exc
            if not value.is_finite():
                raise ValueError("weather value is not finite")
            return value, str(row.get("obsTime", "unknown"))
    raise ValueError(f"no observation for {station}")


def fetch_weather(station: str, user_agent: str) -> object:
    query = urlencode({"ids": station, "format": "json"})
    request = Request(f"{API_URL}?{query}", headers={"User-Agent": user_agent, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=10) as response:
            if response.status == 204:
                raise ValueError("no weather observations available")
            return json.load(response)
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"weather API error: {exc}") from exc


def active_group(config: dict, now: datetime) -> str | None:
    for group in config["groups"]:
        for minute in group["minutes"]:
            start = minute * 60
            second_of_hour = now.minute * 60 + now.second
            if start <= second_of_hour < start + group["window_seconds"]:
                return group["name"]
    return None


class PaperBot:
    def __init__(self, config: dict, rules_path: Path, logger: logging.Logger,
                 weather: Callable[[], object], quote: Callable[[], dict]):
        self.config = config
        self.rules_path = rules_path
        self.log = logger
        self.weather = weather
        self.quote = quote
        self.rules: list[Rule] = []
        self.last_rules_read = 0.0
        self.last_request = 0.0
        self.last_group_poll: dict[str, float] = {}
        self.last_observation: str | None = None
        self.ordered: set[tuple[str, str]] = set()

    def reload_rules(self, monotonic_now: float, force: bool = False) -> None:
        if not force and monotonic_now - self.last_rules_read < self.config["rules_reload_seconds"]:
            return
        self.last_rules_read = monotonic_now
        try:
            updated = load_rules(self.rules_path)
        except (OSError, ValueError) as exc:
            self.log.error("rule reload failed; keeping last valid rules: %s", exc)
            return
        self.rules = updated
        self.log.info("loaded %d rule(s)", len(updated))

    def tick(self, now: datetime, monotonic_now: float, force: bool = False) -> str:
        self.reload_rules(monotonic_now, force)
        group = active_group(self.config, now)
        if group is None and not force:
            return "outside-window"
        if group and not force:
            group_config = next(item for item in self.config["groups"] if item["name"] == group)
            if monotonic_now - self.last_group_poll.get(group, -1e9) < group_config["poll_seconds"]:
                return "group-interval"
        if not force and monotonic_now - self.last_request < self.config["min_request_interval_seconds"]:
            return "rate-limited"
        self.last_request = monotonic_now
        if group:
            self.last_group_poll[group] = monotonic_now
        try:
            value, observation_id = extract_weather(
                self.weather(), self.config["station"], self.config["weather_field"])
            self.log.info("group=%s observation=%s value=%s", group or "manual", observation_id, value)
            if observation_id == self.last_observation:
                return "stale-observation"
            self.last_observation = observation_id
            day = now.date().isoformat()
            matches = [rule for rule in self.rules if rule.matches(day, value)]
            if not matches:
                return "no-match"
            quote = self.quote()
            price = Decimal(str(quote["price"]))
            available = Decimal(str(quote["available_size"]))
            if not price.is_finite() or not available.is_finite() or price <= 0 or available < 0:
                raise ValueError("invalid quote")
            for rule in matches:
                key = (observation_id, str(rule))
                if key in self.ordered:
                    continue
                self.ordered.add(key)
                size = min(available, rule.max_size)
                if price <= rule.quote_ceiling and size > 0:
                    self.log.info("PAPER order observation=%s limit=%s size=%s quote=%s", observation_id, rule.quote_ceiling, size, price)
                    return "paper-order"
                self.log.info("PAPER skipped price=%s ceiling=%s available=%s", price, rule.quote_ceiling, available)
            return "quote-rejected"
        except (ValueError, KeyError, TypeError, RuntimeError) as exc:
            self.log.error("tick failed safely: %s", exc)
            return "error"


def configure_log(path: Path) -> logging.Logger:
    logger = logging.getLogger("weather-paper-bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def main() -> int:
    parser = argparse.ArgumentParser(description="Weather-triggered PAPER order prototype")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--rules", type=Path, default=Path("rules.txt"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--sample", type=Path, help="offline weather JSON fixture")
    parser.add_argument("--once", action="store_true", help="run one check immediately")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    env = {**read_env(args.env), **os.environ}
    if env.get("MODE", "PAPER").upper() != "PAPER":
        parser.error("LIVE is disabled: exchange API and signed client are not specified")
    zone = ZoneInfo(env.get("TIMEZONE", "UTC"))
    logger = configure_log(Path("bot.log"))
    weather = (lambda: json.loads(args.sample.read_text(encoding="utf-8"))) if args.sample else (
        lambda: fetch_weather(config["station"], env.get("WEATHER_USER_AGENT", "WeatherPaperDemo/0.1")))
    # The quote API is unknown in the brief. This fixture is intentionally explicit.
    quote = lambda: {"price": "0.42", "available_size": "12"}
    bot = PaperBot(config, args.rules, logger, weather, quote)
    stop = False

    def on_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, on_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, on_stop)
    if args.once:
        print("result:", bot.tick(datetime.now(zone), time.monotonic(), force=True))
        return 0
    logger.info("PAPER scheduler started, timezone=%s", zone)
    while not stop:
        now = datetime.now(zone)
        bot.tick(now, time.monotonic())
        time.sleep(1)
    logger.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
