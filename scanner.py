from __future__ import annotations

import argparse
import csv
import os
import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


class SingleInstanceLock:
    """Prevent two watch processes from sending duplicate alerts on Windows."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        import msvcrt

        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            self.handle.close()
            self.handle = None
            return False


@dataclass(frozen=True)
class Filters:
    min_price: float = 1.0
    max_price: float = 20.0
    min_change_pct: float = 10.0
    min_relative_volume: float = 5.0
    min_premarket_relative_volume: float = 1.0
    max_float_millions: float = 20.0
    require_known_float: bool = True
    min_pullback_bars: int = 1
    max_pullback_bars: int = 3
    max_spread_pct: float = 2.0
    min_dollar_volume: float = 250_000.0


@dataclass(frozen=True)
class AccountRules:
    cash_balance: float = 200.0
    risk_pct: float = 0.5
    daily_loss_pct: float = 1.5

    @property
    def risk_budget(self) -> float:
        return self.cash_balance * self.risk_pct / 100

    @property
    def daily_loss_limit(self) -> float:
        return self.cash_balance * self.daily_loss_pct / 100


class AlpacaData:
    base_url = "https://data.alpaca.markets"

    def __init__(self, key: str, secret: str, feed: str = "iex") -> None:
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self.feed = feed

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            self.base_url + path + "?" + urlencode(params), headers=self.headers
        )
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Alpaca API error {error.code}: {detail}") from error

    def market_movers(self, top: int = 20) -> list[dict[str, Any]]:
        data = self._get(
            "/v1beta1/screener/stocks/movers",
            {"top": top},
        )
        return data.get("gainers", [])

    def daily_bars(self, symbols: list[str], days: int = 25) -> dict[str, list[dict]]:
        if not symbols:
            return {}
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days * 2)
        data = self._get(
            "/v2/stocks/bars",
            {
                "symbols": ",".join(symbols),
                "timeframe": "1Day",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 10000,
                "adjustment": "raw",
                "feed": self.feed,
            },
        )
        return data.get("bars", {})

    def minute_bars(self, symbols: list[str], minutes: int = 30) -> dict[str, list[dict]]:
        if not symbols:
            return {}
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=minutes)
        data = self._get(
            "/v2/stocks/bars",
            {
                "symbols": ",".join(symbols),
                "timeframe": "1Min",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 10000,
                "adjustment": "raw",
                "feed": self.feed,
            },
        )
        return data.get("bars", {})

    def latest_quotes(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        data = self._get(
            "/v2/stocks/quotes/latest",
            {"symbols": ",".join(symbols), "feed": self.feed},
        )
        return data.get("quotes", {})


class TradingViewFloatData:
    """Batch free-float lookup with a disk cache and stale-cache fallback."""

    endpoint = "https://scanner.tradingview.com/america/scan"

    def __init__(self, cache_path: Path, ttl_hours: int = 24) -> None:
        self.cache_path = cache_path
        self.ttl_seconds = ttl_hours * 60 * 60
        self.cache = self._load_cache()

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save_cache(self) -> None:
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.cache, indent=2), encoding="utf-8")
        temporary.replace(self.cache_path)

    def _fetch(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        payload = {
            "filter": [
                {"left": "name", "operation": "in_range", "right": symbols},
                {"left": "type", "operation": "equal", "right": "stock"},
            ],
            "options": {"lang": "en"},
            "markets": ["america"],
            "columns": ["name", "exchange", "typespecs", "float_shares_outstanding"],
        }
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            method="POST",
        )
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        floats: dict[str, float] = {}
        exchange_rank = {"NASDAQ": 3, "NYSE": 2, "AMEX": 1}
        ranks: dict[str, int] = {}
        for row in result.get("data", []):
            values = row.get("d", [])
            if len(values) < 4 or values[3] is None:
                continue
            symbol, exchange, typespecs, shares = values[:4]
            if typespecs and "common" not in typespecs:
                continue
            symbol = str(symbol).upper()
            rank = exchange_rank.get(str(exchange).upper(), 0)
            if symbol in floats and rank <= ranks[symbol]:
                continue
            shares = float(shares)
            if shares > 0:
                floats[symbol] = shares / 1_000_000
                ranks[symbol] = rank
        return floats

    def premarket_movers(self, filters: Filters) -> list[dict[str, Any]]:
        """Return today's actual US premarket leaders, not yesterday's movers."""
        payload = {
            "filter": [
                {
                    "left": "premarket_close",
                    "operation": "in_range",
                    "right": [filters.min_price, filters.max_price],
                },
                {
                    "left": "premarket_change",
                    "operation": "egreater",
                    "right": filters.min_change_pct,
                },
                {
                    "left": "float_shares_outstanding",
                    "operation": "eless",
                    "right": filters.max_float_millions * 1_000_000,
                },
                {"left": "type", "operation": "equal", "right": "stock"},
            ],
            "options": {"lang": "en"},
            "markets": ["america"],
            "columns": [
                "name",
                "exchange",
                "typespecs",
                "premarket_close",
                "premarket_change",
                "premarket_volume",
                "float_shares_outstanding",
                "average_volume_10d_calc",
            ],
            "sort": {"sortBy": "premarket_change", "sortOrder": "desc"},
            "range": [0, 49],
        }
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            method="POST",
        )
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        now = time.time()
        movers = []
        for row in result.get("data", []):
            values = row.get("d", [])
            if len(values) < 8 or values[6] is None:
                continue
            symbol, _exchange, typespecs, price, change, volume, shares, average_volume = values[:8]
            if typespecs and "common" not in typespecs:
                continue
            float_m = float(shares) / 1_000_000
            symbol = str(symbol).upper()
            self.cache[symbol] = {
                "float_millions": float_m,
                "fetched_at": now,
                "source": "TradingView",
            }
            movers.append(
                {
                    "symbol": symbol,
                    "price": float(price),
                    "percent_change": float(change),
                    "session_volume": float(volume or 0),
                    "session_relative_volume": (
                        float(volume or 0) / float(average_volume)
                        if average_volume and float(average_volume) > 0
                        else 0.0
                    ),
                }
            )
        if movers:
            self._save_cache()
        return movers

    def regular_movers(self, filters: Filters) -> list[dict[str, Any]]:
        payload = {
            "filter": [
                {"left": "close", "operation": "in_range", "right": [filters.min_price, filters.max_price]},
                {"left": "change", "operation": "egreater", "right": filters.min_change_pct},
                {"left": "float_shares_outstanding", "operation": "eless", "right": filters.max_float_millions * 1_000_000},
                {"left": "type", "operation": "equal", "right": "stock"},
            ],
            "options": {"lang": "en"},
            "markets": ["america"],
            "columns": [
                "name", "exchange", "typespecs", "close", "change", "volume",
                "float_shares_outstanding", "average_volume_10d_calc",
            ],
            "sort": {"sortBy": "change", "sortOrder": "desc"},
            "range": [0, 49],
        }
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            method="POST",
        )
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        now = time.time()
        movers = []
        for row in result.get("data", []):
            values = row.get("d", [])
            if len(values) < 8 or values[6] is None:
                continue
            symbol, _exchange, typespecs, price, change, volume, shares, average_volume = values[:8]
            if typespecs and "common" not in typespecs:
                continue
            symbol = str(symbol).upper()
            float_m = float(shares) / 1_000_000
            self.cache[symbol] = {
                "float_millions": float_m, "fetched_at": now, "source": "TradingView"
            }
            movers.append(
                {
                    "symbol": symbol,
                    "price": float(price),
                    "percent_change": float(change),
                    "session_volume": float(volume or 0),
                    "session_relative_volume": (
                        float(volume or 0) / float(average_volume)
                        if average_volume and float(average_volume) > 0 else 0.0
                    ),
                }
            )
        if movers:
            self._save_cache()
        return movers

    def get_float_millions(self, symbols: list[str]) -> dict[str, float]:
        now = time.time()
        requested = list(dict.fromkeys(symbol.upper() for symbol in symbols if symbol))
        fresh = {
            symbol: float(self.cache[symbol]["float_millions"])
            for symbol in requested
            if symbol in self.cache
            and now - float(self.cache[symbol].get("fetched_at", 0)) < self.ttl_seconds
        }
        missing = [symbol for symbol in requested if symbol not in fresh]
        if missing:
            try:
                fetched = self._fetch(missing)
                for symbol, value in fetched.items():
                    self.cache[symbol] = {
                        "float_millions": value,
                        "fetched_at": now,
                        "source": "TradingView",
                    }
                if fetched:
                    self._save_cache()
                fresh.update(fetched)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
                pass
        # If TradingView is temporarily unavailable, a previously verified value
        # is safer and more useful than turning it into an unknown value.
        for symbol in requested:
            cached = self.cache.get(symbol, {})
            if symbol not in fresh and cached.get("float_millions") is not None:
                fresh[symbol] = float(cached["float_millions"])
        return fresh


class YahooMinuteData:
    """Fallback premarket candles when the free IEX feed has no prints."""

    endpoint = "https://query1.finance.yahoo.com/v8/finance/chart/"

    def minute_bars(self, symbol: str) -> list[dict[str, Any]]:
        request = Request(
            self.endpoint + symbol + "?" + urlencode(
                {"interval": "1m", "range": "1d", "includePrePost": "true"}
            ),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = (payload.get("chart", {}).get("result") or [None])[0]
        if not result:
            return []
        timestamps = result.get("timestamp", [])
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        opens, highs = quote.get("open", []), quote.get("high", [])
        lows, closes = quote.get("low", []), quote.get("close", [])
        volumes = quote.get("volume", [])
        bars = []
        for index, timestamp in enumerate(timestamps):
            values = [opens[index], highs[index], lows[index], closes[index]]
            if any(value is None for value in values):
                continue
            bars.append(
                {
                    "t": timestamp,
                    "o": float(values[0]),
                    "h": float(values[1]),
                    "l": float(values[2]),
                    "c": float(values[3]),
                    "v": float(volumes[index] or 0) if index < len(volumes) else 0,
                }
            )
        return bars[-60:]


class TelegramBot:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    def send(self, message: str) -> None:
        payload = urlencode({"chat_id": self.chat_id, "text": message}).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=payload,
            method="POST",
        )
        try:
            with urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram API error {error.code}: {detail}") from error
        except URLError:
            if os.name != "nt":
                raise
            result = self._send_with_windows_https(message)
        if not result.get("ok"):
            raise RuntimeError("Telegram rejected the message.")

    def _send_with_windows_https(self, message: str) -> dict[str, Any]:
        environment = os.environ.copy()
        environment["SCANNER_TG_TOKEN"] = self.token
        environment["SCANNER_TG_CHAT"] = self.chat_id
        environment["SCANNER_TG_TEXT"] = message
        script = (
            "$body=@{chat_id=$env:SCANNER_TG_CHAT;text=$env:SCANNER_TG_TEXT};"
            "$uri='https://api.telegram.org/bot'+$env:SCANNER_TG_TOKEN+'/sendMessage';"
            "Invoke-RestMethod -Method Post -Uri $uri -Body $body | ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
            check=True,
        )
        return json.loads(completed.stdout)

    @staticmethod
    def find_chat_id(token: str) -> str | None:
        request = Request(f"https://api.telegram.org/bot{token}/getUpdates")
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        for update in reversed(result.get("result", [])):
            message = update.get("message") or update.get("channel_post")
            if message and message.get("chat", {}).get("id") is not None:
                return str(message["chat"]["id"])
        return None

def load_halal_universe(path: Path) -> dict[str, dict[str, Any]]:
    universe: dict[str, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row.get("halal", "").strip().lower() not in {"true", "yes", "1"}:
                continue
            symbol = row["symbol"].strip().upper()
            raw_float = row.get("float_millions", "").strip()
            universe[symbol] = {
                "company": row.get("company", "").strip(),
                "float_millions": float(raw_float) if raw_float else None,
                "notes": row.get("notes", "").strip(),
            }
    return universe


def relative_volume(today_volume: float, daily: list[dict], lookback: int = 20) -> float:
    completed = [float(bar["v"]) for bar in daily[:-1]][-lookback:]
    if not completed:
        return 0.0
    average = sum(completed) / len(completed)
    return today_volume / average if average else 0.0


def us_market_phase(now: datetime | None = None) -> str:
    eastern = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    if eastern.weekday() >= 5:
        return "CLOSED"
    minutes = eastern.hour * 60 + eastern.minute
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return "PREMARKET"
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "REGULAR"
    return "CLOSED"


def pullback_signal(bars: list[dict], filters: Filters) -> dict[str, Any]:
    """Detect green impulse, 1-3 red candles, then first candle making a new high."""
    if len(bars) < 5:
        return {"status": "WAIT", "reason": "not enough one-minute bars"}

    latest = bars[-1]
    red_count = 0
    index = len(bars) - 2
    while index >= 0 and bars[index]["c"] < bars[index]["o"]:
        red_count += 1
        index -= 1

    if not filters.min_pullback_bars <= red_count <= filters.max_pullback_bars:
        return {"status": "WAIT", "reason": "no clean 1-3 candle pullback"}

    pullback = bars[index + 1 : len(bars) - 1]
    impulse = bars[max(0, index - 2) : index + 1]
    if len(impulse) < 2 or sum(b["c"] > b["o"] for b in impulse) < 2:
        return {"status": "WAIT", "reason": "no clear green impulse"}

    trigger = float(pullback[-1]["h"])
    stop = min(float(bar["l"]) for bar in pullback)
    current = float(latest["c"])
    risk_per_share = trigger - stop
    target_low = trigger + (risk_per_share * 2)
    target_high = trigger + (risk_per_share * 3)
    if latest["c"] > latest["o"] and latest["h"] > trigger:
        return {
            "status": "TRIGGERED",
            "entry": trigger,
            "stop": stop,
            "target_low": target_low,
            "target_high": target_high,
            "current": current,
        }
    return {
        "status": "WATCH",
        "trigger": trigger,
        "stop": stop,
        "target_low": target_low,
        "target_high": target_high,
        "current": current,
    }


def size_position(entry: float, stop: float, rules: AccountRules) -> dict[str, Any]:
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0 or entry <= 0:
        return {"shares": 0, "reason": "invalid entry or stop"}
    risk_sized = int(rules.risk_budget // risk_per_share)
    cash_sized = int(rules.cash_balance // entry)
    shares = min(risk_sized, cash_sized)
    return {
        "shares": shares,
        "position_value": shares * entry,
        "planned_risk": shares * risk_per_share,
        "risk_budget": rules.risk_budget,
        "daily_loss_limit": rules.daily_loss_limit,
        "reason": "ok" if shares > 0 else "account too small for this stop distance",
    }


def scan(
    client: AlpacaData,
    universe: dict[str, dict],
    filters: Filters,
    rules: AccountRules | None = None,
    float_data: TradingViewFloatData | None = None,
    market_phase: str | None = None,
) -> list[dict]:
    rules = rules or AccountRules()
    phase = market_phase or us_market_phase()
    if phase == "CLOSED":
        return []
    if phase == "PREMARKET" and float_data:
        movers = float_data.premarket_movers(filters)
    else:
        movers = client.market_movers(top=50)
    candidates = []
    for mover in movers:
        symbol = str(mover.get("symbol", "")).upper()
        price = float(mover.get("price", 0))
        change = float(mover.get("percent_change", 0))
        if not (filters.min_price <= price <= filters.max_price):
            continue
        if change < filters.min_change_pct:
            continue
        candidates.append(
            {
                "symbol": symbol,
                "price": price,
                "change_pct": change,
                "session_volume": mover.get("session_volume"),
                "session_relative_volume": mover.get("session_relative_volume"),
            }
        )

    live_floats = (
        float_data.get_float_millions([item["symbol"] for item in candidates])
        if float_data
        else {}
    )
    float_checked = []
    for item in candidates:
        symbol = item["symbol"]
        halal_record = universe.get(symbol)
        local_float = halal_record["float_millions"] if halal_record else None
        float_m = live_floats.get(symbol, local_float)
        # Never send an alert with an unknown float. Alpaca's movers feed does not
        # include this field, so it must come from TradingView or our local dataset.
        if filters.require_known_float and float_m is None:
            continue
        if float_m is not None and float_m > filters.max_float_millions:
            continue
        item["halal_status"] = "VERIFIED LIST" if halal_record else "CHECK IN ZOYA"
        item["float_millions"] = float_m
        item["float_source"] = "TradingView" if symbol in live_floats else "local list"
        float_checked.append(item)

    candidates = float_checked

    symbols = [item["symbol"] for item in candidates]
    daily = client.daily_bars(symbols)
    minute = client.minute_bars(symbols)
    quotes = client.latest_quotes(symbols)
    results = []
    for item in candidates:
        symbol = item["symbol"]
        day_bars = daily.get(symbol, [])
        supplied_volume = item.pop("session_volume", None)
        supplied_rvol = item.pop("session_relative_volume", None)
        today_volume = (
            float(supplied_volume)
            if supplied_volume is not None
            else (float(day_bars[-1]["v"]) if day_bars else 0.0)
        )
        dollar_volume = today_volume * item["price"]
        if dollar_volume < filters.min_dollar_volume:
            continue
        if supplied_rvol is not None:
            rvol = float(supplied_rvol)
        else:
            rvol = relative_volume(today_volume, day_bars)
        required_rvol = (
            filters.min_premarket_relative_volume
            if phase == "PREMARKET"
            else filters.min_relative_volume
        )
        if rvol < required_rvol:
            continue
        item["relative_volume"] = rvol
        item["dollar_volume"] = dollar_volume
        quote = quotes.get(symbol, {})
        bid, ask = float(quote.get("bp", 0)), float(quote.get("ap", 0))
        midpoint = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
        spread_pct = ((ask - bid) / midpoint * 100) if midpoint else None
        item["spread_pct"] = spread_pct
        # Free IEX quotes can be stale or absent before the opening bell. Do not
        # reject a live TradingView premarket leader using yesterday's quote.
        # The Telegram message explicitly requires a manual live-spread check.
        if phase == "REGULAR" and (
            spread_pct is None or spread_pct > filters.max_spread_pct
        ):
            continue
        item["setup"] = pullback_signal(minute.get(symbol, []), filters)
        setup = item["setup"]
        if setup["status"] in {"WATCH", "TRIGGERED"}:
            entry = float(setup.get("entry", setup.get("trigger")))
            setup["sizing"] = size_position(entry, float(setup["stop"]), rules)
            setup["chase_pct"] = max(0.0, (float(setup["current"]) - entry) / entry * 100)
        results.append(item)
    return sorted(results, key=lambda row: row["relative_volume"], reverse=True)


def print_results(results: list[dict]) -> None:
    if not results:
        print("No stocks currently pass every stock-selection filter.")
        return
    for row in results:
        setup = row["setup"]
        float_text = f'{row["float_millions"]:.1f}M'
        print(
            f'{row["symbol"]:6} {row["halal_status"]:13}  ${row["price"]:7.2f}  '
            f'+{row["change_pct"]:6.1f}%  RVOL {row["relative_volume"]:5.1f}x  '
            f'Float {float_text:>6}  {setup["status"]}: '
            f'{setup.get("reason", "trigger/stop calculated")}'
        )
        if setup["status"] in {"WATCH", "TRIGGERED"}:
            trigger = setup.get("entry", setup.get("trigger"))
            print(
                f'       Trigger ${trigger:.2f} | Stop ${setup["stop"]:.2f} | '
                f'Target zone ${setup["target_low"]:.2f}-${setup["target_high"]:.2f}'
            )
            sizing = setup["sizing"]
            print(
                f'       $200 sizing: {sizing["shares"]} shares | '
                f'Planned risk ${sizing.get("planned_risk", 0):.2f} | '
                f'Spread {row["spread_pct"]:.2f}%'
            )


def format_telegram(results: list[dict]) -> str:
    if not results:
        return ""
    messages = []
    for row in results:
        if row.get("float_millions") is None:
            continue
        setup = row["setup"]
        float_text = f'{row["float_millions"]:.1f}M'
        lines = [
            row["symbol"],
            "",
            f'Price: ${row["price"]:.2f}',
            f'Change: +{row["change_pct"]:.1f}%',
            f'Relative volume: {row["relative_volume"]:.1f}x',
            f'Float: {float_text}',
            "",
            "QUALIFIED MOMENTUM STOCK — ANALYZE THE ENTRY",
        ]
        if setup["status"] in {"WATCH", "TRIGGERED"}:
            trigger = float(setup.get("entry", setup.get("trigger")))
            status = "PULLBACK TRIGGERED" if setup["status"] == "TRIGGERED" else "PULLBACK WATCH"
            lines.extend(
                [
                    "",
                    status,
                    f'Trigger: ${trigger:.2f}',
                    f'Stop: ${setup["stop"]:.2f}',
                    f'Take-profit zone: approximately ${setup["target_low"]:.2f}-${setup["target_high"]:.2f}',
                ]
            )
            if setup["chase_pct"] > 2:
                lines.extend(["", "DO NOT CHASE: price is more than 2% above the trigger."])
        else:
            lines.extend(["", "No automated entry recommendation yet. Check the chart for your setup."])
        lines.extend(
            [
                "",
                "Confirm halal status in Zoya, catalyst/news, spread and chart before considering a trade.",
            ]
        )
        messages.append("\n".join(lines))
    if not messages:
        return ""
    return "\n\n--------------------\n\n".join(messages)


def format_daily_watchlist(results: list[dict], limit: int = 5) -> str:
    """Build one concise Adelaide-evening summary; this is not an entry signal."""
    lines = [
        "9:30 PM ADELAIDE MOMENTUM WATCHLIST",
        "",
        "Stocks currently passing the automated selection rules:",
    ]
    if not results:
        lines.extend(["", "None right now. No forced picks tonight."])
    else:
        seen: set[str] = set()
        position = 0
        for row in results:
            symbol = row["symbol"].upper()
            if symbol in seen:
                continue
            seen.add(symbol)
            position += 1
            float_value = row.get("float_millions")
            float_text = f"{float_value:.1f}M" if float_value is not None else "CHECK"
            lines.append(
                f'{position}. {symbol} | ${row["price"]:.2f} | '
                f'+{row["change_pct"]:.1f}% | RVOL {row["relative_volume"]:.1f}x | '
                f'Float {float_text}'
            )
            if position >= limit:
                break
    lines.extend(
        [
            "",
            "Watchlist only—not a prediction or an entry signal. Confirm halal status in Zoya, current news, live spread and your chart setup.",
        ]
    )
    return "\n".join(lines)


def practice_scan(
    float_data: TradingViewFloatData,
    yahoo: YahooMinuteData,
    filters: Filters,
    max_candidates: int = 5,
    market_phase: str = "PREMARKET",
) -> list[dict[str, Any]]:
    """Broader paper-trading list; never represented as a strict entry signal."""
    movers = (
        float_data.premarket_movers(filters)
        if market_phase == "PREMARKET"
        else float_data.regular_movers(filters)
    )
    floats = float_data.get_float_millions([row["symbol"] for row in movers])
    eligible = [
        row
        for row in movers
        if row["session_relative_volume"] >= 0.25
        and row["session_volume"] * row["price"] >= 100_000
    ]
    eligible.sort(
        key=lambda row: (row["session_relative_volume"], row["percent_change"]),
        reverse=True,
    )
    results = []
    for mover in eligible[:max_candidates]:
        symbol = mover["symbol"]
        try:
            bars = yahoo.minute_bars(symbol)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
            bars = []
        setup = pullback_signal(bars, filters)
        results.append(
            {
                "symbol": symbol,
                "price": mover["price"],
                "change_pct": mover["percent_change"],
                "relative_volume": mover["session_relative_volume"],
                "float_millions": floats.get(symbol),
                "setup": setup,
            }
        )
    return results


def format_practice_telegram(row: dict[str, Any]) -> str:
    float_value = row.get("float_millions")
    float_text = f"{float_value:.1f}M" if float_value is not None else "CHECK MANUALLY"
    lines = [
        "PRACTICE WATCH — PAPER TRADE ONLY",
        "",
        row["symbol"],
        f'Price: ${row["price"]:.2f}',
        f'Change: +{row["change_pct"]:.1f}%',
        f'Relative volume: {row["relative_volume"]:.1f}x',
        f"Float: {float_text}",
    ]
    setup = row["setup"]
    if setup["status"] in {"WATCH", "TRIGGERED"}:
        trigger = float(setup.get("entry", setup.get("trigger")))
        lines.extend(
            [
                "",
                "PULLBACK TRIGGERED" if setup["status"] == "TRIGGERED" else "PULLBACK WATCH",
                f"Trigger: ${trigger:.2f}",
                f'Stop: ${setup["stop"]:.2f}',
                f'Take-profit zone: approximately ${setup["target_low"]:.2f}-${setup["target_high"]:.2f}',
            ]
        )
    else:
        lines.extend(["", "No clean pullback yet — open the chart and practise waiting."])
    lines.extend(["", "Confirm halal status in Zoya, news and live spread. Do not use this practice alert as an automatic entry."])
    return "\n".join(lines)


def signal_key(row: dict) -> str | None:
    setup = row["setup"]
    if setup["status"] not in {"WATCH", "TRIGGERED"}:
        return f'{row["symbol"]}:QUALIFIED'
    entry = setup.get("entry", setup.get("trigger"))
    return f'{row["symbol"]}:{setup["status"]}:{float(entry):.4f}'


class AlertCooldown:
    """Persistent per-symbol cooldown shared by strict and practice alerts."""

    def __init__(self, path: Path, hours: float = 2.0) -> None:
        self.path = path
        self.seconds = hours * 60 * 60
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.history = {str(key): float(value) for key, value in data.items()}
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            self.history: dict[str, float] = {}

    def allowed(self, symbol: str, now: float | None = None) -> bool:
        timestamp = time.time() if now is None else now
        return timestamp - self.history.get(symbol.upper(), 0) >= self.seconds

    def mark(self, symbols: list[str], now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        for symbol in symbols:
            self.history[symbol.upper()] = timestamp
        cutoff = timestamp - (24 * 60 * 60)
        self.history = {symbol: value for symbol, value in self.history.items() if value >= cutoff}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.history, indent=2), encoding="utf-8")
        temporary.replace(self.path)


class DailyWatchlistState:
    """Persist the last Adelaide date so restarts cannot duplicate the daily summary."""

    timezone = ZoneInfo("Australia/Adelaide")

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.last_sent_date = str(data.get("last_sent_date", ""))
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
            self.last_sent_date = ""

    def due(self, now: datetime | None = None) -> bool:
        local = (now or datetime.now(timezone.utc)).astimezone(self.timezone)
        return (
            local.weekday() < 5
            and (local.hour, local.minute) >= (21, 30)
            and self.last_sent_date != local.date().isoformat()
        )

    def mark(self, now: datetime | None = None) -> None:
        local = (now or datetime.now(timezone.utc)).astimezone(self.timezone)
        self.last_sent_date = local.date().isoformat()
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"last_sent_date": self.last_sent_date}, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Halal momentum pullback scanner")
    parser.add_argument("--halal-file", default="halal_symbols.csv")
    parser.add_argument("--telegram", action="store_true", help="send scan results")
    parser.add_argument("--telegram-test", action="store_true", help="send a test message")
    parser.add_argument("--telegram-demo", action="store_true", help="send a sample scanner alert")
    parser.add_argument("--telegram-find-chat", action="store_true", help="find chat ID after messaging bot")
    parser.add_argument("--watch", action="store_true", help="scan continuously and send new alerts")
    parser.add_argument("--interval", type=int, default=60, help="watch interval in seconds")
    args = parser.parse_args()
    load_env_file()
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("Copy .env.example to .env and add your Alpaca API keys.")
    if args.telegram_find_chat:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token or token == "replace_me":
            raise SystemExit("Add TELEGRAM_BOT_TOKEN to .env first.")
        chat_id = TelegramBot.find_chat_id(token)
        if not chat_id:
            raise SystemExit("No chat found. Open the bot in Telegram, press Start, then retry.")
        print(f"TELEGRAM_CHAT_ID={chat_id}")
        return
    telegram = None
    if args.telegram or args.telegram_test or args.telegram_demo:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id or "replace_me" in {token, chat_id}:
            raise SystemExit("Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env first.")
        telegram = TelegramBot(token, chat_id)
    if args.telegram_test:
        telegram.send("Halal Momentum Scanner is connected successfully.")
        print("Telegram test message sent.")
        return
    if args.telegram_demo:
        telegram.send(
            "DEMO ONLY - NOT A LIVE STOCK\n\n"
            "Momentum scanner candidate\n\n"
            "EXAMPLE | CHECK IN ZOYA\n"
            "Price: $6.20\n"
            "Change: +32.5%\n"
            "Relative volume: 8.4x\n"
            "Float: 12.5M\n\n"
            "PULLBACK WATCH\n"
            "Trigger: $6.28\n"
            "Stop: $6.05\n\n"
            "Take-profit zone: approximately $6.74-$6.97 (2R-3R)\n\n"
            "Confirm halal status, news, spread and chart in TradeZero before considering a trade."
        )
        print("Telegram demo alert sent.")
        return
    universe = load_halal_universe(Path(args.halal_file))
    client = AlpacaData(key, secret, os.getenv("ALPACA_FEED", "iex"))
    float_data = TradingViewFloatData(Path(__file__).with_name("float_cache.json"))
    rules = AccountRules(
        cash_balance=float(os.getenv("TRADING_CASH_BALANCE", "200")),
        risk_pct=float(os.getenv("RISK_PER_TRADE_PCT", "0.5")),
        daily_loss_pct=float(os.getenv("DAILY_LOSS_LIMIT_PCT", "1.5")),
    )
    if args.watch:
        instance_lock = SingleInstanceLock(Path(__file__).with_name("scanner-watch.lock"))
        if not instance_lock.acquire():
            raise SystemExit("Scanner watch is already running; duplicate process stopped.")
        if telegram is None:
            token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
            if not token or not chat_id or "replace_me" in {token, chat_id}:
                raise SystemExit("Watch mode requires Telegram credentials in .env.")
            telegram = TelegramBot(token, chat_id)
        cooldown = AlertCooldown(Path(__file__).with_name("alert_history.json"), hours=2)
        daily_watchlist = DailyWatchlistState(
            Path(__file__).with_name("daily_watchlist_history.json")
        )
        last_console_symbols: tuple[str, ...] | None = None
        try:
            while True:
                try:
                    results = scan(client, universe, Filters(), rules, float_data)
                    console_symbols = tuple(row["symbol"] for row in results)
                    if console_symbols != last_console_symbols:
                        print_results(results)
                        last_console_symbols = console_symbols
                    new_rows = []
                    for row in results:
                        key_value = signal_key(row)
                        if key_value and cooldown.allowed(row["symbol"]):
                            new_rows.append(row)
                    message = format_telegram(new_rows)
                    if message:
                        telegram.send(message)
                        cooldown.mark([row["symbol"] for row in new_rows])
                        print(f"Sent {len(new_rows)} new setup alert(s).")
                    if daily_watchlist.due():
                        telegram.send(format_daily_watchlist(results))
                        daily_watchlist.mark()
                        print("Sent the 9:30 PM Adelaide watchlist summary.")
                except Exception as error:
                    print(f"Scan error: {error}")
                time.sleep(max(30, args.interval))
        except KeyboardInterrupt:
            print("Scanner watch stopped.")
    else:
        results = scan(client, universe, Filters(), rules, float_data)
        print_results(results)
        if telegram:
            message = format_telegram(results)
            if message:
                telegram.send(message)
                print("Results sent to Telegram.")


if __name__ == "__main__":
    main()
