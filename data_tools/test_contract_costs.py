from datetime import date,datetime,timedelta,timezone
import pytest
from data_tools.contract_costs import *
T=datetime(2026,10,16,10,tzinfo=timezone.utc)
C=Contract('NIFTY',date(2026,10,20),23000,'CE',65)
def q(px=100,t=T,contract=C,synthetic=False,bid=None,ask=None):return Quote(contract,t,px,100,1000,bid,ask,synthetic)
def test_roundtrip_fees_included():
    r=basket_round_trip([('BUY',q(100))],[('SELL',q(110,T+timedelta(minutes=5)))],65,Rates())
    assert r['total_fees']>40 and r['net_pnl']<650 and r['uses_assumed_spread']
def test_brokered_bid_ask():
    f=estimate_fill(q(100,bid=99,ask=101),'BUY',65,Rates())
    assert f.execution>101 and f.spread_source=='observed'
def test_roll_contract_rejected():
    with pytest.raises(DataIntegrityError):validate_chain([q(synthetic=True)])
def test_cross_expiry_rejected():
    d=Contract('NIFTY',date(2026,10,27),23000,'PE',65)
    with pytest.raises(DataIntegrityError):validate_legs([('BUY',q()),('SELL',q(contract=d))],65)
def test_same_contract_exit_only():
    d=Contract('NIFTY',date(2026,10,20),23100,'CE',65)
    with pytest.raises(DataIntegrityError):basket_round_trip([('BUY',q())],[('SELL',q(t=T+timedelta(minutes=5),contract=d))],65,Rates())
def test_lots_rejected():
    with pytest.raises(ValueError):estimate_fill(q(),'BUY',30,Rates())
def test_stale_or_crossed_quote():
    with pytest.raises(DataIntegrityError):q(bid=102,ask=100)
def test_require_observed_bidask():
    with pytest.raises(DataIntegrityError):estimate_fill(q(),'BUY',65,Rates(),require_actual_bid_ask=True)
def test_no_duplicate_contract():
    with pytest.raises(DataIntegrityError):validate_legs([('BUY',q()),('SELL',q())],65)
