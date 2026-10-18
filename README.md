# Capital Guard AI — DhanHQ Paper Research Repository

**State: research MVP with optional SANDBOX/LIVE Dhan execution (V9). No validated profitable edge.** Default mode is PAPER. LIVE needs `CG_ALLOW_LIVE=YES`, a whitelisted static IP, dashboard confirmation and your chosen number of completed paper sessions. Hindi deployment guide: [DEPLOY_VPS_HINDI.md](DEPLOY_VPS_HINDI.md).

## V9: SANDBOX / LIVE execution

- `live_broker.py` — Dhan adapter: super order entry (LIMIT buy + broker-side SL + target), order/position lookup, targeted exit, exit-all, kill switch, P&L exit. Order writes are never auto-retried; lookups use `correlationId`.
- `live_engine.py` — gates (`CG_ALLOW_LIVE`, static IP, `min_paper_sessions`), entry recording before send, reconciliation, time exit / 15:15 square-off (cancel legs first, then market sell), daily broker-side loss guard, kill switch.
- `candle_feed.py` — fetches completed 5-minute index candles from Dhan `/charts/intraday` for the autopilot.
- `notify.py` — optional Telegram alerts.
- Dashboard tab **🔴 Live trading**: mode switch, session requirement (user-set, 0–1000), gate status, autopilot, kill switch, trades. **Risk & logs** tab edits the event calendar in the data volume.
- Separate databases: `paper.db`, `sandbox.db`, `live.db`.
- Run: `docker compose --profile trading up -d --build`.

Fixed in V9: worker stored chain timestamps as datetime objects, which crashed every market-hours cycle.

## What runs
- Read-only DhanHQ option-chain and expiry polling (`worker.py`, requires credentials)
- Delta / IV / theta / spread / expiry / lot-size risk checks and paper order entry from dashboard
- Experimental multi-leg debit-spread and condor payoff evaluator (`adaptive_v3.py`). **Not connected to autonomous order execution**. Condors and pyramiding disabled.
- 15m EMA directional trend + 5m prior breakout research classifier (`strategy_lab.py`). **Not connected to worker or dashboard**, needs historical input and rigorous validation.
- Time window, event calendar fail-closed, daily loss and overtrade constraints
- Streamlit paper dashboard (`dashboard.py`)
- Offline synthetic demo, unit tests, Docker, and CI.

## Start without Dhan credentials (safe offline demo)
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q
python strategy_lab.py --demo
streamlit run dashboard.py
```
Dashboard will not have market quotes until read-only worker is started. Synthetic demo is **not a backtest**.

## Read-only Dhan connectivity
1. Copy `.env.example` to `.env`, fill `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN`; never commit `.env`.
2. Verify contract lot sizes and expiry using current exchange data and review `config.json`.
3. Update `event_calendar.json` daily from verified primary sources. Missing/unverified calendar **blocks entries**.
4. In terminal A: `python worker.py`; in terminal B: `streamlit run dashboard.py`.
5. Live orders never execute. All orders are simulated, stops are not guaranteed, and unattended auto-squareoff is not implemented.

## Standalone regime lab
`python strategy_lab.py --candles YOUR_5M.csv --ivp 45 --event-verified`
CSV columns: `timestamp,open,high,low,close,volume`. Supply clean, **closed**, chronologically sorted 5-minute bars aligned to 15-minute windows. IV percentile must come from independent historic data. This is a **signal hypothesis**, not a backtest or live trading recommendation.

## Docker
`docker compose up --build dashboard` runs UI only. For read-only Dhan polling, configure `.env` and run `docker compose --profile dhan-read-only up --build` (both services). A persistent runtime volume is declared, but existing MVP stores paper DB beside code; persist the paper DB with a bind mount before relying on Docker continuity.

## Model/edge validity
This project has **no demonstrated positive expectancy**. Preliminary rule: trend on 15m, prior 12-bar 5m breakout, delta 0.45-0.65, spread <=1%, theta and IV safeguards; options costs and spreads can overwhelm signal. Iron condor and credit structures are not executable. Require proper out-of-sample walk-forward testing, 30-60+ paper sessions, costs, partial-fill and failure simulations before considering separate live execution development.

## Risk defaults
₹2L capital, ₹1k/trade, ₹2k/day, ₹6k/week, max 2 trades/day, one open position, verified event calendar required. Loss caps are **budgets, not loss guarantees**. NSE lots and Dhan API behavior may change.

## Deployment warning
GitHub Actions is CI **not a persistent trading server**. To run continuously, host on private VPS/VM with secure credentials, monitoring, and persistence. Do not run automated unattended real trading from ephemeral GitHub Actions.

## Documentation
- https://dhanhq.co/docs/v2/option-chain/
- https://dhanhq.co/docs/v2/historical-data/

## Web Control Center (V5, paper only)

The authenticated Streamlit interface includes five tabs: Overview/P&L, Dhan setup,
Trading setup, Manual paper requests, and Risk/logs. Broker credentials are encrypted
with Fernet into the persistent `data/web_settings.db` and require a separate
`CG_ENCRYPTION_KEY`. Set `CG_ADMIN_PASSWORD` to a long random secret. Never put
`.env` in git. Streamlit's shared-password gate is suitable **only for a private,
access-controlled deployment**, not an internet-facing production trading console.
Run behind HTTPS + VPN/private network. Dashboard port is bound to 127.0.0.1.

Generate a Fernet key:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Copy `.env.example` to `.env`, set `CG_ADMIN_PASSWORD` and `CG_ENCRYPTION_KEY`,
then run:

```bash
docker compose up --build -d dashboard
# Open http://localhost:8501 on the same machine.
# Add Dhan credentials using the dashboard before launching worker.
docker compose --profile dhan-read-only up --build -d
```

For a remote VPS, use a private VPN or SSH port-forward; do not open port 8501 to the public internet.
The worker takes Dhan credentials at startup; restart it after changing credentials.
Settings are reloaded on each scan. GitHub Actions execute tests but **do not**
run a permanent trading service.

**Cannot do yet:** unattended strategy signals, real-money trading, reliable
multi-leg execution, guaranteed stops, verified backtest edge, reliable forced
square-off. A paper-entry request requires explicit manual directional selection.
Unknown fills and stale marks make simulated P&L estimates, not account balances.

## V6: One-button paper autopilot (experimental)

Open the **Autopilot** tab in the Streamlit web control. **START paper autopilot** turns
on autonomous paper-entry evaluation during each data worker cycle; **STOP** blocks
new paper entries. The existing Risk & logs HALT remains a higher-priority block.
The worker must be running separately (`docker compose --profile dhan-read-only up -d worker`).

The autopilot **does not automatically obtain underlying 5-minute OHLCV candles**.
A verified external process must atomically update both files below with genuine
completed 5-minute IST bars (timestamps are bar *start*, naive IST):

- `marketdata/candles/NIFTY_5m.csv`
- `marketdata/candles/BANKNIFTY_5m.csv`

Columns: `timestamp,open,high,low,close,volume`. At least 85 bars are required,
and the latest completed candle must be between 5 and 15 minutes old. Synthetic
candles and future or stale timestamps are rejected. The calendar must be
verified through the current trading date and enough daily IV observations must
be available. No trade is permitted if any required gate fails.

The strategy is experimental, not backtested to positive expectancy. Strike
ranking supports a single CE or PE paper order, not multi-leg basket execution.
No pyramiding or broker order execution is implemented. A stop in the UI disables
new orders but does **not** immediately liquidate existing positions; the paper
worker continues its normal monitoring. The dashboard is not a production
trading server and must not be exposed publicly.


## V7: Execution Guardian (SIMULATION ONLY)

V7 adds `guardian_v7.py`, a durable SQLite **paper execution intent** and risk simulation module. It provides:
- Unique idempotency keys for intended transactions (duplicates rejected).
- One-leg long entries and long-first debit-spread paper leg sequencing.
- Persisted simulated fills and `PARTIAL_HEDGE_ONLY` state to expose unmatched legs.
- Configurable capital-based gate: ₹1,000 individual max risk, ₹1,500 portfolio risk, ₹2,000/day, ₹6,000/week, ₹12,000 drawdown, ₹70,000 lock ceiling.
- Pyramid candidate rule: >+1R with confirmed new signal, max ₹500 hypothetical incremental risk, no averaging down. **Pyramiding never fires an order automatically.**
- Emergency HALT and audit log in independent V7 tab. This tab **does not control** V6 worker.

**NOT IMPLEMENTED:** Dhan live order adapter, live/simulated order bridge from V6, automated position unwind, mark-to-market in guardian, continuous broker reconciliation, partial fill quantity reconciliation, broker-side protective stops, margin preflight, fill slippage, automated multi-leg execution, automatic pyramiding, full basket cost optimizer, actual P&L reporting in guardian. This is a safety-oriented reference module, not a deployable real-money bot. Do not interpret paper sim fills as trades. Live order placement remains impossible.

Dhan official docs: https://dhanhq.co/docs/v2/orders/ and https://dhanhq.co/docs/v2/super-order/ and https://dhanhq.co/docs/v2/order-update/ . Dhan requires static-IP whitelisting for order APIs. Never assume bracket/super orders support every derivative or guarantee a stop fill.

## V8 historical execution validation (research only)

New `data_tools/contract_costs.py`: contract-keyed validation `(underlying, expiry, strike, right, lot_size)`, timestamp-aware leg alignment, strict roll-over rejection, round-trip buy/sell transaction charges, configurable Dhan brokerage, regulatory levies, GST, bid/ask or penalized proxy execution, and 4-leg basket support as **offline payoff/P&L only**.

**Do not equate historical rolling ATM± series with a fixed contract**: its `strike` can change while rolling. Reconstruction requires a unique historical contract master including dated expiry, historical lot size and correct candle-to-contract mapping. Without that evidence the validator rejects `synthetic_contract=True` by default. Dhan rolling history has OHLC, IV, OI, volume, spot and strike but not historical bid/ask depth; proxy results are scenarios rather than accurate fills. With actual bid/ask still account for displayed depth, leg sequencing, non-atomic hedge fill and broker margin. This package does **not** download real data by itself or assert actual backtested performance.

**Historical fees require date-dependent rates**: STT on option sales is 0.15% starting 2026-04-01, whereas earlier periods differ. `cost_config.example.json` is a current-era illustrative scenario, NOT automatically valid across 2024–2026. Validate all broker/exchange/IPFT rates against historical tariff schedules and Dhan contract notes. Settlement/exercise STT is not modeled; use intraday exits only and reject expiry exercise. Conservative entry fill at assumed ask and exit at assumed bid plus impact may still understate losses during events/gaps. Synthetic reconstruction must not be used to certify Iron Condor profitability.

Run: `python -m pytest -q`. Live execution remains disabled.
