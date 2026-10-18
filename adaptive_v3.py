"""Capital Guard V3 experimental, deterministic PAPER-ONLY basket selector.
No broker order placements, no live execution functions. Not investment advice.
Candidate estimates are NOT validated expectancy forecasts.
"""
from dataclasses import dataclass, asdict
from typing import List, Optional
import math

@dataclass(frozen=True)
class MarketRegime:
    trend_strength: float  # -1 strongly bearish to +1 strongly bullish; externally computed and validated
    iv_percentile: float   # 0..100, supplied from independent historical observations
    event_verified: bool
    event_blackout: bool
    data_fresh: bool
    breakout_expected: bool = False

@dataclass(frozen=True)
class Leg:
    side: str   # BUY or SELL
    option_type: str  # CE or PE
    strike: float
    bid: float
    ask: float
    lot_size: int
    security_id: str
    expiry: str
    depth: int = 0

@dataclass
class Basket:
    name: str
    legs: List[Leg]
    lots: int
    max_loss: float
    max_profit: Optional[float]
    entry_cashflow: float
    estimated_cost: float
    collateral_required: Optional[float] = None
    reason: str = ''

CONFIG = {
    'capital': 200000.0,
    'risk_per_trade': 1000.0,
    'max_total_open_risk': 1500.0,
    'max_daily_loss': 2000.0,
    'max_margin_fraction': 0.35,
    'minimum_lots': 1,
    'max_lots_per_basket': 2,
    'max_leg_spread_pct': 1.0,
    'max_trades_per_day': 2,
    'pyramid_enabled': False,
}

def regime_candidates(regime: MarketRegime):
    """Names are hypotheses, not signals or profit predictions."""
    if not regime.event_verified or regime.event_blackout or not regime.data_fresh:
        return ['NO_TRADE']
    t, iv = regime.trend_strength, regime.iv_percentile
    if any(not math.isfinite(x) for x in (t,iv)) or abs(t)>1 or not 0<=iv<=100:
        return ['NO_TRADE']
    if t>=0.65:
        return ['BULL_CALL_DEBIT', 'LONG_CALL'] if iv<70 else ['BULL_CALL_DEBIT']
    if t<=-0.65:
        return ['BEAR_PUT_DEBIT', 'LONG_PUT'] if iv<70 else ['BEAR_PUT_DEBIT']
    if t>=0.25:
        return ['BULL_PUT_CREDIT'] if iv>=55 else ['BULL_CALL_DEBIT']
    if t<=-0.25:
        return ['BEAR_CALL_CREDIT'] if iv>=55 else ['BEAR_PUT_DEBIT']
    if iv>=65:
        return ['IRON_CONDOR']
    if iv<35 and regime.breakout_expected:
        return ['LONG_STRANGLE']
    return ['NO_TRADE']

def preflight(legs, max_spread_pct=1.0):
    if not legs: return 'No legs'
    first=legs[0]
    if any(l.expiry!=first.expiry or l.lot_size!=first.lot_size or l.lot_size<=0 for l in legs):
        return 'Mixed expiry or lot size'
    if len({l.security_id for l in legs})!=len(legs):return 'Repeated security ID'
    for l in legs:
        if l.side not in ('BUY','SELL') or l.option_type not in ('CE','PE'):
            return 'Invalid leg type'
        if any(not math.isfinite(x) for x in (l.bid,l.ask,l.strike)) or l.ask<=0 or l.bid<=0 or l.ask<l.bid:
            return 'Bad quote'
        if 100*(l.ask-l.bid)/l.ask>max_spread_pct:return 'Wide leg spread'
        if l.depth<first.lot_size:return 'Insufficient displayed depth'
    return None

def defined_risk_basket(name, legs, lots=1, estimated_fees=140.0, cfg=None):
    """Conservative expiry-based payoff bound; includes configured fee estimate.
    Assumes all legs filled at displayed BID (sells) / ASK (buys), same underlying + expiry,
    no settlement/exercise complications, and market risk for underlying indexed options.
    Margin must be separately verified from broker; otherwise credit structures are blocked.
    """
    cfg = {**CONFIG, **(cfg or {})}
    why=preflight(legs,cfg['max_leg_spread_pct'])
    if why:return None,why
    if not 1<=lots<=cfg['max_lots_per_basket']:return None,'Lot count exceeds limit'
    lot=legs[0].lot_size; qty=lot*lots
    cashflow=sum((l.bid if l.side=='SELL' else -l.ask) for l in legs)*qty
    if name == 'BULL_CALL_DEBIT':
        if len(legs)!=2 or any(l.option_type!='CE' for l in legs):return None,'Invalid bull call legs'
        buys=[l for l in legs if l.side=='BUY'];sells=[l for l in legs if l.side=='SELL']
        if len(buys)!=1 or len(sells)!=1 or buys[0].strike>=sells[0].strike:return None,'Wrong strike ordering'
    elif name == 'BEAR_PUT_DEBIT':
        if len(legs)!=2 or any(l.option_type!='PE' for l in legs):return None,'Invalid bear put legs'
        buys=[l for l in legs if l.side=='BUY'];sells=[l for l in legs if l.side=='SELL']
        if len(buys)!=1 or len(sells)!=1 or buys[0].strike<=sells[0].strike:return None,'Wrong strike ordering'
    elif name == 'IRON_CONDOR':
        if len(legs)!=4:return None,'Condor needs four legs'
        puts=sorted([l for l in legs if l.option_type=='PE'],key=lambda x:x.strike)
        calls=sorted([l for l in legs if l.option_type=='CE'],key=lambda x:x.strike)
        if len(puts)!=2 or len(calls)!=2:return None,'Condor requires two puts and two calls'
        if not (puts[0].side=='BUY' and puts[1].side=='SELL' and calls[0].side=='SELL' and calls[1].side=='BUY' and puts[1].strike<calls[0].strike):
            return None,'Not a protected iron condor'
    else:return None,'Unimplemented structure; NO TRADE'
    # Sample expiry payoff across all strike breakpoints plus extreme wings.
    strikes=[l.strike for l in legs]
    samples=sorted(set([0.0,*strikes,max(strikes)*3]))
    def expiry_pnl(spot):
        val=0.0
        for l in legs:
            intrinsic=max(spot-l.strike,0.0) if l.option_type=='CE' else max(l.strike-spot,0.0)
            val += (intrinsic if l.side=='BUY' else -intrinsic)*qty
        return cashflow+val-estimated_fees
    payoffs=[expiry_pnl(s) for s in samples]
    worst=min(payoffs);best=max(payoffs)
    if worst>=0:return None,'Unrealistic/incorrect pricing assumption'
    max_loss=-worst
    if max_loss>cfg['risk_per_trade'] or max_loss>cfg['max_total_open_risk']:
        return None,'Worst case exceeds risk budget'
    if name == 'IRON_CONDOR':
        return None,'Credit basket blocked until broker margin and hedged leg execution checks implemented'
    if -cashflow+estimated_fees>cfg['capital']*cfg['max_margin_fraction']:
        return None,'Capital lock exceeds cap'
    return Basket(name,legs,lots,round(max_loss,2),round(best,2),round(cashflow,2),estimated_fees), 'PAPER_CANDIDATE_ONLY'

def pyramid_gate(unrealized_profit, initial_risk, current_total_open_risk, add_risk,
                 consecutive_losses, fresh_signal, cfg=None):
    """Conservative default denies pyramiding entirely. Future mode still risk bounded."""
    cfg={**CONFIG,**(cfg or {})}
    if not cfg['pyramid_enabled']:return False,'Pyramiding disabled by default'
    if not fresh_signal:return False,'No new independent signal'
    if consecutive_losses>0:return False,'Loss cooldown'
    if unrealized_profit < initial_risk:return False,'Profit below +1R'
    if current_total_open_risk+add_risk>cfg['max_total_open_risk']:
        return False,'Combined worst-case risk above limit'
    if add_risk>cfg['risk_per_trade']/2:return False,'Add-on risk > 0.5 of base risk'
    return True,'Only if protective stops and total open risk verified'
