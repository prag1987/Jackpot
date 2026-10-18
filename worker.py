"""Read-only Dhan data polling + paper simulation. NO LIVE ORDERS."""
import os,time,json,argparse
from datetime import date,datetime
from dotenv import load_dotenv
from engine import (ROOT,now,load_config,init_db,log,put_state,state,DhanReadOnly,
 choose_expiry,normalized_chain,snapshot_iv,mark_and_exit,paper_enter,open_positions)

def cycle(api,db,cfg):
    from web_admin import effective_config
    cfg=effective_config()
    stamp=now(); all_rows=[]; notes=[]
    if stamp.weekday()>4:
        put_state(db,'status','Weekend: no trading');return
    for name,info in cfg['indices'].items():
        try:
            expiries=api.expiries(info['underlying_id'])
            exp=choose_expiry(expiries,cfg,stamp)
            if not exp:
                notes.append(f'{name}: no suitable expiry');continue
            chain=api.chain(info['underlying_id'],exp)
            rows=normalized_chain(chain,name,exp,now())
            all_rows.extend(rows)
            snapshot_iv(db,name,rows,stamp.date().isoformat())
            notes.append(f'{name}: {len(rows)} contracts, expiry {exp}')
        except Exception as ex:
            notes.append(f'{name}: DATA ERROR {type(ex).__name__}: {str(ex)[:120]}')
            log(db,'DATA_ERROR',notes[-1])
    # Even if data is not available, NEVER invent an exit fill. Flag risk instead.
    if any('DATA ERROR' in x for x in notes):
        put_state(db,'status','; '.join(notes))
        if open_positions(db):put_state(db,'halt','1')
    else:put_state(db,'status','; '.join(notes))
    quotes={r['security_id']:r for r in all_rows if r['security_id']}
    mark_and_exit(db,quotes,now(),cfg)
    # Autonomous paper-only engine runs when ON and valid closed-candle CSV is supplied.
    from autopilot import autopilot_paper_cycle
    if not any("DATA ERROR" in x for x in notes):
        autopilot_paper_cycle(db,cfg,all_rows,now())
    # Only use a one-shot explicit dashboard-selected contract and direction.
    signal=state(db,'pending_signal')
    if signal:
        put_state(db,'pending_signal','')
        if state(db,'halt')=='1':log(db,'REJECTED','Halted');return
        try:
            request=json.loads(signal)
            row=quotes.get(request['security_id'])
            if not row or row['side']!=request['side']:log(db,'REJECTED','Contract missing or side mismatched');return
            ok,detail=paper_enter(db,row,request['side'],now(),cfg)
            log(db,'SIGNAL_RESULT',json.dumps({'accepted':ok,'detail':detail},default=str))
        except Exception as ex:log(db,'REJECTED',str(ex))
    put_state(db,'last_scan',now().isoformat())
    (ROOT/'marketdata').mkdir(exist_ok=True)
    (ROOT/'marketdata'/'latest_chain.json').write_text(json.dumps({'timestamp':now().isoformat(),'contracts':all_rows}))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');args=parser.parse_args()
    load_dotenv(ROOT/'.env');cfg=load_config()
    assert cfg['mode']=='PAPER_ONLY','Live trading is not implemented'
    from web_admin import dhan_credentials
    db=init_db();client,token=dhan_credentials();api=DhanReadOnly(client,token)
    while True:
        try:cycle(api,db,cfg)
        except KeyboardInterrupt:break
        except Exception as ex:
            log(db,'WORKER_ERROR',repr(ex));put_state(db,'halt','1');put_state(db,'status','Error: '+str(ex))
        if args.once:break
        time.sleep(max(cfg['chain_refresh_seconds'],10))
if __name__=='__main__':main()
