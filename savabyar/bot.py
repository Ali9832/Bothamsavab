"""Savabyar: three intentions, Bale wallet invoices and anonymous receipts."""

import argparse
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import jdatetime
import requests
from dotenv import load_dotenv

from storage import Store

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger('savabyar')
TEST_PROVIDER = 'WALLET-TEST-1111111111111111'
INTENTS = {
    'mahdavi': 'مهدویت',
    'heyat': 'هیئت',
    'unrestricted': 'بدون نیت مشخص (مهدویت یا هیئت)',
}
BUTTONS = [
    ('🌿 مشارکت در هزینه‌های مهدویت', 'mahdavi'),
    ('🏴 مشارکت در هزینه‌های هیئت', 'heyat'),
    ('🤲 مشارکت بدون نیت مشخص', 'unrestricted'),
]
INTRO = (
    '🌱 به ثواب‌یار خوش آمدید\n\n'
    'ثواب‌یار راهی برای همراهی در تأمین هزینه‌های فعالیت‌های مهدوی و برنامه‌های هیئت است. '
    'شما نیت و مبلغ مشارکت خود را انتخاب می‌کنید.\n\n'
    '🌿 مهدویت: مشارکت شما برای فعالیت‌ها و برنامه‌های مهدوی مصرف می‌شود.\n'
    '🏴 هیئت: مشارکت شما برای برگزاری برنامه‌ها و تأمین هزینه‌های هیئت مصرف می‌شود.\n'
    '🤲 بدون نیت مشخص: اجازه می‌دهید مشارکت شما بر اساس نیاز در هر یک از دو زمینه مهدویت یا هیئت مصرف شود.\n\n'
    'پس از انتخاب نیت، مبلغ دلخواه را به تومان وارد کنید و از طریق کیف پول بله بپردازید. '
    'بعد از تأیید پرداخت، رسید شامل تاریخ، مبلغ و نیت، بدون نام و اطلاعات بانکی شما در کانال ثواب‌یار منتشر می‌شود.'
)


def vercel_production_url():
    """Stable production domain Vercel assigns to this project, if any."""
    if os.getenv('VERCEL_ENV') != 'production':
        return ''
    host = os.getenv('VERCEL_PROJECT_PRODUCTION_URL', '').strip().strip('/')
    return f'https://{host}' if host else ''


@dataclass(frozen=True)
class Config:
    token: str
    provider: str
    channel: str
    username: str = 'savabyarbot'
    webhook_url: str = ''
    webhook_secret: str = ''
    minimum: int = 1000
    maximum: int = 10_000_000
    database: Path = ROOT / 'data' / 'savabyar.db'
    database_url: str = ''
    miniapp_url: str = ''
    miniapp_host: str = '127.0.0.1'
    miniapp_port: int = 8081
    admin_ids: tuple[int, ...] = ()
    admin_bootstrap_username: str = ''
    admin_password_hash: str = ''
    admin_session_minutes: int = 15

    @property
    def is_test(self):
        return self.provider.startswith('WALLET-TEST-')

    @classmethod
    def load(cls):
        # Explicit path: never inherit the neighbouring fund bot's .env file.
        load_dotenv(ROOT / '.env')
        path = Path(os.getenv('DATABASE_PATH', 'data/savabyar.db'))
        database_url = os.getenv('DATABASE_URL', '').strip()
        from admin_auth import hash_password
        password = os.getenv('ADMIN_PASSWORD', '')
        encoded = ''
        if password:
            try:
                encoded = hash_password(password)
            except ValueError:
                LOG.warning('Administrator password must contain 8 to 128 characters; panel remains locked')
        cfg = cls(
            token=os.getenv('BOT_TOKEN', '').strip(),
            provider=os.getenv('PAYMENT_PROVIDER_TOKEN', '').strip(),
            channel=os.getenv('RECEIPT_CHANNEL', '').strip(),
            username=os.getenv('BOT_USERNAME', 'savabyarbot').strip().lstrip('@'),
            webhook_url=os.getenv('WEBHOOK_URL', '').strip() or vercel_production_url(),
            webhook_secret=os.getenv('WEBHOOK_SECRET', '').strip(),
            minimum=int(os.getenv('MIN_AMOUNT_TOMAN', '1000')),
            maximum=int(os.getenv('MAX_AMOUNT_TOMAN', '10000000')),
            database=path if path.is_absolute() else ROOT / path,
            database_url=database_url,
            miniapp_url=os.getenv('MINIAPP_URL', '').strip(),
            miniapp_host=os.getenv('MINIAPP_HOST', '127.0.0.1').strip(),
            miniapp_port=int(os.getenv('MINIAPP_PORT', '8081')),
            admin_ids=tuple(int(item.strip()) for item in os.getenv('ADMIN_USER_IDS', '').split(',') if item.strip()),
            admin_bootstrap_username=os.getenv('ADMIN_BOOTSTRAP_USERNAME', '').strip().lstrip('@').lower(),
            admin_password_hash=encoded,
            admin_session_minutes=int(os.getenv('ADMIN_SESSION_MINUTES', '15')),
        )
        if os.getenv('VERCEL') and not cfg.database_url:
            # Vercel's filesystem is read-only: never fall back to SQLite there.
            raise ValueError('DATABASE_URL is not set in the Vercel environment variables '
                             '(Production); redeploy after adding it.')
        if not cfg.token:
            raise ValueError('توکن ثواب‌یار را در savabyar/.env وارد کنید: BOT_TOKEN')
        if not cfg.provider or not cfg.channel:
            raise ValueError('توکن کیف پول و مقصد رسیدها باید تنظیم شوند.')
        if not cfg.username or not 0 < cfg.minimum <= cfg.maximum:
            raise ValueError('نام بات یا محدوده مبلغ معتبر نیست.')
        if cfg.miniapp_url:
            url = urlsplit(cfg.miniapp_url)
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment:
                raise ValueError('MINIAPP_URL باید یک آدرس عمومی HTTPS باشد.')
        if cfg.webhook_url:
            url = urlsplit(cfg.webhook_url)
            if (url.scheme != 'https' or not url.hostname or url.username or url.password
                    or url.query or url.fragment or url.port not in (None, 443, 88)):
                raise ValueError('WEBHOOK_URL must be a public HTTPS URL on port 443 or 88.')
        if not 1 <= cfg.miniapp_port <= 65535:
            raise ValueError('درگاه مینی‌اپ معتبر نیست.')
        if any(not 0 < item < 2**53 for item in cfg.admin_ids):
            raise ValueError('شناسه عددی مدیر معتبر نیست.')
        if not 1 <= cfg.admin_session_minutes <= 120:
            raise ValueError('مدت نشست مدیر باید بین ۱ تا ۱۲۰ دقیقه باشد.')
        return cfg


class APIError(Exception):
    def __init__(self, message, code=None, reason=None):
        super().__init__(message)
        self.code = code
        self.reason = reason

    @property
    def retryable(self):
        return self.code is None or self.code == 429 or self.code >= 500


class BaleAPI:
    def __init__(self, token):
        self.url = 'https://tapi.bale.ai/bot' + token + '/'
        self.http = requests.Session()

    def call(self, method, *, request_timeout=15, **payload):
        try:
            response = self.http.post(self.url + method, json=payload, timeout=request_timeout)
            data = response.json()
        except (requests.RequestException, ValueError):
            # requests exceptions contain the token-bearing URL. Never expose them.
            raise APIError(f'{method}: network or response error') from None
        if not isinstance(data, dict) or not data.get('ok'):
            code = data.get('error_code', response.status_code) if isinstance(data, dict) else response.status_code
            description = str(data.get('description', '')).lower() if isinstance(data, dict) else ''
            reason = ('invalid_payment_provider' if 'provider_token' in description
                      and any(word in description for word in ('not found', 'invalid')) else None)
            if 'message is not modified' in description:
                reason = 'message_not_modified'
            elif any(value in description for value in ('message to edit not found', "message can't be edited", 'message can not be edited')):
                reason = 'message_unavailable'
            # Retain only a classification: the error description may echo tokens.
            raise APIError(f'{method}: API error {code}', code, reason)
        return data.get('result')


def menu(miniapp_url=''):
    if miniapp_url:
        url = urlsplit(miniapp_url)
        query = dict(parse_qsl(url.query))
        return {'inline_keyboard': [[{
            'text': title,
            'web_app': {'url': urlunsplit(url._replace(query=urlencode(dict(query, intent=key))))},
        }] for title, key in BUTTONS]}
    return {'inline_keyboard': [
        [{'text': title, 'callback_data': 'intent:' + key}] for title, key in BUTTONS
    ]}


def parse_toman(text, minimum, maximum):
    translated = text.strip().translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'))
    if re.fullmatch(r'[0-9]+', translated):
        normalized = translated
    elif re.fullmatch(r'[0-9]{1,3}(?:[,٬][0-9]{3})+', translated):
        normalized = translated.replace(',', '').replace('٬', '')
    else:
        raise ValueError('invalid amount')
    if len(normalized) > 15:
        raise ValueError('amount too large')
    amount = int(normalized)
    if not minimum <= amount <= maximum:
        raise ValueError('amount out of range')
    return amount


def receipt_text(order):
    local = datetime.fromtimestamp(order['paid_at'], ZoneInfo('Asia/Tehran'))
    date = jdatetime.datetime.fromgregorian(datetime=local).strftime('%Y/%m/%d')
    status = '🧪 رسید آزمایشی — وجهی جابه‌جا نشده است' if order['is_test'] else '✅ رسید مشارکت موفق'
    return (
        f'🌱 ثواب‌یار\n{status}\n\n'
        f"شماره رسید: SY-{order['id'].upper()}\n"
        f'تاریخ شمسی: {date}\n'
        f'ساعت: {local:%H:%M:%S} (تهران)\n'
        f"مبلغ: {order['amount_rial'] // 10:,} تومان\n"
        f"نیت: {INTENTS[order['intent']]}\n"
        'روش پرداخت: کیف پول بله\n\n'
        'از همراهی شما سپاسگزاریم 🌿'
    )


def payment_matches(order, payment, user_id, *, allow_paid=False):
    return bool(
        order and (allow_paid or order['paid_at'] is None)
        and order['user_id'] == user_id
        and payment.get('currency') == 'IRR'
        and type(payment.get('total_amount')) is int
        and payment['total_amount'] == order['amount_rial']
    )


def balance_text(balances, *, is_test=False):
    lines = ['📊 مانده آزمایشی بخش‌ها' if is_test else '📊 مانده ثبت‌شده صندوق']
    for key, label in [('mahdavi', 'مهدویت'), ('heyat', 'هیئت'), ('unrestricted', 'بدون نیت مشخص')]:
        lines.append(f"{label}: {balances[key]['balance_rial'] // 10:,} تومان")
    lines.append('بر اساس واریزی‌های تأییدشده و هزینه‌های ثبت‌شده در بات.')
    return '\n'.join(lines)


def expense_text(expense):
    local = datetime.fromtimestamp(expense['created_at'], ZoneInfo('Asia/Tehran'))
    date = jdatetime.datetime.fromgregorian(datetime=local).strftime('%Y/%m/%d')
    return (('🧪 گزارش هزینه آزمایشی' if expense['is_test'] else '🧾 گزارش هزینه صندوق')
            + f"\nشماره گزارش: EX-{expense['id'].upper()}\n"
            + f"تاریخ ثبت: {date} — {local:%H:%M} (تهران)\n"
            + f"محل مصرف: {INTENTS[expense['purpose']]}\n"
            + f"از محل مشارکت‌های: {INTENTS[expense['fund']]}\n"
            + f"مبلغ: {expense['amount_rial'] // 10:,} تومان\n"
            + f"بابت: {expense['description']}\n"
            + 'نوع ثبت: هزینه انجام‌شده خارج از بات، با تأیید مدیر.')


class SavabyarBot:
    def __init__(self, config, api, store):
        self.cfg, self.api, self.store = config, api, store
        self.bot_user_id = None

    def admin_ids(self):
        if self.cfg.admin_ids:
            return set(self.cfg.admin_ids)
        bound = self.store.bound_admin(self.cfg.admin_bootstrap_username) if self.cfg.admin_bootstrap_username else None
        return {bound} if bound else set()

    def bind_admin(self, user):
        expected = self.cfg.admin_bootstrap_username
        if (not self.cfg.admin_ids and expected and user.get('id')
                and str(user.get('username', '')).lower() == expected):
            self.store.bootstrap_admin(expected, user['id'])

    def is_admin(self, user_id):
        return user_id in self.admin_ids()

    def send(self, chat_id, text, **kwargs):
        return self.api.call('sendMessage', request_timeout=5, chat_id=chat_id, text=text, **kwargs)

    def answer_button(self, query_id, text):
        try:
            self.api.call('answerCallbackQuery', request_timeout=5, callback_query_id=query_id, text=text)
        except APIError as exc:
            # The visual click acknowledgement must not prevent navigation.
            # Checkout approval has its own strict, separate payment handler.
            LOG.warning('Button acknowledgement failed; continuing navigation: %s', exc)

    def track_screen(self, chat_id, message):
        if message.get('message_id'):
            self.store.remember_screen(chat_id, message['message_id'], str(message.get('text') or ''),
                                       message.get('reply_markup') or {'inline_keyboard': []})

    def show_screen(self, chat_id, text, *, reply_markup=None, message=None):
        """All navigation uses one persistent UI message; financial notices use send()."""
        keyboard = reply_markup if reply_markup is not None else {'inline_keyboard': []}
        if message:
            self.track_screen(chat_id, message)
        screen = self.store.ui_screen(chat_id)
        if screen:
            if screen['text'] == text and screen['reply_markup'] == keyboard:
                return
            try:
                self.api.call('editMessageText', request_timeout=5, chat_id=chat_id,
                              message_id=screen['message_id'], text=text, reply_markup=keyboard)
            except APIError as exc:
                if exc.reason == 'message_not_modified':
                    pass
                elif exc.reason == 'message_unavailable':
                    result = self.send(chat_id, text, reply_markup=keyboard)
                    self.store.remember_screen(chat_id, result['message_id'], text, keyboard)
                    return
                else:
                    raise
            self.store.remember_screen(chat_id, screen['message_id'], text, keyboard)
            return
        result = self.send(chat_id, text, reply_markup=keyboard)
        self.store.remember_screen(chat_id, result['message_id'], text, keyboard)

    def show_menu(self, chat_id):
        prefix = '🧪 حالت آزمایشی؛ پرداخت‌ها واقعی نیستند.\n\n' if self.cfg.is_test else ''
        intro = INTRO
        if self.cfg.miniapp_url:
            intro = intro.replace('پس از انتخاب نیت، مبلغ دلخواه را به تومان وارد کنید و از طریق کیف پول بله بپردازید.',
                                  'با انتخاب نیت، صفحه مشارکت داخل بله باز می‌شود. مبلغ دلخواه را در آن صفحه به تومان وارد کنید و «ادامه پرداخت» را بزنید.')
        self.show_screen(chat_id, prefix + intro, reply_markup=menu(self.cfg.miniapp_url))

    def validate_channel(self, bot_id=None):
        if bot_id is None:
            bot_id = self.bot_user_id or self.api.call('getMe')['id']
        chat = self.api.call('getChat', chat_id=self.cfg.channel)
        if chat.get('type') != 'channel':
            raise ValueError('مقصد رسید باید کانال باشد.')
        membership = self.api.call('getChatMember', chat_id=chat['id'], user_id=bot_id)
        if membership.get('status') not in {'administrator', 'creator'}:
            raise ValueError('بات باید مدیر کانال رسیدها باشد.')
        if membership.get('status') == 'administrator' and not membership.get('can_post_messages'):
            raise ValueError('دسترسی ارسال پیام در کانال را به بات بدهید.')

    def validate_connection(self, *, require_channel=True):
        me = self.api.call('getMe')
        if me.get('username', '').lower() != self.cfg.username.lower():
            raise ValueError('توکن متعلق به بات مورد انتظار نیست؛ تنظیم BOT_USERNAME را بررسی کنید.')
        self.bot_user_id = me['id']
        try:
            self.validate_channel(me['id'])
        except (APIError, ValueError) as exc:
            if require_channel:
                raise
            LOG.warning('Menu available; payments wait for channel access: %s', exc)
        return me

    def handle(self, update):
        event = next((name for name in ('pre_checkout_query', 'callback_query', 'message') if name in update), 'other')
        LOG.info('Received update %s (%s)', update.get('update_id'), event)
        if 'pre_checkout_query' in update:
            self.pre_checkout(update['pre_checkout_query'])
        elif 'callback_query' in update:
            self.callback(update['callback_query'])
        elif 'message' in update:
            self.message(update['message'], update['update_id'])

    def callback(self, query):
        message = query.get('message') or {}
        chat = message.get('chat') or {}
        user_id = (query.get('from') or {}).get('id')
        LOG.info('Button action=%s message=%s', ':'.join(str(query.get('data', '')).split(':')[:2]), message.get('message_id'))
        if str(query.get('data', '')).startswith(('admin:', 'expense:')):
            from admin_panel import handle_callback
            handle_callback(self, query)
            return
        key = str(query.get('data', '')).removeprefix('intent:')
        valid = (chat.get('type') == 'private' and chat.get('id') == user_id
                 and query.get('data') == 'intent:' + key and key in INTENTS)
        self.answer_button(query['id'], 'مبلغ مشارکت را به تومان وارد کنید.' if valid else 'لطفاً /start را در گفتگوی خصوصی بات بفرستید.')
        if not valid:
            return
        self.store.clear_admin_session(user_id)
        if self.cfg.miniapp_url:
            self.store.clear_session(user_id)
            text = 'برای ورود به صفحه مشارکت، نیت خود را از گزینه‌های زیر انتخاب کنید.'
            self.show_screen(chat['id'], text, reply_markup=menu(self.cfg.miniapp_url), message=message)
            return
        self.store.select_intent(user_id, key)
        prompt = (f'نیت انتخابی: {INTENTS[key]}\n\n'
                  f'محدوده مبلغ: {self.cfg.minimum:,} تا {self.cfg.maximum:,} تومان.\n'
                  'مبلغ دلخواه را در کادر پیام بنویسید و ارسال نمائید.\n\n'
                  'همچنین میتوانید برای تغییر نیت، یکی از دکمه‌های زیر را بزنید.')
        keyboard = menu()
        self.show_screen(chat['id'], prompt, reply_markup=keyboard, message=message)

    def message(self, message, update_id):
        chat = message.get('chat') or {}
        user_id = (message.get('from') or {}).get('id')
        if chat.get('type') != 'private' or chat.get('id') != user_id:
            return
        self.bind_admin(message.get('from') or {})
        if 'successful_payment' in message:
            self.successful_payment(message)
            return
        text = str(message.get('text') or '').strip()
        command = text.split(maxsplit=1)[0].split('@')[0] if text else ''
        if command in {'/start', '/help', '/cancel'}:
            self.store.clear_session(user_id)
            self.store.clear_admin_session(user_id)
            self.show_menu(chat['id'])
            return
        from admin_panel import handle_message
        if handle_message(self, message):
            return
        if self.cfg.miniapp_url:
            self.store.clear_session(user_id)
            self.show_menu(chat['id'])
            return
        intent = self.store.session(user_id)
        if not intent:
            self.show_menu(chat['id'])
            return
        try:
            amount = parse_toman(text, self.cfg.minimum, self.cfg.maximum)
        except ValueError:
            self.show_screen(chat['id'], f'مبلغ را به صورت عدد صحیح، بین {self.cfg.minimum:,} و '
                             f'{self.cfg.maximum:,} تومان وارد کنید. برای بازگشت /start را بفرستید.', reply_markup=menu())
            return
        try:
            self.validate_channel()
        except (APIError, ValueError):
            self.show_screen(chat['id'], 'پرداخت ثواب‌یار هنوز آماده نشده است. لطفاً کمی بعد دوباره تلاش کنید. '
                             'برای بازگشت به منو /start را بفرستید.', reply_markup=menu())
            LOG.warning('Invoice withheld: receipt channel is not available')
            return
        order = self.store.create_order(update_id, user_id, chat['id'], intent, amount * 10,
                                        self.cfg.is_test, self.cfg.channel)
        if order['invoice_message_id'] is None:
            try:
                invoice = self.api.call(
                    'sendInvoice', request_timeout=5, chat_id=chat['id'], title='مشارکت در ثواب‌یار',
                    description=('آزمایشی؛ بدون انتقال وجه. ' if order['is_test'] else '')
                    + f"نیت: {INTENTS[order['intent']]}؛ مبلغ: {order['amount_rial'] // 10:,} تومان",
                    payload='savabyar:' + order['id'], provider_token=self.cfg.provider,
                    prices=[{'label': INTENTS[order['intent']], 'amount': order['amount_rial']}],
                )
            except APIError as exc:
                if exc.reason != 'invalid_payment_provider':
                    raise
                LOG.error('Bale rejected PAYMENT_PROVIDER_TOKEN; configure this bot wallet token from BotFather')
                self.show_screen(chat['id'], f"درخواست مشارکت به مبلغ {order['amount_rial'] // 10:,} تومان برای «{INTENTS[order['intent']]}» ثبت شد.\n\n"
                                 'سرویس پرداخت در حال حاضر در دسترس نیست؛ وجهی دریافت نشده است. '
                                 'لطفاً پس از فعال‌شدن پرداخت دوباره اقدام کنید.', reply_markup=menu())
                return
            self.store.invoice_sent(order['id'], invoice['message_id'])
        # State is intentionally kept until /start or another intention is selected:
        # an API retry can resolve the same update into the same order.

    def payment_order(self, payment):
        payload = payment.get('invoice_payload', '')
        if not isinstance(payload, str) or not re.fullmatch(r'savabyar:[0-9a-f]{16}', payload):
            return None
        return self.store.order(payload.split(':', 1)[1])

    def pre_checkout(self, query):
        order = self.payment_order(query)
        ok = payment_matches(order, query, (query.get('from') or {}).get('id'))
        payload = {'pre_checkout_query_id': query['id'], 'ok': ok}
        if not ok:
            payload['error_message'] = 'اطلاعات پرداخت معتبر نیست یا این درخواست قبلاً پرداخت شده است. از /start دوباره اقدام کنید.'
        self.api.call('answerPreCheckoutQuery', request_timeout=8, **payload)

    def successful_payment(self, message):
        # Never accept a forwarded payment receipt as a new payment event.
        if any(key in message for key in ('forward_from', 'forward_from_chat', 'forward_date', 'forward_origin')):
            LOG.warning('Forwarded payment event ignored')
            return
        payment = message['successful_payment']
        order = self.payment_order(payment)
        user_id = message['from']['id']
        charge = payment.get('telegram_payment_charge_id')
        provider_charge = payment.get('provider_payment_charge_id')
        date = message.get('date')
        if (not payment_matches(order, payment, user_id, allow_paid=True)
                or not isinstance(charge, str) or not charge
                or not isinstance(provider_charge, str) or not provider_charge
                or type(date) is not int or date <= 0):
            LOG.error('Payment event rejected: invalid order, owner, amount, currency or transaction data')
            return
        self.store.mark_paid(order['id'], charge, provider_charge, date)

    def flush_receipts(self):
        for delivery in self.store.pending_deliveries():
            order = self.store.order(delivery['order_id'])
            try:
                text = receipt_text(order)
                if delivery['kind'] == 'channel':
                    text += '\n\n' + balance_text(self.store.balances(is_test=bool(order['is_test'])),
                                                  is_test=bool(order['is_test']))
                result = self.send(delivery['target'], text)
                self.store.delivered(order['id'], delivery['kind'], result['message_id'])
            except APIError:
                self.store.delivery_failed(order['id'], delivery['kind'])
                LOG.warning('Receipt delivery deferred: %s (%s)', order['id'], delivery['kind'])
        for admin_id in self.admin_ids():
            for order in self.store.deposits_missing_alert(admin_id):
                text = ('🔔 اعلان واریز برای مدیر\n\n' + receipt_text(order) + '\n\n'
                        + balance_text(self.store.balances(is_test=bool(order['is_test'])),
                                       is_test=bool(order['is_test'])))
                self.store.queue_notification('deposit:' + order['id'], admin_id, text)
        for expense in self.store.expenses_missing_report():
            text = (expense_text(expense) + '\n\n'
                    + balance_text(self.store.balances(is_test=bool(expense['is_test'])),
                                   is_test=bool(expense['is_test'])))
            self.store.queue_notification('expense:' + expense['id'], expense['channel_id'], text)
        for notification in self.store.pending_notifications():
            try:
                result = self.send(notification['target'], notification['text'])
                self.store.finish_notification(notification['event_id'], notification['target'], result['message_id'])
            except APIError:
                self.store.defer_notification(notification['event_id'], notification['target'])

    def poll_once(self):
        updates = self.api.call('getUpdates', request_timeout=15, offset=self.store.offset(), limit=20, timeout=5)
        # Respond to checkout queries before other messages in the same batch.
        for update in updates:
            if update['update_id'] >= self.store.offset() and 'pre_checkout_query' in update:
                try:
                    self.handle(update)
                except APIError as exc:
                    if exc.retryable:
                        raise
                    LOG.warning('Expired/rejected checkout query: %s', exc)
        for update in updates:
            if update['update_id'] < self.store.offset():
                continue
            try:
                if 'pre_checkout_query' not in update:
                    self.handle(update)
            except APIError as exc:
                if exc.retryable:
                    raise
                LOG.error('Update %s rejected by API: %s', update['update_id'], exc)
                message = update.get('message') or {}
                if 'text' in message and (message.get('chat') or {}).get('type') == 'private':
                    self.show_screen(message['chat']['id'], 'ارسال درخواست ناموفق بود. پرداختی تأیید نشده است؛ لطفاً دوباره تلاش کنید یا /start را بفرستید.')
            self.store.set_offset(update['update_id'] + 1)

    def process_webhook_update(self, update):
        update_id = update.get('update_id') if isinstance(update, dict) else None
        if not isinstance(update_id, int) or isinstance(update_id, bool):
            return False
        if update_id < self.store.offset():
            return True
        try:
            self.handle(update)
        except APIError as exc:
            if exc.retryable:
                LOG.warning('Temporary update failure; asking Bale to retry')
                return False
            LOG.error('Update %s rejected by API: %s', update_id, exc)
        self.store.set_offset(update_id + 1)
        return True

    def receipt_worker(self, stop):
        # Independent connection and HTTP session: channel outages must not hold
        # checkout confirmations behind receipt delivery requests.
        store = Store(self.cfg.database, url=self.cfg.database_url)
        worker = SavabyarBot(self.cfg, BaleAPI(self.cfg.token), store)
        try:
            while not stop.is_set():
                worker.flush_receipts()
                stop.wait(3)
        finally:
            store.close()

    def run(self, port=8080):
        LOG.info('Savabyar started (%s)', 'TEST' if self.cfg.is_test else 'LIVE')
        stop = threading.Event()
        worker = threading.Thread(target=self.receipt_worker, args=(stop,), daemon=True)
        worker.start()
        try:
            if self.cfg.webhook_url:
                self.run_webhook(port)
            else:
                self.run_polling(port)
        finally:
            stop.set()
            worker.join(timeout=5)

    def run_polling(self, port):
        webhook = self.api.call('getWebhookInfo')
        if webhook.get('url'):
            self.api.call('deleteWebhook')
            LOG.info('Removed previous webhook; starting long polling')
        health = make_health_server(port)
        health.timeout = 1
        health_thread = threading.Thread(target=health.serve_forever, daemon=True)
        health_thread.start()
        try:
            while True:
                try:
                    self.poll_once()
                except APIError as exc:
                    if not exc.retryable:
                        raise
                    LOG.warning('%s; retrying', exc)
                    time.sleep(3)
        finally:
            health.shutdown()
            health.server_close()
            health_thread.join(timeout=5)

    def run_webhook(self, port):
        public_url = urlsplit(self.cfg.webhook_url)
        secret_path = secrets.token_urlsafe(32)
        webhook_path = public_url.path.rstrip('/') + '/webhook/' + secret_path
        webhook_address = public_url._replace(path=webhook_path, query='', fragment='').geturl()
        bot = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in ('/', '/health', '/healthz'):
                    self.send_error(404)
                    return
                self.respond(200, b'SavabYar is running\n')

            def do_POST(self):
                if self.path != webhook_path:
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 1_000_000:
                        self.respond(413, b'payload too large\n')
                        return
                    update = json.loads(self.rfile.read(length))
                    if not bot.process_webhook_update(update):
                        self.respond(500, b'retry\n')
                        return
                    self.respond(200, b'ok\n')
                except (ValueError, json.JSONDecodeError):
                    self.respond(400, b'invalid update\n')
                except Exception as exc:
                    LOG.error('Webhook handler failed: %s', type(exc).__name__)
                    self.respond(500, b'failed\n')

            def respond(self, status, body):
                self.send_response(status)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = HTTPServer(('0.0.0.0', port), Handler)
        try:
            self.api.call('setWebhook', url=webhook_address)
            info = self.api.call('getWebhookInfo')
            if info.get('url') != webhook_address:
                raise APIError('setWebhook: verification failed')
            LOG.info('Webhook registered for host %s', public_url.hostname)
            server.serve_forever()
        finally:
            server.server_close()


def make_health_server(port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ('/', '/health', '/healthz'):
                self.send_error(404)
                return
            body = b'SavabYar is running\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    return HTTPServer(('0.0.0.0', port), Handler)


def webhook_address(config):
    """Public endpoint Bale must call; matches the serverless route layout."""
    if not config.webhook_url:
        raise ValueError('برای ثبت وبهوک، WEBHOOK_URL را روی دامنه عمومی HTTPS تنظیم کنید.')
    return config.webhook_url.rstrip('/') + '/api/webhook'


def register_webhook(config, api=None):
    """Point Bale at the serverless endpoint. Safe to run repeatedly."""
    api = api or BaleAPI(config.token)
    address = webhook_address(config)
    try:
        if config.webhook_secret:
            api.call('setWebhook', url=address, secret_token=config.webhook_secret)
        else:
            api.call('setWebhook', url=address)
    except APIError as exc:
        if not config.webhook_secret or exc.code is None or not 400 <= exc.code < 500:
            raise
        # Older Bale deployments ignore secret_token; fall back to the plain call.
        LOG.warning('setWebhook rejected secret_token; retrying without it: %s', exc)
        api.call('setWebhook', url=address)
    info = api.call('getWebhookInfo')
    if info.get('url') != address:
        raise APIError('setWebhook: verification failed')
    return info


def delete_webhook(config):
    api = BaleAPI(config.token)
    api.call('deleteWebhook')
    return api.call('getWebhookInfo')


def serve_update(config, raw):
    """Process one webhook update. Shared by the serverless entry point."""
    try:
        update = json.loads(raw)
    except (ValueError, TypeError):
        return 400, b'invalid update\n'
    if not isinstance(update, dict):
        return 400, b'invalid update\n'
    store = Store(config.database, url=config.database_url)
    try:
        bot = SavabyarBot(config, BaleAPI(config.token), store)
        if not bot.process_webhook_update(update):
            return 500, b'retry\n'
        try:
            bot.flush_receipts()
        except APIError as exc:
            LOG.warning('Receipt flush deferred: %s', exc)
        return 200, b'ok\n'
    finally:
        store.close()


def flush_pending(config):
    """Deliver queued receipts and alerts; used by the scheduled endpoint."""
    store = Store(config.database, url=config.database_url)
    try:
        SavabyarBot(config, BaleAPI(config.token), store).flush_receipts()
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description='بات ثواب‌یار')
    parser.add_argument('--check', action='store_true', help='Read-only connection and channel access check')
    parser.add_argument('--set-webhook', action='store_true',
                        help='Register the Bale webhook on WEBHOOK_URL (serverless hosting)')
    parser.add_argument('--delete-webhook', action='store_true', help='Remove the registered Bale webhook')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    store = None
    miniapp_server = None
    try:
        config = Config.load()
        if args.set_webhook or args.delete_webhook:
            if args.set_webhook:
                info = register_webhook(config)
                LOG.info('Webhook registered: %s', info.get('url'))
            else:
                delete_webhook(config)
                LOG.info('Webhook removed')
            return 0
        store = Store(config.database, url=config.database_url)
        bot = SavabyarBot(config, BaleAPI(config.token), store)
        me = bot.validate_connection(require_channel=args.check)
        LOG.info('Bot connection confirmed: @%s', me['username'])
        if not args.check:
            if config.miniapp_url:
                from miniapp import make_server
                miniapp_server = make_server(config)
                threading.Thread(target=miniapp_server.serve_forever, daemon=True).start()
                LOG.info('Miniapp server listening on port %s', config.miniapp_port)
            bot.run()
        return 0
    except (ValueError, APIError) as exc:
        LOG.error('%s', exc)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if miniapp_server:
            miniapp_server.shutdown()
            miniapp_server.server_close()
        if store:
            store.close()


if __name__ == '__main__':
    raise SystemExit(main())
