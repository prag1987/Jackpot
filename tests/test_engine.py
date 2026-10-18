from datetime import datetime
from zoneinfo import ZoneInfo
from engine import init_db,load_config,evaluate,choose_expiry,risk_budget,normalized_chain

def test_risk():
    assert risk_budget(load_config())==1000

def test_expiry():
    cfg=load_config();d=datetime(2026,10,19,10,0,tzinfo=ZoneInfo('Asia/Kolkata'))
    assert choose_expiry(['2026-10-19','2026-10-20','2026-10-27'],cfg,d)=='2026-10-20'

def test_reject_missing_greeks(tmp_path):
    db=init_db(tmp_path/'test.db');cfg=load_config()
    t=datetime(2026,10,19,10,0,tzinfo=ZoneInfo('Asia/Kolkata'))
    row={'side':'CE','bid':100,'ask':101,'stamp':t.isoformat(),'delta':None}
    plan,reason=evaluate(row,cfg,db,t,'CE')
    assert plan is None and reason=='Incomplete chain/Greeks'

def test_normalize():
    raw={'last_price':25000,'oc':{'25000.000000':{'ce':{'security_id':12,'top_bid_price':100,'top_ask_price':101,'greeks':{'delta':.5,'theta':-6,'vega':12,'gamma':.001},'implied_volatility':13}}}}
    out=normalized_chain(raw,'NIFTY','2026-10-27','2026-10-19T10:00:00+05:30')
    assert out[0]['strike']==25000 and out[0]['side']=='CE'

from engine import event_exclusion,can_enter,score_strikes

def test_event_calendar_fail_closed(tmp_path):
    t=datetime(2026,10,19,10,0,tzinfo=ZoneInfo('Asia/Kolkata'))
    assert event_exclusion(t,tmp_path/'missing.json')
    path=tmp_path/'calendar.json'
    path.write_text('{"verified_through":"2026-10-19","events":[{"date":"2026-10-19","start":"09:00","end":"12:00","reason":"RBI"}]}')
    assert 'Event blackout' in event_exclusion(t,path)

def test_trade_blocked_until_event_verification(tmp_path):
    db=init_db(tmp_path/'empty.db')
    t=datetime(2026,10,19,10,0,tzinfo=ZoneInfo('Asia/Kolkata'))
    assert 'Event calendar' in can_enter(db,load_config(),t)

def test_strike_rank_no_candidates_without_data(tmp_path):
    db=init_db(tmp_path/'empty.db')
    t=datetime(2026,10,19,10,0,tzinfo=ZoneInfo('Asia/Kolkata'))
    assert score_strikes([],load_config(),db,t,'CE')==[]
