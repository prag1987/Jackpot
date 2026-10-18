"""Persistent, authenticated paper-only control-plane settings."""
import os, json, sqlite3
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
from engine import ROOT,load_config
DATA=Path(os.getenv('CG_DATA_DIR',str(ROOT/'data')))
DATA.mkdir(parents=True,exist_ok=True)
SETTINGS=DATA/'web_settings.db'
ALLOWED={'capital':(10000,10000000),'risk_per_trade_pct':(0.05,1.0),'daily_loss_pct':(0.1,3.0),
         'weekly_loss_pct':(0.5,6.0),'max_trades_per_day':(1,5),
         'max_spread_pct':(0.1,2.0),'stop_loss_pct':(3,35),'target_reward_risk':(1,4),
         'min_days_to_expiry':(0,7)}
def init():
    with sqlite3.connect(SETTINGS) as c:
        c.execute('CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS secrets(k TEXT PRIMARY KEY, encrypted BLOB NOT NULL)')
def save_settings(changes):
    init()
    if set(changes)-set(ALLOWED):raise ValueError('Unknown setting')
    for k,v in changes.items():
        lo,hi=ALLOWED[k]
        if not isinstance(v,(int,float)) or not lo<=v<=hi:raise ValueError(f'Invalid {k}')
    with sqlite3.connect(SETTINGS) as c:
        for k,v in changes.items(): c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(k,json.dumps(v)))
def effective_config():
    cfg=load_config();init()
    with sqlite3.connect(SETTINGS) as c:
        for k,v in c.execute('SELECT k,v FROM settings'):
            if k in ALLOWED:cfg[k]=json.loads(v)
    cfg['mode']='PAPER_ONLY'
    return cfg

def cipher():
    key=os.environ.get('CG_ENCRYPTION_KEY','')
    if not key:raise ValueError('CG_ENCRYPTION_KEY not configured')
    return Fernet(key.encode())
def save_dhan_credentials(client_id,access_token):
    if not client_id.strip() or not access_token.strip():raise ValueError('Credentials required')
    enc=cipher()
    with sqlite3.connect(SETTINGS) as c:
        for k,v in [('client_id',client_id.strip()),('access_token',access_token.strip())]:
            c.execute('INSERT OR REPLACE INTO secrets VALUES (?,?)',(k,enc.encrypt(v.encode())))
def dhan_credentials():
    init()
    with sqlite3.connect(SETTINGS) as c:
        rows=dict(c.execute('SELECT k,encrypted FROM secrets').fetchall())
    if not rows:return os.getenv('DHAN_CLIENT_ID',''),os.getenv('DHAN_ACCESS_TOKEN','')
    enc=cipher()
    return tuple(enc.decrypt(rows[k]).decode() for k in ('client_id','access_token'))
def clear_credentials():
    init()
    with sqlite3.connect(SETTINGS) as c:c.execute('DELETE FROM secrets')
