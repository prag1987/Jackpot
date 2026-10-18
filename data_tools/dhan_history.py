"""Read-only DhanHQ expired-option downloader. NEVER makes order requests."""
import argparse
import csv
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib import request, error

API = 'https://api.dhan.co/v2/charts/rollingoption'
IDS = {'NIFTY': 13, 'BANKNIFTY': 25}
FIELDS = ['open', 'high', 'low', 'close', 'iv', 'volume', 'strike', 'oi', 'spot']


def chunks(start, end, days=29):
    """Half-open ranges; Dhan allows <= 30 days per request."""
    while start < end:
        stop = min(start + timedelta(days=days), end)
        yield start, stop
        start = stop


def body(index, start, end, strike, side, interval, expiry_flag, expiry_code):
    return {'exchangeSegment': 'NSE_FNO', 'interval': str(interval),
            'securityId': IDS[index], 'instrument': 'OPTIDX',
            'expiryFlag': expiry_flag, 'expiryCode': expiry_code,
            'strike': strike, 'drvOptionType': side,
            'requiredData': FIELDS,
            'fromDate': start.isoformat(), 'toDate': end.isoformat()}


def fetch(payload, token, timeout=30, retries=3):
    data = json.dumps(payload).encode('utf8')
    for attempt in range(retries):
        req = request.Request(API, data=data, method='POST', headers={
            'access-token': token, 'Content-Type': 'application/json',
            'Accept': 'application/json'})
        try:
            with request.urlopen(req, timeout=timeout) as response:
                result = json.load(response)
            if isinstance(result, dict) and result.get('status') == 'failure':
                raise RuntimeError('Dhan returned failure: ' + str(result.get('remarks') or result.get('message')))
            return result
        except error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError('Dhan authorization failed. Check API permissions/token.') from exc
            if exc.code not in (429, 500, 502, 503, 504) or attempt == retries-1:
                raise RuntimeError('Dhan HTTP ' + str(exc.code)) from exc
        except (error.URLError, TimeoutError) as exc:
            if attempt == retries-1:
                raise RuntimeError('Dhan request failed: '+ str(exc)) from exc
        time.sleep(2 ** attempt + 1)
    raise RuntimeError('Retries exhausted')


def flatten(result, side, meta):
    data = result.get('data', {}) if isinstance(result, dict) else {}
    rows = data.get('ce' if side == 'CALL' else 'pe')
    if not isinstance(rows, dict) or not isinstance(rows.get('timestamp'), list):
        raise ValueError('Missing expected Dhan timestamp/options payload; no output saved')
    length = len(rows['timestamp'])
    if any(not isinstance(rows.get(k), list) or len(rows[k]) != length for k in FIELDS):
        raise ValueError('Missing columns or inconsistent field lengths; no output saved')
    return [{**meta, 'timestamp': rows['timestamp'][i],
             **{k: rows[k][i] for k in FIELDS}} for i in range(length)]


def run(args):
    if args.end <= args.start:
        raise ValueError('end must be later than start (end exclusive)')
    if args.strike_steps > 3 or args.strike_steps < 0:
        raise ValueError('Default-safe strike_steps must be between 0 and 3')
    if args.end > date.today() + timedelta(days=1):
        raise ValueError('Cannot request future history')
    token = os.environ.get('DHAN_ACCESS_TOKEN', '').strip()
    if not args.dry_run and not token:
        raise RuntimeError('Set DHAN_ACCESS_TOKEN as a local environment variable. Do not paste in chat.')
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    manifest = []
    strikes = ['ATM'] + [f'ATM{sign}{i}' for i in range(1, args.strike_steps + 1) for sign in ('+', '-')]
    for idx in args.indices:
        for start, end in chunks(args.start, args.end):
            for strike in strikes:
                for side in ('CALL', 'PUT'):
                    payload = body(idx, start, end, strike, side, args.interval, args.expiry_flag, args.expiry_code)
                    name = f'{idx}_{start}_{end}_{args.expiry_flag}{args.expiry_code}_{strike.replace("+","p").replace("-","m")}_{side}_{args.interval}m.csv'
                    path = output / name
                    if path.exists() and not args.overwrite:
                        manifest.append({'file': str(path), 'status': 'skipped_existing'})
                        continue
                    if args.dry_run:
                        manifest.append({'file': str(path), 'status': 'dry_run', 'request': payload})
                        continue
                    try:
                        resp = fetch(payload, token)
                        rows = flatten(resp, side, {'index': idx, 'option_type': side,
                           'relative_strike': strike, 'expiry_flag': args.expiry_flag,
                           'expiry_code': args.expiry_code})
                        if not rows:
                            manifest.append({'file': str(path), 'status': 'empty'})
                            continue
                        with path.open('w', newline='', encoding='utf8') as f:
                            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                            writer.writeheader(); writer.writerows(rows)
                        manifest.append({'file': str(path), 'status': 'ok', 'rows': len(rows)})
                    except Exception as exc:
                        manifest.append({'file': str(path), 'status': 'error', 'reason': str(exc)})
                        print(f'ERROR {idx} {strike} {side}: {exc}', file=sys.stderr)
                    time.sleep(args.delay)
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf8')
    print('Output:', output, '| Requests:', len(manifest), '| success:', sum(x['status']=='ok' for x in manifest), '| errors:', sum(x['status']=='error' for x in manifest))
    return 1 if any(x['status'] == 'error' for x in manifest) else 0


def main():
    p = argparse.ArgumentParser(description='DhanHQ expired options historical downloader (READ-ONLY)')
    p.add_argument('--start', type=date.fromisoformat, default=date.today()-timedelta(days=7))
    p.add_argument('--end', type=date.fromisoformat, default=date.today())
    p.add_argument('--indices', nargs='+', choices=IDS, default=list(IDS))
    p.add_argument('--interval', type=int, choices=[1,5,15,25,60], default=5)
    p.add_argument('--expiry-flag', choices=['MONTH','WEEK'], default='MONTH')
    p.add_argument('--expiry-code', type=int, default=1)
    p.add_argument('--strike-steps', type=int, default=1)
    p.add_argument('--output', default='historical_data')
    p.add_argument('--delay', type=float, default=1.0)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--overwrite', action='store_true')
    args = p.parse_args(); sys.exit(run(args))

if __name__ == '__main__':
    main()
