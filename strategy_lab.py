"""Deterministic, offline strategy research. No broker order API access.

Usage: python strategy_lab.py --demo
       python strategy_lab.py --candles data/your_5m.csv --ivp 45 --event-verified
CSV: timestamp,open,high,low,close,volume (IST timestamps).
Uses only CLOSED candles; output is an unvalidated research candidate, never a trade.
"""
import argparse, json, math
from dataclasses import dataclass, asdict
from pathlib import Path
import pandas as pd
from adaptive_v3 import MarketRegime,regime_candidates

@dataclass(frozen=True)
class Verdict:
    direction: str
    strength: float
    atr_pct: float
    adx_proxy: float
    candidate_names: list
    reason: str

def analyze(candles, iv_percentile, event_verified=False, event_blackout=False):
    if not event_verified or event_blackout:
        return Verdict('NO_TRADE',0,0,0,['NO_TRADE'],'Event calendar unverified or blackout')
    x=candles.copy()
    required={'timestamp','open','high','low','close','volume'}
    if not required.issubset(x.columns) or len(x)<85:
        return Verdict('NO_TRADE',0,0,0,['NO_TRADE'],'At least 85 valid closed 5m candles required')
    x['timestamp']=pd.to_datetime(x['timestamp'],errors='coerce')
    for c in ('open','high','low','close','volume'):
        x[c]=pd.to_numeric(x[c],errors='coerce')
    if x[list(required)].isna().any().any() or not x.timestamp.is_monotonic_increasing or x.timestamp.duplicated().any():
        return Verdict('NO_TRADE',0,0,0,['NO_TRADE'],'Invalid data, ordering or duplicates')
    if ((x.high<x.low)|(x.close<=0)|(x.high<x[['open','close']].max(axis=1))|
        (x.low>x[['open','close']].min(axis=1))|(x.volume<0)).any():
        return Verdict('NO_TRADE',0,0,0,['NO_TRADE'],'Invalid OHLC or volume')
    # 15m trend derived from CLOSED 5m bars, not incomplete resampled candle.
    # Input must consist of whole completed 15-minute intervals: last 3n rows.
    y=x.iloc[-(len(x)//3)*3:].copy()
    grp=pd.Series(range(len(y)),index=y.index)//3
    trend=y.groupby(grp).agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'})
    if len(trend)<28:
        return Verdict('NO_TRADE',0,0,0,['NO_TRADE'],'Insufficient 15m history')
    ema9=trend.close.ewm(span=9,adjust=False).mean().iloc[-1]
    ema21=trend.close.ewm(span=21,adjust=False).mean().iloc[-1]
    prior=trend.close.shift(1)
    tr=pd.concat([(trend.high-trend.low).abs(),(trend.high-prior).abs(),(trend.low-prior).abs()],axis=1).max(axis=1)
    atr=tr.rolling(14).mean().iloc[-1]
    if not math.isfinite(atr) or atr<=0:
        return Verdict('NO_TRADE',0,0,0,['NO_TRADE'],'ATR invalid')
    # Not Wilder ADX: transparent directional efficiency proxy only.
    efficiency=abs(trend.close.iloc[-1]-trend.close.iloc[-15]) / max(1e-9,trend.close.diff().abs().iloc[-14:].sum())
    sign=1 if ema9>ema21 else -1
    strength=round(sign*min(1.,float(efficiency)),3)
    # Require breakout of PRIOR 12 completed 5m highs/lows, no lookahead.
    high=x.high.iloc[-13:-1].max(); low=x.low.iloc[-13:-1].min()
    last=x.close.iloc[-1]
    direction='BULL' if strength>=.65 and last>high else 'BEAR' if strength<=-.65 and last<low else 'NO_TRADE'
    candidates=regime_candidates(MarketRegime(strength,float(iv_percentile),True,False,True)) if direction!='NO_TRADE' else ['NO_TRADE']
    if direction=='BULL': candidates=[s for s in candidates if s in ('BULL_CALL_DEBIT','LONG_CALL')]
    if direction=='BEAR': candidates=[s for s in candidates if s in ('BEAR_PUT_DEBIT','LONG_PUT')]
    if not candidates:candidates=['NO_TRADE']
    return Verdict(direction,strength,round(100*atr/last,3),round(100*float(efficiency),2),candidates,'Offline hypothesis; requires full historical and fill validation')

def demo():
    # Deterministic SYNTHETIC data; NOT backtest or real market evidence.
    from datetime import datetime,timedelta
    start=datetime(2026,1,1,9,15)
    z=[];prev=23000.
    for i in range(150):
        price=prev+3+(0.3 if i%5 else -0.2)
        z.append({'timestamp':(start+timedelta(minutes=5*i)).isoformat(),'open':prev,'high':price+2,'low':prev-2,'close':price,'volume':12000+i})
        prev=price
    return pd.DataFrame(z)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--demo',action='store_true');p.add_argument('--candles');p.add_argument('--ivp',type=float,default=50);p.add_argument('--event-verified',action='store_true')
    a=p.parse_args()
    if not a.demo and not a.candles:p.error('Specify --demo or --candles')
    data=demo() if a.demo else pd.read_csv(a.candles)
    v=analyze(data,a.ivp,event_verified=a.demo or a.event_verified)
    print(json.dumps({'data_source':'SYNTHETIC' if a.demo else 'USER_CSV','verdict':asdict(v),'live_orders_enabled':False},indent=2))
