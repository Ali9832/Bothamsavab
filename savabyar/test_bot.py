import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot import APIError, BaleAPI, Config, INTENTS, SavabyarBot, menu, parse_toman, receipt_text
from storage import Store


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.fail_channel = False

    def call(self, method, **payload):
        self.calls.append((method, payload))
        if method == 'sendMessage' and payload['chat_id'] == '@savabyar' and self.fail_channel:
            raise APIError('simulated channel outage')
        if method == 'getMe':
            return {'id': 123, 'username': 'savabyarbot'}
        if method == 'getChat':
            return {'id': -1001, 'type': 'channel'}
        if method == 'getChatMember':
            return {'status': 'administrator', 'can_post_messages': True}
        if method == 'getWebhookInfo':
            return {'url': ''}
        return {'message_id': len(self.calls)}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.api = FakeAPI()
        self.cfg = Config('not-a-real-token', 'WALLET-TEST-1111111111111111', '@savabyar')
        self.bot = SavabyarBot(self.cfg, self.api, self.store)

    def message(self, text, update_id=1):
        self.bot.handle({'update_id': update_id, 'message': {
            'chat': {'id': 7, 'type': 'private'}, 'from': {'id': 7}, 'text': text,
        }})

    def select(self, intent):
        self.bot.handle({'update_id': 10, 'callback_query': {
            'id': 'callback', 'data': 'intent:' + intent, 'from': {'id': 7},
            'message': {'message_id': 42, 'chat': {'id': 7, 'type': 'private'}},
        }})

    def order(self, intent='mahdavi', update_id=2):
        self.select(intent)
        self.message('۵۰٬۰۰۰', update_id)
        return self.store.order(self.api.calls[-1][1]['payload'].split(':')[1])

    def payment(self, order, **changes):
        data = {'currency': 'IRR', 'total_amount': order['amount_rial'],
                'invoice_payload': 'savabyar:' + order['id'],
                'telegram_payment_charge_id': 'charge-1',
                'provider_payment_charge_id': 'provider-1'}
        data.update(changes)
        return data

    def pay(self, order, **changes):
        self.bot.handle({'update_id': 20, 'message': {
            'chat': {'id': 7, 'type': 'private'}, 'from': {'id': 7},
            'date': 1774126800, 'successful_payment': self.payment(order, **changes),
        }})

    def test_start_contains_explanation_and_exactly_three_buttons(self):
        self.message('/start')
        payload = self.api.calls[-1][1]
        self.assertEqual(len([b for row in menu()['inline_keyboard'] for b in row]), 3)
        self.assertIn('هر یک از دو زمینه', payload['text'])
        self.assertIn('بدون نام', payload['text'])

    def test_intention_buttons_edit_the_visible_message_in_place(self):
        for intent in INTENTS:
            self.api.calls.clear()
            self.select(intent)
            self.assertEqual([method for method, _ in self.api.calls],
                             ['answerCallbackQuery', 'editMessageText'])
            prompt = self.api.calls[-1][1]
            self.assertEqual(prompt['message_id'], 42)
            self.assertEqual(prompt['reply_markup'], menu())
            self.assertIn(INTENTS[intent], prompt['text'])
            self.assertEqual(self.store.session(7), intent)

    def test_expired_button_acknowledgement_does_not_block_navigation(self):
        original = self.api.call

        def call(method, **payload):
            if method == 'answerCallbackQuery':
                raise APIError('expired callback acknowledgement', 400)
            return original(method, **payload)

        self.api.call = call
        self.select('mahdavi')
        self.assertEqual(self.store.session(7), 'mahdavi')
        self.assertEqual(self.api.calls[-1][0], 'editMessageText')

    def test_reselecting_the_visible_intent_does_not_send_a_new_message(self):
        self.select('heyat')
        prompt = self.api.calls[-1][1]
        self.api.calls.clear()
        self.bot.callback({'id': 'again', 'data': 'intent:heyat', 'from': {'id': 7},
                           'message': {'message_id': 42, 'chat': {'id': 7, 'type': 'private'},
                                       'text': prompt['text'], 'reply_markup': menu()}})
        self.assertEqual([method for method, _ in self.api.calls], ['answerCallbackQuery'])

    def test_all_three_intents_create_native_invoices_in_rial(self):
        for number, intent in enumerate(INTENTS, 1):
            with self.subTest(intent=intent):
                order = self.order(intent, 100 + number)
                method, invoice = self.api.calls[-1]
                self.assertEqual(method, 'sendInvoice')
                self.assertEqual(invoice['prices'][0]['amount'], 500000)
                self.assertEqual(order['intent'], intent)
                self.assertEqual(self.store.pending_deliveries(), [])

    def test_persian_arabic_digits_and_separators(self):
        for text in ['۵۰٬۰۰۰', '٥٠٠٠٠', '50,000', '50000']:
            self.assertEqual(parse_toman(text, 1000, 100000), 50000)
        for text in ['-1000', '1.5', '۵۰ تومان', '5,00', '0', '100001', '۱e۵', '9' * 5000]:
            with self.subTest(text=text[:20]), self.assertRaises(ValueError):
                parse_toman(text, 1000, 100000)

    def test_cancel_returns_to_three_button_menu(self):
        self.select('heyat')
        self.message('/cancel')
        self.assertIsNone(self.store.session(7))
        self.assertEqual(self.api.calls[-1][1]['reply_markup'], menu())

    def test_precheckout_checks_owner_amount_currency_and_paid_state(self):
        order = self.order()
        for changes, user, expected in [({}, 7, True), ({'total_amount': 50000}, 7, False),
                                       ({'currency': 'USD'}, 7, False), ({}, 8, False),
                                       ({'invoice_payload': 'arbitrary'}, 7, False)]:
            query = dict(self.payment(order, **changes), id='checkout', **{'from': {'id': user}})
            self.bot.pre_checkout(query)
            self.assertEqual(self.api.calls[-1][1]['ok'], expected)
        self.assertIsNone(self.store.order(order['id'])['paid_at'])
        self.assertEqual(self.store.pending_deliveries(), [])
        self.pay(order)
        self.bot.pre_checkout(dict(self.payment(order), id='checkout', **{'from': {'id': 7}}))
        self.assertFalse(self.api.calls[-1][1]['ok'])

    def test_success_creates_two_anonymous_receipts_and_replay_does_not_duplicate(self):
        order = self.order('unrestricted')
        self.pay(order)
        self.pay(order)
        self.assertEqual(len(self.store.pending_deliveries()), 2)
        self.bot.flush_receipts()
        sent = [p for method, p in self.api.calls if method == 'sendMessage' and 'شماره رسید' in p['text']]
        self.assertEqual({str(p['chat_id']) for p in sent}, {'7', '@savabyar'})
        for payload in sent:
            self.assertIn('بدون نیت مشخص', payload['text'])
            self.assertIn('50,000 تومان', payload['text'])
            # 21:00 UTC falls after midnight in Tehran: use the local day.
            self.assertIn('تاریخ شمسی: 1405/01/02', payload['text'])
            self.assertIn('ساعت: 00:30:00', payload['text'])
            self.assertIn('آزمایشی', payload['text'])
            self.assertNotIn('provider-1', payload['text'])
            self.assertNotIn('charge-1', payload['text'])
        self.bot.flush_receipts()
        self.assertEqual(self.store.pending_deliveries(), [])
        self.assertEqual(len([p for m, p in self.api.calls if m == 'sendMessage' and 'شماره رسید' in p['text']]), 2)

    def test_invalid_success_does_not_publish(self):
        order = self.order()
        for change in [{'total_amount': 1}, {'currency': 'USD'}, {'telegram_payment_charge_id': ''},
                       {'provider_payment_charge_id': ''}, {'invoice_payload': 'wrong'}]:
            self.pay(order, **change)
        self.assertIsNone(self.store.order(order['id'])['paid_at'])
        self.assertEqual(self.store.pending_deliveries(), [])

    def test_forwarded_and_other_users_payments_are_ignored(self):
        order = self.order()
        for user, forward in [(8, {}), (7, {'forward_date': 1774126800})]:
            self.bot.message({'chat': {'id': user, 'type': 'private'}, 'from': {'id': user},
                              'date': 1774126800, 'successful_payment': self.payment(order), **forward}, 20)
        self.assertIsNone(self.store.order(order['id'])['paid_at'])

    def test_charge_id_cannot_pay_two_orders(self):
        first = self.order(update_id=2)
        second = self.order(update_id=3)
        self.pay(first)
        self.pay(second)
        self.assertIsNone(self.store.order(second['id'])['paid_at'])

    def test_invoice_retry_reuses_order(self):
        first = self.order(update_id=2)
        count = len([m for m, p in self.api.calls if m == 'sendInvoice'])
        self.message('۵۰٬۰۰۰', update_id=2)
        self.assertEqual(len([m for m, p in self.api.calls if m == 'sendInvoice']), count)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 1)
        self.assertIsNone(self.store.order(first['id'])['paid_at'])

    def test_failed_channel_delivery_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bot.db'
            first = Store(path)
            order = first.create_order(1, 7, 7, 'heyat', 500000, True, '@savabyar')
            first.mark_paid(order['id'], 'charge', 'trace', 1774126800)
            first.set_offset(21)
            first.close()
            second = Store(path)
            self.addCleanup(second.close)
            bot = SavabyarBot(self.cfg, self.api, second)
            self.api.fail_channel = True
            bot.flush_receipts()
            rows = second.db.execute('SELECT * FROM deliveries').fetchall()
            self.assertEqual(sum(r['message_id'] is not None for r in rows), 1)
            self.assertEqual(second.offset(), 21)
            self.api.fail_channel = False
            with second.db:
                second.db.execute('UPDATE deliveries SET next_attempt=0')
            bot.flush_receipts()
            self.assertEqual(second.pending_deliveries(), [])
            second.close()

    def test_wrong_bot_token_and_non_channel_target_rejected(self):
        self.bot.validate_connection()
        self.api.call = Mock(return_value={'username': 'sandoghtaavonibot'})
        with self.assertRaises(ValueError):
            self.bot.validate_connection()

    def test_channel_outage_allows_start_but_blocks_new_invoices(self):
        original = self.api.call

        def api_call(method, **payload):
            if method == 'getChat':
                raise APIError('channel not found', 404)
            return original(method, **payload)

        self.api.call = api_call
        self.bot.validate_connection(require_channel=False)
        self.message('/start')
        self.assertEqual(self.api.calls[-1][1]['reply_markup'], menu())
        self.select('heyat')
        self.message('50000', 5)
        self.assertIn('هنوز آماده نشده', self.api.calls[-1][1]['text'])
        self.assertFalse(any(method == 'sendInvoice' for method, _ in self.api.calls))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 0)
        with self.assertRaises(APIError):
            self.bot.validate_connection(require_channel=True)
        self.api.call = Mock(side_effect=[{'id': 123, 'username': 'savabyarbot'}, {'type': 'group'}])
        with self.assertRaises(ValueError):
            self.bot.validate_connection()

    def test_missing_post_permission_or_active_webhook_rejected(self):
        for member, webhook in [({'status': 'member'}, {}),
                                ({'status': 'administrator', 'can_post_messages': False}, {}),
                                ({'status': 'administrator', 'can_post_messages': True}, {'url': 'https://example.test/hook'})]:
            self.api.call = Mock(side_effect=[{'id': 123, 'username': 'savabyarbot'},
                                              {'id': -1001, 'type': 'channel'}, member, webhook])
            with self.assertRaises(ValueError):
                self.bot.validate_connection()

    def test_polling_prioritizes_checkout_and_saves_cursor(self):
        order = self.order()
        updates = [
            {'update_id': 50, 'message': {'chat': {'id': 7, 'type': 'private'}, 'from': {'id': 7}, 'text': '/start'}},
            {'update_id': 51, 'pre_checkout_query': dict(self.payment(order), id='pc', **{'from': {'id': 7}})},
        ]
        self.api.calls.clear()
        original = self.api.call

        def api_call(method, **payload):
            if method == 'getUpdates':
                self.assertEqual(payload['timeout'], 5)
                self.assertEqual(payload['request_timeout'], 15)
                return updates
            return original(method, **payload)

        self.api.call = api_call
        self.bot.poll_once()
        self.assertEqual(self.api.calls[0][0], 'answerPreCheckoutQuery')
        self.assertEqual(self.store.offset(), 52)

    def test_expired_checkout_does_not_block_update_cursor(self):
        self.api.call = Mock(side_effect=[
            [{'update_id': 99, 'pre_checkout_query': {'id': 'expired', 'invoice_payload': 'invalid'}}],
            APIError('expired query', 400),
        ])
        self.bot.poll_once()
        self.assertEqual(self.store.offset(), 100)

    def test_transient_api_failure_keeps_update_for_retry(self):
        self.api.call = Mock(side_effect=[
            [{'update_id': 99, 'message': {'chat': {'id': 7, 'type': 'private'}, 'from': {'id': 7}, 'text': '/start'}}],
            APIError('network outage'),
        ])
        with self.assertRaises(APIError):
            self.bot.poll_once()
        self.assertEqual(self.store.offset(), 0)

    def test_live_receipt_does_not_claim_test_payment(self):
        order = self.order()
        self.pay(order)
        saved = self.store.order(order['id'])
        saved['is_test'] = 0
        text = receipt_text(saved)
        self.assertIn('مشارکت موفق', text)
        self.assertNotIn('آزمایشی', text)

    def test_api_network_errors_do_not_expose_token(self):
        api = BaleAPI('secret-token')
        api.http.post = Mock(side_effect=__import__('requests').ConnectionError('url secret-token'))
        with self.assertRaises(APIError) as caught:
            api.call('getMe')
        self.assertNotIn('secret-token', str(caught.exception))

    def test_rejected_wallet_token_does_not_claim_payment_or_expose_secret(self):
        original = self.api.call

        def call(method, **payload):
            if method == 'sendInvoice':
                raise APIError('sendInvoice: API error 400', 400, 'invalid_payment_provider')
            return original(method, **payload)

        self.api.call = call
        self.select('mahdavi')
        self.message('۱۰۰۰۰', 123)
        self.assertIn('10,000 تومان', self.api.calls[-1][1]['text'])
        self.assertIn('وجهی دریافت نشده', self.api.calls[-1][1]['text'])
        self.assertEqual(self.store.pending_deliveries(), [])
        self.assertIsNone(self.store.db.execute('SELECT paid_at FROM orders').fetchone()[0])

    def test_provider_error_classification_does_not_retain_echoed_token(self):
        api = BaleAPI('bot-secret')
        response = Mock()
        response.status_code = 400
        response.json.return_value = {'ok': False, 'error_code': 400,
                                      'description': 'Bad Request: provider_token wallet-secret not found'}
        api.http.post = Mock(return_value=response)
        with self.assertRaises(APIError) as caught:
            api.call('sendInvoice')
        self.assertEqual(caught.exception.reason, 'invalid_payment_provider')
        self.assertNotIn('wallet-secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
