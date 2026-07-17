# Dip-and-Recover Trading Bot

Watches the S&P 500. Buys a stock when it drops 5%+ vs. the previous close.
Sells (closes) any position once it has recovered 7%+ from its average entry
price. Can re-buy and re-sell the same stock any number of times. Runs for
free on GitHub Actions and trades on Alpaca's **paper trading** account by
default, so no real money is at risk unless you deliberately switch it to
live mode.

**This is not financial advice, and this is not a proven profitable
strategy** -- "buy the dip, sell the bounce" can lose money badly in a real
downtrend (a stock down 5% can keep falling another 30%). Paper trade it for
a while and look at the results before ever considering live money.

## What's in here

- `trading_bot.py` -- the strategy logic (buy/sell decisions, order submission).
- - `requirements.txt` -- Python dependencies.
  - - `.github/workflows/trading-bot.yml` -- schedules the bot to run every 15
    -   minutes during market hours, for free, on GitHub's infrastructure.
    -   - `trade_log.csv` -- created automatically; a running log of every buy/sell
        -   the bot makes, committed back to the repo after each run.
     
        -   ## One-time setup (about 10 minutes, all free)
     
        -   1. Create a free Alpaca account: go to alpaca.markets, sign up, then switch
            2.    to Paper Trading in the dashboard and generate a Paper API Key ID + Secret
            3.   Key. (Do not use live keys yet.)
         
            4.   2. Create a GitHub repository (also free) and add these files to it.
              
                 3. 3. Add your Alpaca keys as GitHub secrets: in your repo, go to
                    4.    Settings -> Secrets and variables -> Actions -> New repository secret,
                    5.   and add ALPACA_API_KEY and ALPACA_SECRET_KEY.
                   
                    6.   4. Enable the workflow: go to the Actions tab of your repo. Once enabled,
                         5.    the bot runs automatically every 15 minutes while the market is open.
                         6.   You can also trigger a run manually any time from
                         7.      Actions -> Trading Bot -> Run workflow.
                      
                         8.  5. Check on it: open trade_log.csv in your repo to see every buy/sell, or
                             6.    log into your Alpaca dashboard to see positions and P/L directly.
                           
                             7.## Configuration

                             Edit the env block in .github/workflows/trading-bot.yml:

                             - DROP_THRESHOLD_PCT (default -5.0): buy trigger, stock down this % or more
                             -   vs. previous close
                             -   - SELL_THRESHOLD_PCT (default 7.0): sell trigger, position up this % or more
                                 -   vs. average entry price
                                 -   - TRADE_DOLLARS (default 500): dollars spent on each new buy signal
                                     - - ALPACA_PAPER (default true): true = simulated money, false = real money
                                      
                                       - The task brief said "sell when it recovers 7-10%" -- the bot sells as soon
                                       - as the gain crosses 7% rather than waiting for 10%, so it doesn't risk
                                       - giving back profit while waiting. Raise SELL_THRESHOLD_PCT to 10.0 if you'd
                                       - rather hold out for the bigger bounce (with more risk of the gain
                                       - evaporating first).
                                      
                                       - ## Switching to live trading (real money)
                                      
                                       - Only do this after you've watched the paper version run for a while and are
                                       - comfortable with what it does:
                                      
                                       - 1. Generate live API keys in Alpaca (separate from paper keys).
                                         2. 2. Replace the ALPACA_API_KEY / ALPACA_SECRET_KEY secrets with the live ones.
                                            3. 3. Change ALPACA_PAPER: "true" to ALPACA_PAPER: "false" in the workflow file.
                                              
                                               4. Alpaca will then place real orders with real funds in your account. You are
                                               5. fully responsible for funding, monitoring, and any losses from that point on.
                                              
                                               6. ## Known limitations
                                              
                                               7. - Price data comes from yfinance (free, unofficial Yahoo Finance data). It's
                                                  -   fine for a strategy like this but isn't institutional-grade real-time
                                                  -     data -- there can be brief delays or gaps.
                                                  - - News reading: this version triggers purely on price (a clean, reliable
                                                    -   signal with no extra API cost). It doesn't parse news articles to explain
                                                    -     why a stock dropped -- it can't tell a temporary overreaction from a stock
                                                    -   that's down 5% because of a real, ongoing problem.
                                                    -   - GitHub Actions free tier: public repos get unlimited free Action minutes;
                                                        -   private repos get 2,000 free minutes/month, which comfortably covers a
                                                        -     bot running every 15 minutes during market hours.
                                                        - - Position sizing is a flat dollar amount per signal -- it doesn't account
                                                          -   for your total account risk, diversification, or stop-losses if a stock
                                                          -     keeps falling instead of recovering.
                                                          - 
