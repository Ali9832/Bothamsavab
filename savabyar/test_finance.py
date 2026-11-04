import concurrent.futures
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from bot import Config, SavabyarBot
from storage import Store
from test_bot import FakeAPI
from admin_auth import hash_password, password_version

TEST_ADMIN_HASH = hash_password('finance-test-password')


class FinanceTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.cfg = Config('fake-token', 'fake-live-wallet', '@savabyar', admin_ids=(99,),
                          admin_password_hash=TEST_ADMIN_HASH)
        self.store.grant_admin_login(99, password_version(TEST_ADMIN_HASH), 900)
        self.api = FakeAPI()
        self.bot = SavabyarBot(self.cfg, self.api, self.store)
        self.sequence = 100

    def deposit(self, fund='mahdavi', toman=100000, is_test=False, paid=True):
        self.sequence += 1
        order = self.store.create_order(self.sequence, 7, 7, fund, toman * 10, is_test, '@savabyar')
        if paid:
            self.store.mark_paid(order['id'], f'charge-{self.sequence}', f'provider-{self.sequence}', int(time.time()))
        return order

    def expense(self, **changes):
        values = dict(request_id='request-1', admin_id=99, fund='mahdavi', purpose='mahdavi',
                      amount_rial=300000, description='خرید کتاب', transfer_reference='private-ref-123',
                      is_test=False, channel_id='@savabyar')
        values.update(changes)
        return self.store.record_expense(**values)

    def message(self, text, user_id=99, username=None):
        self.sequence += 1
        user = {'id': user_id}
        if username:
            user['username'] = username
        self.bot.message({'chat': {'id': user_id, 'type': 'private'}, 'from': user, 'text': text}, self.sequence)

    def callback(self, data, user_id=99):
        self.bot.callback({'id': 'callback', 'data': data, 'from': {'id': user_id},
                           'message': {'message_id': 42, 'chat': {'id': user_id, 'type': 'private'}}})

    def test_three_balances_exclude_unpaid_and_separate_test_payments(self):
        self.deposit('mahdavi', 100000)
        self.deposit('heyat', 20000)
        self.deposit('unrestricted', 30000)
        self.deposit('mahdavi', 999999, paid=False)
        self.deposit('mahdavi', 888888, is_test=True)
        balances = self.store.balances()
        self.assertEqual({key: value['balance_rial'] // 10 for key, value in balances.items()},
                         {'mahdavi': 100000, 'heyat': 20000, 'unrestricted': 30000})
        self.assertEqual(self.store.balances(is_test=True)['mahdavi']['balance_rial'], 8888880)

    def test_expense_debits_only_its_fund_and_replays_are_idempotent(self):
        self.deposit('mahdavi', 100000)
        self.deposit('heyat', 50000)
        first = self.expense()
        self.assertEqual(self.expense()['id'], first['id'])
        self.assertEqual(self.store.balances()['mahdavi']['balance_rial'], 700000)
        self.assertEqual(self.store.balances()['heyat']['balance_rial'], 500000)
        with self.assertRaises(ValueError):
            self.expense(amount_rial=100000)
        with self.assertRaises(ValueError):
            self.expense(request_id='another-request')

    def test_overspending_and_cross_purpose_spending_are_rejected(self):
        self.deposit('mahdavi', 10000)
        for changes in [{'amount_rial': 100010}, {'amount_rial': 0}, {'purpose': 'heyat'}]:
            with self.assertRaises(ValueError):
                self.expense(**changes)
        self.assertEqual(self.store.balances()['mahdavi']['balance_rial'], 100000)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM expenses').fetchone()[0], 0)
        self.deposit('unrestricted', 10000)
        self.expense(fund='unrestricted', purpose='heyat', amount_rial=100000)
        self.assertEqual(self.store.balances()['unrestricted']['balance_rial'], 0)

    def test_concurrent_expenses_cannot_overdraw_the_same_fund(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fund.db'
            setup = Store(path)
            order = setup.create_order(1, 7, 7, 'heyat', 1000000, False, '@savabyar')
            setup.mark_paid(order['id'], 'charge', 'provider', int(time.time()))
            setup.close()

            def spend(number):
                store = Store(path)
                try:
                    store.record_expense(request_id=f'request-{number}', admin_id=99, fund='heyat', purpose='heyat',
                                         amount_rial=700000, description='هزینه برنامه', transfer_reference=f'trace-{number}',
                                         is_test=False, channel_id='@savabyar')
                    return True
                except ValueError:
                    return False
                finally:
                    store.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(spend, (1, 2)))
            self.assertEqual(sum(results), 1)
            final = Store(path)
            try:
                self.assertEqual(final.balances()['heyat']['balance_rial'], 300000)
            finally:
                final.close()

    def test_bootstrap_binds_only_named_account_and_cannot_be_reassigned(self):
        self.bot.cfg = replace(self.cfg, admin_ids=(), admin_bootstrap_username='molibden4')
        self.message('/admin', user_id=8, username='different-account')
        self.assertFalse(self.bot.is_admin(8))
        self.message('/admin', user_id=99, username='molibden4')
        self.assertTrue(self.bot.is_admin(99))
        self.message('/admin', user_id=8, username='molibden4')
        self.assertFalse(self.bot.is_admin(8))
        self.assertTrue(self.bot.is_admin(99))

    def test_unauthorized_callbacks_cannot_open_expense_forms(self):
        self.callback('admin:expense', user_id=7)
        self.assertIsNone(self.store.admin_session(7))
        self.assertIn('فقط برای مدیر', self.api.calls[-1][1]['text'])

    def test_manual_expense_requires_confirmation_and_publishes_anonymous_report(self):
        self.deposit('unrestricted', 100000)
        self.callback('admin:expense')
        self.callback('expense:fund:unrestricted')
        self.message('۳۰٬۰۰۰')
        self.callback('expense:purpose:heyat')
        self.message('خرید اقلام برنامه')
        self.message('private-transfer-reference')
        state = self.store.admin_session(99)
        self.assertEqual(state['step'], 'confirm')
        self.assertEqual(self.store.balances()['unrestricted']['balance_rial'], 1000000)
        self.callback('expense:confirm:' + state['request_id'])
        self.callback('expense:confirm:' + state['request_id'])
        self.assertEqual(self.store.balances()['unrestricted']['balance_rial'], 700000)
        self.assertFalse(any(method == 'sendMessage' for method, _ in self.api.calls))
        self.assertEqual({p['message_id'] for method, p in self.api.calls if method == 'editMessageText'}, {42})
        self.bot.flush_receipts()
        reports = [p['text'] for method, p in self.api.calls
                   if method == 'sendMessage' and p['chat_id'] == '@savabyar' and 'EX-' in p['text']]
        self.assertEqual(len(reports), 1)
        self.assertIn('هیئت', reports[0])
        self.assertIn('70,000 تومان', reports[0])
        self.assertNotIn('private-transfer-reference', reports[0])
        self.assertIn('خارج از بات', reports[0])
        self.assertFalse(any(method in ('transfer', 'withdraw', 'sendInvoice') for method, _ in self.api.calls))

    def test_channel_receipt_has_three_balances_and_admin_alert_is_sent_once(self):
        self.deposit('mahdavi', 100000)
        self.bot.flush_receipts()
        self.bot.flush_receipts()
        alerts = [p for method, p in self.api.calls if method == 'sendMessage'
                  and str(p['chat_id']) == '99' and 'اعلان واریز' in p['text']]
        self.assertEqual(len(alerts), 1)
        channel = [p['text'] for method, p in self.api.calls if method == 'sendMessage' and p['chat_id'] == '@savabyar']
        self.assertEqual(len(channel), 1)
        for line in ['مهدویت: 100,000 تومان', 'هیئت: 0 تومان', 'بدون نیت مشخص: 0 تومان']:
            self.assertIn(line, channel[0])

    def test_failed_channel_expense_report_remains_queued_without_a_second_debit(self):
        self.deposit()
        self.expense()
        self.api.fail_channel = True
        self.bot.flush_receipts()
        self.assertTrue(self.store.db.execute('SELECT 1 FROM notifications WHERE message_id IS NULL').fetchone())
        self.api.fail_channel = False
        with self.store.db:
            self.store.db.execute('UPDATE notifications SET next_attempt=0')
            self.store.db.execute('UPDATE deliveries SET next_attempt=0')
        self.bot.flush_receipts()
        self.assertEqual(self.store.balances()['mahdavi']['balance_rial'], 700000)
        self.assertEqual(self.store.pending_notifications(), [])

    def test_all_admin_navigation_updates_the_clicked_message(self):
        self.deposit()
        self.message('/admin')
        self.api.calls.clear()
        for data in ('admin:balances', 'admin:payout', 'admin:home', 'admin:expense',
                     'expense:fund:mahdavi', 'admin:home'):
            self.callback(data)
        self.assertFalse(any(method == 'sendMessage' for method, _ in self.api.calls))
        edits = [p for method, p in self.api.calls if method == 'editMessageText']
        self.assertEqual(len(edits), 6)
        self.assertTrue(all(p['message_id'] == 42 for p in edits))
        self.assertIsNone(self.store.admin_session(99))

    def test_navigation_does_not_replace_financial_notifications(self):
        self.message('/admin')
        screen_id = self.store.ui_screen(99)['message_id']
        self.bot.send(99, 'اعلان واریز مستقل')
        self.api.calls.clear()
        self.bot.show_screen(99, 'مرحله بعد')
        self.assertEqual(self.api.calls[-1][0], 'editMessageText')
        self.assertEqual(self.api.calls[-1][1]['message_id'], screen_id)

    def test_ui_message_is_reused_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'bot.db'
            first = Store(path)
            bot = SavabyarBot(self.cfg, self.api, first)
            bot.show_menu(99)
            screen_id = first.ui_screen(99)['message_id']
            first.close()
            second = Store(path)
            try:
                restarted = SavabyarBot(self.cfg, self.api, second)
                self.api.calls.clear()
                restarted.show_menu(99)
                self.assertEqual(self.api.calls, [])
                restarted.show_screen(99, 'صفحه تازه')
                self.assertEqual(self.api.calls[-1][0], 'editMessageText')
                self.assertEqual(self.api.calls[-1][1]['message_id'], screen_id)
            finally:
                second.close()


if __name__ == '__main__':
    unittest.main()
