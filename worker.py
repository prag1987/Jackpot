"""Market-data worker + PAPER simulation + SANDBOX/LIVE execution.

Trading mode is chosen in the dashboard (Live trading tab). LIVE additionally
needs CG_ALLOW_LIVE=YES and CG_STATIC_IP in the server .env.
Open SANDBOX/LIVE positions are always managed (reconcile + time exits), even
after you switch the mode back to PAPER.
"""
import time, json, argparse
from dotenv import load_dotenv
from engine import (ROOT, now, init_db, log, put_state, state, DhanReadOnly,
                    choose_expiry, normalized_chain, snapshot_iv, mark_and_exit, paper_enter, open_positions)
import live_engine as le


class Context:
    def __init__(self):
        self.traders = {}
        self.exec_dbs = {}
        self.feed = None
        self.last_candles = 0.0

    def db(self, mode):
        if mode not in self.exec_dbs:
            self.exec_dbs[mode] = le.exec_db(mode)
        return self.exec_dbs[mode]

    def trader(self, mode, creds):
        if mode not in self.traders:
            self.traders[mode] = le.make_trader(mode, creds)
        return self.traders[mode]


def fetch_market(api, db, cfg, stamp):
    all_rows, notes = [], []
    for name, info in cfg['indices'].items():
        try:
            exp = choose_expiry(api.expiries(info['underlying_id']), cfg, stamp)
            if not exp:
                notes.append(f'{name}: no suitable expiry'); continue
            rows = normalized_chain(api.chain(info['underlying_id'], exp), name, exp, now().isoformat())
            all_rows.extend(rows)
            snapshot_iv(db, name, rows, stamp.date().isoformat())
            notes.append(f'{name}: {len(rows)} contracts, expiry {exp}')
        except Exception as ex:
            notes.append(f'{name}: DATA ERROR {type(ex).__name__}: {str(ex)[:120]}')
            log(db, 'DATA_ERROR', notes[-1])
    return all_rows, notes


def refresh_candles(ctx, cfg, db, stamp):
    if time.monotonic() - ctx.last_candles < cfg.get('candle_refresh_seconds', 60):
        return
    from web_admin import dhan_credentials
    from candle_feed import CandleFeed
    if ctx.feed is None:
        ctx.feed = CandleFeed(*dhan_credentials())
    ctx.last_candles = time.monotonic()
    notes = ctx.feed.refresh(cfg['indices'], stamp)
    put_state(db, 'candle_status', json.dumps(notes))
    for name, note in notes.items():
        if 'ERROR' in note:
            log(db, 'CANDLE_ERROR', f'{name}: {note}')


def run_exec_mode(ctx, mode, active_mode, cfg, paper_db, rows, stamp, data_ok):
    """Run one SANDBOX/LIVE cycle. Inactive modes only manage leftover open positions."""
    from web_admin import dhan_credentials
    from autopilot import decide
    db = ctx.db(mode)
    if mode != active_mode and not open_positions(db) and not state(db, 'kill_request'):
        return
    try:
        trader = ctx.trader(mode, dhan_credentials)
    except ValueError as ex:
        put_state(db, 'autopilot_last_decision', f'LOCKED: {ex}')
        return
    le.sync_iv(paper_db, db)
    problems = le.live_gate(mode, cfg, paper_db) if mode == active_mode else [f'Mode is {active_mode}; managing exits only']
    put_state(db, 'gate', json.dumps(problems))
    if state(db, 'halt') == '1' and not problems:
        problems = ['HALTED']
    result = le.live_cycle(db, trader, cfg, rows, stamp, mode, problems,
                           lambda d, c, r, s: decide(d, c, r, s), data_ok=data_ok)
    put_state(db, 'last_scan', stamp.isoformat())
    return result


def cycle(api, db, ctx):
    from web_admin import effective_config
    cfg = effective_config()
    mode = cfg['trading_mode']
    stamp = now()
    put_state(db, 'trading_mode', mode)
    if stamp.weekday() > 4:
        put_state(db, 'status', 'Weekend: no trading'); return
    all_rows, notes = fetch_market(api, db, cfg, stamp)
    data_ok = not any('DATA ERROR' in x for x in notes)
    put_state(db, 'status', '; '.join(notes))
    if not data_ok and open_positions(db):
        put_state(db, 'halt', '1')
    try:
        refresh_candles(ctx, cfg, db, stamp)
    except Exception as ex:
        log(db, 'CANDLE_ERROR', repr(ex)[:300])
    quotes = {r['security_id']: r for r in all_rows if r['security_id']}
    mark_and_exit(db, quotes, now(), cfg)

    # ---- SANDBOX / LIVE (real broker) ----
    for exec_mode in ('SANDBOX', 'LIVE'):
        try:
            run_exec_mode(ctx, exec_mode, mode, cfg, db, all_rows, now(), data_ok)
        except Exception as ex:
            log(ctx.db(exec_mode), 'EXEC_ERROR', repr(ex)[:300])
            put_state(ctx.db(exec_mode), 'halt', '1')
            le.notify.send(f'[{exec_mode}] EXEC_ERROR {ex!r}'[:500])

    # ---- PAPER ----
    if mode == 'PAPER':
        from autopilot import autopilot_paper_cycle
        if data_ok:
            autopilot_paper_cycle(db, cfg, all_rows, now())
            if state(db, 'autopilot', '0') == '1':
                le.record_paper_cycle(db, now())
        signal = state(db, 'pending_signal')
        if signal:
            put_state(db, 'pending_signal', '')
            if state(db, 'halt') == '1':
                log(db, 'REJECTED', 'Halted')
            else:
                try:
                    request = json.loads(signal)
                    row = quotes.get(request['security_id'])
                    if not row or row['side'] != request['side']:
                        log(db, 'REJECTED', 'Contract missing or side mismatched')
                    else:
                        ok, detail = paper_enter(db, row, request['side'], now(), cfg)
                        log(db, 'SIGNAL_RESULT', json.dumps({'accepted': ok, 'detail': detail}, default=str))
                except Exception as ex:
                    log(db, 'REJECTED', str(ex))
    put_state(db, 'last_scan', now().isoformat())
    (ROOT / 'marketdata').mkdir(exist_ok=True)
    (ROOT / 'marketdata' / 'latest_chain.json').write_text(json.dumps({'timestamp': now().isoformat(), 'contracts': all_rows}))


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--once', action='store_true'); args = parser.parse_args()
    load_dotenv(ROOT / '.env')
    from web_admin import dhan_credentials, effective_config
    db = init_db(); client, token = dhan_credentials(); api = DhanReadOnly(client, token)
    ctx = Context()
    le.notify.send(f'Worker started. Mode: {effective_config()["trading_mode"]}')
    while True:
        try:
            cycle(api, db, ctx)
        except KeyboardInterrupt:
            break
        except Exception as ex:
            log(db, 'WORKER_ERROR', repr(ex)); put_state(db, 'halt', '1'); put_state(db, 'status', 'Error: ' + str(ex))
        if args.once:
            break
        time.sleep(max(effective_config()['chain_refresh_seconds'], 10))


if __name__ == '__main__':
    main()
