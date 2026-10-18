"""Autonomous decision gate. Signals require fresh CLOSED 5m candles (see candle_feed.py).
This module never talks to the broker; live_engine.py executes SANDBOX/LIVE decisions.
"""
import json
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd
from engine import ROOT,now,state,put_state,log,can_enter,score_strikes,paper_enter,iv_percentile,event_exclusion
from strategy_lab import analyze

CANDLE_DIR=ROOT/'marketdata'/'candles'

def set_autopilot(db, enabled:bool):
    put_state(db,'autopilot','1' if enabled else '0')
    if not enabled: put_state(db,'autopilot_last_decision','OFF — existing paper positions still monitored')

def load_closed_candles(index_name, stamp, path=None):
    path=Path(path) if path is not None else CANDLE_DIR/f'{index_name}_5m.csv'
    if not path.exists(): return None,'Candle file absent'
    try:
        candles=pd.read_csv(path)
        if len(candles)<85:return None,'Insufficient closed candle history'
        times=pd.to_datetime(candles['timestamp'],errors='coerce')
        if times.isna().any() or times.dt.tz is not None:return None,'Candle timestamp must be naive IST'
        if times.duplicated().any() or not times.is_monotonic_increasing:return None,'Out of order candles'
        last=times.iloc[-1].to_pydatetime()
        local=stamp.replace(tzinfo=None)
        if last.date()!=local.date() or (local-last).total_seconds()<300 or (local-last).total_seconds()>900:
            return None,'Last 5m candle not freshly completed (timestamp must be candle start IST)'
        if last.minute%5 or last.second:return None,'Invalid candle boundary'
        return candles,None
    except (OSError,KeyError,ValueError,TypeError) as ex:
        return None,'Invalid candles: '+type(ex).__name__

def decide(db,cfg,rows,stamp,candle_loader=load_closed_candles):
    """Shared signal step for PAPER, SANDBOX and LIVE.

    Returns (None, 'NO_TRADE: reasons') or ((index_name, side, ranked_choice), 'SIGNAL').
    `db` is the database whose positions/halt/caps apply (paper.db or live.db)."""
    reason=can_enter(db,cfg,stamp)
    if reason:return None,'NO_TRADE: '+reason
    candidates=[];reasons=[]
    for index_name in ('NIFTY','BANKNIFTY'):
        candles,error=candle_loader(index_name,stamp)
        if error: reasons.append(index_name+': '+error);continue
        chain=[r for r in rows if r.get('index_name')==index_name]
        if not chain:reasons.append(index_name+': no chain');continue
        vals=[r for r in chain if r.get('iv') is not None and r.get('strike') is not None]
        if not vals:reasons.append(index_name+': no IV');continue
        vals.sort(key=lambda r:abs(r['strike']-r['underlying']))
        ivp=iv_percentile(db,index_name,vals[0]['iv'])
        if ivp is None:reasons.append(index_name+': IV calibration missing');continue
        verdict=analyze(candles,ivp,event_verified=event_exclusion(stamp) is None)
        side={'BULL':'CE','BEAR':'PE'}.get(verdict.direction)
        if not side:reasons.append(index_name+': no validated breakout');continue
        ranked=score_strikes(chain,cfg,db,stamp,side)
        if ranked:
            candidates.append((ranked[0]['score'],index_name,side,ranked[0]))
        else:reasons.append(index_name+': no eligible strike')
    if not candidates:return None,'NO_TRADE: '+'; '.join(reasons)
    _,idx,side,choice=max(candidates,key=lambda x:x[0])
    return (idx,side,choice),'SIGNAL'

def autopilot_paper_cycle(db,cfg,rows,stamp=None,candle_loader=load_closed_candles):
    stamp=stamp or now()
    if state(db,'autopilot','0')!='1':return 'OFF'
    pick,msg=decide(db,cfg,rows,stamp,candle_loader)
    if pick is None:
        put_state(db,'autopilot_last_decision',msg)
        return msg
    idx,side,choice=pick
    # Idempotence: one signal per closed 5m bar + instrument, independent of worker retries.
    stamp_key=stamp.strftime('%Y-%m-%d %H:%M')
    fingerprint=f'{stamp_key}|{idx}|{side}|{choice["row"]["security_id"]}'
    if state(db,'autopilot_last_fingerprint')==fingerprint:return 'Duplicate signal ignored'
    put_state(db,'autopilot_last_fingerprint',fingerprint)
    ok,detail=paper_enter(db,choice['row'],side,stamp,cfg)
    result=('PAPER_ENTRY' if ok else 'REJECTED')+f': {idx} {side} {detail}'
    put_state(db,'autopilot_last_decision',result)
    log(db,'AUTOPILOT',result)
    return result
