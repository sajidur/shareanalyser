# BD Stock Tracker

A personal analysis and daily monitoring tool for the **Dhaka Stock Exchange (DSE)**.
It runs on your PC: a local web dashboard, data stored in your **MySQL** server, and a
Windows scheduled task that checks the market **every trading day**.

> Signals are rule-based summaries of chart indicators for personal research. They are not
> investment advice. Always check company disclosures and fundamentals on DSE.

## What you get

| Page | What it shows |
|---|---|
| **Dashboard** | DSEX / DS30 / DSES, turnover (Tk crore), volume, breadth (advanced / declined), index trend, daily turnover bars, sector performance, top gainers, losers and turnover, your watchlist, portfolio and recent alerts |
| **Market** | All ~630 DSE instruments with live price, change, volume, value, RSI, signal and P/E. Filter by sector, category (A/B/N/Z) and type. Star a row to add it to the watchlist |
| **Stock page** | Candlestick chart with SMA 20/50/200 and Bollinger bands, plus linked RSI and MACD panes. Technical score with the reasons behind it, key levels and pivot points, 1W–1Y returns, fundamentals (P/E, EPS, NAV, P/NAV, dividend yield, market cap, free float), earnings and dividend history, shareholding pattern, and your position |
| **Prediction** | Type any trading code to see its likely price range for the next session, 1 week, 1 month and 3 months, a forecast cone chart, planning levels (support/resistance, volatility stop, target, reward:risk), what the model sees, and that stock's past forecasts against what actually happened. Also two **Top 10** lists: short-term setups (1–4 weeks) and long-term regular-buying candidates |
| **Accuracy** | Daily check of forecasts against real closing prices: % inside the likely/wide range, direction hit rate, average miss, per day and stock by stock (filter to your watchlist/holdings or misses only) |
| **News** | DSE company announcements on the dashboard, each stock's prediction page and the daily report, tagged (earnings vs last year, dividend / no dividend, record date, spot market, negative/positive cues) |
| **Screener** | Presets: bullish/bearish score, oversold/overbought RSI, golden cross, MACD crossover, volume spike, new 52-week high, near 52-week low, above 200-day average, low P/E, near upper circuit. Liquidity filters and a Z-category exclusion |
| **Watchlist** | Stocks you follow, with signals, 52-week position and flags |
| **Portfolio** | Buy/sell transactions, average-cost holdings, unrealised, realised and today's P/L, sector allocation. Broker commission is applied automatically (default 0.40%) |
| **Alerts** | Price above/below, day gain/loss %, RSI above/below, volume spike, score thresholds, new 52-week high/low, golden cross, MACD crossover |
| **Daily reports** | One report per session: market summary, movers, sectors, signal highlights, watchlist, portfolio and triggered alerts |
| **Notifications** | Windows desktop toasts (built in). Optional Telegram messages to your phone |

## Setup (once)

```powershell
cd "C:\Users\sajidur\Project\Share Market"
pip install -r requirements.txt
python run.py setup          # MySQL host/user/password -> config.ini, creates the `bd_stock` database
python run.py daily          # first run downloads ~1 year of history for every stock (~15-20 min)
powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -StartServerAtLogon
```

Then open the dashboard: double-click **start_dashboard.bat** (or `python run.py serve --open`)
and go to <http://127.0.0.1:8050>.

`config.ini` holds your MySQL password in plain text. It is git-ignored; keep it private.

## How the daily monitoring works

* **Scheduled task "BD Stock Tracker - Daily"** runs `python run.py daily` at **2:30 PM, Sunday to Thursday**.
  If the PC was off or asleep, Windows runs it as soon as possible afterwards. Each run:
  1. fetches the DSE snapshot and stores the session's closing prices for every instrument;
  2. refreshes index history (last 30 sessions) and the P/E table;
  3. finds missing trading days (PC off, holidays you missed) and re-downloads history to fill them;
  4. runs the technical analysis for every stock;
  5. checks all your alerts;
  6. builds the daily report;
  7. sends one summary notification, for example "DSE 22 Nov: DSEX 5,566 (+1.09%)".

  It is safe to run more than once. A MySQL lock prevents two runs at the same time, and you
  are notified only once per session.
* **While the dashboard is running** (`-StartServerAtLogon` starts it when you sign in), it
  refreshes prices every 5 minutes during market hours (10:00–14:00) and checks price and %-change
  alerts as they happen. It also starts the end-of-day run by itself about 10 minutes after DSE's
  post-closing session.
* On non-trading days DSE shows the last session, so nothing new is stored.

## Technical score (−100 … +100)

| Rule | Points |
|---|---|
| Close above/below 20-day average | ±8 |
| Close above/below 50-day average | ±10 |
| 50-day average above/below 200-day | ±12 |
| Golden / death cross in last 5 sessions | ±10 |
| MACD above/below signal line | ±8 |
| MACD crossover in last 3 sessions | ±10 |
| RSI < 30 (oversold) / > 70 (overbought) | +12 / −12 |
| RSI ≥ 55 / ≤ 45 | +5 / −5 |
| Close below lower / above upper Bollinger band | +6 / −4 |
| Volume ≥ 2× 20-day average on an up / down day | ±10 |
| New 52-week closing high / low | ±8 |

Labels: ≥ 40 Strong Bullish, ≥ 15 Bullish, between −15 and 15 Neutral, ≤ −15 Bearish, ≤ −40 Strong Bearish.
Each stock page lists exactly which rules fired. Category Z and moves near the circuit-breaker
limit are flagged but do not add or subtract points.

## Prediction: what it does and how far to trust it

Retrained after every end-of-day run (about 5 seconds) and re-tested each time on recent data it
has never seen. Code: `bdstock/predict.py`.

* **Likely ranges (the main forecast).** Each stock's recent volatility is scaled by the measured
  spread of DSE moves over that horizon (fat tails included). In tests on unseen periods the price
  ended inside the 68% "likely" range about 66–68% of the time for the next session and 1 week,
  and 58% for 1 month. The 3-month range is marked **low confidence** (only 32% in tests)
  until more history builds up.
* **Up/down direction is gated.** A logistic + ridge model trained on all shares (momentum,
  moving averages, RSI, MACD, volatility, volume, liquidity, market momentum, category) is tested on
  three rolling unseen windows. It is shown as "chance of rising" only when it beats chance in every
  window. With one year of history it currently does not, so the page says *"no reliable signal"*
  instead of showing a misleading number. It turns on automatically once it proves itself.
* **Short-term top 10** is a transparent rule: liquid A/B/N shares in an uptrend (price above the
  50-day average, 20-day above 50-day) that have pulled back (RSI 40–65, within 5% of the 20-day
  average). Stop = price − 1.5 × ATR; target = top of the 1-month likely range. Over the past year
  these picks beat the average liquid share in about 65% of 1-month periods, by about 0.4% on
  average. That is a small edge, so always use the stop.
* **Long-term top 10 (regular buying)** screens category-A, profitable, liquid companies. Each factor
  is scored by percentile against the other candidates: valuation (P/E, P/NAV) 25%, dividends
  (yield + years paid out of 5) 25%, EPS growth 20%, stability (low volatility + long-term trend) 30%.
  At most 3 per sector. It suits buying a fixed amount every month. It cannot be backtested because
  DSE publishes only current fundamentals.
* **Inputs (49, in 10 groups, all point-in-time):** price and volume (technical), liquidity and DSE
  category, market conditions (momentum, breadth, turnover trend), sector strength, fundamentals
  (earnings yield, book/price, dividend yield, EPS growth, each used only from ~120 days after the
  fiscal year end, when it was published), **news** (announcements in the last 7 days, 30-day
  tone, latest earnings vs last year, dividend declared / no dividend, record date within 10 days),
  **trading activity** (close position in the day's range, opening gap, intraday high-low range,
  average trade size and number of trades vs normal, up/down streak, circuit-limit days),
  **risk** (60-day beta, stock-specific volatility, distance from 52-week high/low), **ownership & size**
  (market cap, sponsor / institutional / foreign holding from the dated shareholding reports, free float)
  and **day of week** (Sunday / Thursday sessions). Each group is re-tested daily *with and without it*,
  and the Prediction page shows what it really adds.
* **Range width.** Three range methods are tested every day on unseen windows: 60-day volatility × √days,
  a volatility model (17 inputs), and the volatility model with separate scaling for calm/jumpy stocks.
  The one with the best interval score (width plus a penalty for misses) is used. The Prediction page
  can show the **50% (narrowest, default)**, 68% or 90% range. In tests the next-session 50% range was
  about 2.6% wide and held about 50% of the time, against 4.1% wide / 67% for the 68% range.

### Formulas

```text
1. Size of move     ln(|ln(P[t+h]/P[t])| + 0.002) = b0 + Σ b_j·v_j        (ridge regression, 17 volatility inputs v_j)
                    σ̂_h = exp(b0 + Σ b_j·v_j)
2. Shape of moves   z = ln(P[t+h]/P[t]) / σ̂_h  over all past stock-days;  q_p = p-th percentile of z
3. Range (conf. c)  Low = P[t]·exp(q_(1−c)/2 · σ̂_h)   High = P[t]·exp(q_(1+c)/2 · σ̂_h)   Mid = P[t]·exp(q_0.5 · σ̂_h)
                    50% range: q25..q75   68%: q16..q84   90%: q5..q95
4. Direction        z_i = (clip(x_i) − mean_i) / sd_i                      (49 inputs, clipped to 1st–99th pct)
                    P(up) = 1 / (1 + exp(−(w0 + Σ w_i·z_i)))              (logistic regression, L2 penalty)
                    E[ln(P[t+h]/P[t])] = β0 + Σ β_i·z_i                    (ridge regression, λ = 0.1·n)
5. Shown only if    rank-IC > 0.03 in each of 3 unseen 15-session windows, AUC > 0.53, top-10 beat average
6. Planning         Stop = P − 1.5·ATR14   Target = top of 1-month 68% range   R:R = (Target − P)/(P − Stop)
7. Long-term score  100 × (0.25·Valuation + 0.25·Dividends + 0.20·Growth + 0.30·Stability)   (percentile ranks)
8. Range scoring    interval score = (High − Low) + (2/α)·max(Low − actual, 0) + (2/α)·max(actual − High, 0)
```
* **News collection:** about 35 recent announcements per company from the company pages (history),
  plus the DSE news page (newest ~50 disclosures) polled every 10 minutes while the market is open
  and every 30 minutes otherwise (dashboard running), and again in the end-of-day run. Companies
  with news that day get their company page re-read for full text and updated fundamentals.
* **Updated every trading day:** the end-of-day run retrains every model, re-tests it, and stores
  new forecasts for all shares.
* **Checked against real prices every day:** forecasts whose horizon has ended are compared with
  the actual close (inside range? direction right? how far off?) and stored in `prediction_checks`.
  The **Accuracy** page shows this per day and per stock. On first run a 40-session walk-forward
  history is created (each day's model trained only on what was known then), so the record does not
  start empty. Live and walk-forward results are shown separately.

## Data sources (public DSE website)

| Data | Source |
|---|---|
| Live prices for all instruments, indices, breadth, session status | `https://www.dse.com.bd/api/live/snapshot` (the feed the DSE site itself uses) |
| ~1 year of daily OHLCV + fundamentals, dividends, shareholding | `https://www.dse.com.bd/company/<CODE>` |
| P/E for all companies | `https://www.dse.com.bd/pe` |
| Latest company disclosures (news) | `https://www.dse.com.bd/news` |
| Last 30 sessions of DSEX/DS30/DSES and market totals | `https://www.dse.com.bd/recent-market-information` |

The history download fetches company pages one at a time with a short pause between them
(`backfill_delay_seconds` in `config.ini`). If DSE changes its website, the parsers in
`bdstock/dse_client.py` are the place to update. The offline tests in `tests/` use saved copies
of real pages.

## Commands

```text
python run.py setup                 configure MySQL and create tables
python run.py serve [--open]        dashboard + live monitor on http://127.0.0.1:8050
python run.py daily [--force]       end-of-day job (what the scheduled task runs)
python run.py backfill [--codes GP,BATBC]   re-download history (all stocks if no codes)
python run.py fetch                 fetch the latest snapshot once
python run.py status                what is in the database, recent jobs
python -m unittest discover -s tests   run the tests
powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -Uninstall   remove the tasks
```

## Project layout

```text
run.py                      command line
bdstock/dse_client.py       DSE website client and parsers
bdstock/collector.py        live refresh, end-of-day storage, history backfill, daily job
bdstock/indicators.py       SMA, EMA, RSI, MACD, Bollinger, ATR
bdstock/analysis.py         score, reasons, flags; signals for every stock
bdstock/alerts.py           alert rules (intraday + end of day)
bdstock/portfolio.py        transactions, average-cost holdings, P/L
bdstock/predict.py          forecasts, backtests, input-group tests, daily accuracy checks, top-10 lists
bdstock/news.py             DSE announcements: collection, tagging, point-in-time news features
bdstock/report.py           daily report
bdstock/notifier.py         Windows toast + Telegram
bdstock/monitor.py          background market monitor inside the web server
bdstock/web.py              Flask JSON API
bdstock/static/             dashboard UI (HTML/CSS/JS, TradingView Lightweight Charts)
bdstock/db.py               MySQL schema and helpers
scripts/install_tasks.ps1   Windows scheduled tasks
logs/bdstock.log            log file (rotating)
```

## Telegram notifications (optional)

1. In Telegram, message **@BotFather**, send `/newbot` and copy the bot token.
2. Send any message to your new bot. Then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": ...}`.
3. In **Settings**, paste the token and chat ID, tick *Telegram messages*, save, then click *Send test notification*.

## Troubleshooting

* **"Can't use the MySQL database"**: check that the `MySQL80` service is running, then run `python run.py setup`.
* **No notification appeared**: open Settings and click *Send test notification*. Check that Windows
  notifications are on for "Windows PowerShell" (Settings › System › Notifications).
* **Did the scheduled task run?** Look at Settings › Recent jobs, or `logs\bdstock.log`, or Task Scheduler.
* **A stock's chart looks wrong after a bonus/rights issue**: open the stock and click **↻ Update** to
  re-download its history from DSE.
