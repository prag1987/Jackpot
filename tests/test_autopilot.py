from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
from autopilot import set_autopilot, load_closed_candles, autopilot_paper_cycle
from engine import init_db, state

def test_switch(tmp_path):
    db=init_db(tmp_path/'switch.sqlite')
    set_autopilot(db,True)
    assert state(db,'autopilot')=='1'
    set_autopilot(db,False)
    assert state(db,'autopilot')=='0'

def test_missing_candles_fail_closed(tmp_path):
    data,error=load_closed_candles('NIFTY',datetime(2026,10,19,11,0,tzinfo=ZoneInfo('Asia/Kolkata')),tmp_path/'absent.csv')
    assert data is None and error

def test_stale_candles_fail_closed(tmp_path):
    p=tmp_path/'nifty.csv'
    pd.DataFrame({'timestamp':['2026-10-16 10:00']*85}).to_csv(p,index=False)
    data,error=load_closed_candles('NIFTY',datetime(2026,10,19,11,0,tzinfo=ZoneInfo('Asia/Kolkata')),p)
    assert data is None and error

def test_off_no_order(tmp_path):
    db=init_db(tmp_path/'off.sqlite')
    assert autopilot_paper_cycle(db,{},[],datetime(2026,10,19,11,0,tzinfo=ZoneInfo('Asia/Kolkata')))=='OFF'
    assert db.execute('SELECT COUNT(*) FROM positions').fetchone()[0]==0
