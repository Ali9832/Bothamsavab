"""Persistent orders, input state, update cursor and receipt delivery queue."""

import secrets
import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY, intent TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, source_update INTEGER UNIQUE NOT NULL,
                user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                intent TEXT NOT NULL, amount_rial INTEGER NOT NULL CHECK(amount_rial > 0),
                is_test INTEGER NOT NULL, channel_id TEXT NOT NULL,
                created_at INTEGER NOT NULL, invoice_message_id INTEGER,
                paid_at INTEGER, charge_id TEXT UNIQUE, provider_charge_id TEXT
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                order_id TEXT NOT NULL REFERENCES orders(id),
                kind TEXT NOT NULL CHECK(kind IN ('private', 'channel')),
                target TEXT NOT NULL, message_id INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(order_id, kind)
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_sessions (
                user_id INTEGER PRIMARY KEY, state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ui_screens (
                chat_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL,
                text TEXT NOT NULL, reply_markup TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_logins (
                user_id INTEGER PRIMARY KEY, password_version TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_login_failures (
                user_id INTEGER PRIMARY KEY, failures INTEGER NOT NULL,
                locked_until INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS expenses (
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                admin_id INTEGER NOT NULL, fund TEXT NOT NULL, purpose TEXT NOT NULL,
                amount_rial INTEGER NOT NULL CHECK(amount_rial > 0),
                description TEXT NOT NULL, transfer_reference TEXT NOT NULL,
                is_test INTEGER NOT NULL, channel_id TEXT NOT NULL, created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                event_id TEXT NOT NULL, target TEXT NOT NULL, text TEXT NOT NULL,
                message_id INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(event_id, target)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS expenses_transfer_reference
                ON expenses(is_test, transfer_reference);
        """)
        if 'invoice_params' not in {row['name'] for row in self.db.execute('PRAGMA table_info(orders)')}:
            with self.db:
                self.db.execute('ALTER TABLE orders ADD COLUMN invoice_params TEXT')

    def close(self):
        self.db.close()

    def ui_screen(self, chat_id):
        row = self.db.execute('SELECT * FROM ui_screens WHERE chat_id=?', (chat_id,)).fetchone()
        if not row:
            return None
        screen = dict(row)
        screen['reply_markup'] = json.loads(screen['reply_markup'])
        return screen

    def remember_screen(self, chat_id, message_id, text, reply_markup):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO ui_screens VALUES (?,?,?,?)',
                            (chat_id, message_id, text, json.dumps(reply_markup, ensure_ascii=False)))

    def session(self, user_id):
        row = self.db.execute("SELECT intent FROM sessions WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else None

    def select_intent(self, user_id, intent):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?)", (user_id, intent))

    def clear_session(self, user_id):
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def create_order(self, update_id, user_id, chat_id, intent, amount_rial, is_test, channel_id):
        with self.db:
            self.db.execute("""
                INSERT OR IGNORE INTO orders
                (id,source_update,user_id,chat_id,intent,amount_rial,is_test,channel_id,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (secrets.token_hex(8), update_id, user_id, chat_id, intent,
                  amount_rial, int(is_test), str(channel_id), int(time.time())))
        row = self.db.execute("SELECT * FROM orders WHERE source_update=?", (update_id,)).fetchone()
        return dict(row)

    def order(self, order_id):
        row = self.db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return dict(row) if row else None

    def invoice_sent(self, order_id, message_id):
        with self.db:
            self.db.execute("UPDATE orders SET invoice_message_id=? WHERE id=?", (message_id, order_id))

    def set_invoice_params(self, order_id, params):
        with self.db:
            self.db.execute('UPDATE orders SET invoice_params=? WHERE id=?', (params, order_id))

    def mark_paid(self, order_id, charge_id, provider_charge_id, paid_at):
        """Payment and both deliveries are committed together; retries are idempotent."""
        with self.db:
            row = self.order(order_id)
            if not row or row['paid_at'] is not None:
                return False
            try:
                self.db.execute("""UPDATE orders SET paid_at=?,charge_id=?,provider_charge_id=?
                    WHERE id=? AND paid_at IS NULL""", (paid_at, charge_id, provider_charge_id, order_id))
            except sqlite3.IntegrityError:
                return False
            self.db.executemany("INSERT INTO deliveries(order_id,kind,target) VALUES(?,?,?)", [
                (order_id, 'private', str(row['chat_id'])),
                (order_id, 'channel', row['channel_id']),
            ])
        return True

    def pending_deliveries(self):
        rows = self.db.execute("""SELECT * FROM deliveries
            WHERE message_id IS NULL AND next_attempt<=? ORDER BY rowid LIMIT 10""", (int(time.time()),))
        return [dict(row) for row in rows]

    def delivered(self, order_id, kind, message_id):
        with self.db:
            self.db.execute("UPDATE deliveries SET message_id=? WHERE order_id=? AND kind=?",
                            (message_id, order_id, kind))

    def delivery_failed(self, order_id, kind):
        with self.db:
            self.db.execute("""UPDATE deliveries SET attempts=attempts+1,
                next_attempt=? + MIN(3600, 15 * (1 << MIN(attempts, 8)))
                WHERE order_id=? AND kind=?""", (int(time.time()), order_id, kind))

    def offset(self):
        row = self.db.execute("SELECT value FROM settings WHERE key='offset'").fetchone()
        return row[0] if row else 0

    def set_offset(self, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('offset', ?)", (value,))

    def bootstrap_admin(self, username, user_id):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)',
                            ('admin:' + username.lower(), user_id))
        return self.bound_admin(username)

    def bound_admin(self, username):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', ('admin:' + username.lower(),)).fetchone()
        return row[0] if row else None

    def admin_session(self, user_id):
        row = self.db.execute('SELECT state FROM admin_sessions WHERE user_id=?', (user_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def admin_login_valid(self, user_id, version):
        row = self.db.execute('SELECT * FROM admin_logins WHERE user_id=?', (user_id,)).fetchone()
        return bool(row and row['password_version'] == version and row['expires_at'] > int(time.time()))

    def admin_lock_remaining(self, user_id):
        row = self.db.execute('SELECT locked_until FROM admin_login_failures WHERE user_id=?', (user_id,)).fetchone()
        return max(0, row[0] - int(time.time())) if row else 0

    def grant_admin_login(self, user_id, version, ttl):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO admin_logins VALUES (?,?,?)',
                            (user_id, version, int(time.time()) + ttl))
            self.db.execute('DELETE FROM admin_login_failures WHERE user_id=?', (user_id,))

    def revoke_admin_login(self, user_id):
        with self.db:
            self.db.execute('DELETE FROM admin_logins WHERE user_id=?', (user_id,))
            self.db.execute('DELETE FROM admin_sessions WHERE user_id=?', (user_id,))

    def fail_admin_login(self, user_id):
        now = int(time.time())
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT * FROM admin_login_failures WHERE user_id=?', (user_id,)).fetchone()
            if row and row['locked_until'] > now:
                return
            failures = row['failures'] if row and row['locked_until'] == 0 else 0
            failures += 1
            locked_until = now + 600 if failures >= 5 else 0
            self.db.execute('INSERT OR REPLACE INTO admin_login_failures VALUES (?,?,?)',
                            (user_id, failures, locked_until))

    def save_admin_session(self, user_id, state):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO admin_sessions VALUES (?,?)',
                            (user_id, json.dumps(state, ensure_ascii=False)))

    def clear_admin_session(self, user_id):
        with self.db:
            self.db.execute('DELETE FROM admin_sessions WHERE user_id=?', (user_id,))

    def balances(self, *, is_test=False):
        result = {key: {'received_rial': 0, 'spent_rial': 0, 'balance_rial': 0}
                  for key in ('mahdavi', 'heyat', 'unrestricted')}
        rows = self.db.execute('''SELECT fund, SUM(income) AS income, SUM(cost) AS cost FROM (
            SELECT intent AS fund, amount_rial AS income, 0 AS cost FROM orders
                WHERE paid_at IS NOT NULL AND is_test=?
            UNION ALL
            SELECT fund, 0 AS income, amount_rial AS cost FROM expenses WHERE is_test=?
        ) GROUP BY fund''', (int(is_test), int(is_test)))
        for row in rows:
            if row['fund'] in result:
                result[row['fund']] = {'received_rial': row['income'], 'spent_rial': row['cost'],
                                       'balance_rial': row['income'] - row['cost']}
        return result

    def record_expense(self, *, request_id, admin_id, fund, purpose, amount_rial,
                       description, transfer_reference, is_test, channel_id):
        if fund not in ('mahdavi', 'heyat', 'unrestricted') or purpose not in ('mahdavi', 'heyat'):
            raise ValueError('بخش هزینه معتبر نیست.')
        if fund != 'unrestricted' and fund != purpose:
            raise ValueError('هزینه باید با نیت همان بخش مطابقت داشته باشد.')
        if type(amount_rial) is not int or amount_rial <= 0 or amount_rial % 10:
            raise ValueError('مبلغ هزینه معتبر نیست.')
        if not 3 <= len(description) <= 150 or not 3 <= len(transfer_reference) <= 80:
            raise ValueError('عنوان هزینه یا شماره پیگیری معتبر نیست.')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            previous = self.db.execute('SELECT * FROM expenses WHERE request_id=?', (request_id,)).fetchone()
            if previous:
                expected = (admin_id, fund, purpose, amount_rial, description, transfer_reference,
                            int(is_test), str(channel_id))
                saved = tuple(previous[key] for key in ('admin_id', 'fund', 'purpose', 'amount_rial',
                              'description', 'transfer_reference', 'is_test', 'channel_id'))
                if saved != expected:
                    raise ValueError('اطلاعات درخواست ثبت‌شده قابل تغییر نیست.')
                return dict(previous)
            if self.db.execute('SELECT 1 FROM expenses WHERE is_test=? AND transfer_reference=?',
                               (int(is_test), transfer_reference)).fetchone():
                raise ValueError('این شماره پیگیری قبلاً برای یک هزینه ثبت شده است.')
            if self.balances(is_test=is_test)[fund]['balance_rial'] < amount_rial:
                raise ValueError('مانده این بخش برای ثبت هزینه کافی نیست.')
            expense_id = secrets.token_hex(8)
            self.db.execute('''INSERT INTO expenses VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                            (expense_id, request_id, admin_id, fund, purpose, amount_rial,
                             description, transfer_reference, int(is_test), str(channel_id), int(time.time())))
            self.db.execute('DELETE FROM admin_sessions WHERE user_id=?', (admin_id,))
        return dict(self.db.execute('SELECT * FROM expenses WHERE id=?', (expense_id,)).fetchone())

    def deposits_missing_alert(self, target):
        return [dict(row) for row in self.db.execute('''SELECT o.* FROM orders o
            WHERE o.paid_at IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM notifications n WHERE n.event_id='deposit:' || o.id AND n.target=?
            ) ORDER BY o.paid_at LIMIT 50''', (str(target),))]

    def expenses_missing_report(self):
        return [dict(row) for row in self.db.execute('''SELECT e.* FROM expenses e WHERE NOT EXISTS (
            SELECT 1 FROM notifications n WHERE n.event_id='expense:' || e.id AND n.target=e.channel_id
        ) ORDER BY e.created_at LIMIT 50''')]

    def queue_notification(self, event_id, target, text):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO notifications(event_id,target,text) VALUES(?,?,?)',
                            (event_id, str(target), text))

    def pending_notifications(self):
        return [dict(row) for row in self.db.execute('''SELECT * FROM notifications
            WHERE message_id IS NULL AND next_attempt<=? ORDER BY rowid LIMIT 10''', (int(time.time()),))]

    def finish_notification(self, event_id, target, message_id):
        with self.db:
            self.db.execute('UPDATE notifications SET message_id=? WHERE event_id=? AND target=?',
                            (message_id, event_id, str(target)))

    def defer_notification(self, event_id, target):
        with self.db:
            self.db.execute('''UPDATE notifications SET attempts=attempts+1,
                next_attempt=? + MIN(3600, 15 * (1 << MIN(attempts,8)))
                WHERE event_id=? AND target=?''', (int(time.time()), event_id, str(target)))
