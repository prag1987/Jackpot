"""PAPER-ONLY Dhan option-buying engine; no broker order integration."""
import json, math, sqlite3, time, os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

ROOT = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")
API = "https://api.dhan.co/v2"

def now(): return datetime.now(IST)
def load_config(): return json.loads((ROOT/'config.json').read_text())
def finite(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (TypeError,ValueError): return None

def init_db(path=None):
    db_path=path or (Path(os.getenv('CG_DATA_DIR',str(ROOT)))/'paper.db')
    Path(db_path).parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(str(db_path),timeout=10)
    c.row_factory=sqlite3.Row
    c.execute('''CREATE TABLE IF NOT EXISTS positions(
       id INTEGER PRIMARY KEY, index_name TEXT, side TEXT, security_id TEXT, expiry TEXT,
       strike REAL, qty INTEGER, entry REAL, stop REAL, target REAL,
       entry_time TEXT, exit_time TEXT, exit_price REAL, pnl REAL, reason TEXT,
       last_bid REAL, last_update TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,
       time TEXT, event TEXT, detail TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS iv_history(
       day TEXT, index_name TEXT, iv REAL, PRIMARY KEY(day,index_name))''')
    c.execute('''CREATE TABLE IF NOT EXISTS states(key TEXT PRIMARY KEY,value TEXT)''')
    c.commit(); return c

def log(db,event,detail):
    db.execute('INSERT INTO events(time,event,detail) VALUES(?,?,?)',(now().isoformat(),event,str(detail)[:1000]));db.commit()

def state(db,k,default=None):
    r=db.execute('SELECT value FROM states WHERE key=?',(k,)).fetchone()
    return r['value'] if r else default

def put_state(db,k,v):
    db.execute('INSERT OR REPLACE INTO states VALUES(?,?)',(k,str(v)));db.commit()

class DhanReadOnly:
    """Only POSTs to two explicit market DATA endpoints; never orders."""
    def __init__(self,client_id,token):
        if not client_id or not token: raise ValueError('Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN')
        self.session=requests.Session()
        self.session.headers.update({'access-token':token,'client-id':client_id,'Content-Type':'application/json'})
        self.last_call=0.0
    def _post(self,endpoint,payload):
        if endpoint not in ('/optionchain','/optionchain/expirylist'): raise ValueError('Not a read-only endpoint')
        lag=3.2-(time.monotonic()-self.last_call)
        if lag>0: time.sleep(lag)
        self.last_call=time.monotonic()
        res=self.session.post(API+endpoint,json=payload,timeout=12)
        res.raise_for_status(); obj=res.json()
        if obj.get('status')!='success' or 'data' not in obj: raise ValueError(f'Dhan data error {str(obj)[:250]}')
        return obj['data']
    def expiries(self,underlying_id):
        return self._post('/optionchain/expirylist',{'UnderlyingScrip':underlying_id,'UnderlyingSeg':'IDX_I'})
    def chain(self,underlying_id,expiry):
        return self._post('/optionchain',{'UnderlyingScrip':underlying_id,'UnderlyingSeg':'IDX_I','Expiry':expiry})

def choose_expiry(expiries,cfg,at=None):
    at=at or now(); days=cfg['min_days_to_expiry']
    valid=sorted(d for d in expiries if date.fromisoformat(d) >= at.date()+timedelta(days=days))
    return valid[0] if valid else None

def normalized_chain(raw,index_name,expiry,stamp):
    under=finite(raw.get('last_price'))
    if under is None or under<=0: raise ValueError('Bad underlying')
    result=[]
    for strike,legs in raw.get('oc',{}).items():
        for side in ('ce','pe'):
            d=legs.get(side) or {}; g=d.get('greeks') or {}
            if not d: continue
            result.append({'index_name':index_name,'expiry':expiry,'strike':finite(strike),
                'side':side.upper(),'security_id':str(d.get('security_id','')),
                'bid':finite(d.get('top_bid_price')),'ask':finite(d.get('top_ask_price')),
                'bid_qty':finite(d.get('top_bid_quantity')),'ask_qty':finite(d.get('top_ask_quantity')),
                'delta':finite(g.get('delta')),'theta':finite(g.get('theta')),
                'vega':finite(g.get('vega')),'gamma':finite(g.get('gamma')),
                'iv':finite(d.get('implied_volatility')),'oi':finite(d.get('oi')),
                'volume':finite(d.get('volume')),'underlying':under,'stamp':stamp})
    return result

def iv_percentile(db,index_name,iv):
    # Percentile unavailable until 20 independent daily observations exist.
    rows=db.execute('SELECT iv FROM iv_history WHERE index_name=? ORDER BY day DESC LIMIT 252',(index_name,)).fetchall()
    values=[r['iv'] for r in rows]
    return 100*sum(v<=iv for v in values)/len(values) if len(values)>=20 else None

def snapshot_iv(db,index_name,rows,day):
    atm=[r for r in rows if r['iv'] is not None and r['strike'] is not None]
    if not atm:return
    atm.sort(key=lambda r:abs(r['strike']-r['underlying']))
    closest=atm[0]['strike']; vals=[r['iv'] for r in atm if r['strike']==closest and r['iv']>0]
    if vals:
        db.execute('INSERT OR REPLACE INTO iv_history VALUES(?,?,?)',(day,index_name,sum(vals)/len(vals)));db.commit()

def risk_budget(cfg):return cfg['capital']*cfg['risk_per_trade_pct']/100

def evaluate(row,cfg,db,stamp,signal):
    """signal must be separately verified externally; MVP deliberately has NO auto-signal generator."""
    if signal not in ('CE','PE') or row['side']!=signal:return None,'No verified signal'
    needed=('strike','bid','ask','delta','theta','vega','gamma','iv','oi','volume','bid_qty','ask_qty')
    if any(row.get(k) is None for k in needed):return None,'Incomplete chain/Greeks'
    if (stamp-datetime.fromisoformat(row['stamp'])).total_seconds()>cfg['max_quote_age_seconds']:return None,'Stale quote'
    ask,bid=row['ask'],row['bid']
    if ask<=0 or bid<=0 or ask<bid:return None,'Invalid bid/ask'
    if row['ask_qty']<=0 or row['bid_qty']<=0 or row['oi']<=0 or row['volume']<=0:return None,'Insufficient liquidity'
    spread=100*(ask-bid)/ask
    if spread>cfg['max_spread_pct']:return None,'Wide spread'
    if not cfg['min_abs_delta']<=abs(row['delta'])<=cfg['max_abs_delta']:return None,'Delta outside range'
    if row['iv']<=0 or row['vega']<=0 or row['gamma']<0:return None,'Invalid Greeks'
    if row['theta']>=0:return None,'Unexpected theta units/value'
    if (row['side']=='CE' and row['delta']<=0) or (row['side']=='PE' and row['delta']>=0):return None,'Wrong delta sign'
    pct=iv_percentile(db,row['index_name'],row['iv'])
    if pct is None:return None,'Need 20 IV daily samples; entry blocked'
    if pct>cfg['max_iv_percentile']:return None,'IV percentile too high'
    expiry=date.fromisoformat(row['expiry'])
    if (expiry-stamp.date()).days<cfg['min_days_to_expiry']:return None,'Too near expiry'
    # Dhan theta display is typically per calendar day, but live units must be verified.
    # Filter is intentionally conservative: full calendar-day theta as hold-period stress,
    # NOT a claim of calibrated intraday theta prediction.
    theta_stress=abs(row['theta'])
    stop_distance=ask*cfg['stop_loss_pct']/100
    unit_risk=stop_distance+cfg['cost_reserve_per_unit']
    lot=int(cfg['indices'][row['index_name']]['lot_size'])
    raw_qty=math.floor((risk_budget(cfg)-cfg['fee_estimate_per_round_trip'])/(unit_risk*lot))*lot
    if raw_qty<lot:return None,'One lot exceeds risk budget'
    if theta_stress>stop_distance*cfg['max_theta_decay_fraction_of_risk']:return None,'Theta stress too large'
    if raw_qty*ask+cfg['fee_estimate_per_round_trip']>cfg['capital']:return None,'Capital insufficient'
    if row['ask_qty']<raw_qty or row['bid_qty']<raw_qty:return None,'Displayed depth too small'
    return {'qty':raw_qty,'stop':max(0.05,ask-stop_distance),
        'target':ask+cfg['target_reward_risk']*stop_distance,
        'iv_percentile':round(pct,1),'theta_stress':theta_stress,'spread_pct':spread},'PASS'

def realized_stats(db,stamp,cfg):
    rows=db.execute('SELECT pnl,exit_time FROM positions WHERE exit_time IS NOT NULL ORDER BY exit_time').fetchall()
    daily=sum(r['pnl'] for r in rows if datetime.fromisoformat(r['exit_time']).date()==stamp.date())
    week_start=stamp.date()-timedelta(days=stamp.weekday())
    weekly=sum(r['pnl'] for r in rows if datetime.fromisoformat(r['exit_time']).date()>=week_start)
    loss_run=0
    for r in reversed(rows):
        if r['pnl']<0:loss_run+=1
        else:break
    trades=db.execute('SELECT COUNT(*) n FROM positions WHERE substr(entry_time,1,10)=?',(stamp.date().isoformat(),)).fetchone()['n']
    return daily,weekly,loss_run,trades

def open_positions(db):return db.execute('SELECT * FROM positions WHERE exit_time IS NULL').fetchall()

def mark_and_exit(db, quotes,stamp,cfg):
    # quotes maps security_id => recent row; prices must be FRESH.
    for pos in open_positions(db):
        row=quotes.get(pos['security_id'])
        if not row:continue
        age=(stamp-datetime.fromisoformat(row['stamp'])).total_seconds()
        bid=row['bid']
        if age>cfg['max_quote_age_seconds'] or bid is None or bid<=0:continue
        db.execute('UPDATE positions SET last_bid=?,last_update=? WHERE id=?',(bid,stamp.isoformat(),pos['id']));db.commit()
        reason=None
        if bid<=pos['stop']:reason='STOP'
        elif bid>=pos['target']:reason='TARGET'
        elif (stamp-datetime.fromisoformat(pos['entry_time'])).total_seconds()>cfg['max_hold_minutes']*60:reason='TIME'
        elif stamp.strftime('%H:%M')>=cfg['square_off_ist']:reason='SQUARE_OFF'
        if reason:
            pnl=(bid-pos['entry'])*pos['qty']-cfg['fee_estimate_per_round_trip']
            db.execute('UPDATE positions SET exit_time=?,exit_price=?,pnl=?,reason=? WHERE id=?',
                       (stamp.isoformat(),bid,pnl,reason,pos['id']));db.commit()
            log(db,'PAPER_EXIT',f"{pos['index_name']} {pos['side']} PnL={pnl:.2f} {reason}")

def calendar_file():
    """Dashboard-maintained copy in the data volume wins over the one baked into the image."""
    live=Path(os.getenv('CG_DATA_DIR',str(ROOT/'data')))/'event_calendar.json'
    return live if live.exists() else ROOT/'event_calendar.json'

def event_exclusion(stamp, calendar_path=None):
    """Fail closed: a missing/unverified event calendar blocks new paper entries."""
    path=Path(calendar_path) if calendar_path else calendar_file()
    try:
        data=json.loads(path.read_text())
        verified=date.fromisoformat(data['verified_through'])
        if verified<stamp.date():return 'Event calendar not verified for trading day'
        for event in data.get('events',[]):
            if event.get('date')!=stamp.date().isoformat():continue
            start=event.get('start','00:00');end=event.get('end','23:59')
            if start<=stamp.strftime('%H:%M')<=end:return 'Event blackout: '+str(event.get('reason','scheduled event'))
    except (OSError,ValueError,KeyError,TypeError):return 'Event calendar missing or invalid'
    return None

def score_strikes(rows,cfg,db,stamp,signal):
    """Rank only fully eligible option contracts; never infer trading direction."""
    ranked=[]
    for row in rows:
        if row.get('side')!=signal:continue
        ask=row.get('ask')
        if ask is None or not cfg['min_option_premium']<=ask<=cfg['max_option_premium']:continue
        spot=row.get('underlying');strike=row.get('strike')
        if not spot or strike is None or abs(strike-spot)/spot*100>cfg['max_atm_strike_distance_pct']:continue
        plan,reason=evaluate(row,cfg,db,stamp,signal)
        if plan is None:continue
        # Rank is relative, not a proven probability of success.
        delta_dist=abs(abs(row['delta'])-cfg['target_abs_delta'])
        theta_fraction=plan['theta_stress']/max(0.01,ask)
        score=100-100*delta_dist-3*plan['spread_pct']-20*theta_fraction
        ranked.append({'score':round(score,2),'row':row,'plan':plan})
    return sorted(ranked,key=lambda item:item['score'],reverse=True)

def can_enter(db,cfg,stamp):
    if state(db,'halt')=='1':return 'HALTED'
    if stamp.weekday()>4:return 'Market closed/weekend'
    hhmm=stamp.strftime('%H:%M')
    if not cfg['entry_start_ist']<=hhmm<=cfg['entry_end_ist']:return 'Outside entry hours'
    if open_positions(db):return 'Existing open position'
    evt=event_exclusion(stamp)
    if evt:return evt
    windows=cfg.get('entry_windows_ist',[])
    if windows and not any(a<=hhmm<=b for a,b in windows):return 'Outside approved trading windows'
    previous=db.execute('SELECT entry_time,exit_time,pnl FROM positions ORDER BY id DESC LIMIT 1').fetchone()
    if previous:
        prev_start=datetime.fromisoformat(previous['entry_time'])
        if prev_start.date()==stamp.date() and (stamp-prev_start).total_seconds()<cfg.get('entry_cooldown_minutes',0)*60:
            return 'Post-entry cooldown'
        if previous['exit_time'] and previous['pnl'] is not None and previous['pnl']<0:
            previous_exit=datetime.fromisoformat(previous['exit_time'])
            if previous_exit.date()==stamp.date() and (stamp-previous_exit).total_seconds()<cfg.get('loss_cooldown_minutes',0)*60:
                return 'Post-loss cooldown'

    d,w,streak,count=realized_stats(db,stamp,cfg)
    if d<=-cfg['capital']*cfg['daily_loss_pct']/100:return 'Daily loss cap'
    if w<=-cfg['capital']*cfg['weekly_loss_pct']/100:return 'Weekly loss cap'
    if streak>=cfg['max_consecutive_losses']:return 'Consecutive loss cap'
    if count>=cfg['max_trades_per_day']:return 'Trade count cap'
    realized=db.execute('SELECT COALESCE(SUM(pnl),0) s FROM positions WHERE exit_time IS NOT NULL').fetchone()['s']
    if realized<=-cfg['capital']*cfg['max_drawdown_pct']/100:return 'Drawdown cap'
    return None

def paper_enter(db,row,approval,stamp,cfg):
    reason=can_enter(db,cfg,stamp)
    if reason:return False,reason
    n=db.execute('SELECT COUNT(*) n FROM positions WHERE index_name=? AND substr(entry_time,1,10)=?',(row['index_name'],stamp.date().isoformat())).fetchone()['n']
    if n>=cfg.get('max_trades_per_index_per_day',999):return False,'Index daily trade cap'
    plan,reason=evaluate(row,cfg,db,stamp,approval)
    if not plan:return False,reason
    db.execute('''INSERT INTO positions(index_name,side,security_id,expiry,strike,qty,entry,stop,target,entry_time,last_bid,last_update)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
             (row['index_name'],row['side'],row['security_id'],row['expiry'],row['strike'],
              plan['qty'],row['ask'],plan['stop'],plan['target'],stamp.isoformat(),row['bid'],stamp.isoformat()))
    db.commit();log(db,'PAPER_ENTRY',f"{row['index_name']} {row['side']} {row['strike']} qty={plan['qty']} ask={row['ask']}")
    return True,plan
