"""Vercel entrypoint: one WSGI app serving /api/webhook and /api/cron.

Vercel's Python runtime looks for ``app`` in this file. The request logic
lives in api/webhook.py and api/cron.py so it stays in one place.
"""

import logging
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
for path in (ROOT, os.path.join(ROOT, 'savabyar')):
    if path not in sys.path:
        sys.path.insert(0, path)

from api import cron as cron_route  # noqa: E402
from api import webhook as webhook_route  # noqa: E402
from bot import APIError, flush_pending, serve_update  # noqa: E402

LOG = logging.getLogger('savabyar.vercel')


def _headers(environ):
    class Headers(dict):
        def get(self, name, default=''):
            return super().get(name.upper().replace('-', '_'), default)
    out = Headers()
    for key, value in environ.items():
        if key.startswith('HTTP_'):
            out[key[5:]] = value
    return out


def _reply(start_response, status, body):
    phrases = {200: 'OK', 400: 'Bad Request', 401: 'Unauthorized',
               404: 'Not Found', 405: 'Method Not Allowed',
               413: 'Payload Too Large', 500: 'Internal Server Error'}
    start_response(f'{status} {phrases.get(status, "")}'.strip(), [
        ('Content-Type', 'text/plain; charset=utf-8'),
        ('Content-Length', str(len(body))),
    ])
    return [body]


def app(environ, start_response):
    path = environ.get('PATH_INFO', '').rstrip('/')
    method = environ.get('REQUEST_METHOD', 'GET')
    headers = _headers(environ)

    if path in ('', '/api', '/api/webhook'):
        if method == 'GET':
            return _reply(start_response, 200, b'SavabYar is running\n')
        if method != 'POST':
            return _reply(start_response, 405, b'method not allowed\n')
        if not webhook_route.authorized(headers):
            return _reply(start_response, 401, b'unauthorized\n')
        try:
            length = int(environ.get('CONTENT_LENGTH') or 0)
        except ValueError:
            return _reply(start_response, 400, b'invalid update\n')
        if not 0 < length <= webhook_route.MAX_BODY:
            return _reply(start_response, 413, b'payload too large\n')
        raw = environ['wsgi.input'].read(length)
        try:
            status, body = serve_update(webhook_route.load_config(), raw)
        except APIError as exc:
            LOG.warning('Update rejected, asking Bale to retry: %s', exc)
            status, body = 500, b'retry\n'
        except Exception as exc:
            LOG.error('Webhook handler failed: %s', type(exc).__name__)
            status, body = 500, b'failed\n'
        return _reply(start_response, status, body)

    if path == '/api/cron':
        if method != 'GET':
            return _reply(start_response, 405, b'method not allowed\n')
        if not cron_route.authorized(headers):
            return _reply(start_response, 401, b'unauthorized\n')
        try:
            flush_pending(cron_route.load_config())
        except Exception as exc:
            LOG.error('Scheduled flush failed: %s', type(exc).__name__)
            return _reply(start_response, 500, b'failed\n')
        return _reply(start_response, 200, b'ok\n')

    return _reply(start_response, 404, b'not found\n')
