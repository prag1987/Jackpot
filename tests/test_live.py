from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import live_engine as le
from live_broker import round_tick, AuthError, BrokerError, static_ip_problem, DhanTrader
from engine import init_db, load_config, state, put_state, open_positions
from candle_feed import to_frame, completed_only

IST = ZoneInfo('Asia/Kolkata')
T = datetime(2026, 10, 19, 10, 0, tzinfo=IST)
ROW = {'index_name': 'NIFTY', 'side': 'CE', 'security_id': '4242', 'expiry': '2026-10-27',
       'strike': 25000.0, 'ask': 100.0, 'bid': 99.5}
PLAN = {'qty': 65, 'stop': 85.0, 'target': 122.5}


class FakeTrader:
    def __init__(self):
        self.calls, self.orders, self.pos = [], {}, {}

    def place_super_buy(self, sid, qty, price, target, stop, corr, trailing=0.0):
        self.calls.append(('BUY', sid, qty, price, target, stop))
        self.orders['1'] = {'orderId': '1', 'correlationId': corr, 'orderStatus': 'PENDING', 'filledQty': 0,
                            'legDetails': []}
        return {'orderId': '1', 'orderStatus': 'PENDING'}

    def super_order(self, oid): return self.orders.get(str(oid))

    def find_by_correlation(self, corr):
        return next((o for o in self.orders.values() if o['correlationId'] == corr), None)

    def cancel_super_leg(self, oid, leg): self.calls.append(('CANCEL', leg))

    def position(self, sid): return self.pos.get(sid)

    def market_sell(self, sid, qty, corr): self.calls.append(('SELL', sid, qty))

    def set_pnl_exit(self, **kw): self.calls.append(('PNL_EXIT', kw))

    def exit_all(self): self.calls.append(('EXIT_ALL',))


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(le, 'can_enter', lambda db, cfg, stamp: 'Existing open position' if open_positions(db) else None)
    monkeypatch.setattr(le, 'evaluate', lambda row, cfg, db, stamp, side: (dict(PLAN), 'PASS'))
    monkeypatch.setattr(le.notify, 'send', lambda text: False)
    return le.exec_db('LIVE', tmp_path / 'live.db'), FakeTrader(), load_config()


def test_round_tick():
    assert round_tick(100.03, up=True) == 100.05 and round_tick(84.99) == 84.95


def test_live_gate_locks(tmp_path):
    paper = init_db(tmp_path / 'paper.db'); cfg = dict(load_config(), min_paper_sessions=30)
    problems = le.live_gate('LIVE', cfg, paper, ip_check=lambda: None, env={})
    assert any('CG_ALLOW_LIVE' in p for p in problems) and any('0/30' in p for p in problems)
    cfg['min_paper_sessions'] = 0
    assert le.live_gate('LIVE', cfg, paper, ip_check=lambda: None, env={'CG_ALLOW_LIVE': 'YES'}) == []
    assert le.live_gate('LIVE', cfg, paper, ip_check=lambda: 'IP mismatch', env={'CG_ALLOW_LIVE': 'YES'}) == ['IP mismatch']


def test_static_ip():
    assert static_ip_problem('1.2.3.4', '1.2.3.4') is None
    assert 'does not match' in static_ip_problem('1.2.3.4', '5.6.7.8')
    assert 'not set' in static_ip_problem('', '5.6.7.8')


def test_paper_sessions_need_enough_cycles(tmp_path):
    paper = init_db(tmp_path / 'paper.db')
    for _ in range(le.SESSION_MIN_CYCLES):
        le.record_paper_cycle(paper, T)
    le.record_paper_cycle(paper, T + timedelta(days=1))  # one cycle only: not a session
    le.record_paper_cycle(paper, T.replace(hour=8))      # pre-market: ignored
    assert le.paper_session_count(paper) == 1


def test_entry_recorded_and_single_position(tmp_path, monkeypatch):
    db, tr, cfg = setup(tmp_path, monkeypatch)
    ok, _ = le.place_entry(db, tr, ROW, 'CE', T, cfg, 'LIVE')
    assert ok and tr.calls[0][:3] == ('BUY', '4242', 65)
    ok2, why = le.place_entry(db, tr, ROW, 'CE', T, cfg, 'LIVE')
    assert not ok2 and why == 'Existing open position' and len(tr.calls) == 1


def test_rejected_entry_closes_with_zero_pnl(tmp_path, monkeypatch):
    db, tr, cfg = setup(tmp_path, monkeypatch)
    le.place_entry(db, tr, ROW, 'CE', T, cfg, 'LIVE')
    tr.orders['1'].update(orderStatus='REJECTED', omsErrorDescription='Insufficient funds')
    le.reconcile(db, tr, T + timedelta(seconds=10), cfg, 'LIVE')
    assert not open_positions(db)
    assert db.execute('SELECT pnl,reason FROM positions').fetchone()['reason'] == 'ENTRY_REJECTED'


def test_fill_then_broker_stop_closes(tmp_path, monkeypatch):
    db, tr, cfg = setup(tmp_path, monkeypatch)
    le.place_entry(db, tr, ROW, 'CE', T, cfg, 'LIVE')
    tr.orders['1'].update(orderStatus='TRADED', filledQty=65, averageTradedPrice=100.2)
    tr.pos['4242'] = {'netQty': 65, 'realizedProfit': 0}
    le.reconcile(db, tr, T + timedelta(seconds=20), cfg, 'LIVE')
    p = db.execute('SELECT * FROM positions').fetchone()
    assert p['broker_status'] == 'FILLED' and p['entry'] == 100.2 and p['exit_time'] is None
    tr.orders['1'].update(orderStatus='CLOSED', legDetails=[{'legName': 'STOP_LOSS_LEG', 'orderStatus': 'TRADED'}])
    tr.pos['4242'] = {'netQty': 0, 'realizedProfit': -975.0}
    le.reconcile(db, tr, T + timedelta(minutes=5), cfg, 'LIVE')
    p = db.execute('SELECT * FROM positions').fetchone()
    assert p['reason'] == 'BROKER_STOP' and p['pnl'] == -975.0 - cfg['fee_estimate_per_round_trip']


def test_square_off_cancels_legs_before_selling(tmp_path, monkeypatch):
    db, tr, cfg = setup(tmp_path, monkeypatch)
    le.place_entry(db, tr, ROW, 'CE', T, cfg, 'LIVE')
    tr.orders['1'].update(orderStatus='TRADED', filledQty=65, averageTradedPrice=100.0)
    tr.pos['4242'] = {'netQty': 65, 'realizedProfit': 0}
    late = T.replace(hour=15, minute=16)
    le.reconcile(db, tr, late, cfg, 'LIVE')
    le.time_exits(db, tr, late, cfg, 'LIVE')
    kinds = [c[0] if c[0] != 'CANCEL' else c[1] for c in tr.calls[1:]]
    assert kinds == ['TARGET_LEG', 'STOP_LOSS_LEG', 'SELL']
    le.time_exits(db, tr, late, cfg, 'LIVE')  # same minute: no duplicate sell
    assert sum(c[0] == 'SELL' for c in tr.calls) == 1


def test_auth_error_halts(tmp_path, monkeypatch):
    db, tr, cfg = setup(tmp_path, monkeypatch)
    put_state(db, 'autopilot', '1')

    def boom(*a, **k): raise AuthError('token expired')
    tr.super_order = boom
    le.place_entry(db, tr, ROW, 'CE', T, cfg, 'LIVE')
    assert le.live_cycle(db, tr, cfg, [], T, 'LIVE', [], lambda *a: (None, 'x')) == 'AUTH_ERROR'
    assert state(db, 'halt') == '1'


def test_locked_gate_never_orders(tmp_path, monkeypatch):
    db, tr, cfg = setup(tmp_path, monkeypatch)
    put_state(db, 'autopilot', '1')
    pick = (('NIFTY', 'CE', {'row': ROW}), 'SIGNAL')
    assert le.live_cycle(db, tr, cfg, [ROW], T, 'LIVE', ['Paper sessions 0/30'], lambda *a: pick) == 'LOCKED'
    assert not tr.calls


def test_kill_request_exits(tmp_path, monkeypatch):
    db, tr, cfg = setup(tmp_path, monkeypatch)
    le.place_entry(db, tr, ROW, 'CE', T, cfg, 'LIVE')
    tr.orders['1'].update(orderStatus='TRADED', filledQty=65, averageTradedPrice=100.0)
    tr.pos['4242'] = {'netQty': 65, 'realizedProfit': 0}
    put_state(db, 'kill_request', 'BOT'); put_state(db, 'autopilot', '1')
    le.live_cycle(db, tr, cfg, [], T + timedelta(minutes=1), 'LIVE', [], lambda *a: (None, 'x'))
    assert ('SELL', '4242', 65) in tr.calls and state(db, 'halt') == '1' and state(db, 'autopilot') == '0'


def test_trader_refuses_bad_order():
    t = DhanTrader('1', 'x', 'SANDBOX')
    try:
        t.place_super_buy('1', 65, 100, 90, 110, 'c')
        assert False
    except BrokerError:
        pass


def test_candles_completed_only():
    base = int(datetime(2026, 10, 19, 9, 15, tzinfo=IST).timestamp())
    ts = [base + 300 * i for i in range(10)]
    frame = to_frame({'timestamp': ts, 'open': [1] * 10, 'high': [2] * 10, 'low': [0.5] * 10,
                      'close': [1.5] * 10, 'volume': [10] * 10})
    assert frame.timestamp.iloc[0] == pd.Timestamp('2026-10-19 09:15')
    done = completed_only(frame, datetime(2026, 10, 19, 10, 2, tzinfo=IST))
    assert done.timestamp.iloc[-1] == pd.Timestamp('2026-10-19 09:55')


def test_no_double_sell_when_broker_stop_fired(tmp_path, monkeypatch):
    db, tr, cfg = setup(tmp_path, monkeypatch)
    le.place_entry(db, tr, ROW, 'CE', T, cfg, 'LIVE')
    tr.orders['1'].update(orderStatus='TRADED', filledQty=65, averageTradedPrice=100.0,
                          legDetails=[{'legName': 'STOP_LOSS_LEG', 'orderStatus': 'TRIGGERED'}])
    tr.pos['4242'] = {'netQty': 65, 'realizedProfit': 0}
    late = T.replace(hour=15, minute=16)
    le.reconcile(db, tr, late, cfg, 'LIVE')
    le.time_exits(db, tr, late, cfg, 'LIVE')
    assert not any(c[0] == 'SELL' for c in tr.calls)
