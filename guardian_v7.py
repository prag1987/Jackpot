"""V7 execution-guardian simulation. ALL fills simulated; NO broker write endpoints.

Risk budgeting, durable intent idempotency, leg-by-leg basket state and pyramiding.
This component DOES NOT implement broker protective orders or margin confirmation.
"""
from __future__ import annotations
import json, math, sqlite3, uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CAPITAL = 200_000

def _utc(): return datetime.now(timezone.utc).isoformat()
def _positive(n): return isinstance(n,(float,int)) and not isinstance(n,bool) and math.isfinite(n) and n>0

class ExecutionGuardian:
    def __init__(self,db_path='data/guardian_v7.db',capital=DEFAULT_CAPITAL):
        self.db_path=str(db_path);Path(self.db_path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.db_path,timeout=10,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute("CREATE TABLE IF NOT EXISTS intents (key TEXT PRIMARY KEY, created TEXT NOT NULL, symbol TEXT NOT NULL, strategy TEXT NOT NULL, qty INTEGER NOT NULL, risk REAL NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY AUTOINCREMENT, intent_key TEXT NOT NULL, leg INTEGER NOT NULL, quantity INTEGER NOT NULL, price REAL NOT NULL, side TEXT NOT NULL, at TEXT NOT NULL, UNIQUE(intent_key,leg))")
        self.db.execute("CREATE TABLE IF NOT EXISTS controls (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS audit (at TEXT,event TEXT,detail TEXT)")
        self.capital=capital
    def audit(self,event,detail):self.db.execute('INSERT INTO audit VALUES(?,?,?)',(_utc(),event,str(detail)[:2000]))
    def setting(self,key,default='0'):
        row=self.db.execute('SELECT value FROM controls WHERE key=?',(key,)).fetchone()
        return row['value'] if row else default
    def set_control(self,key,value):
        if key not in ('halt','autopilot','paper_only'):raise ValueError('Unsupported control')
        if key=='paper_only' and str(value)!='1':raise ValueError('V7 cannot enable live orders')
        self.db.execute('INSERT OR REPLACE INTO controls VALUES(?,?)',(key,str(value)))
        self.audit('CONTROL',f'{key}={value}')
    def permitted(self):
        return self.setting('autopilot')=='1' and self.setting('halt')!='1' and self.setting('paper_only','1')=='1'
    def risk_check(self,max_loss,locked_capital=0,day_loss=0,week_loss=0,drawdown=0,existing_open_risk=0):
        if not _positive(max_loss):return False,'Invalid defined max loss'
        if self.capital<=0:return False,'Capital invalid'
        if day_loss>=self.capital*.01:return False,'Daily loss circuit breaker'
        if week_loss>=self.capital*.03:return False,'Weekly loss circuit breaker'
        if drawdown>=self.capital*.06:return False,'Drawdown circuit breaker'
        if existing_open_risk+max_loss>self.capital*.0075:return False,'Portfolio open-risk ceiling'
        if max_loss>self.capital*.005:return False,'Per-trade risk ceiling'
        if locked_capital>self.capital*.35:return False,'Capital lock ceiling'
        return True,'OK'
    def create_intent(self,key,symbol,strategy,legs,qty,max_loss,locked_capital,**risk):
        """Idempotent intent reservation. No orders are sent. Inputs must be independently validated.

        legs: [{security_id,side,limit_price,lot_size}]. No naked short leg allowed.
        """
        existing=self.db.execute('SELECT * FROM intents WHERE key=?',(key,)).fetchone()
        if existing:return dict(existing),False
        if not self.permitted():raise PermissionError('Autopilot halted or not paper-enabled')
        if not isinstance(key,str) or not (1<=len(key)<=120):raise ValueError('Invalid intent key')
        if strategy not in ('LONG_CALL','LONG_PUT','BULL_CALL_DEBIT','BEAR_PUT_DEBIT'):
            raise ValueError('Strategy not supported by V7 simulator')
        expected=1 if strategy.startswith('LONG_') else 2
        if not isinstance(legs,list) or len(legs)!=expected:raise ValueError('Incorrect legs')
        if not isinstance(qty,int) or isinstance(qty,bool) or qty<=0:raise ValueError('Invalid quantity')
        for leg in legs:
            if not isinstance(leg,dict) or leg.get('side') not in ('BUY','SELL') or not str(leg.get('security_id','')):raise ValueError('Bad leg')
            if not _positive(leg.get('limit_price')) or not isinstance(leg.get('lot_size'),int) or leg['lot_size']<=0 or qty%leg['lot_size']:raise ValueError('Invalid quote or lot size')
        if expected==1 and legs[0]['side']!='BUY':raise ValueError('Long only')
        if expected==2 and [x['side'] for x in legs]!=['BUY','SELL']:raise ValueError('Long-hedge-first debit spread only')
        ok,reason=self.risk_check(max_loss,locked_capital,**risk)
        if not ok:raise ValueError(reason)
        data=json.dumps({'legs':legs,'locked_capital':locked_capital,'max_loss':max_loss})
        try:
            self.db.execute('INSERT INTO intents VALUES(?,?,?,?,?,?,?,?)',(key,_utc(),symbol,strategy,qty,max_loss,'RESERVED',data))
        except sqlite3.IntegrityError:
            return dict(self.db.execute('SELECT * FROM intents WHERE key=?',(key,)).fetchone()),False
        self.audit('RESERVE',key)
        return dict(self.db.execute('SELECT * FROM intents WHERE key=?',(key,)).fetchone()),True
    def fill_paper_leg(self,key,leg_index,price,quantity):
        if not self.permitted():raise PermissionError('Paper engine paused')
        row=self.db.execute('SELECT * FROM intents WHERE key=?',(key,)).fetchone()
        if row is None:raise KeyError(key)
        info=json.loads(row['data']);legs=info['legs']
        if row['status'] in ('ABORTED','CLOSED','UNKNOWN'):raise ValueError('Intent inactive')
        if not 0<=leg_index<len(legs):raise IndexError('Leg')
        if leg_index>0 and self.db.execute('SELECT 1 FROM fills WHERE intent_key=? AND leg=0',(key,)).fetchone() is None:
            raise ValueError('Long protective leg must fill first')
        if not _positive(price) or not isinstance(quantity,int) or quantity!=row['qty']:raise ValueError('Simulated fill invalid')
        prior=self.db.execute('SELECT * FROM fills WHERE intent_key=? AND leg=?',(key,leg_index)).fetchone()
        if prior:return dict(prior),False
        self.db.execute('INSERT INTO fills(intent_key,leg,quantity,price,side,at) VALUES(?,?,?,?,?,?)',(key,leg_index,quantity,price,legs[leg_index]['side'],_utc()))
        count=self.db.execute('SELECT COUNT(*) FROM fills WHERE intent_key=?',(key,)).fetchone()[0]
        self.db.execute('UPDATE intents SET status=? WHERE key=?',('OPEN' if count==len(legs) else 'PARTIAL_HEDGE_ONLY',key))
        self.audit('SIM_FILL',f'{key}: leg={leg_index},qty={quantity},price={price}')
        return dict(self.db.execute('SELECT * FROM fills WHERE intent_key=? AND leg=?',(key,leg_index)).fetchone()),True
    def abort_unfilled(self,key):
        row=self.db.execute('SELECT status FROM intents WHERE key=?',(key,)).fetchone()
        if row is None:raise KeyError(key)
        count=self.db.execute('SELECT COUNT(*) FROM fills WHERE intent_key=?',(key,)).fetchone()[0]
        if count:raise ValueError('Already filled exposure: requires explicit unwind, not cancellation')
        self.db.execute('UPDATE intents SET status=? WHERE key=?',('ABORTED',key));self.audit('ABORT',key)
    def plan_pyramid(self,profit_r,add_loss,existing_open_risk,already_added=False,signal_confirmed=False):
        if already_added or not signal_confirmed or profit_r<1:return False,'No add-on confirmation'
        if not _positive(add_loss) or add_loss>self.capital*.0025:return False,'Add-on risk exceeds 0.25%'
        if existing_open_risk+add_loss>self.capital*.0075:return False,'Portfolio risk ceiling'
        return True,'Paper add-on candidate, requires new intent and broker-independent risk assessment'
    def intents(self):return [dict(x) for x in self.db.execute('SELECT * FROM intents ORDER BY created DESC LIMIT 100')]
    def close(self):self.db.close()
