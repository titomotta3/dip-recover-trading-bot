"""
Dip-and-recover trading bot.

Strategy:
  - Watches the S&P 500 for stocks that have dropped 5%+ vs. the previous close.
  - Buys a fixed dollar amount of each stock that trips the drop threshold
    (skips it if we already hold an open position in it).
  - Watches all open positions and sells (closes) any position once it has
    recovered enough from its average entry price.
  - Runs against Alpaca's paper trading API by default. Nothing here places
    real trades unless ALPACA_PAPER is explicitly set to "false" AND you
    supply live API keys.

Adaptive mode (ADAPTIVE_STRATEGY=true, used by Portfolio 2 / "aggressive"):
  Instead of fixed buy/sell thresholds and a fixed trade size, the bot tunes
  its own parameters run over run based on how its own trades have gone --
  aiming to land each completed trade's realized gain somewhere in a
  10-15% band. This is a small, transparent, rule-based auto-tuner (not
  machine learning): every adjustment is a bounded step, and every
  parameter has a hard floor/ceiling it can never cross. See
  adapt_strategy() below for the exact rules and bounds. Current parameters
  and the reasoning behind the last change are written to
  STRATEGY_STATE_PATH and into the public snapshot each run, so the
  dashboard can show what it's currently doing and why.

This is meant to run on a schedule (see .github/workflows/trading-bot.yml),
each run doing one buy-scan + one sell-scan and then exiting.
"""

import csv
import datetime as dt
import json
import os
import sys
import traceback

import pandas as pd
import yfinance as yf
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

# ---------------------------------------------------------------------------
# Configuration (all overridable via environment variables / repo secrets)
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("ALPACA_API_KEY")
API_SECRET = os.environ.get("ALPACA_SECRET_KEY")
PAPER = os.environ.get("ALPACA_PAPER", "true").strip().lower() != "false"

# Buy trigger: stock is down this % (or more) vs. previous close.
DROP_THRESHOLD_PCT = float(os.environ.get("DROP_THRESHOLD_PCT", "-5.0"))

# Sell trigger: open position is up this % (or more) vs. average entry price.
# The task brief says "sell when it recovers 7-10%" -- we sell as soon as the
# gain crosses the low end of that band so we don't risk giving profit back
# waiting for the high end. Tune via SELL_THRESHOLD_PCT if you'd rather wait
# for a bigger bounce.
SELL_THRESHOLD_PCT = float(os.environ.get("SELL_THRESHOLD_PCT", "7.0"))

# Hard floor on position size: no buy order is ever placed for less than
# this, no matter what TRADE_DOLLARS is set to. Protects against a
# misconfigured/too-small env var silently placing tiny trades.
MIN_TRADE_DOLLARS = 10000.0

# Dollar amount to spend on each new buy signal (notional order). Clamped up
# to MIN_TRADE_DOLLARS if a smaller value is configured.
TRADE_DOLLARS = max(float(os.environ.get("TRADE_DOLLARS", "10000")), MIN_TRADE_DOLLARS)

# Starting account balance, written into the snapshot so the dashboard can
# compute total return ($ and %) without hard-coding it client-side.
STARTING_EQUITY = float(os.environ.get("STARTING_EQUITY", "500000"))

# ---------------------------------------------------------------------------
# News-based trade filtering (both portfolios). Uses Alpaca's News API with
# the same credentials already configured -- no extra signup or secrets.
# This is a transparent keyword scan, not an ML/sentiment model: every
# decision is traceable to the exact matched word(s), logged on every trade.
#   - Buy side: skip a dip if recent headlines look like a real problem
#     rather than a routine, recoverable drop.
#   - Sell side: exit a position early (even before it hits its normal
#     recovery target) if fresh bad news breaks while we're holding it.
#     This is the one place the strategy can realize a loss -- everywhere
#     else it only ever sells at a profit.
# A failed/empty news fetch is treated as "no signal" (not bad news) so a
# news-API hiccup degrades gracefully instead of freezing all trading.
# ---------------------------------------------------------------------------

NEWS_LOOKBACK_HOURS = float(os.environ.get("NEWS_LOOKBACK_HOURS", "72"))
NEWS_HEADLINE_LIMIT = int(os.environ.get("NEWS_HEADLINE_LIMIT", "10"))

BAD_NEWS_KEYWORDS = [
    "bankrupt", "bankruptcy", "chapter 11", "fraud", "lawsuit", "sued", "sues",
    "class action", "investigation", "investigated", "subpoena", "sec probe",
    "recall", "recalls", "recalled", "downgrade", "downgraded", "guidance cut",
    "cuts guidance", "lowers guidance", "profit warning", "warns", "layoffs",
    "restatement", "restates", "going concern", "default", "delisting",
    "delisted", "trading halt", "halted", "data breach", "hacked",
    "cyberattack", "resigns", "resignation", "steps down", "misses estimates",
    "misses expectations", "plunges", "plummets", "collapse", "scandal",
]

# ---------------------------------------------------------------------------
# Adaptive strategy (Portfolio 2 / "aggressive" only -- see adapt_strategy()).
# Every bound below is a hard safety limit: the auto-tuner can move its own
# parameters anywhere inside these ranges, but never outside them.
# ---------------------------------------------------------------------------

ADAPTIVE_STRATEGY = os.environ.get("ADAPTIVE_STRATEGY", "false").strip().lower() == "true"
STRATEGY_STATE_PATH = os.environ.get("STRATEGY_STATE_PATH", "strategy_state.json")

# Target band for realized gain per completed trade. The bot tunes its own
# sell threshold to stay inside this band -- it can never sell for less than
# SELL_MIN, and never holds out past SELL_MAX looking for more.
SELL_MIN = 10.0
SELL_MAX = 15.0

# Buy-the-dip selectivity range. More negative = requires a bigger drop =
# more selective / fewer trades. Less negative = smaller drop needed = more
# trades.
DROP_MIN = -8.0
DROP_MAX = -3.0

# Position-size range the auto-tuner can move TRADE_DOLLARS within.
TRADE_DOLLARS_MAX = 20000.0

# Step sizes for each nudge -- kept small on purpose so parameters drift
# gradually based on evidence, instead of swinging wildly run to run.
SELL_STEP = 0.5
DROP_STEP = 0.5
TRADE_DOLLARS_STEP = 500.0

TRADE_LOG_PATH = os.environ.get("TRADE_LOG_PATH", "trade_log.csv")

# Public snapshot of account state, read by the static dashboard (index.html).
# Contains no credentials -- just equity/cash/positions, which for a paper
# account is simulated money anyway.
SNAPSHOT_PATH = os.environ.get("SNAPSHOT_PATH", "account_snapshot.json")

# Append-only log of equity/cash/buying_power over time, one row per run, so
# the dashboard can plot an equity-over-time chart instead of just a single
# current snapshot.
HISTORY_PATH = os.environ.get("HISTORY_PATH", "equity_history.csv")

# Ticker -> company name lookup, read by the dashboard so it can show
# "AAPL -- Apple Inc." instead of just the bare ticker.
COMPANIES_PATH = os.environ.get("COMPANIES_PATH", "companies.json")

# Free, regularly-updated CSV of current S&P 500 constituents.
UNIVERSE_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "master/data/constituents.csv"
)

# Small hard-coded fallback in case the CSV fetch fails (rate limit, outage).
FALLBACK_TICKERS = [
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "NVDA", "TSLA", "BRK.B", "JPM",
    "V", "UNH", "HD", "PG", "MA", "XOM", "COST", "JNJ", "ABBV", "MRK", "BAC",
    "KO", "PEP", "AVGO", "WMT", "CVX", "ADBE", "CRM", "NFLX", "DIS", "PFE",
]

# Matching company names for the fallback list above.
FALLBACK_NAMES = {
    "AAPL": "Apple Inc.",
    "MSFT": "Microsoft Corporation",
    "AMZN": "Amazon.com, Inc.",
    "GOOGL": "Alphabet Inc.",
    "META": "Meta Platforms, Inc.",
    "NVDA": "NVIDIA Corporation",
    "TSLA": "Tesla, Inc.",
    "BRK.B": "Berkshire Hathaway Inc.",
    "JPM": "JPMorgan Chase & Co.",
    "V": "Visa Inc.",
    "UNH": "UnitedHealth Group Incorporated",
    "HD": "The Home Depot, Inc.",
    "PG": "The Procter & Gamble Company",
    "MA": "Mastercard Incorporated",
    "XOM": "Exxon Mobil Corporation",
    "COST": "Costco Wholesale Corporation",
    "JNJ": "Johnson & Johnson",
    "ABBV": "AbbVie Inc.",
    "MRK": "Merck & Co., Inc.",
    "BAC": "Bank of America Corporation",
    "KO": "The Coca-Cola Company",
    "PEP": "PepsiCo, Inc.",
    "AVGO": "Broadcom Inc.",
    "WMT": "Walmart Inc.",
    "CVX": "Chevron Corporation",
    "ADBE": "Adobe Inc.",
    "CRM": "Salesforce, Inc.",
    "NFLX": "Netflix, Inc.",
    "DIS": "The Walt Disney Company",
    "PFE": "Pfizer Inc.",
}


def log(message: str) -> None:
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def append_trade_log(action: str, symbol: str, detail: str) -> None:
    is_new = not os.path.exists(TRADE_LOG_PATH)
    with open(TRADE_LOG_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp_utc", "action", "symbol", "detail"])
        writer.writerow([
            dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            action,
            symbol,
            detail,
        ])


def append_equity_history(equity: float, cash: float, buying_power: float) -> None:
    is_new = not os.path.exists(HISTORY_PATH)
    with open(HISTORY_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp_utc", "equity", "cash", "buying_power"])
        writer.writerow([
            dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            f"{equity:.2f}",
            f"{cash:.2f}",
            f"{buying_power:.2f}",
        ])


def load_strategy_state() -> dict:
    """Load the adaptive strategy's current parameters, or seed defaults."""
    if os.path.exists(STRATEGY_STATE_PATH):
        try:
            with open(STRATEGY_STATE_PATH) as f:
                state = json.load(f)
            # Re-clamp on load in case bounds changed since this file was
            # last written, or the file is malformed/from an older version.
            state["drop_threshold_pct"] = min(DROP_MAX, max(DROP_MIN, float(state.get("drop_threshold_pct", DROP_THRESHOLD_PCT))))
            state["sell_threshold_pct"] = min(SELL_MAX, max(SELL_MIN, float(state.get("sell_threshold_pct", SELL_THRESHOLD_PCT))))
            state["trade_dollars"] = min(TRADE_DOLLARS_MAX, max(MIN_TRADE_DOLLARS, float(state.get("trade_dollars", TRADE_DOLLARS))))
            state["completed_trades"] = int(state.get("completed_trades", 0))
            return state
        except Exception as exc:  # noqa: BLE001
            log(f"Could not load strategy state ({exc}); starting from defaults.")
    return {
        "drop_threshold_pct": DROP_THRESHOLD_PCT,
        "sell_threshold_pct": min(SELL_MAX, max(SELL_MIN, SELL_THRESHOLD_PCT)),
        "trade_dollars": min(TRADE_DOLLARS_MAX, max(MIN_TRADE_DOLLARS, TRADE_DOLLARS)),
        "completed_trades": 0,
        "last_adjustment": "initial defaults",
    }


def save_strategy_state(state: dict) -> None:
    try:
        state["updated_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with open(STRATEGY_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:  # noqa: BLE001
        log(f"Could not save strategy state: {exc}")


def adapt_strategy(state: dict, sell_events: list, buy_events: list, cash: float, equity: float) -> dict:
    """Nudge this portfolio's own thresholds based on how its own recent
    trades went. Bounded, rule-based auto-tuning -- not machine learning --
    but genuinely adaptive: every change below is a small step driven by
    this run's evidence, and every parameter has a hard floor/ceiling
    (SELL_MIN/MAX, DROP_MIN/MAX, MIN_TRADE_DOLLARS/TRADE_DOLLARS_MAX) it can
    never cross, so the strategy can't drift into reckless territory.
    """
    global DROP_THRESHOLD_PCT, SELL_THRESHOLD_PCT, TRADE_DOLLARS
    notes = []

    # Separate genuine profit-take sells from forced bad-news exits -- only
    # the former are evidence about whether the sell target is well-tuned.
    # A news-driven exit is a risk-management event, not a signal that the
    # target itself needs adjusting.
    target_sells = [ev for ev in sell_events if ev.get("reason") != "bad_news"]
    news_exits = [ev for ev in sell_events if ev.get("reason") == "bad_news"]

    if news_exits:
        state["news_exits"] = state.get("news_exits", 0) + len(news_exits)
        notes.append(f"{len(news_exits)} early exit(s) on bad news this run "
                     f"({', '.join(ev['symbol'] for ev in news_exits)})")

    # Learn from each completed trade: did the price blow well past our
    # target before we sold (room to aim higher), or did it just barely
    # scrape over the line (aim lower so trades complete more reliably)?
    for ev in target_sells:
        overshoot = ev["gain_pct"] - state["sell_threshold_pct"]
        if overshoot > 3.0:
            new_val = min(SELL_MAX, round(state["sell_threshold_pct"] + SELL_STEP, 2))
            if new_val != state["sell_threshold_pct"]:
                notes.append(f"{ev['symbol']} cleared target by {overshoot:.1f}pp -> raised sell target to {new_val:.1f}%")
            state["sell_threshold_pct"] = new_val
        elif overshoot < 0.5:
            new_val = max(SELL_MIN, round(state["sell_threshold_pct"] - SELL_STEP, 2))
            if new_val != state["sell_threshold_pct"]:
                notes.append(f"{ev['symbol']} barely cleared target -> lowered sell target to {new_val:.1f}%")
            state["sell_threshold_pct"] = new_val
        state["completed_trades"] = state.get("completed_trades", 0) + 1

    # How much of the account is currently tied up in open positions?
    utilization = (1.0 - (cash / equity)) if equity else 0.0

    # Every 3rd completed (profit-take) trade, reconsider position size:
    # press size up when capital is mostly free (there's room to do more),
    # trim it down when too much capital is already tied up in open
    # positions.
    if target_sells and state["completed_trades"] % 3 == 0:
        if utilization < 0.5:
            new_size = min(TRADE_DOLLARS_MAX, state["trade_dollars"] + TRADE_DOLLARS_STEP)
            if new_size != state["trade_dollars"]:
                notes.append(f"capital utilization low ({utilization * 100:.0f}%) -> raised trade size to ${new_size:,.0f}")
            state["trade_dollars"] = new_size
        elif utilization > 0.8:
            new_size = max(MIN_TRADE_DOLLARS, state["trade_dollars"] - TRADE_DOLLARS_STEP)
            if new_size != state["trade_dollars"]:
                notes.append(f"capital utilization high ({utilization * 100:.0f}%) -> trimmed trade size to ${new_size:,.0f}")
            state["trade_dollars"] = new_size

    # Learn entry selectivity from capital pressure: tighten the dip
    # requirement when nearly all capital is already deployed (don't
    # overextend), loosen it when there's plenty of idle cash and this
    # scan didn't find anything to buy.
    if utilization > 0.85:
        new_drop = max(DROP_MIN, round(state["drop_threshold_pct"] - DROP_STEP, 2))
        if new_drop != state["drop_threshold_pct"]:
            notes.append(f"capital nearly fully deployed -> tightened dip threshold to {new_drop:.1f}%")
        state["drop_threshold_pct"] = new_drop
    elif utilization < 0.3 and not buy_events:
        new_drop = min(DROP_MAX, round(state["drop_threshold_pct"] + DROP_STEP, 2))
        if new_drop != state["drop_threshold_pct"]:
            notes.append(f"plenty of idle cash, no dips found -> loosened dip threshold to {new_drop:.1f}%")
        state["drop_threshold_pct"] = new_drop

    DROP_THRESHOLD_PCT = state["drop_threshold_pct"]
    SELL_THRESHOLD_PCT = state["sell_threshold_pct"]
    TRADE_DOLLARS = state["trade_dollars"]
    state["last_adjustment"] = "; ".join(notes) if notes else "no change this run"
    return state


def get_client() -> TradingClient:
    if not API_KEY or not API_SECRET:
        log("ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY are not set.")
        sys.exit(1)
    return TradingClient(API_KEY, API_SECRET, paper=PAPER)


def get_news_client() -> NewsClient:
    """News data is a separate, free Alpaca endpoint available to every
    account (paper or live) -- same API_KEY/API_SECRET, no extra setup."""
    return NewsClient(API_KEY, API_SECRET)


def fetch_recent_headlines(news_client: NewsClient, symbol: str) -> list:
    """Returns recent headline+summary text for symbol from the last
    NEWS_LOOKBACK_HOURS. Returns [] on any failure or if nothing is found --
    callers must treat that as "no signal", not as bad news, so a news-API
    hiccup or a quiet stock never blocks trading on its own.
    """
    try:
        start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=NEWS_LOOKBACK_HOURS)
        request = NewsRequest(
            symbols=symbol,
            start=start,
            limit=NEWS_HEADLINE_LIMIT,
            include_content=False,
        )
        news_set = news_client.get_news(request)
        articles = news_set.data.get("news", []) if news_set and news_set.data else []
        return [f"{a.headline or ''} {a.summary or ''}".strip() for a in articles]
    except Exception as exc:  # noqa: BLE001
        log(f"Could not fetch news for {symbol}: {exc}")
        return []


def classify_news(texts: list) -> dict:
    """Transparent keyword scan of recent headlines/summaries -- not an
    ML/sentiment model. Flags 'bad' if any bad-news keyword shows up
    anywhere in the recent coverage; every match is reported so the trade
    log always shows exactly why a trade was skipped or an exit was forced.
    """
    matched = []
    for text in texts:
        lower = text.lower()
        for kw in BAD_NEWS_KEYWORDS:
            if kw in lower:
                matched.append(kw)
    return {
        "bad": bool(matched),
        "matched": sorted(set(matched)),
        "headline_count": len(texts),
    }


def market_is_open(client: TradingClient) -> bool:
    clock = client.get_clock()
    return bool(clock.is_open)


def get_universe() -> tuple:
    """Returns (tickers, names) where names maps ticker -> company name."""
    try:
        df = pd.read_csv(UNIVERSE_URL)
        df["Symbol"] = df["Symbol"].astype(str).str.replace(".", "-", regex=False)
        tickers = df["Symbol"].tolist()
        names = dict(zip(df["Symbol"], df["Security"].astype(str)))
        if tickers:
            return tickers, names
    except Exception as exc:  # noqa: BLE001
        log(f"Could not fetch S&P 500 list ({exc}); using fallback ticker list.")
    return FALLBACK_TICKERS, FALLBACK_NAMES


def write_companies(names: dict) -> None:
    """Dump the ticker -> company name lookup for the dashboard to read."""
    try:
        with open(COMPANIES_PATH, "w") as f:
            json.dump(names, f, indent=2, sort_keys=True)
    except Exception as exc:  # noqa: BLE001
        log(f"Could not save companies file: {exc}")


def has_open_position(client: TradingClient, symbol: str) -> bool:
    """True if we currently hold shares of symbol.

    This checks live position state rather than order history on purpose:
    the bot is meant to be able to buy the same stock as many times as it
    wants in a day, including re-entering right after a prior round trip
    (buy -> recover 7% -> sell) closes out earlier the same day. Checking
    order history instead would keep blocking re-buys for the rest of the
    day after the very first fill, even once that position is flat again.
    """
    try:
        client.get_open_position(symbol)
        return True
    except Exception:
        # Alpaca raises when there is no open position for the symbol.
        return False


def fetch_price_changes(tickers: list) -> dict:
    """Returns {symbol: pct_change_vs_prev_close} using one batched download."""
    changes = {}
    if not tickers:
        return changes
    try:
        data = yf.download(
            tickers,
            period="5d",
            interval="1d",
            group_by="ticker",
            threads=True,
            progress=False,
            auto_adjust=False,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"Batch price download failed: {exc}")
        return changes

    for symbol in tickers:
        try:
            if len(tickers) == 1:
                closes = data["Close"].dropna()
            else:
                closes = data[symbol]["Close"].dropna()
            if len(closes) < 2:
                continue
            prev_close = float(closes.iloc[-2])
            last_close = float(closes.iloc[-1])
            if prev_close <= 0:
                continue
            pct_change = (last_close - prev_close) / prev_close * 100
            changes[symbol] = pct_change
        except Exception:  # noqa: BLE001
            continue
    return changes


def check_buys(client: TradingClient, news_client: NewsClient, tickers: list, names: dict) -> list:
    """Returns the list of buy events executed this run (symbol + drop %)."""
    events = []
    changes = fetch_price_changes(tickers)
    dropped = {s: c for s, c in changes.items() if c <= DROP_THRESHOLD_PCT}
    log(f"Scanned {len(changes)} tickers, {len(dropped)} down {DROP_THRESHOLD_PCT}% or more.")

    for symbol, pct_change in dropped.items():
        if has_open_position(client, symbol):
            log(f"Skip {symbol}: already holding an open position.")
            continue

        headlines = fetch_recent_headlines(news_client, symbol)
        news = classify_news(headlines)
        if news["bad"]:
            log(f"Skip {symbol}: bad news detected ({', '.join(news['matched'])}) "
                f"across {news['headline_count']} recent headline(s).")
            continue

        try:
            order = MarketOrderRequest(
                symbol=symbol,
                notional=round(TRADE_DOLLARS, 2),
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
            client.submit_order(order)
            company = names.get(symbol, "")
            news_note = f"news=clear({news['headline_count']})" if news["headline_count"] else "news=none"
            detail = f"drop={pct_change:.2f}% notional=${TRADE_DOLLARS:.2f} {news_note}"
            log(f"BUY {symbol} ({company}): {detail}")
            append_trade_log("BUY", symbol, detail)
            events.append({"symbol": symbol, "drop_pct": pct_change})
        except Exception as exc:  # noqa: BLE001
            log(f"BUY order failed for {symbol}: {exc}")
    return events


def check_sells(client: TradingClient, news_client: NewsClient, names: dict) -> list:
    """Returns the list of sell events executed this run (symbol + realized
    gain % + reason: "target_reached" or "bad_news"). A bad_news exit is the
    one path where this strategy can realize a loss -- everywhere else it
    only ever sells once a position is already profitable.
    """
    events = []
    try:
        positions = client.get_all_positions()
    except Exception as exc:  # noqa: BLE001
        log(f"Could not fetch positions: {exc}")
        return events

    for pos in positions:
        try:
            gain_pct = float(pos.unrealized_plpc) * 100
        except (TypeError, ValueError):
            continue

        reason = None
        matched_keywords = []
        if gain_pct >= SELL_THRESHOLD_PCT:
            reason = "target_reached"
        else:
            headlines = fetch_recent_headlines(news_client, pos.symbol)
            news = classify_news(headlines)
            if news["bad"]:
                reason = "bad_news"
                matched_keywords = news["matched"]

        if reason is None:
            continue

        try:
            client.close_position(pos.symbol)
            company = names.get(pos.symbol, "")
            if reason == "target_reached":
                detail = f"gain={gain_pct:.2f}% qty={pos.qty} reason=target_reached"
            else:
                detail = f"gain={gain_pct:.2f}% qty={pos.qty} reason=bad_news:{','.join(matched_keywords)}"
            log(f"SELL {pos.symbol} ({company}): {detail}")
            append_trade_log("SELL", pos.symbol, detail)
            events.append({"symbol": pos.symbol, "gain_pct": gain_pct, "reason": reason})
        except Exception as exc:  # noqa: BLE001
            log(f"SELL order failed for {pos.symbol}: {exc}")
    return events


def write_snapshot(client: TradingClient, names: dict, state: dict = None) -> None:
    """Dump equity/cash/positions to a public JSON file for the dashboard.

    No API keys or secrets are ever written here -- only account totals and
    position data, which for a paper account is simulated money anyway.
    """
    try:
        account = client.get_account()
        positions = client.get_all_positions()
    except Exception as exc:  # noqa: BLE001
        log(f"Could not write snapshot: {exc}")
        return

    snapshot = {
        "updated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "paper": PAPER,
        "equity": float(account.equity),
        "cash": float(account.cash),
        "buying_power": float(account.buying_power),
        "starting_equity": STARTING_EQUITY,
        "drop_threshold_pct": DROP_THRESHOLD_PCT,
        "sell_threshold_pct": SELL_THRESHOLD_PCT,
        "trade_dollars": TRADE_DOLLARS,
        "adaptive": ADAPTIVE_STRATEGY,
        "positions": [
            {
                "symbol": p.symbol,
                "name": names.get(p.symbol, ""),
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price) if p.current_price else None,
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc) * 100,
            }
            for p in positions
        ],
    }
    if ADAPTIVE_STRATEGY and state:
        snapshot["adaptive_target_band_pct"] = [SELL_MIN, SELL_MAX]
        snapshot["completed_trades"] = state.get("completed_trades", 0)
        snapshot["last_adjustment"] = state.get("last_adjustment", "")
    try:
        with open(SNAPSHOT_PATH, "w") as f:
            json.dump(snapshot, f, indent=2)
        log(f"Wrote snapshot: equity=${snapshot['equity']:.2f}, "
            f"{len(snapshot['positions'])} open position(s).")
    except Exception as exc:  # noqa: BLE001
        log(f"Could not save snapshot file: {exc}")

    try:
        append_equity_history(snapshot["equity"], snapshot["cash"], snapshot["buying_power"])
    except Exception as exc:  # noqa: BLE001
        log(f"Could not append equity history: {exc}")


def main() -> None:
    global DROP_THRESHOLD_PCT, SELL_THRESHOLD_PCT, TRADE_DOLLARS
    client = get_client()
    news_client = get_news_client()

    # One-off diagnostic path: set DEBUG_NEWS_SYMBOL to sanity-check the
    # News API connection/parsing without touching any trading logic, even
    # outside market hours. No side effects -- does not place orders, does
    # not write any files.
    debug_symbol = os.environ.get("DEBUG_NEWS_SYMBOL", "").strip()
    if debug_symbol:
        headlines = fetch_recent_headlines(news_client, debug_symbol)
        news = classify_news(headlines)
        log(f"[news self-test] {debug_symbol}: headline_count={news['headline_count']} "
            f"bad={news['bad']} matched={news['matched']}")
        for h in headlines[:5]:
            log(f"[news self-test] headline: {h[:200]}")
        return

    state = None
    if ADAPTIVE_STRATEGY:
        state = load_strategy_state()
        DROP_THRESHOLD_PCT = state["drop_threshold_pct"]
        SELL_THRESHOLD_PCT = state["sell_threshold_pct"]
        TRADE_DOLLARS = state["trade_dollars"]
        log(f"Adaptive strategy loaded: drop={DROP_THRESHOLD_PCT}%, sell={SELL_THRESHOLD_PCT}%, "
            f"trade_dollars=${TRADE_DOLLARS}, completed_trades={state['completed_trades']} "
            f"(last: {state.get('last_adjustment', '-')}).")

    log(f"Starting run (paper={PAPER}, drop_threshold={DROP_THRESHOLD_PCT}%, "
        f"sell_threshold={SELL_THRESHOLD_PCT}%, trade_dollars=${TRADE_DOLLARS}, "
        f"adaptive={ADAPTIVE_STRATEGY}).")

    if not market_is_open(client):
        log("Market is closed. Exiting without scanning.")
        if ADAPTIVE_STRATEGY and state is not None:
            # Persist immediately (even with no trades this run) so the
            # state file always exists on disk/in the repo from the very
            # first run, rather than only appearing once the market is
            # next open and a scan actually happens.
            save_strategy_state(state)
        write_snapshot(client, {}, state)
        return

    tickers, names = get_universe()
    write_companies(names)

    # Check exits first so a sale can free up buying power for new dips.
    sell_events = check_sells(client, news_client, names)
    buy_events = check_buys(client, news_client, tickers, names)

    if ADAPTIVE_STRATEGY:
        try:
            account = client.get_account()
            state = adapt_strategy(state, sell_events, buy_events, float(account.cash), float(account.equity))
            save_strategy_state(state)
            log(f"Adaptive strategy updated: {state.get('last_adjustment')}")
        except Exception as exc:  # noqa: BLE001
            log(f"Could not update adaptive strategy: {exc}")

    write_snapshot(client, names, state)
    log("Run complete.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("Unhandled error:")
        traceback.print_exc()
        sys.exit(1)
