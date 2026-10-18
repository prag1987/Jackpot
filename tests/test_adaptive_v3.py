from adaptive_v3 import MarketRegime,Leg,regime_candidates,defined_risk_basket,pyramid_gate

def test_sideways_condor_candidate():
    assert regime_candidates(MarketRegime(0.02,78,True,False,True))==['IRON_CONDOR']

def test_event_blocks():
    assert regime_candidates(MarketRegime(.8,20,False,False,True))==['NO_TRADE']

def test_debit_spread_in_budget():
    legs=[Leg('BUY','CE',23000,99.5,100,65,'a','2026-10-27',65),Leg('SELL','CE',23100,90,90.5,65,'b','2026-10-27',65)]
    basket,reason=defined_risk_basket('BULL_CALL_DEBIT',legs,estimated_fees=120)
    assert basket is not None and basket.max_loss==770 and reason=='PAPER_CANDIDATE_ONLY'

def test_credit_condor_not_executable():
    legs=[Leg('BUY','PE',22000,10,10.05,65,'a','2026-10-27',65),Leg('SELL','PE',22100,50,50.1,65,'b','2026-10-27',65),Leg('SELL','CE',24000,50,50.1,65,'c','2026-10-27',65),Leg('BUY','CE',24100,10,10.05,65,'d','2026-10-27',65)]
    assert defined_risk_basket('IRON_CONDOR',legs)[0] is None

def test_pyramid_disabled():
    assert not pyramid_gate(2000,1000,100,250,0,True)[0]
