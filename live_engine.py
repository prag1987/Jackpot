"""SANDBOX / LIVE execution on top of the existing risk engine.

The same can_enter / evaluate / risk caps used for paper trading govern every
real order. Positions live in their own database (live.db or sandbox.db) so
paper results and real-money results never mix.

Locks before a LIVE order can leave the server:
  1. .env on the server has CG_ALLOW_LIVE=YES
  2. Dashboard trading mode set to LIVE (typed confirmation)
  3. Completed paper sessions >= min_paper_sessions (your own setting; 0 allowed)
  4. Server public IP == CG_STATIC_IP (Dhan whitelisted)
  5. Normal engine gates: halt, event calendar, time windows, loss caps, trade caps
"""
import os
from datetime import datetime
from pathlib import Path
from engine import ROOT, init_db, log, state, put_state, open_positions, evaluate, can_enter
from live_broker import DhanTrader, BrokerError, AuthError, static_ip_problem
import notify

DATA = Path(os.getenv('CG_DATA_DIR', str(ROOT / 'data')))
EXTRA_COLUMNS = {'broker_order_id': 'TEXT', 'correlation_id': 'TEXT', 'broker_status': 'TEXT'}
DONE_ENTRY = ('REJECTED', 'CANCELLED', 'EXPIRED')
SESSION_MIN_CYCLES = 200  # ~67 minutes of autopilot cycles at 20s refresh


# ---------------- databases ----------------
def exec_db(mode, path=None):
    db = init_db(path or DATA / ('live.db' if mode == 'LIVE' else 'sandbox.db'))
    have = {r[1] for r in db.execute('PRAGMA table_info(positions)')}
    for col, kind in EXTRA_COLUMNS.items():
        if col not in have:
            db.execute(f'ALTER TABLE positions ADD COLUMN {col} {kind}')
    db.commit()
    return db


def sync_iv(src, dst):
    rows = src.execute('SELECT day,index_name,iv FROM iv_history').fetchall()
    dst.executemany('INSERT OR REPLACE INTO iv_history VALUES(?,?,?)', [tuple(r) for r in rows])
    dst.commit()


def _sessions_table(db):
    db.execute('CREATE TABLE IF NOT EXISTS paper_sessions(day TEXT PRIMARY KEY, cycles INTEGER NOT NULL)')


def record_paper_cycle(paper_db, stamp):
    """Count an autopilot paper cycle during market hours (09:15-15:30 IST)."""
    if stamp.weekday() > 4 or not '09:15' <= stamp.strftime('%H:%M') <= '15:30':
        return
    _sessions_table(paper_db)
    paper_db.execute('INSERT INTO paper_sessions VALUES(?,1) ON CONFLICT(day) DO UPDATE SET cycles=cycles+1',
                     (stamp.date().isoformat(),))
    paper_db.commit()


def paper_session_count(paper_db, min_cycles=SESSION_MIN_CYCLES):
    _sessions_table(paper_db)
    return paper_db.execute('SELECT COUNT(*) FROM paper_sessions WHERE cycles>=?', (min_cycles,)).fetchone()[0]


# ---------------- gate ----------------
def live_gate(mode, cfg, paper_db, ip_check=static_ip_problem, env=None):
    """Returns a list of blocking problems. Empty list = orders allowed (subject to engine caps)."""
    env = os.environ if env is None else env
    problems = []
    if mode == 'PAPER':
        return ['Mode is PAPER']
    if mode == 'SANDBOX':
        if not env.get('DHAN_SANDBOX_CLIENT_ID') or not env.get('DHAN_SANDBOX_ACCESS_TOKEN'):
            problems.append('Sandbox credentials missing (DHAN_SANDBOX_CLIENT_ID / DHAN_SANDBOX_ACCESS_TOKEN)')
        return problems
    if env.get('CG_ALLOW_LIVE', '').strip().upper() != 'YES':
        problems.append('Server lock: set CG_ALLOW_LIVE=YES in .env')
    need = int(cfg.get('min_paper_sessions', 30))
    have = paper_session_count(paper_db)
    if have < need:
        problems.append(f'Paper sessions {have}/{need} (change in Live trading tab)')
    ip = ip_check()
    if ip:
        problems.append(ip)
    return problems


def make_trader(mode, live_credentials):
    if mode == 'SANDBOX':
        return DhanTrader(os.getenv('DHAN_SANDBOX_CLIENT_ID'), os.getenv('DHAN_SANDBOX_ACCESS_TOKEN'), 'SANDBOX')
    client, token = live_credentials()
    return DhanTrader(client, token, 'LIVE')


# ---------------- helpers ----------------
def _alert(db, mode, event, text):
    log(db, event, text)
    notify.send(f'[{mode}] {event}: {text}')


def _close(db, pos, stamp, exit_price, pnl, reason, status='CLOSED'):
    db.execute('UPDATE positions SET exit_time=?,exit_price=?,pnl=?,reason=?,broker_status=? WHERE id=?',
               (stamp.isoformat(), exit_price, pnl, reason, status, pos['id']))
    db.commit()


def _correlation(stamp, index_name, side):
    return f'CG{stamp:%y%m%d%H%M%S}{index_name[:2]}{side}'


# ---------------- entry ----------------
def place_entry(db, trader, row, side, stamp, cfg, mode):
    reason = can_enter(db, cfg, stamp)
    if reason:
        return False, reason
    n = db.execute('SELECT COUNT(*) n FROM positions WHERE index_name=? AND substr(entry_time,1,10)=?',
                   (row['index_name'], stamp.date().isoformat())).fetchone()['n']
    if n >= cfg.get('max_trades_per_index_per_day', 999):
        return False, 'Index daily trade cap'
    plan, reason = evaluate(row, cfg, db, stamp, side)
    if not plan:
        return False, reason
    corr = _correlation(stamp, row['index_name'], side)
    # Record BEFORE sending: caps count it and a crash cannot cause a blind second order.
    cur = db.execute('''INSERT INTO positions(index_name,side,security_id,expiry,strike,qty,entry,stop,target,
                        entry_time,last_bid,last_update,correlation_id,broker_status)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                     (row['index_name'], row['side'], row['security_id'], row['expiry'], row['strike'],
                      plan['qty'], row['ask'], plan['stop'], plan['target'], stamp.isoformat(),
                      row['bid'], stamp.isoformat(), corr, 'SUBMITTING'))
    db.commit()
    pid = cur.lastrowid
    try:
        res = trader.place_super_buy(row['security_id'], plan['qty'], row['ask'], plan['target'], plan['stop'], corr)
        oid, status = str(res.get('orderId', '')), res.get('orderStatus', 'UNKNOWN')
        db.execute('UPDATE positions SET broker_order_id=?,broker_status=? WHERE id=?', (oid or None, status, pid))
        db.commit()
        _alert(db, mode, 'ORDER_SENT', f"{row['index_name']} {row['strike']:.0f}{side} qty={plan['qty']} "
               f"@{row['ask']} SL={plan['stop']:.2f} TGT={plan['target']:.2f} id={oid} {status}")
        return True, plan
    except AuthError:
        raise
    except BrokerError as ex:
        # Unknown outcome: keep the row open; reconcile() finds it by correlationId or expires it.
        db.execute('UPDATE positions SET broker_status=? WHERE id=?', ('UNKNOWN', pid))
        db.commit()
        _alert(db, mode, 'ORDER_UNCERTAIN', f'{corr}: {ex}')
        return False, str(ex)


# ---------------- reconcile & exits ----------------
def _realized_before(db, pos, fees):
    """Dhan's realizedProfit is per security per day; subtract earlier bot trades in the same contract."""
    return db.execute('''SELECT COALESCE(SUM(pnl+?),0) s FROM positions WHERE security_id=? AND id<>?
                         AND exit_time IS NOT NULL AND exit_price IS NOT NULL AND substr(entry_time,1,10)=?''',
                      (fees, pos['security_id'], pos['id'], pos['entry_time'][:10])).fetchone()['s']


def reconcile(db, trader, stamp, cfg, mode, quotes=None):
    """Bring local open positions in line with Dhan. Never places new BUY orders."""
    fees = cfg['fee_estimate_per_round_trip']
    for pos in open_positions(db):
        age = (stamp - datetime.fromisoformat(pos['entry_time'])).total_seconds()
        q = (quotes or {}).get(pos['security_id'])
        if q and q.get('bid'):
            db.execute('UPDATE positions SET last_bid=?,last_update=? WHERE id=?', (q['bid'], stamp.isoformat(), pos['id']))
            db.commit()
        order = trader.super_order(pos['broker_order_id']) if pos['broker_order_id'] else \
            trader.find_by_correlation(pos['correlation_id'] or '')
        if order is None:
            if age > 180 and pos['broker_status'] in ('UNKNOWN', 'SUBMITTING'):
                _close(db, pos, stamp, None, 0.0, 'ENTRY_NOT_FOUND', 'NOT_FOUND')
                _alert(db, mode, 'ENTRY_NOT_FOUND', f"{pos['correlation_id']} not on broker after 3 min")
            continue
        if not pos['broker_order_id']:
            db.execute('UPDATE positions SET broker_order_id=? WHERE id=?', (str(order.get('orderId')), pos['id']))
            db.commit()
        status = order.get('orderStatus', '')
        filled = int(order.get('filledQty') or 0)
        if status in DONE_ENTRY and filled == 0:
            _close(db, pos, stamp, None, 0.0, f'ENTRY_{status}', status)
            _alert(db, mode, 'ENTRY_' + status, str(order.get('omsErrorDescription') or '')[:200])
            continue
        if filled == 0 and status in ('PENDING', 'TRANSIT') and age > cfg.get('entry_fill_timeout_seconds', 45):
            try:
                trader.cancel_super_leg(order['orderId'], 'ENTRY_LEG')
                log(db, 'ENTRY_TIMEOUT_CANCEL', order['orderId'])
            except BrokerError as ex:
                log(db, 'CANCEL_FAILED', str(ex))
            continue
        if filled > 0 and pos['broker_status'] not in ('FILLED', 'EXITING'):
            avg = float(order.get('averageTradedPrice') or pos['entry'])
            db.execute('UPDATE positions SET entry=?,qty=?,broker_status=? WHERE id=?', (avg, filled, 'FILLED', pos['id']))
            db.commit()
            _alert(db, mode, 'FILLED', f"{pos['index_name']} {pos['strike']:.0f}{pos['side']} qty={filled} avg={avg}")
            pos = db.execute('SELECT * FROM positions WHERE id=?', (pos['id'],)).fetchone()
        if filled > 0 or pos['broker_status'] in ('FILLED', 'EXITING'):
            bp = trader.position(pos['security_id'])
            net = int(bp.get('netQty') or 0) if bp else 0
            if bp and net == 0:
                realized = float(bp.get('realizedProfit') or 0) - _realized_before(db, pos, fees)
                pnl = realized - fees
                exit_price = pos['entry'] + realized / max(1, pos['qty'])
                legs = {l.get('legName'): l.get('orderStatus') for l in order.get('legDetails') or []}
                why = 'BROKER_TARGET' if legs.get('TARGET_LEG') in ('TRADED', 'TRIGGERED') else \
                      'BROKER_STOP' if legs.get('STOP_LOSS_LEG') in ('TRADED', 'TRIGGERED') else \
                      (pos['reason'] or 'CLOSED')
                _close(db, pos, stamp, exit_price, pnl, why)
                _alert(db, mode, 'EXIT', f"{pos['index_name']} {pos['strike']:.0f}{pos['side']} {why} P&L≈₹{pnl:,.0f}")


def flatten(db, trader, pos, reason, stamp, mode):
    """Exit one position: cancel target/SL legs FIRST (so nothing else can sell), then market-sell net qty."""
    if pos['broker_order_id']:
        order = trader.super_order(pos['broker_order_id'])
        if order and int(order.get('filledQty') or 0) == 0 and order.get('orderStatus') in ('PENDING', 'TRANSIT'):
            trader.cancel_super_leg(pos['broker_order_id'], 'ENTRY_LEG')
            log(db, 'EXIT_CANCEL_ENTRY', reason)
            return
        for leg in ('TARGET_LEG', 'STOP_LOSS_LEG'):
            try:
                trader.cancel_super_leg(pos['broker_order_id'], leg)
            except BrokerError as ex:
                log(db, 'LEG_CANCEL_NOTE', f'{leg}: {str(ex)[:150]}')
        # If the broker's SL/target already fired, its fill may still be in flight:
        # selling now could double-sell into a SHORT. Let reconcile() close it instead.
        after = trader.super_order(pos['broker_order_id']) or {}
        fired = [l.get('legName') for l in after.get('legDetails') or []
                 if l.get('legName') in ('TARGET_LEG', 'STOP_LOSS_LEG')
                 and l.get('orderStatus') in ('TRIGGERED', 'TRADED', 'TRANSIT', 'PART_TRADED')]
        if fired or after.get('orderStatus') == 'CLOSED':
            log(db, 'EXIT_SKIPPED_BROKER_LEG_FIRED', ','.join(fired) or 'CLOSED')
            db.execute('UPDATE positions SET broker_status=?,reason=? WHERE id=?', ('EXITING', reason, pos['id']))
            db.commit()
            return
    bp = trader.position(pos['security_id'])
    net = int(bp.get('netQty') or 0) if bp else 0
    attempts = int(state(db, f"exit_attempts_{pos['id']}", '0'))
    if net > 0 and attempts < 3:
        put_state(db, f"exit_attempts_{pos['id']}", attempts + 1)
        trader.market_sell(pos['security_id'], net, f"CGX{pos['id']}A{attempts}{stamp:%H%M%S}")
        _alert(db, mode, 'EXIT_SENT', f"{pos['index_name']} {pos['strike']:.0f}{pos['side']} qty={net} reason={reason}")
    elif net > 0:
        _alert(db, mode, 'EXIT_STUCK', f"{pos['security_id']} still {net} after 3 sells — CHECK DHAN APP NOW")
        put_state(db, 'halt', '1')
    db.execute('UPDATE positions SET broker_status=?,reason=? WHERE id=?', ('EXITING', reason, pos['id']))
    db.commit()


def time_exits(db, trader, stamp, cfg, mode):
    for pos in open_positions(db):
        held = (stamp - datetime.fromisoformat(pos['entry_time'])).total_seconds()
        reason = 'SQUARE_OFF' if stamp.strftime('%H:%M') >= cfg['square_off_ist'] else \
                 'TIME' if held > cfg['max_hold_minutes'] * 60 else None
        if reason and state(db, f"exit_attempt_ts_{pos['id']}") != stamp.strftime('%H:%M'):
            put_state(db, f"exit_attempt_ts_{pos['id']}", stamp.strftime('%H:%M'))
            flatten(db, trader, pos, reason, stamp, mode)


def daily_broker_guard(db, trader, stamp, cfg, mode):
    """Broker-side daily loss stop (Dhan P&L exit + kill switch) — works even if this server dies."""
    day = stamp.date().isoformat()
    if mode != 'LIVE' or state(db, 'pnl_exit_day') == day:
        return
    loss = cfg['capital'] * cfg['daily_loss_pct'] / 100
    try:
        trader.set_pnl_exit(loss_value=loss, profit_value=cfg['capital'])
        put_state(db, 'pnl_exit_day', day)
        log(db, 'BROKER_PNL_EXIT_SET', f'loss ₹{loss:,.0f}')
    except BrokerError as ex:
        _alert(db, mode, 'BROKER_PNL_EXIT_FAILED', str(ex)[:200])


def emergency_exit(db, trader, mode, stamp, broker_wide=False):
    """Kill switch: stop new entries and exit everything this bot holds."""
    put_state(db, 'halt', '1')
    put_state(db, 'autopilot', '0')
    for pos in open_positions(db):
        try:
            flatten(db, trader, pos, 'KILL_SWITCH', stamp, mode)
        except BrokerError as ex:
            _alert(db, mode, 'KILL_EXIT_ERROR', str(ex)[:200])
    if broker_wide:
        try:
            trader.exit_all()
            log(db, 'BROKER_EXIT_ALL', 'sent')
        except BrokerError as ex:
            _alert(db, mode, 'BROKER_EXIT_ALL_FAILED', str(ex)[:200])
    _alert(db, mode, 'KILL_SWITCH', 'Halted. Autopilot OFF. Exits sent.')


# ---------------- main cycle ----------------
def live_cycle(db, trader, cfg, rows, stamp, mode, gate_problems, decide_fn, data_ok=True):
    quotes = {r['security_id']: r for r in rows if r.get('security_id')}
    try:
        reconcile(db, trader, stamp, cfg, mode, quotes)
        time_exits(db, trader, stamp, cfg, mode)
        if action := state(db, 'kill_request'):
            put_state(db, 'kill_request', '')
            emergency_exit(db, trader, mode, stamp, broker_wide=(action == 'BROKER_WIDE'))
        if gate_problems:
            put_state(db, 'autopilot_last_decision', 'LOCKED: ' + '; '.join(gate_problems))
            return 'LOCKED'
        daily_broker_guard(db, trader, stamp, cfg, mode)
        if state(db, 'autopilot', '0') != '1':
            put_state(db, 'autopilot_last_decision', 'OFF')
            return 'OFF'
        if not data_ok:
            put_state(db, 'autopilot_last_decision', 'NO_TRADE: market data error')
            return 'NO_TRADE'
        pick, msg = decide_fn(db, cfg, rows, stamp)
        if pick is None:
            put_state(db, 'autopilot_last_decision', msg)
            return msg
        idx, side, choice = pick
        fingerprint = f"{stamp:%Y-%m-%d %H:%M}|{idx}|{side}|{choice['row']['security_id']}"
        if state(db, 'autopilot_last_fingerprint') == fingerprint:
            return 'Duplicate signal ignored'
        put_state(db, 'autopilot_last_fingerprint', fingerprint)
        ok, detail = place_entry(db, trader, choice['row'], side, stamp, cfg, mode)
        result = (f'{mode}_ORDER' if ok else 'REJECTED') + f': {idx} {side} {detail}'
        put_state(db, 'autopilot_last_decision', result)
        log(db, 'AUTOPILOT', result)
        return result
    except AuthError as ex:
        put_state(db, 'halt', '1')
        _alert(db, mode, 'AUTH_ERROR', f'{ex} — new entries halted. Broker stop-loss legs remain active.')
        return 'AUTH_ERROR'
    except BrokerError as ex:
        # Network/HTTP hiccup: broker-side SL/target still protect open positions; retry next cycle.
        log(db, 'BROKER_ERROR', str(ex)[:300])
        return 'BROKER_ERROR'
