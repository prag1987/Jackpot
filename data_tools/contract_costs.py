"""Conservative and auditable NSE option backtest fills. Research only.

Never equate Dhan rolling ATM-relative history with unique contract identity.
Rates are config inputs; validate using actual dated contract notes before a run.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime
import math

class DataIntegrityError(ValueError): pass

@dataclass(frozen=True)
class Contract:
    underlying: str
    expiry: date
    strike: float
    right: str
    lot_size: int
    security_id: str = ''
    def __post_init__(self):
        if self.underlying not in ('NIFTY','BANKNIFTY') or self.right not in ('CE','PE'):
            raise DataIntegrityError('Unsupported underlying/right')
        if self.strike <= 0 or self.lot_size<=0 or not math.isfinite(self.strike):
            raise DataIntegrityError('Bad contract metadata')
    @property
    def key(self):return (self.underlying,self.expiry.isoformat(),self.strike,self.right,self.lot_size)

@dataclass(frozen=True)
class Quote:
    contract: Contract
    at: datetime
    close: float
    volume: float
    open_interest: float
    bid: float|None = None
    ask: float|None = None
    synthetic_contract: bool = False
    def __post_init__(self):
        if self.at.tzinfo is None:raise DataIntegrityError('Timezone-aware candle timestamps required')
        if not all(math.isfinite(x) for x in (self.close,self.volume,self.open_interest)) or self.close<=0 or self.volume<0 or self.open_interest<0:
            raise DataIntegrityError('Invalid market data')
        if (self.bid is None) != (self.ask is None):raise DataIntegrityError('Both bid and ask required')
        if self.bid is not None and (self.bid<=0 or self.ask<self.bid):raise DataIntegrityError('Crossed/invalid quote')

@dataclass(frozen=True)
class Rates:
    brokerage_per_order: float = 20.0
    stt_sell: float = 0.0015  # 0.15% from 1 Apr 2026; date-specific rates required for earlier periods
    exchange_txn: float = 0.0003553  # illustrative input; VERIFY dated NSE/broker schedule
    sebi_turnover: float = 0.000001
    stamp_buy: float = 0.00003
    gst: float = 0.18
    ipft_turnover: float = 0.0  # configurable, not presumed zero in real contract note
    slippage_bps: float = 15.0
    assumed_full_spread_bps: float = 50.0
    fixed_impact_per_unit: float = 0.05
    def __post_init__(self):
        for value in vars(self).values():
            if not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
                raise ValueError('Nonnegative finite cost parameters required')

@dataclass(frozen=True)
class Fill:
    side: str
    execution: float
    qty: int
    charges: float
    spread_source: str
    @property
    def signed_cashflow(self): return (1 if self.side=='SELL' else -1)*self.execution*self.qty-self.charges

def validate_chain(quotes:list[Quote], *, allow_synthetic=False):
    if not quotes:raise DataIntegrityError('No quotes')
    seen=set()
    for q in quotes:
        if q.synthetic_contract and not allow_synthetic:
            raise DataIntegrityError('Rolling ATM-relative series cannot prove contract continuity')
        if q.at.date()>q.contract.expiry:raise DataIntegrityError('Quote after expiry')
        k=(q.contract.key,q.at)
        if k in seen:raise DataIntegrityError('Duplicate candle for same contract')
        seen.add(k)
    return True

def validate_legs(legs:list[tuple[str,Quote]], qty:int, *, allow_synthetic=False):
    validate_chain([q for _,q in legs],allow_synthetic=allow_synthetic)
    if not legs or qty<=0 or isinstance(qty,bool):raise DataIntegrityError('Invalid basket quantity')
    keys=set()
    first=legs[0][1]
    for side,q in legs:
        if side not in ('BUY','SELL'):raise DataIntegrityError('Unknown side')
        if q.at!=first.at or q.contract.underlying!=first.contract.underlying or q.contract.expiry!=first.contract.expiry:
            raise DataIntegrityError('Mismatched timestamp, expiry or underlying')
        if qty%q.contract.lot_size:raise DataIntegrityError('Invalid lot multiple')
        if q.contract.key in keys:raise DataIntegrityError('Duplicate leg')
        keys.add(q.contract.key)
    return True

def estimate_fill(q:Quote,side:str,qty:int,rates:Rates,*,require_actual_bid_ask=False):
    if side not in ('BUY','SELL') or qty<=0 or qty%q.contract.lot_size:raise ValueError('Invalid order')
    if require_actual_bid_ask and q.bid is None:raise DataIntegrityError('Historical bid/ask unavailable')
    if q.bid is not None:
        base=q.ask if side=='BUY' else q.bid
        spread_source='observed'
    else:
        # Candle close is *not* an executable quote. Full assumed spread around close.
        half=q.close*rates.assumed_full_spread_bps/20000
        base=q.close+half if side=='BUY' else max(0.01,q.close-half)
        spread_source='assumed'
    impact=base*rates.slippage_bps/10000+rates.fixed_impact_per_unit
    px=base+impact if side=='BUY' else max(0.01,base-impact)
    turnover=px*qty
    brokerage=rates.brokerage_per_order
    exchange=turnover*rates.exchange_txn
    sebi=turnover*rates.sebi_turnover
    ipft=turnover*rates.ipft_turnover
    gst=rates.gst*(brokerage+exchange+sebi+ipft)
    stamp=turnover*rates.stamp_buy if side=='BUY' else 0
    stt=turnover*rates.stt_sell if side=='SELL' else 0
    fees=brokerage+exchange+sebi+ipft+gst+stamp+stt
    return Fill(side,round(px,4),qty,round(fees,4),spread_source)

def basket_round_trip(entry:list[tuple[str,Quote]],exit:list[tuple[str,Quote]],qty:int,rates:Rates,*,allow_synthetic=False,require_actual_bid_ask=False):
    validate_legs(entry,qty,allow_synthetic=allow_synthetic)
    validate_legs(exit,qty,allow_synthetic=allow_synthetic)
    if len(entry)!=len(exit):raise DataIntegrityError('Leg count mismatch')
    if any(e[1].contract.key!=x[1].contract.key or e[0]==x[0] or e[1].at>=x[1].at for e,x in zip(entry,exit)):
        raise DataIntegrityError('Exit does not close the same contract or timestamp order invalid')
    fills=[estimate_fill(q,s,qty,rates,require_actual_bid_ask=require_actual_bid_ask) for s,q in entry+exit]
    return {'net_pnl':round(sum(f.signed_cashflow for f in fills),2),'total_fees':round(sum(f.charges for f in fills),2),'fills':[vars(f) for f in fills],'uses_assumed_spread':any(f.spread_source=='assumed' for f in fills),'execution_accuracy':'observed_top_of_book_plus_impact' if all(f.spread_source=='observed' for f in fills) else 'scenario_estimate_only'}
