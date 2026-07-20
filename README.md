# Dip-and-Recover Trading Bot

Watches the S&P 500. Buys a stock when it drops 5%+ vs. the previous close.
Sells (closes) any position once it has recovered 7%+ from its average entry
price. Can re-buy and re-sell the same stock any number of times. Runs for
free on GitHub Actions and trades on Alpaca's **paper trading** accounts by
default, so no real money is at risk unless you deliberately switch it to
live mode.

Runs **two separate paper portfolios side by side**, both starting with
$500,000 and using the identical buy/sell signals — the only difference is
position size:

| | Trade size | Alpaca account |
|---|---|---|
| **Portfolio 1 — Standard** | $10,000 per position | your first paper account |
| **Portfolio 2 — Aggressive** | $12,500 per position (+25%) | a second, separate paper account |

A live public dashboard (`index.html`, hosted free on GitHub Pages) shows
both portfolios' equity, open positions, and trade history side by side.

**This is not financial advice, and this is not a proven profitable
strategy** — "buy the dip, sell the bounce" can lose money badly in a real
downtrend (a stock down 5% can keep falling another 30%). Paper trade it for
a while and look at the results before ever considering live money.

## What's in here

- `trading_bot.py` — the strategy logic (buy/sell decisions, order submission).
- `requirements.txt` — Python dependencies.
- `.github/workflows/trading-bot.yml` — runs both portfolios every 15 minutes
  during market hours, for free, on GitHub's infrastructure. Portfolio 2 runs
  after Portfolio 1 so their commits don't collide.
- `index.html` — the public dashboard, served by GitHub Pages.
- `trade_log.csv` / `trade_log_aggressive.csv` — running logs of every
  buy/sell for portfolio 1 / portfolio 2, committed back to the repo after
  each run.
- `account_snapshot.json` / `account_snapshot_aggressive.json` — current
  equity, cash, and open positions for each portfolio (no credentials in
  here — just totals, which for a paper account are simulated anyway).
- `companies.json` — ticker → company name lookup, shared by both portfolios,
  so the dashboard can show "AAPL — Apple Inc." instead of a bare ticker.

## One-time setup (about 15 minutes, all free)

1. **Create two Alpaca paper accounts:** go to [alpaca.markets](https://alpaca.markets),
   sign up, and make sure you're on **Paper Trading**. Alpaca's dashboard lets
   you create more than one paper account under the same login — set one up
   for Portfolio 1 and a second, separate one for Portfolio 2. For each
   account: set its starting balance to **$500,000**, then generate a Paper
   API Key ID + Secret Key. You'll end up with two independent key pairs.
   (Do not use live keys yet.)

2. **Create a GitHub repository** (also free) and add these files to it —
   either drag-and-drop them in the GitHub web UI, or:
   ```
   git init
   git add .
   git commit -m "Initial trading bot"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<your-repo>.git
   git push -u origin main
   ```

3. **Add your Alpaca keys as GitHub secrets:** in your repo, go to
   `Settings → Secrets and variables → Actions → New repository secret` and add
   all four (from the **Repository secrets** section, not Environment secrets):
   - `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` — Portfolio 1's keys
   - `ALPACA_API_KEY_2` / `ALPACA_SECRET_KEY_2` — Portfolio 2's keys

4. **Enable the workflow:** go to the `Actions` tab of your repo — GitHub
   may ask you to confirm you want workflows enabled. Once enabled, both
   portfolios run automatically every 15 minutes while the market is open.
   You can also trigger a run manually any time from
   `Actions → Trading Bot → Run workflow`.

5. **Enable GitHub Pages:** in `Settings → Pages`, set Source to
   "Deploy from a branch", branch `main`, folder `/ (root)`, then Save. Your
   dashboard will be live at `https://<your-username>.github.io/<your-repo>/`.

6. **Check on it:** open the dashboard link above to see both portfolios'
   equity and trade history, or log into each Alpaca account to see the
   same thing directly.

## Configuration

Edit the `env:` block for each job in `.github/workflows/trading-bot.yml`
(`run-bot` = Portfolio 1, `run-bot-aggressive` = Portfolio 2):

| Variable | Portfolio 1 | Portfolio 2 | Meaning |
|---|---|---|---|
| `DROP_THRESHOLD_PCT` | `-5.0` | `-5.0` | Buy trigger: stock down this % or more vs. previous close |
| `SELL_THRESHOLD_PCT` | `7.0` | `7.0` | Sell trigger: position up this % or more vs. average entry price |
| `TRADE_DOLLARS` | `10000` | `12500` | Dollars spent on each new buy signal |
| `ALPACA_PAPER` | `true` | `true` | `true` = simulated money, `false` = real money (see below) |

The task brief said "sell when it recovers 7-10%" — the bot sells as soon as
the gain crosses 7% rather than waiting for 10%, so it doesn't risk giving
back profit while waiting. Raise `SELL_THRESHOLD_PCT` to `10.0` if you'd
rather hold out for the bigger bounce (with more risk of the gain evaporating
first).

Want a third portfolio, or a different flavor of "aggressive" (e.g. buying
on smaller dips instead of bigger trade size)? Copy the `run-bot-aggressive`
job block, give it a new name, point it at a third Alpaca account's secrets,
and give it its own `TRADE_LOG_PATH` / `SNAPSHOT_PATH` file names — then add
a matching entry to the `PORTFOLIOS` array near the top of the `<script>` in
`index.html`.

## Switching to live trading (real money)

Only do this after you've watched the paper versions run for a while and are
comfortable with what they do:

1. Generate **live** API keys in Alpaca (separate from paper keys), for
   whichever portfolio(s) you want to go live.
2. Replace that portfolio's `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` secrets
   with the live ones.
3. Change that job's `ALPACA_PAPER: "true"` to `ALPACA_PAPER: "false"` in the
   workflow file.

Alpaca will then place real orders with real funds in your account. You are
fully responsible for funding, monitoring, and any losses from that point on.

## Known limitations

- **Price data** comes from `yfinance` (free, unofficial Yahoo Finance data).
  It's fine for a strategy like this but isn't institutional-grade real-time
  data — there can be brief delays or gaps.
- **News reading:** this version triggers purely on price (a clean, reliable
  signal with no extra API cost). It doesn't parse news articles to explain
  *why* a stock dropped — it can't tell a temporary overreaction from a stock
  that's down 5% because of a real, ongoing problem. Add a news/sentiment
  filter later if you want that nuance (it adds cost and complexity).
- **GitHub Actions free tier**: public repos get unlimited free Action
  minutes; private repos get 2,000 free minutes/month, which comfortably
  covers two portfolios running every 15 minutes during market hours.
- **Position sizing** is a flat dollar amount per signal — it doesn't account
  for your total account risk, diversification, or stop-losses if a stock
  keeps falling instead of recovering.
- **Public dashboard**: since the repo (and Pages site) is public, anyone
  with the link can see both portfolios' balances and trade history. That's
  harmless for a paper-trading demo, but worth remembering if you ever go
  live with real money.
