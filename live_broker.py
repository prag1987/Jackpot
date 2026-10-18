"""DhanHQ order adapter for SANDBOX and LIVE execution.

Design rules (do not weaken without thinking):
* Every entry is a Dhan SUPER ORDER, so a stop-loss and target live on the
  broker side. If this VPS crashes, the stop still protects the position.
* Order-writing calls are NEVER retried automatically. A timeout on a POST may
  still have reached the exchange; we look the order up by correlationId
  instead of sending a second order.
* LIVE requires the server's public IP to match CG_STATIC_IP (SEBI 2026 rule:
  Dhan accepts orders only from a whitelisted static IP).
"""
import os
import time
import requests

LIVE_BASE = 'https://api.dhan.co/v2'
# Verify this against the API docs linked from developer.dhanhq.co → Sandbox tab.
SANDBOX_BASE = os.getenv('DHAN_SANDBOX_BASE_URL', 'https://sandbox.dhan.co/v2')
TICK = 0.05
MODES = ('PAPER', 'SANDBOX', 'LIVE')


class BrokerError(RuntimeError):
    pass


class AuthError(BrokerError):
    """Token expired/invalid. Worker must halt and alert."""


def round_tick(price, up=False):
    steps = price / TICK
    steps = int(steps + 0.999999) if up else int(steps + 1e-9)
    return round(max(TICK, steps * TICK), 2)


def public_ip(timeout=6):
    for url in ('https://api.ipify.org', 'https://ifconfig.me/ip'):
        try:
            r = requests.get(url, timeout=timeout)
            if r.ok and r.text.strip():
                return r.text.strip()
        except requests.RequestException:
            continue
    return None


def static_ip_problem(expected=None, actual=None):
    """None when OK, else a human-readable reason. Used only for LIVE."""
    expected = (expected if expected is not None else os.getenv('CG_STATIC_IP', '')).strip()
    if not expected:
        return 'CG_STATIC_IP not set in .env (Dhan whitelisted static IP)'
    actual = actual if actual is not None else public_ip()
    if not actual:
        return 'Could not detect server public IP'
    if actual != expected:
        return f'Server IP {actual} does not match whitelisted {expected}'
    return None


class DhanTrader:
    def __init__(self, client_id, token, mode='LIVE', session=None, min_gap=0.35):
        if mode not in ('SANDBOX', 'LIVE'):
            raise ValueError('DhanTrader is only for SANDBOX or LIVE')
        if not client_id or not token:
            raise ValueError(f'Missing Dhan credentials for {mode}')
        self.mode = mode
        self.client_id = str(client_id)
        self.base = LIVE_BASE if mode == 'LIVE' else SANDBOX_BASE
        self.session = session or requests.Session()
        self.session.headers.update({'access-token': token, 'client-id': self.client_id,
                                     'Content-Type': 'application/json', 'Accept': 'application/json'})
        self.min_gap = min_gap
        self._last = 0.0

    # ---------- transport ----------
    def _call(self, method, path, payload=None, params=None, write=False):
        gap = self.min_gap - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()
        attempts = 1 if write else 3
        for attempt in range(attempts):
            try:
                res = self.session.request(method, self.base + path, json=payload, params=params, timeout=10)
            except requests.RequestException as ex:
                if attempt == attempts - 1:
                    raise BrokerError(f'{method} {path} network error: {type(ex).__name__}') from ex
                time.sleep(1 + attempt)
                continue
            if res.status_code in (401, 403):
                raise AuthError(f'Dhan auth failed ({res.status_code}): token expired or static IP/app not whitelisted. {res.text[:200]}')
            if res.status_code == 429 and attempt < attempts - 1:
                time.sleep(2 + attempt)
                continue
            if res.status_code >= 400:
                raise BrokerError(f'{method} {path} HTTP {res.status_code}: {res.text[:300]}')
            if not res.text.strip():
                return {}
            try:
                return res.json()
            except ValueError:
                return {'raw': res.text[:300]}
        raise BrokerError(f'{method} {path} failed')

    # ---------- orders ----------
    def place_super_buy(self, security_id, qty, price, target, stop, correlation_id, trailing=0.0):
        """Intraday BUY LIMIT entry with broker-side target & stop-loss legs."""
        if qty <= 0 or not (0 < stop < price < target):
            raise BrokerError(f'Refusing malformed order: qty={qty} stop={stop} price={price} target={target}')
        body = {'dhanClientId': self.client_id, 'correlationId': correlation_id[:30],
                'transactionType': 'BUY', 'exchangeSegment': 'NSE_FNO', 'productType': 'INTRADAY',
                'orderType': 'LIMIT', 'securityId': str(security_id), 'quantity': int(qty),
                'price': round_tick(price, up=True), 'targetPrice': round_tick(target, up=True),
                'stopLossPrice': round_tick(stop), 'trailingJump': float(trailing)}
        return self._call('POST', '/super/orders', body, write=True)

    def super_orders(self):
        out = self._call('GET', '/super/orders')
        return out if isinstance(out, list) else out.get('data', []) if isinstance(out, dict) else []

    def find_by_correlation(self, correlation_id):
        for o in self.super_orders():
            if o.get('correlationId') == correlation_id[:30]:
                return o
        return None

    def super_order(self, order_id):
        for o in self.super_orders():
            if str(o.get('orderId')) == str(order_id):
                return o
        return None

    def cancel_super_leg(self, order_id, leg):
        return self._call('DELETE', f'/super/orders/{order_id}/{leg}', write=True)

    def market_sell(self, security_id, qty, correlation_id):
        body = {'dhanClientId': self.client_id, 'correlationId': correlation_id[:30],
                'transactionType': 'SELL', 'exchangeSegment': 'NSE_FNO', 'productType': 'INTRADAY',
                'orderType': 'MARKET', 'validity': 'DAY', 'securityId': str(security_id),
                'quantity': int(qty), 'price': 0}
        return self._call('POST', '/orders', body, write=True)

    def order(self, order_id):
        return self._call('GET', f'/orders/{order_id}')

    # ---------- portfolio / controls ----------
    def positions(self):
        out = self._call('GET', '/positions')
        return out if isinstance(out, list) else out.get('data', []) if isinstance(out, dict) else []

    def position(self, security_id):
        for p in self.positions():
            if str(p.get('securityId')) == str(security_id) and p.get('productType') == 'INTRADAY':
                return p
        return None

    def exit_all(self):
        """Broker-wide: exits ALL intraday positions and cancels ALL open orders."""
        return self._call('DELETE', '/positions', write=True)

    def kill_switch(self, activate=True):
        status = 'ACTIVATE' if activate else 'DEACTIVATE'
        return self._call('POST', '/killswitch', params={'killSwitchStatus': status}, write=True)

    def set_pnl_exit(self, loss_value, profit_value):
        body = {'profitValue': float(profit_value), 'lossValue': float(loss_value),
                'productType': ['INTRADAY'], 'enableKillSwitch': True}
        return self._call('POST', '/pnlExit', body, write=True)

    def pnl_exit_status(self):
        return self._call('GET', '/pnlExit')
