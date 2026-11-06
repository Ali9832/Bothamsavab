"""Vercel serverless entry point: receives Bale updates over webhook.

Bale (Telegram-compatible) posts each update to this route. The update is
processed synchronously and any queued receipt or alert is flushed before the
response, because serverless functions cannot keep a background worker.
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

from bot import APIError, Config, serve_update  # noqa: E402

LOG = logging.getLogger('savabyar.vercel')
MAX_BODY = 1_000_000
_CONFIG = None


def load_config():
    """Read configuration once per warm instance; env vars do not change."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config.load()
    return _CONFIG


def authorized(headers):
    secret = os.getenv('WEBHOOK_SECRET', '').strip()
    if not secret:
        return True
    provided = headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    return hmac.compare_digest(provided, secret)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Read-only health probe; never touches the database or the Bale API.
        self.reply(200, b'SavabYar is running\n')

    def do_POST(self):
        if not authorized(self.headers):
            return self.reply(401, b'unauthorized\n')
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return self.reply(400, b'invalid update\n')
        if not 0 < length <= MAX_BODY:
            return self.reply(413, b'payload too large\n')
        raw = self.rfile.read(length)
        try:
            status, body = serve_update(load_config(), raw)
        except APIError as exc:
            LOG.warning('Update rejected, asking Bale to retry: %s', exc)
            status, body = 500, b'retry\n'
        except Exception as exc:
            LOG.error('Webhook handler failed: %s', type(exc).__name__)
            status, body = 500, b'failed\n'
        self.reply(status, body)

    def reply(self, status, body):
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        # Update bodies and callback data are never logged.
        pass
