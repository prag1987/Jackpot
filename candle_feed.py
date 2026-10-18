"""Read-only 5-minute index candle feed from Dhan /charts/intraday.

Writes marketdata/candles/{NIFTY,BANKNIFTY}_5m.csv (only COMPLETED candles,
naive IST candle-start timestamps), which autopilot.load_closed_candles needs.
"""
import os
from datetime import datetime, timedelta, timezone, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
import requests

IST = ZoneInfo('Asia/Kolkata')
API = 'https://api.dhan.co/v2/charts/intraday'
ROOT = Path(__file__).resolve().parent
CANDLE_DIR = ROOT / 'marketdata' / 'candles'


def _in_session(ts):
    return dtime(9, 15) <= ts.time() <= dtime(15, 25)


def to_frame(payload):
    """Dhan returns parallel arrays. Epoch is normally UTC seconds; if that puts
    candles outside market hours we fall back to treating it as IST-shifted."""
    data = payload.get('data', payload) if isinstance(payload, dict) else {}
    cols = ('timestamp', 'open', 'high', 'low', 'close', 'volume')
    if not all(isinstance(data.get(c), list) for c in cols):
        raise ValueError('Unexpected Dhan candle payload')
    n = len(data['timestamp'])
    if any(len(data[c]) != n for c in cols):
        raise ValueError('Candle arrays have different lengths')
    epochs = [float(x) for x in data['timestamp']]
    as_utc = [datetime.fromtimestamp(e, IST).replace(tzinfo=None) for e in epochs]
    as_ist = [datetime.fromtimestamp(e, timezone.utc).replace(tzinfo=None) for e in epochs]
    stamps = as_utc if sum(map(_in_session, as_utc)) >= sum(map(_in_session, as_ist)) else as_ist
    frame = pd.DataFrame({'timestamp': stamps, **{c: data[c] for c in cols[1:]}})
    frame = frame[frame.timestamp.map(_in_session)]
    return frame.drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)


def completed_only(frame, stamp):
    cutoff = stamp.astimezone(IST).replace(tzinfo=None) - timedelta(minutes=5)
    return frame[frame.timestamp <= cutoff]


class CandleFeed:
    def __init__(self, client_id, token, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({'access-token': token, 'client-id': str(client_id),
                                     'Content-Type': 'application/json'})

    def fetch(self, underlying_id, stamp, days=7):
        body = {'securityId': str(underlying_id), 'exchangeSegment': 'IDX_I', 'instrument': 'INDEX',
                'interval': '5', 'oi': False,
                'fromDate': (stamp - timedelta(days=days)).strftime('%Y-%m-%d 09:15:00'),
                'toDate': stamp.strftime('%Y-%m-%d %H:%M:%S')}
        res = self.session.post(API, json=body, timeout=15)
        res.raise_for_status()
        return completed_only(to_frame(res.json()), stamp)

    def refresh(self, indices, stamp, out_dir=CANDLE_DIR):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        notes = {}
        for name, info in indices.items():
            try:
                frame = self.fetch(info['underlying_id'], stamp)
                frame = frame.copy()
                frame['timestamp'] = frame.timestamp.dt.strftime('%Y-%m-%d %H:%M:%S')
                tmp = out_dir / f'.{name}_5m.csv.tmp'
                frame.to_csv(tmp, index=False)
                os.replace(tmp, out_dir / f'{name}_5m.csv')
                notes[name] = f'{len(frame)} candles, last {frame.timestamp.iloc[-1] if len(frame) else "none"}'
            except Exception as ex:  # feed failure => autopilot fails closed on stale file
                notes[name] = f'CANDLE ERROR {type(ex).__name__}: {str(ex)[:120]}'
        return notes
