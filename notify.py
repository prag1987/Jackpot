"""Best-effort Telegram alerts. Never raises; trading must not depend on it."""
import os
import requests


def send(text):
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    chat = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat:
        return False
    try:
        r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                          json={'chat_id': chat, 'text': text[:3500]}, timeout=6)
        return r.ok
    except requests.RequestException:
        return False
