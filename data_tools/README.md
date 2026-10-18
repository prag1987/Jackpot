# DhanHQ Historical Downloader (read-only)

DhanHQ rolling expired options API: https://dhanhq.co/docs/v2/expired-options-data/

This program has NO order execution code. It downloads historical ATM-relative index option candles/IV/OI/volume/spot. Outputs CSVs and a `manifest.json` report. End date is exclusive. Requests span no more than 29 days. BankNifty monthly data is the default; NIFTY weekly can be requested separately. `expiryCode=1` uses Dhan's rolling expiry code and requires confirmation against your intended backtest semantics.

Run from repository root (Python 3.10+):

```bash
export DHAN_ACCESS_TOKEN='paste-your-token-locally'  # Linux/macOS
python data_tools/dhan_history.py --start 2026-10-01 --end 2026-10-16 --dry-run
python data_tools/dhan_history.py --start 2026-10-01 --end 2026-10-16 --indices NIFTY BANKNIFTY --interval 5 --strike-steps 1
```

Windows PowerShell: `$env:DHAN_ACCESS_TOKEN='paste-your-token-locally'`.

For NIFTY weekly run additionally with `--indices NIFTY --expiry-flag WEEK --output historical_weekly`.

**Limitations:** ATM-relative rolling strikes are not stable individual option contract identifiers. Do not stitch rolling premiums and treat them as tradeable single positions. Greeks, historical bid/ask, full cross-strike price alignment, full event calendar and corporate/exchange contract changes are not supplied. A rigorous multi-leg backtest requires individual strike/expiry execution-consistent data and cost/slippage models. Never commit tokens or downloads to public GitHub.
