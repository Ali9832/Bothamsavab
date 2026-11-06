"""Amount form and authenticated backend for Bale's native openInvoice flow."""

import hashlib
import hmac
import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from bot import APIError, BaleAPI, Config, INTENTS, SavabyarBot, parse_toman
from storage import Store

WEB = Path(__file__).resolve().parent / 'web'
LOG = logging.getLogger('savabyar.miniapp')


class Unauthorized(ValueError):
    pass


def authenticated_user(raw, token, now=None):
    if not isinstance(raw, str) or len(raw) > 16000:
        raise Unauthorized('برای پرداخت، صفحه را از داخل بات ثواب‌یار باز کنید.')
    try:
        pairs = parse_qsl(raw, strict_parsing=True)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError()
        signature = fields.pop('hash')
        check = '\n'.join(f'{key}={value}' for key, value in sorted(fields.items()))
        secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError()
        age = (time.time() if now is None else now) - int(fields['auth_date'])
        if not -30 <= age <= 3600:
            raise ValueError()
        user = json.loads(fields['user'])
        if type(user.get('id')) is not int or not 0 < user['id'] < 2**53:
            raise ValueError()
        return user['id']
    except (ValueError, KeyError, TypeError, AttributeError):
        raise Unauthorized('نشست معتبر نیست یا منقضی شده است؛ صفحه را از داخل بات دوباره باز کنید.') from None


class MiniappService:
    def __init__(self, config, api, store):
        self.cfg, self.api, self.store = config, api, store

    def invoice(self, user_id, body):
        intent = body.get('intent')
        request_id = body.get('request_id')
        if not isinstance(intent, str) or intent not in INTENTS:
            raise ValueError('نیت مشارکت معتبر نیست؛ صفحه را از منوی بات باز کنید.')
        if not isinstance(request_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', request_id):
            raise ValueError('شناسه درخواست معتبر نیست؛ صفحه را دوباره باز کنید.')
        try:
            amount = parse_toman(str(body.get('amount_toman', '')), self.cfg.minimum, self.cfg.maximum)
        except ValueError:
            raise ValueError(f'مبلغ باید بین {self.cfg.minimum:,} و {self.cfg.maximum:,} تومان باشد.') from None
        # Keep miniapp requests separate from nonnegative Bale update IDs.
        digest = hashlib.sha256(f'{user_id}:{request_id}'.encode()).digest()
        source_id = -1 - int.from_bytes(digest[:7], 'big')
        try:
            SavabyarBot(self.cfg, self.api, self.store).validate_channel()
        except (ValueError, APIError):
            raise APIError('Receipt channel is unavailable') from None
        order = self.store.create_order(source_id, user_id, user_id, intent, amount * 10,
                                        self.cfg.is_test, self.cfg.channel)
        if (order['user_id'] != user_id or order['intent'] != intent or order['amount_rial'] != amount * 10
                or bool(order['is_test']) != self.cfg.is_test or order['channel_id'] != self.cfg.channel):
            raise ValueError('اطلاعات درخواست تغییر کرده است؛ صفحه را دوباره باز کنید.')
        if order['paid_at'] is not None:
            return {'order_id': order['id'], 'status': 'paid', 'is_test': bool(order['is_test'])}
        params = order['invoice_params']
        if params is None:
            params = self.api.call(
                'createInvoiceLink', request_timeout=8, title='مشارکت در ثواب‌یار',
                description=('آزمایشی؛ بدون انتقال وجه. ' if order['is_test'] else '')
                + f'نیت: {INTENTS[intent]}؛ مبلغ: {amount:,} تومان',
                payload='savabyar:' + order['id'], provider_token=self.cfg.provider,
                prices=[{'label': INTENTS[intent], 'amount': amount * 10}],
            )
            if not isinstance(params, str) or not params:
                raise APIError('createInvoiceLink: invalid response')
            self.store.set_invoice_params(order['id'], params)
        return {'order_id': order['id'], 'invoice_params': params, 'status': 'pending',
                'is_test': bool(order['is_test'])}

    def status(self, user_id, order_id):
        if not isinstance(order_id, str) or not re.fullmatch(r'[0-9a-f]{16}', order_id):
            raise ValueError('رسید پیدا نشد.')
        order = self.store.order(order_id)
        if not order or order['user_id'] != user_id:
            raise Unauthorized('به این درخواست دسترسی ندارید.')
        return {'status': 'paid' if order['paid_at'] is not None else 'pending',
                'receipt_id': 'SY-' + order['id'].upper() if order['paid_at'] is not None else None,
                'is_test': bool(order['is_test'])}


def make_server(config, port=None):
    invoice_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, data, mime='application/json; charset=utf-8'):
            raw = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, dict) else data
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/api/config':
                return self.reply(200, {'minimum': config.minimum, 'maximum': config.maximum,
                                        'is_test': config.is_test})
            files = {'/': ('index.html', 'text/html; charset=utf-8'),
                     '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if path not in files:
                return self.reply(404, {'error': 'صفحه پیدا نشد.'})
            filename, mime = files[path]
            self.reply(200, (WEB / filename).read_bytes(), mime)

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in {'/api/invoice', '/api/status'}:
                return self.reply(404, {'error': 'صفحه پیدا نشد.'})
            store = None
            try:
                if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                    raise ValueError('نوع درخواست معتبر نیست.')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 20000:
                    raise ValueError('اندازه درخواست معتبر نیست.')
                self.connection.settimeout(5)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('درخواست معتبر نیست.')
                user_id = authenticated_user(body.get('init_data'), config.token)
                store = Store(config.database, url=config.database_url)
                service = MiniappService(config, BaleAPI(config.token), store)
                if path == '/api/invoice':
                    # Serialize retries of the same invoice to avoid two links.
                    with invoice_lock:
                        result = service.invoice(user_id, body)
                else:
                    result = service.status(user_id, body.get('order_id'))
                self.reply(200, result)
            except Unauthorized as exc:
                self.reply(401, {'error': str(exc)})
            except ValueError as exc:
                self.reply(400, {'error': str(exc)})
            except APIError as exc:
                LOG.warning('Miniapp payment unavailable: code=%s reason=%s', exc.code, exc.reason)
                self.reply(503, {'error': 'پرداخت فعلاً در دسترس نیست. لطفاً کمی بعد دوباره تلاش کنید.'})
            except Exception as exc:
                LOG.error('Miniapp request failed: %s', type(exc).__name__)
                self.reply(500, {'error': 'درخواست انجام نشد؛ لطفاً دوباره تلاش کنید.'})
            finally:
                if store:
                    store.close()

        def log_message(self, *_):
            # URLs, request bodies and initData are never logged.
            pass

    return ThreadingHTTPServer((config.miniapp_host, config.miniapp_port if port is None else port), Handler)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    cfg = Config.load()
    server = make_server(cfg)
    print(f'Local amount-page preview: http://{cfg.miniapp_host}:{cfg.miniapp_port}/?intent=mahdavi', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
