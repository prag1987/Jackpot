"""Authenticated web control panel. LIVE BUY/SELL NEVER IMPLEMENTED."""
import os,json,hmac
from datetime import datetime
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from engine import ROOT,init_db,state,put_state,now,risk_budget,score_strikes,iv_percentile,DhanReadOnly
from web_admin import effective_config,save_settings,save_dhan_credentials,dhan_credentials,clear_credentials
load_dotenv(ROOT/'.env')
st.set_page_config(page_title='Capital Guard AI — Control Center',layout='wide',page_icon='🛡️')
password=os.getenv('CG_ADMIN_PASSWORD','')
if not password:
    st.error('Admin password not configured. Set CG_ADMIN_PASSWORD in the server environment.');st.stop()
if not st.session_state.get('authenticated',False):
    st.title('🔐 Capital Guard AI')
    entered=st.text_input('Admin password',type='password')
    if st.button('Sign in'):
        if hmac.compare_digest(entered,password):st.session_state.authenticated=True;st.rerun()
        else:st.error('Incorrect password')
    st.stop()
cfg=effective_config();db=init_db()
st.title('🛡️ Capital Guard AI | Web Control Center')
st.error('LIVE MONEY TRADING LOCKED IN SOURCE CODE — Broker endpoints are read-only. PAPER ONLY.')
if st.sidebar.button('Sign out'):
    st.session_state.authenticated=False;st.rerun()
tabs=st.tabs(['Overview & P&L','Dhan integration','Trading setup','Paper orders','Risk & logs','Autopilot','V7 Guardian'])
with tabs[0]:
    a,b,c,d=st.columns(4)
    positions=pd.read_sql_query('SELECT * FROM positions ORDER BY id DESC',db)
    closed=positions.dropna(subset=['pnl']) if not positions.empty else pd.DataFrame()
    realized=float(closed.pnl.sum()) if not closed.empty else 0.
    opening=positions[positions.exit_time.isna()] if not positions.empty else pd.DataFrame()
    unrealized=0.
    for _,r in opening.iterrows():
        if pd.notna(r.last_bid):unrealized+=(float(r.last_bid)-float(r.entry))*int(r.qty)-float(cfg['fee_estimate_per_round_trip'])
    a.metric('Initial capital',f"₹{cfg['capital']:,.0f}")
    b.metric('Realized paper P&L',f'₹{realized:,.2f}')
    c.metric('Marked paper P&L (stale possible)',f'₹{unrealized:,.2f}')
    d.metric('Estimated equity',f"₹{cfg['capital']+realized+unrealized:,.2f}")
    st.caption('Unrealized P&L uses last available bid. If quotes are stale, this is NOT an executable exit valuation.')
    st.write('**Worker status:**',state(db,'status','Worker not started'))
    st.write('**Last market scan:**',state(db,'last_scan','None'))
    st.write('**New entries halted:**',state(db,'halt','0')=='1')
    st.subheader('Orders / positions')
    if positions.empty:st.info('No paper orders yet.')
    else:st.dataframe(positions,use_container_width=True,hide_index=True)
with tabs[1]:
    st.subheader('DhanHQ market-data integration')
    st.write('Credentials are encrypted at rest with server-side CG_ENCRYPTION_KEY. They are never sent to GitHub.')
    with st.form('dhan-form',clear_on_submit=True):
        client=st.text_input('Dhan Client ID')
        token=st.text_input('Dhan access token',type='password')
        save=st.form_submit_button('Encrypt & save credentials')
        if save:
            try:save_dhan_credentials(client,token);st.success('Encrypted credentials stored on server.')
            except Exception as e:st.error(f'Configuration failed: {type(e).__name__}')
    col1,col2=st.columns(2)
    if col1.button('Test Dhan market-data access'):
        try:
            a,b=dhan_credentials();api=DhanReadOnly(a,b)
            expiries=api.expiries(cfg['indices']['NIFTY']['underlying_id'])
            st.success(f'Read-only access OK; NIFTY expiry response: {str(expiries)[:180]}')
        except Exception as e:st.error(f'Connection failed: {type(e).__name__}: {str(e)[:150]}')
    if col2.button('Clear saved Dhan credentials'):
        clear_credentials();st.success('Saved credentials cleared; environment fallback remains if configured.')
    st.info('Dhan token validity and data subscription are broker-controlled. No order credentials or broker order API calls are used.')
with tabs[2]:
    st.subheader('First-time trade configuration')
    st.radio('Trading mode',['PAPER — ENABLED','LIVE — LOCKED'],index=0,disabled=True)
    with st.form('settings'):
        capital=st.number_input('Initial paper capital (₹)',min_value=10000,max_value=10000000,value=int(cfg['capital']),step=10000)
        risk=st.number_input('Per-trade risk %',min_value=0.05,max_value=1.,value=float(cfg['risk_per_trade_pct']),step=0.05)
        daily=st.number_input('Daily loss limit %',min_value=0.1,max_value=3.,value=float(cfg['daily_loss_pct']),step=0.1)
        weekly=st.number_input('Weekly loss limit %',min_value=0.5,max_value=6.,value=float(cfg['weekly_loss_pct']),step=0.5)
        maxtrades=st.number_input('Max paper trades per day',min_value=1,max_value=5,value=int(cfg['max_trades_per_day']))
        spread=st.number_input('Maximum option bid-ask spread %',min_value=0.1,max_value=2.,value=float(cfg['max_spread_pct']),step=0.1)
        sl=st.number_input('Option premium stop %',min_value=3.,max_value=35.,value=float(cfg['stop_loss_pct']),step=1.)
        reward=st.number_input('Reward / risk target',min_value=1.,max_value=4.,value=float(cfg['target_reward_risk']),step=0.25)
        days=st.number_input('Minimum calendar days to expiry',min_value=0,max_value=7,value=int(cfg['min_days_to_expiry']))
        if st.form_submit_button('Save configuration'):
            save_settings({'capital':capital,'risk_per_trade_pct':risk,'daily_loss_pct':daily,'weekly_loss_pct':weekly,'max_trades_per_day':maxtrades,'max_spread_pct':spread,'stop_loss_pct':sl,'target_reward_risk':reward,'min_days_to_expiry':days})
            st.success('Settings saved. Worker loads revised settings on its next cycle.');st.rerun()
    st.caption('Instrument lot sizes are in config.json and MUST be verified against current contract specifications. Final quantities round to whole lots.')
    st.metric('Configured per-trade risk budget',f'₹{risk_budget(cfg):,.0f}')
with tabs[3]:
    st.subheader('Paper execution requests — manual confirmation')
    st.info('Automatic directional trading is NOT implemented. Orders are simulated only after a manually confirmed side and contract pass all risk gates.')
    p=ROOT/'marketdata'/'latest_chain.json'
    if p.exists():
        chain=json.loads(p.read_text());df=pd.DataFrame(chain.get('contracts',[]))
        if not df.empty:
            index=st.selectbox('Index',list(cfg['indices']))
            side=st.radio('Side',['CE','PE'],horizontal=True)
            view=df[(df.index_name==index)&(df.side==side)].copy()
            view['distance']=(view.strike-view.underlying).abs();view=view.sort_values('distance').head(20)
            st.dataframe(view[['strike','expiry','bid','ask','delta','theta','vega','iv','oi','volume','security_id']],use_container_width=True)
            ranked=score_strikes(df[df.index_name==index].to_dict('records'),cfg,db,now(),side)
            if ranked:st.success(f"Top eligible strike (heuristic, not buy recommendation): {ranked[0]['row']['strike']} {side}")
            if len(view):
                contract=st.selectbox('Contract ID',view.security_id.tolist())
                confirm=st.checkbox('I understand this is a simulated paper request, not a verified automatic signal.')
                if st.button('Queue ONE paper entry',disabled=not confirm):
                    r=view[view.security_id==contract].iloc[0]
                    age=(now()-datetime.fromisoformat(r['stamp'])).total_seconds()
                    if age>cfg['max_quote_age_seconds']:st.error('Data stale — request rejected.')
                    elif state(db,'pending_signal'):st.error('Previous request pending.')
                    else:put_state(db,'pending_signal',json.dumps({'security_id':contract,'side':side}));st.success('Request queued for worker safety checks.')
        else:st.info('Option chain is empty.')
    else:st.info('Start paper worker for live Dhan read-only market snapshots.')
with tabs[4]:
    a,b=st.columns(2)
    if a.button('HALT paper entries',type='primary'):put_state(db,'halt','1');st.rerun()
    if b.button('Reset paper halt'):put_state(db,'halt','0');st.rerun()
    st.metric('Risk per trade',f'₹{risk_budget(cfg):,.0f}')
    st.metric('Daily loss budget',f"₹{cfg['capital']*cfg['daily_loss_pct']/100:,.0f}")
    events=pd.read_sql_query('SELECT * FROM events ORDER BY id DESC LIMIT 150',db)
    st.dataframe(events,use_container_width=True,hide_index=True)
    st.warning('HALT stops new entries only. Exit fills cannot be guaranteed. Paper simulation does not model broker outage or real market impact.')

with tabs[5]:
    from autopilot import set_autopilot
    st.subheader('One-button PAPER autopilot')
    st.warning('LIVE MONEY DISABLED. Autonomous paper entries require separately supplied verified, CLOSED 5-minute IST candles for NIFTY / BANKNIFTY. The system does NOT fetch those candles itself yet.')
    running=state(db,'autopilot','0')=='1'
    st.metric('Paper autopilot', 'ON' if running else 'OFF')
    if st.button('STOP autopilot' if running else 'START paper autopilot',type='primary'):
        set_autopilot(db,not running);st.rerun()
    st.write('Last decision:',state(db,'autopilot_last_decision','No decision yet'))
    st.caption('STOP prevents NEW paper entries; existing positions remain governed by the worker risk/exit loop. HALT in Risk & logs overrides ON.')
    st.code('marketdata/candles/NIFTY_5m.csv\nmarketdata/candles/BANKNIFTY_5m.csv')
    st.caption('Required CSV columns: timestamp,open,high,low,close,volume. Timestamp = start of COMPLETED 5-minute candle, naive IST. Supply through a trusted feed; do not use synthetic/demo candles.')

with tabs[6]:
    from guardian_v7 import ExecutionGuardian
    from pathlib import Path
    guardian=ExecutionGuardian(Path(__file__).resolve().parent/'data'/'guardian_v7.db')
    st.subheader('V7 execution guardian — paper simulation only')
    st.warning('LIVE ORDER PLACEMENT IS NOT IMPLEMENTED. UI switch does not authorize real-money orders.')
    st.write('Autopilot:', 'ON' if guardian.setting('autopilot')=='1' else 'OFF')
    st.write('Emergency halt:', 'ON' if guardian.setting('halt')=='1' else 'OFF')
    a,b=st.columns(2)
    with a:
        if st.button('Enable guardian paper mode'):
            guardian.set_control('autopilot','1');guardian.set_control('halt','0');st.rerun()
    with b:
        if st.button('EMERGENCY HALT guardian',type='primary'):
            guardian.set_control('halt','1');guardian.set_control('autopilot','0');st.rerun()
    st.caption('This guardian is an independent paper execution lab; not wired to V6 data worker. HALT prevents new guardian fills; exposure already simulated requires manual closure. V6 autopilot has separate controls.')
    st.dataframe(guardian.intents(),use_container_width=True)
    guardian.close()
