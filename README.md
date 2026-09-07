# Halal Momentum Scanner

Scans Alpaca's strongest US stock gainers, checks Ross Cameron-style momentum filters, and detects a basic one-minute pullback entry. Symbols in the local halal allowlist are marked `VERIFIED LIST`; all others are marked `CHECK IN ZOYA` and must not be treated as halal until checked.

Premarket candidates come from TradingView's live extended-hours screener because
Alpaca's movers endpoint does not reset until the 9:30 a.m. US open. During regular
hours, Alpaca supplies the movers, quotes, and candles.

## Setup

1. Create a free Alpaca account and obtain Market Data API keys.
2. Copy `.env.example` to `.env` and add the keys.
3. Add Zoya-verified symbols to `halal_symbols.csv` as you verify them.
4. Run:

```powershell
python scanner.py
```

## Current filters

- Clear Zoya verification status
- Price $1-$20
- Up at least 10%
- Premarket relative volume at least 1x average full-day volume
- Regular-session relative volume at least 5x
- Verified numeric float required, sourced from TradingView with a 24-hour cache
- Float at most 20M
- 1-3 red one-minute candles after a green impulse
- Alert when the first green candle breaks the prior candle's high
- Approximate take-profit zone at 2R-3R based on trigger-to-stop risk
- Maximum 2% bid/ask spread and minimum $250,000 current-session dollar volume
- Cash-only sizing for a $200 account at 0.5% risk per trade and 1.5% daily stop
- Persistent two-hour per-symbol cooldown shared by strict and practice alerts
- Single-instance protection to prevent duplicate Telegram alerts

The bot alerts as soon as a stock passes every stock-selection filter. A completed
pullback is not required. When the candle data also contains a clean pullback, the
message adds the calculated trigger, stop, and target as optional extra information;
the user remains responsible for technical analysis and entry timing.

The automated 2% spread filter applies during regular hours. In premarket, the free
IEX quote can be stale, so candidates are sent without an automated spread decision
and the live spread must be checked manually in the broker before any paper entry.

Stocks with missing float data are withheld. Every Telegram alert therefore includes
a numeric float, and the program never assumes an unknown float is below 20M. The
local `halal_symbols.csv` float remains available as a fallback.

This is a watchlist tool, not an automatic trading system or financial advice.

## Halal Trading Companion

The companion adds a phone-friendly pre-trade gate, position sizing, TradingView
webhook capture, Telegram notifications, and Obsidian-compatible Markdown notes.
Its dedicated `/calculator` page calculates a cash-only position size, dollar risk,
daily loss limit, and reward-to-risk before opening the checklist.

Start it with:

```powershell
python companion.py
```

On Windows, double-click `Start Companion.cmd`. Keep its terminal window open
while using the companion; closing it stops the local site.

On the same Wi-Fi network, open `http://YOUR-COMPUTER-IP:8787` on your phone.
Every submission is saved inside `Trading Vault/Trade Journal`. If an iCloud
Obsidian vault named `Trading Vault` exists, the companion selects it automatically;
otherwise it uses the project-local vault.

### TradingView webhook

Set a long random `WEBHOOK_SECRET` in `.env`. After deploying the companion behind
HTTPS, use this TradingView webhook URL:

```text
https://YOUR-PUBLIC-URL/webhook/tradingview?secret=YOUR_SECRET
```

Example TradingView alert body:

```json
{"symbol":"{{ticker}}","price":"{{close}}","setup":"1-minute pullback"}
```

TradingView alerts are saved to `Trading Vault/TradingView Alerts` and forwarded to
Telegram when its credentials are configured. Never expose port 8787 directly to
the internet; use an authenticated HTTPS tunnel or hosting service.

## Telegram

Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to `.env`, then test with:

```powershell
python scanner.py --telegram-test
```

Send scan results with `python scanner.py --telegram`.

Run continuously with `python scanner.py --watch`. The bot sends every WATCH and
TRIGGERED setup once per trigger price, regardless of allowlist status. Every
symbol must be checked manually in Zoya before considering a trade.
