import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from admin_auth import authenticated, hash_password, verify_password
from bot import Config, SavabyarBot
from storage import Store
from test_bot import FakeAPI

PASSWORD = 'unit-test-password-123'
ENCODED = hash_password(PASSWORD)


class AdminPasswordTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.api = FakeAPI()
        self.config = Config('fake-token', 'fake-provider', '@savabyar', admin_ids=(99,),
                             admin_password_hash=ENCODED)
        self.bot = SavabyarBot(self.config, self.api, self.store)
        self.sequence = 0

    def message(self, text, user=99):
        self.sequence += 1
        self.bot.message({'message_id': 1000 + self.sequence, 'text': text,
                          'chat': {'id': user, 'type': 'private'}, 'from': {'id': user}}, self.sequence)

    def callback(self, data, user=99):
        self.bot.callback({'id': 'callback', 'data': data, 'from': {'id': user},
                           'message': {'message_id': 42, 'chat': {'id': user, 'type': 'private'}}})

    def login(self):
        self.message('/admin')
        self.message(PASSWORD)
        self.assertTrue(authenticated(self.bot, 99))

    def test_hash_verification_rejects_wrong_and_malformed_passwords(self):
        self.assertTrue(verify_password(PASSWORD, ENCODED))
        self.assertFalse(verify_password('wrong-password', ENCODED))
        self.assertFalse(verify_password(PASSWORD, 'broken'))
        self.assertFalse(verify_password('x' * 1000, ENCODED))
        self.assertNotEqual(hash_password(PASSWORD), ENCODED)

    def test_admin_needs_password_and_input_is_deleted_not_echoed_or_stored(self):
        self.message('/admin')
        self.assertEqual(self.store.admin_session(99)['step'], 'login')
        self.assertFalse(authenticated(self.bot, 99))
        self.message(PASSWORD)
        self.assertTrue(authenticated(self.bot, 99))
        self.assertTrue(any(method == 'deleteMessage' for method, _ in self.api.calls))
        self.assertTrue(any('پنل مدیر' in payload.get('text', '') for _, payload in self.api.calls))
        self.assertNotIn(PASSWORD, '\n'.join(self.store.db.iterdump()))
        self.assertFalse(any(PASSWORD in str(payload) for _, payload in self.api.calls))

    def test_password_alone_does_not_grant_another_account_access(self):
        with patch('admin_auth.verify_password') as verify:
            self.message('/admin', user=7)
            self.message(PASSWORD, user=7)
            self.callback('admin:expense', user=7)
            verify.assert_not_called()
        self.assertFalse(authenticated(self.bot, 7))
        self.assertIsNone(self.store.admin_session(7))

    def test_locked_admin_callbacks_cannot_start_expenses(self):
        self.callback('admin:expense')
        self.assertEqual(self.store.admin_session(99)['step'], 'login')
        self.callback('expense:fund:mahdavi')
        self.assertEqual(self.store.admin_session(99)['step'], 'login')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM expenses').fetchone()[0], 0)

    def test_expiry_blocks_old_confirmation_buttons(self):
        self.login()
        self.store.save_admin_session(99, {'step': 'confirm', 'request_id': 'old-confirmation'})
        with self.store.db:
            self.store.db.execute('UPDATE admin_logins SET expires_at=0')
        self.callback('expense:confirm:old-confirmation')
        self.assertEqual(self.store.admin_session(99)['step'], 'login')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM expenses').fetchone()[0], 0)

    def test_logout_button_and_command_revoke_access(self):
        for mode in ('button', 'command'):
            self.login()
            if mode == 'button':
                self.callback('admin:logout')
            else:
                self.message('/logout')
            self.assertFalse(authenticated(self.bot, 99))
            self.assertEqual(self.store.admin_session(99)['step'], 'login')

    def test_five_failures_lock_login_and_password_does_not_bypass_lock(self):
        self.message('/admin')
        for _ in range(5):
            self.message('wrong-password')
        self.assertGreater(self.store.admin_lock_remaining(99), 0)
        with patch('admin_auth.verify_password') as verify:
            self.message(PASSWORD)
            verify.assert_not_called()
        self.assertFalse(authenticated(self.bot, 99))
        with self.store.db:
            self.store.db.execute('UPDATE admin_login_failures SET locked_until=?', (int(time.time()) - 1,))
        self.message(PASSWORD)
        self.assertTrue(authenticated(self.bot, 99))
        self.assertEqual(self.store.admin_lock_remaining(99), 0)

    def test_password_change_invalidates_existing_login(self):
        self.login()
        self.bot.cfg = replace(self.config, admin_password_hash=hash_password('a-different-password'))
        self.assertFalse(authenticated(self.bot, 99))
        self.callback('admin:balances')
        self.assertEqual(self.store.admin_session(99)['step'], 'login')

    def test_missing_password_locks_admin_without_disabling_public_menu(self):
        self.bot.cfg = replace(self.config, admin_password_hash='')
        self.message('/admin')
        self.assertIn('ADMIN_PASSWORD', self.api.calls[-1][1]['text'])
        self.assertFalse(authenticated(self.bot, 99))
        self.message('/start', user=7)
        self.assertEqual(len(self.api.calls[-1][1]['reply_markup']['inline_keyboard']), 3)


if __name__ == '__main__':
    unittest.main()
