"""Vercel Cron entry point: deliver receipts and alerts still queued.

The webhook flushes after every update, so this is a safety net for quiet
periods and for retrying deliveries that failed earlier.
"""

import hmac
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAVABYAR = os.path.join(ROOT, 'savabyar')
if SAVABYAR not in sys.path:
    sys.path.insert(0, SAVABYAR)

from bot import Config, flush_pending  # noqa: E402

LOG = logging.getLogger('savabyar.vercel')
_CONFIG = None


def load_config():
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config.load()
    return _CONFIG


def authorized(headers):
    cron = os.getenv('CRON_SECRET', '').strip()
    if cron:
        return hmac.compare_digest(headers.get('Authorization', ''), f'Bearer {cron}')
    secret = os.getenv('WEBHOOK_SECRET', '').strip()
    if secret:
        return hmac.compare_digest(headers.get('X-Telegram-Bot-Api-Secret-Token', ''), secret)
    return True


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not authorized(self.headers):
            return self.reply(401, b'unauthorized\n')
        try:
            flush_pending(load_config())
        except Exception as exc:
            LOG.error('Scheduled flush failed: %s', type(exc).__name__)
            return self.reply(500, b'failed\n')
        self.reply(200, b'ok\n')

    def reply(self, status, body):
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass
