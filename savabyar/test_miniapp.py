import hashlib
import hmac
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from bot import Config, menu
from miniapp import MiniappService, Unauthorized, authenticated_user, make_server
from storage import Store
from test_bot import FakeAPI


TOKEN = 'fake-token-for-tests-only'


def signed_init(user_id=7, age=0):
    fields = {'auth_date': str(int(time.time()) - age), 'user': json.dumps({'id': user_id}),
              'query_id': 'a-realistic-miniapp-session'}
    secret = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    check = '\n'.join(f'{k}={v}' for k, v in sorted(fields.items()))
    fields['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class MiniappTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(TOKEN, 'WALLET-TEST-1111111111111111', '@savabyar')
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.api = FakeAPI()
        original = self.api.call

        def call(method, **payload):
            if method == 'createInvoiceLink':
                self.api.calls.append((method, payload))
                return 'opaque-bale-invoice-parameters'
            return original(method, **payload)

        self.api.call = call
        self.service = MiniappService(self.cfg, self.api, self.store)
        self.body = {'intent': 'heyat', 'amount_toman': '۵۰٬۰۰۰',
                     'request_id': 'valid-request-id-123456'}

    def test_three_buttons_open_separate_intents_in_the_same_miniapp(self):
        rows = menu('https://example.test/?source=bot')['inline_keyboard']
        self.assertEqual(len(rows), 3)
        intents = set()
        for row in rows:
            self.assertNotIn('callback_data', row[0])
            query = parse_qs(urlsplit(row[0]['web_app']['url']).query)
            self.assertEqual(query['source'], ['bot'])
            intents.add(query['intent'][0])
        self.assertEqual(intents, {'heyat', 'mahdavi', 'unrestricted'})

    def test_auth_accepts_signed_bale_data_but_rejects_tampering_expiry_and_duplicates(self):
        raw = signed_init()
        self.assertEqual(authenticated_user(raw, TOKEN), 7)
        for invalid in [raw.replace('%3A+7', '%3A+8'), signed_init(age=3601),
                        signed_init(age=-60), raw + '&user=other', '', 'initDataUnsafe']:
            with self.subTest(value=invalid[:20]), self.assertRaises(Unauthorized):
                authenticated_user(invalid, TOKEN)
        with self.assertRaises(Unauthorized):
            authenticated_user(raw, 'different-token')

    def test_invoice_link_preserves_intent_and_rial_amount_and_retries_reuse_it(self):
        first = self.service.invoice(7, self.body)
        second = self.service.invoice(7, self.body)
        self.assertEqual(first, second)
        calls = [p for method, p in self.api.calls if method == 'createInvoiceLink']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['prices'][0]['amount'], 500000)
        self.assertEqual(first['invoice_params'], 'opaque-bale-invoice-parameters')
        order = self.store.order(first['order_id'])
        self.assertEqual(order['intent'], 'heyat')
        self.assertIsNone(order['paid_at'])
        self.assertEqual(self.store.pending_deliveries(), [])

    def test_changed_amount_cannot_reuse_request_id(self):
        self.service.invoice(7, self.body)
        with self.assertRaises(ValueError):
            self.service.invoice(7, dict(self.body, amount_toman='60000'))

    def test_only_owner_can_read_status_and_only_server_confirmation_marks_paid(self):
        invoice = self.service.invoice(7, self.body)
        order_id = invoice['order_id']
        self.assertEqual(self.service.status(7, order_id)['status'], 'pending')
        with self.assertRaises(Unauthorized):
            self.service.status(8, order_id)
        # Browser-supplied paid status is never used by the server.
        self.service.invoice(7, dict(self.body, status='paid'))
        self.assertEqual(self.service.status(7, order_id)['status'], 'pending')
        self.store.mark_paid(order_id, 'charge', 'provider', int(time.time()))
        self.assertEqual(self.service.status(7, order_id)['status'], 'paid')
        self.assertEqual(self.service.invoice(7, self.body)['status'], 'paid')
        self.assertEqual(len(self.store.pending_deliveries()), 2)

    def test_old_database_is_migrated_without_losing_orders(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'old.db'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE orders (id TEXT PRIMARY KEY, source_update INTEGER UNIQUE NOT NULL)')
            db.execute("INSERT INTO orders VALUES ('kept', 2)")
            db.commit()
            db.close()
            migrated = Store(path)
            try:
                self.assertIn('invoice_params', migrated.order('kept'))
                self.assertEqual(migrated.order('kept')['source_update'], 2)
            finally:
                migrated.close()

    def test_http_serves_form_and_rejects_unauthenticated_payment(self):
        with tempfile.TemporaryDirectory() as folder:
            config = replace(self.cfg, database=Path(folder) / 'test.db')
            server = make_server(config, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                form = requests.get(base + '/?intent=heyat', timeout=3)
                self.assertEqual(form.status_code, 200)
                self.assertIn('ادامه پرداخت در بله', form.text)
                response = requests.post(base + '/api/invoice', json=dict(self.body, init_data=''), timeout=3)
                self.assertEqual(response.status_code, 401)
                self.assertNotIn(TOKEN, response.text + form.text)
                self.assertEqual(requests.get(base + '/.env', timeout=3).status_code, 404)
                self.assertEqual(requests.get(base + '/api/config', timeout=3).json()['minimum'], 1000)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
