import pytest
from guardian_v7 import ExecutionGuardian

def make(tmp_path):
    g=ExecutionGuardian(tmp_path/'g.db');g.set_control('autopilot','1');return g

def test_live_lock(tmp_path):
    g=make(tmp_path)
    with pytest.raises(ValueError):g.set_control('paper_only','0')

def test_risk_limits(tmp_path):
    g=make(tmp_path)
    assert g.risk_check(1000)[0]
    assert not g.risk_check(1001)[0]
    assert not g.risk_check(100,day_loss=2000)[0]
    assert not g.risk_check(100,existing_open_risk=1500)[0]

def test_idempotent_and_hedge_first(tmp_path):
    g=make(tmp_path)
    legs=[dict(security_id='1',side='BUY',limit_price=150.,lot_size=65),dict(security_id='2',side='SELL',limit_price=140.,lot_size=65)]
    assert g.create_intent('k','NIFTY','BULL_CALL_DEBIT',legs,65,850,9750)[1]
    assert not g.create_intent('k','NIFTY','BULL_CALL_DEBIT',legs,65,850,9750)[1]
    with pytest.raises(ValueError):g.fill_paper_leg('k',1,140,65)
    assert g.fill_paper_leg('k',0,150,65)[1]
    assert g.intents()[0]['status']=='PARTIAL_HEDGE_ONLY'
    assert g.fill_paper_leg('k',1,140,65)[1]
    assert g.intents()[0]['status']=='OPEN'
    assert not g.fill_paper_leg('k',1,140,65)[1]

def test_halt_and_pyramid(tmp_path):
    g=make(tmp_path)
    assert g.plan_pyramid(1.1,400,900,signal_confirmed=True)[0]
    assert not g.plan_pyramid(.8,400,900,signal_confirmed=True)[0]
    assert not g.plan_pyramid(1.2,600,900,signal_confirmed=True)[0]
    g.set_control('halt','1')
    with pytest.raises(PermissionError):g.create_intent('k','NIFTY','LONG_CALL',[dict(security_id='1',side='BUY',limit_price=150.,lot_size=65)],65,900,9750)

def test_no_unsupported_condor(tmp_path):
    g=make(tmp_path)
    with pytest.raises(ValueError):g.create_intent('c','NIFTY','IRON_CONDOR',[],65,100,1000)
