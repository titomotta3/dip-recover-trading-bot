"""
Dip-and-recover trading bot.

Strategy:
  - Watches the S&P 500 for stocks that have dropped 5%+ vs. the previous close.
  - Buys a fixed dollar amount of each stock that trips the drop threshold
    (skips it if we already bought it earlier today).
  - Watches all open positions and sells (closes) any position once it has
    recovered 7%+ from its average entry price.
  - Runs against Alpaca's paper trading API by default. Nothing here places
    real trades unless ALPACA_PAPER is explicitly set to "false" AND you
    supply live API keys.

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
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

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

# Dollar amount to spend on each new buy signal (notional order).
TRADE_DOLLARS = float(os.environ.get("TRADE_DOLLARS", "10000"))

TRADE_LOG_PATH = os.environ.get("TRADE_LOG_PATH", "trade_log.csv")

# Public snapshot of account state, read by the static dashboard (index.html).
# Contains no credentials -- just equity/cash/positions, which for a paper
# account is simulated money anyway.
SNAPSHOT_PATH = os.environ.get("SNAPSHOT_PATH", "account_snapshot.json")

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


def get_client() -> TradingClient:
    if not API_KEY or not API_SECRET:
        log("ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY are not set.")
        sys.exit(1)
    return TradingClient(API_KEY, API_SECRET, paper=PAPER)


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


def already_bought_today(client: TradingClient, symbol: str) -> bool:
    today_start = dt.datetime.combine(
        dt.datetime.now(dt.timezone.utc).date(), dt.time.min, tzinfo=dt.timezone.utc
    )
    request = GetOrdersRequest(
        status=QueryOrderStatus.ALL,
        symbols=[symbol],
        after=today_start,
    )
    try:
        orders = client.get_orders(request)
    except Exception as exc:  # noqa: BLE001
        log(f"Could not check order history for {symbol}: {exc}")
        return False
    live_states = {
        "filled", "partially_filled", "new", "accepted",
        "pending_new", "accepted_for_bidding",
    }
    return any(
        o.side == OrderSide.BUY and str(o.status.value) in live_states
        for o in orders
    )


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


def check_buys(client: TradingClient, tickers: list, names: dict) -> None:
    changes = fetch_price_changes(tickers)
    dropped = {s: c for s, c in changes.items() if c <= DROP_THRESHOLD_PCT}
    log(f"Scanned {len(changes)} tickers, {len(dropped)} down {DROP_THRESHOLD_PCT}% or more.")

    for symbol, pct_change in dropped.items():
        if already_bought_today(client, symbol):
            log(f"Skip {symbol}: already have an open buy order today.")
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
            detail = f"drop={pct_change:.2f}% notional=${TRADE_DOLLARS:.2f}"
            log(f"BUY {symbol} ({company}): {detail}")
            append_trade_log("BUY", symbol, detail)
        except Exception as exc:  # noqa: BLE001
            log(f"BUY order failed for {symbol}: {exc}")


def check_sells(client: TradingClient, names: dict) -> None:
    try:
        positions = client.get_all_positions()
    except Exception as exc:  # noqa: BLE001
        log(f"Could not fetch positions: {exc}")
        return

    for pos in positions:
        try:
            gain_pct = float(pos.unrealized_plpc) * 100
        except (TypeError, ValueError):
            continue
        if gain_pct >= SELL_THRESHOLD_PCT:
            try:
                client.close_position(pos.symbol)
                company = names.get(pos.symbol, "")
                detail = f"gain={gain_pct:.2f}% qty={pos.qty}"
                log(f"SELL {pos.symbol} ({company}): {detail}")
                append_trade_log("SELL", pos.symbol, detail)
            except Exception as exc:  # noqa: BLE001
                log(f"SELL order failed for {pos.symbol}: {exc}")


def write_snapshot(client: TradingClient, names: dict) -> None:
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
        "drop_threshold_pct": DROP_THRESHOLD_PCT,
        "sell_threshold_pct": SELL_THRESHOLD_PCT,
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
    try:
        with open(SNAPSHOT_PATH, "w") as f:
            json.dump(snapshot, f, indent=2)
        log(f"Wrote snapshot: equity=${snapshot['equity']:.2f}, "
            f"{len(snapshot['positions'])} open position(s).")
    except Exception as exc:  # noqa: BLE001
        log(f"Could not save snapshot file: {exc}")


def main() -> None:
    client = get_client()
    log(f"Starting run (paper={PAPER}, drop_threshold={DROP_THRESHOLD_PCT}%, "
        f"sell_threshold={SELL_THRESHOLD_PCT}%, trade_dollars=${TRADE_DOLLARS}).")

    if not market_is_open(client):
        log("Market is closed. Exiting without scanning.")
        write_snapshot(client, {})
        return

    tickers, names = get_universe()
    write_companies(names)

    # Check exits first so a sale can free up buying power for new dips.
    check_sells(client, names)
    check_buys(client, tickers, names)

    write_snapshot(client, names)
    log("Run complete.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("Unhandled error:")
        traceback.print_exc()
        sys.exit(1)
