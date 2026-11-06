"""Persistent orders, input state, update cursor and receipt delivery queue.

Two interchangeable backends share one public API:

* SQLite (default) — used locally and by the test suite. ``Store(':memory:')``
  or a file path keep the historical behaviour and let tests inspect ``.db``
  with the ``sqlite3`` API.
* PostgreSQL — selected by passing a ``DATABASE_URL`` (Runflare). Money and
  identifiers are stored as ``BIGINT`` so epoch-based timestamps and Bale ids
  keep working unchanged.
"""

import contextlib
import json
import secrets
import sqlite3
import time
from pathlib import Path


class Row(dict):
    """A row usable both as ``row['column']`` and ``row[0]``."""

    __slots__ = ('_values',)

    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)

    def __contains__(self, key):
        if isinstance(key, int):
            return 0 <= key < len(self._values)
        return super().__contains__(key)


SCHEMA_SQLITE = """
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
"""

SCHEMA_POSTGRES = (
    """CREATE TABLE IF NOT EXISTS sessions (
        user_id BIGINT PRIMARY KEY, intent TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS orders (
        id TEXT PRIMARY KEY, source_update BIGINT UNIQUE NOT NULL,
        user_id BIGINT NOT NULL, chat_id BIGINT NOT NULL,
        intent TEXT NOT NULL, amount_rial BIGINT NOT NULL CHECK(amount_rial > 0),
        is_test INTEGER NOT NULL, channel_id TEXT NOT NULL,
        created_at BIGINT NOT NULL, invoice_message_id BIGINT,
        paid_at BIGINT, charge_id TEXT UNIQUE, provider_charge_id TEXT,
        invoice_params TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS deliveries (
        order_id TEXT NOT NULL REFERENCES orders(id),
        kind TEXT NOT NULL CHECK(kind IN ('private', 'channel')),
        target TEXT NOT NULL, message_id BIGINT,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY(order_id, kind)
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS admin_sessions (
        user_id BIGINT PRIMARY KEY, state TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS ui_screens (
        chat_id BIGINT PRIMARY KEY, message_id BIGINT NOT NULL,
        text TEXT NOT NULL, reply_markup TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS admin_logins (
        user_id BIGINT PRIMARY KEY, password_version TEXT NOT NULL,
        expires_at BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS admin_login_failures (
        user_id BIGINT PRIMARY KEY, failures INTEGER NOT NULL,
        locked_until BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS expenses (
        id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
        admin_id BIGINT NOT NULL, fund TEXT NOT NULL, purpose TEXT NOT NULL,
        amount_rial BIGINT NOT NULL CHECK(amount_rial > 0),
        description TEXT NOT NULL, transfer_reference TEXT NOT NULL,
        is_test INTEGER NOT NULL, channel_id TEXT NOT NULL, created_at BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS notifications (
        event_id TEXT NOT NULL, target TEXT NOT NULL, text TEXT NOT NULL,
        message_id BIGINT, attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY(event_id, target)
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS expenses_transfer_reference
        ON expenses(is_test, transfer_reference)""",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS invoice_params TEXT",
)


class _SqliteBackend:
    """SQLite backend preserving the original statement-level semantics."""

    integrity_errors = (sqlite3.IntegrityError,)

    def __init__(self, path):
        if path is None:
            raise ValueError('مسیر پایگاه داده تعیین نشده است.')
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys = ON')
        self._in_transaction = False

    def _commit_if_idle(self):
        if not self._in_transaction and self.connection.in_transaction:
            self.connection.commit()

    def migrate(self):
        self.connection.executescript(SCHEMA_SQLITE)
        columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(orders)')}
        if 'invoice_params' not in columns:
            with self.connection:
                self.connection.execute('ALTER TABLE orders ADD COLUMN invoice_params TEXT')

    def upsert(self, table, columns, conflict, values):
        self.connection.execute(
            f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) "
            f"VALUES ({','.join('?' * len(columns))})", values)
        self._commit_if_idle()

    def insert_ignore(self, table, columns, values):
        self.connection.execute(
            f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) "
            f"VALUES ({','.join('?' * len(columns))})", values)
        self._commit_if_idle()

    @contextlib.contextmanager
    def transaction(self):
        self.connection.commit()
        self._in_transaction = True
        try:
            self.connection.execute('BEGIN IMMEDIATE')
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            self._in_transaction = False

    def lock(self, key):
        # BEGIN IMMEDIATE already serialises writers on SQLite.
        return None

    def fifo(self, table):
        return 'rowid'

    def backoff(self):
        return 'MIN(3600, 15 * (1 << MIN(attempts, 8)))'


def _pyformat(sql):
    # SQLite placeholders are the only '?' characters in the project's SQL;
    # no statement contains a literal percent sign.
    return sql.replace('?', '%s')


class _PostgresCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def _names(self):
        return [column.name for column in (self._cursor.description or [])]

    def fetchone(self):
        row = self._cursor.fetchone()
        return None if row is None else Row(self._names(), row)

    def fetchall(self):
        names = self._names()
        return [Row(names, row) for row in self._cursor.fetchall()]

    def __iter__(self):
        names = self._names()
        for row in self._cursor:
            yield Row(names, row)

    def close(self):
        self._cursor.close()


class _PostgresConnection:
    """Minimal DB-API surface so ``store.db.execute(...)`` keeps working."""

    def __init__(self, connection, backend):
        self._connection = connection
        self._backend = backend

    def execute(self, sql, params=None):
        cursor = self._connection.cursor()
        if params is None:
            cursor.execute(_pyformat(sql))
        else:
            cursor.execute(_pyformat(sql), tuple(params))
        return _PostgresCursor(cursor)

    def executemany(self, sql, sequence):
        cursor = self._connection.cursor()
        cursor.executemany(_pyformat(sql), [tuple(row) for row in sequence])
        return _PostgresCursor(cursor)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self._connection.commit()
        else:
            self._connection.rollback()
        return False

    def iterdump(self):
        # Never echo values: the dump is only used to assert secrets are absent.
        for table in ('sessions', 'orders', 'deliveries', 'settings', 'admin_sessions',
                      'ui_screens', 'admin_logins', 'admin_login_failures',
                      'expenses', 'notifications'):
            yield f'CREATE TABLE {table} (redacted);'

    def close(self):
        self._connection.close()


class _PostgresBackend:
    fifo_columns = {
        'deliveries': 'next_attempt, order_id, kind',
        'notifications': 'next_attempt, event_id, target',
    }

    def __init__(self, url):
        try:
            import psycopg
            from psycopg import errors as psycopg_errors
        except ImportError as exc:  # pragma: no cover - depends on deployment
            raise ValueError(
                'برای استفاده از PostgreSQL بسته psycopg را نصب کنید '
                '(pip install "psycopg[binary]").') from exc
        self.integrity_errors = (psycopg_errors.IntegrityError,)
        self._in_transaction = False
        self._raw = psycopg.connect(url)
        self.connection = _PostgresConnection(self._raw, self)

    def _commit_if_idle(self):
        if not self._in_transaction:
            self._raw.commit()

    def migrate(self):
        for statement in SCHEMA_POSTGRES:
            self.connection.execute(statement)
        self._raw.commit()

    def upsert(self, table, columns, conflict, values):
        placeholders = ','.join(['%s'] * len(columns))
        updates = ','.join(f'{column}=EXCLUDED.{column}' for column in columns if column not in conflict)
        action = f'DO UPDATE SET {updates}' if updates else 'DO NOTHING'
        self.connection.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT ({','.join(conflict)}) {action}", values)
        self._commit_if_idle()

    def insert_ignore(self, table, columns, values):
        placeholders = ','.join(['%s'] * len(columns))
        self.connection.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT DO NOTHING", values)
        self._commit_if_idle()

    @contextlib.contextmanager
    def transaction(self):
        self._in_transaction = True
        try:
            yield
            self._raw.commit()
        except BaseException:
            self._raw.rollback()
            raise
        finally:
            self._in_transaction = False

    def lock(self, key):
        # Advisory transaction lock mirrors SQLite's BEGIN IMMEDIATE exclusion.
        self.connection.execute('SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)', (key,))

    def fifo(self, table):
        return self.fifo_columns[table]

    def backoff(self):
        return 'LEAST(3600, 15 * (1 << LEAST(attempts, 8)))'


class Store:
    def __init__(self, path=None, *, url=''):
        url = (url or '').strip()
        self._postgres = bool(url)
        if self._postgres:
            self._backend = _PostgresBackend(url)
        else:
            self._backend = _SqliteBackend(path)
        self.db = self._backend.connection
        self._backend.migrate()

    @property
    def engine(self):
        return 'postgres' if self._postgres else 'sqlite'

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
        self._backend.upsert('ui_screens', ('chat_id', 'message_id', 'text', 'reply_markup'), ('chat_id',),
                             (chat_id, message_id, text, json.dumps(reply_markup, ensure_ascii=False)))

    def session(self, user_id):
        row = self.db.execute("SELECT intent FROM sessions WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else None

    def select_intent(self, user_id, intent):
        self._backend.upsert('sessions', ('user_id', 'intent'), ('user_id',), (user_id, intent))

    def clear_session(self, user_id):
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def create_order(self, update_id, user_id, chat_id, intent, amount_rial, is_test, channel_id):
        self._backend.insert_ignore(
            'orders',
            ('id', 'source_update', 'user_id', 'chat_id', 'intent', 'amount_rial',
             'is_test', 'channel_id', 'created_at'),
            (secrets.token_hex(8), update_id, user_id, chat_id, intent,
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
            except self._backend.integrity_errors:
                return False
            self.db.executemany("INSERT INTO deliveries(order_id,kind,target) VALUES(?,?,?)", [
                (order_id, 'private', str(row['chat_id'])),
                (order_id, 'channel', row['channel_id']),
            ])
        return True

    def pending_deliveries(self):
        rows = self.db.execute(
            f"""SELECT * FROM deliveries
            WHERE message_id IS NULL AND next_attempt<=? ORDER BY {self._backend.fifo('deliveries')} LIMIT 10""",
            (int(time.time()),))
        return [dict(row) for row in rows]

    def delivered(self, order_id, kind, message_id):
        with self.db:
            self.db.execute("UPDATE deliveries SET message_id=? WHERE order_id=? AND kind=?",
                            (message_id, order_id, kind))

    def delivery_failed(self, order_id, kind):
        with self.db:
            self.db.execute(f"""UPDATE deliveries SET attempts=attempts+1,
                next_attempt=? + {self._backend.backoff()}
                WHERE order_id=? AND kind=?""", (int(time.time()), order_id, kind))

    def offset(self):
        row = self.db.execute("SELECT value FROM settings WHERE key='offset'").fetchone()
        return row[0] if row else 0

    def set_offset(self, value):
        self._backend.upsert('settings', ('key', 'value'), ('key',), ('offset', value))

    def bootstrap_admin(self, username, user_id):
        self._backend.insert_ignore('settings', ('key', 'value'), ('admin:' + username.lower(), user_id))
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
        self._backend.upsert('admin_logins', ('user_id', 'password_version', 'expires_at'), ('user_id',),
                             (user_id, version, int(time.time()) + ttl))
        with self.db:
            self.db.execute('DELETE FROM admin_login_failures WHERE user_id=?', (user_id,))

    def revoke_admin_login(self, user_id):
        with self.db:
            self.db.execute('DELETE FROM admin_logins WHERE user_id=?', (user_id,))
            self.db.execute('DELETE FROM admin_sessions WHERE user_id=?', (user_id,))

    def fail_admin_login(self, user_id):
        now = int(time.time())
        with self._backend.transaction():
            self._backend.lock(f'admin-login:{user_id}')
            row = self.db.execute('SELECT * FROM admin_login_failures WHERE user_id=?', (user_id,)).fetchone()
            if row and row['locked_until'] > now:
                return
            failures = row['failures'] if row and row['locked_until'] == 0 else 0
            failures += 1
            locked_until = now + 600 if failures >= 5 else 0
            self._backend.upsert('admin_login_failures', ('user_id', 'failures', 'locked_until'), ('user_id',),
                                 (user_id, failures, locked_until))

    def save_admin_session(self, user_id, state):
        self._backend.upsert('admin_sessions', ('user_id', 'state'), ('user_id',),
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
        ) AS ledger GROUP BY fund''', (int(is_test), int(is_test)))
        for row in rows:
            if row['fund'] in result:
                income = int(row['income'] or 0)
                cost = int(row['cost'] or 0)
                result[row['fund']] = {'received_rial': income, 'spent_rial': cost,
                                       'balance_rial': income - cost}
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
        with self._backend.transaction():
            self._backend.lock(f'fund:{int(is_test)}:{fund}')
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
        self._backend.insert_ignore('notifications', ('event_id', 'target', 'text'),
                                    (event_id, str(target), text))

    def pending_notifications(self):
        return [dict(row) for row in self.db.execute(
            f"""SELECT * FROM notifications
            WHERE message_id IS NULL AND next_attempt<=? ORDER BY {self._backend.fifo('notifications')} LIMIT 10""",
            (int(time.time()),))]

    def finish_notification(self, event_id, target, message_id):
        with self.db:
            self.db.execute('UPDATE notifications SET message_id=? WHERE event_id=? AND target=?',
                            (message_id, event_id, str(target)))

    def defer_notification(self, event_id, target):
        with self.db:
            self.db.execute(f'''UPDATE notifications SET attempts=attempts+1,
                next_attempt=? + {self._backend.backoff()}
                WHERE event_id=? AND target=?''', (int(time.time()), event_id, str(target)))
